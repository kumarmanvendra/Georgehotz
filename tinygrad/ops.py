from __future__ import annotations
import functools, itertools, operator, random
from enum import Enum, auto
from typing import Union, Type, NamedTuple, Tuple, Any, List, Optional, Dict, Set, Callable
from tinygrad.helpers import prod, DEBUG, getenv, GlobalCounters
from tinygrad.shape.shapetracker import MovementOps
from tinygrad.runtime.lib import RawBuffer

# these are the llops your accelerator must implement, along with toCpu
# the Enum class doesn't work with mypy, this is static. sorry it's ugly
class UnaryOps(Enum): NOOP = auto(); EXP = auto(); LOG = auto(); NEG = auto(); NOT = auto(); CAST = auto() # noqa: E702
class BinaryOps(Enum): ADD = auto(); SUB = auto(); MUL = auto(); DIV = auto(); POW = auto(); CMPEQ = auto(); MAX = auto() # noqa: E702
class ReduceOps(Enum): SUM = auto(); MAX = auto() # noqa: E702
class FusedOps(Enum): MULACC = auto() # noqa: E702
class LoadOps(Enum): FROMCPU = auto(); CONTIGUOUS = auto(); TOCPU = auto(); CUSTOM = auto() # noqa: E702

Op = Union[UnaryOps, BinaryOps, ReduceOps, MovementOps, LoadOps, FusedOps]
OpType = Union[Type[UnaryOps], Type[BinaryOps], Type[ReduceOps], Type[MovementOps], Type[LoadOps], Type[FusedOps]]

class LazyOp(NamedTuple):
  op: Op
  # Any == Union[LazyOp, LazyBuffer, DeviceBuffer]
  src: Tuple[Any, ...]  # type: ignore
  arg: Any = None
  # TODO: add dest to support multiple outputs. on second thought, multiple outputs will have multiple LazyOps.

# Any == Union[LazyBuffer, DeviceBuffer]
def get_buffers(op:LazyOp) -> List[Any]: return functools.reduce(operator.add, [get_buffers(x) if isinstance(x, LazyOp) else [x] for x in op.src], [])
def get_lazyops(op:LazyOp) -> List[LazyOp]: return functools.reduce(operator.add, [get_lazyops(x) for x in op.src if isinstance(x, LazyOp)], [op])
def map_buffers(real_srcs:Dict[Any, Any], x:Any) -> LazyOp:
  if len(real_srcs) and x in real_srcs: return map_buffers(real_srcs, real_srcs[x]) if isinstance(real_srcs[x], LazyOp) else real_srcs[x]
  return LazyOp(x.op, tuple((map_buffers(real_srcs, y) if isinstance(y, LazyOp) else real_srcs[y]) for y in x.src), x.arg)

class ASTRunner:
  def __init__(self, name, prg, bufs_to_delete:Optional[Set[int]]=None, global_size:Optional[List[int]]=None, local_size:Optional[List[int]]=None, op_estimate=0, mem_estimate=0):
    if DEBUG >= 4: print(prg)
    self.name, self.prg, self.global_size, self.local_size, self.bufs_to_delete, self.op_estimate, self.mem_estimate = name, prg, global_size, local_size, bufs_to_delete if bufs_to_delete else set(), op_estimate, mem_estimate

  def build(self, runtime):
    self.clprg = runtime(self.name, self.prg)
    return self

  def exec(self, bufs) -> Optional[float]:
    rawbufs = [x.realized for i,x in enumerate(bufs) if x is not None and i not in self.bufs_to_delete]
    assert all(x is not None for x in rawbufs), "some rawbufs are None, you probably didn't realize them"
    if getenv("OPTLOCAL") and self.global_size is not None and self.local_size is None: self.local_size = self.optimize_local_size(rawbufs)
    if GlobalCounters.cache is not None: GlobalCounters.cache.append((self, rawbufs))
    return self(rawbufs)

  def __call__(self, rawbufs:List[RawBuffer]) -> Optional[float]:
    if et := self.clprg(self.global_size, self.local_size, *rawbufs, wait=DEBUG>=2): GlobalCounters.time_sum_s += et
    if DEBUG >= 2:
      print(f"*** {GlobalCounters.kernel_count:4d} {self.name:20s} arg {len(rawbufs):3d} sz {str(self.global_size):18s} {str(self.local_size):12s} OPs {self.op_estimate/1e6:7.1f}M/{GlobalCounters.global_ops/1e9:7.2f}G  mem {GlobalCounters.mem_used/1e9:5.2f} GB " +
            (str() if et is None else f"tm {et*1e6:9.2f}us/{GlobalCounters.time_sum_s*1e3:9.2f}ms ({self.op_estimate/(et*1e9):8.2f} GFLOPS, {self.mem_estimate/(et*1e9):6.2f} GB/s)"))
    GlobalCounters.kernel_count += 1
    GlobalCounters.global_ops += self.op_estimate
    GlobalCounters.global_mem += self.mem_estimate
    if getenv("EARLY_STOPPING") and GlobalCounters.kernel_count == getenv("EARLY_STOPPING"): exit(0)
    return et

  def timeit(self, rawbufs:List[RawBuffer], local_override=None) -> float:
    try: return self.clprg(self.global_size, local_override if local_override is not None else self.local_size, *rawbufs, wait=True)
    except Exception: return float('inf')

  def optimize_local_size(self, rawbufs:List[RawBuffer], preserve_output=False) -> List[int]:
    assert self.global_size is not None, "needs a global size to optimize local size"
    if preserve_output or any(x == rawbufs[0] for x in rawbufs[1:]):  # this is an assignment, replace the output buffer
      output_replacement = type(rawbufs[0])(rawbufs[0].size, rawbufs[0].dtype)
      rawbufs = [output_replacement if x == rawbufs[0] else x for x in rawbufs]
    MAX_WORKGROUP = self.clprg.max_work_group_size() if hasattr(self.clprg, 'max_work_group_size') else 1024
    local_dims = [[x for x in set([sz, 1, 2, 4, 8, 16, 32, 64, 128, 256, MAX_WORKGROUP]) if x<=sz] for sz in self.global_size]
    local_sizes = [list(x) for x in itertools.product(*local_dims) if prod(x) <= MAX_WORKGROUP] * 2  # try each valid size twice
    return min([(self.timeit(rawbufs, local_size), local_size) for local_size in random.sample(local_sizes, len(local_sizes))])[1]

class Interpreted:
  def __init__(self, buffer, fxn_for_op: Dict[Op, Callable], from_lazybuffer=lambda x: x.realized, to_underlying=lambda x: x._buf):
    self.buffer = buffer
    self.fxn_for_op = fxn_for_op
    self.from_lazybuffer = from_lazybuffer
    self.to_underlying = to_underlying

  def exec_ast(self, ast:LazyOp, output=None, context=None):
    if FusedOps.MULACC in self.fxn_for_op and ast.op == ReduceOps.SUM and isinstance(ast.src[0], LazyOp) and ast.src[0].op == BinaryOps.MUL:
      ast = LazyOp(FusedOps.MULACC, ast.src[0].src, ast.arg)
    created_context = context is None
    if context is None: context = dict()
    if not created_context and ast in context: return context[ast]
    srcs = [self.exec_ast(x, context=context) if isinstance(x, LazyOp) else self.from_lazybuffer(x) for x in ast.src]
    ret = self.buffer(self.fxn_for_op[ast.op](*([self.to_underlying(x) for x in srcs] + ([ast.arg] if ast.arg is not None else []))))
    if DEBUG >= 3: print(f"*** {'exec' if created_context else '    '} {GlobalCounters.mem_used/1e9:5.2f} GB op: {ast.op:20s} out({ret.dtype.name}): {str(ret.shape):30s} in({len(srcs)}):", list(set(x.shape for x in srcs)), ast.arg if ast.arg is not None else "")
    if not created_context: context[ast] = ret
    if output is not None and output.output_buffer is not None:
      assert output.output_buffer.size == ret.size, output.output_buffer.dtype == ret.dtype
      output.output_buffer._buf = ret._buf
      return output.output_buffer
    else:
      return ret

class FlopCounter:
  def __init__(self, tup:Tuple[Tuple[int, ...], DType, int]): self.shape, self.dtype, self.flops = tup
  def consume_flops(self):
    self.flops, ret = 0, self.flops
    return ret
from tinygrad.shape.shapetracker import ShapeTracker
shape_fxn_for_op: Dict[Op, Callable] = {
  UnaryOps.CAST: lambda self,dtype: (self.shape, dtype, self.consume_flops() + prod(self.shape)),
  **{op:lambda self: (self.shape, self.dtype, self.consume_flops() + prod(self.shape)) for op in UnaryOps if op != UnaryOps.CAST},
  **{op:lambda self,y: (self.shape, max(self.dtype, y.dtype), self.consume_flops() + y.consume_flops() + prod(self.shape)) for op in BinaryOps},
  **{op:lambda self,new_shape: (new_shape, self.dtype, self.consume_flops() + prod(self.shape)) for op in ReduceOps},
  **{op:functools.partial(lambda mop,self,arg: (ShapeTracker(self.shape).movement_op(mop, arg).shape, self.dtype, self.consume_flops()), op) for op in MovementOps}}
InterpretedFlopCounter = Interpreted(FlopCounter, shape_fxn_for_op, lambda x: FlopCounter((x.shape, x.dtype, 0)), lambda x: x)
def get_lazyop_info(ast:LazyOp) -> FlopCounter: return InterpretedFlopCounter.exec_ast(ast)

from tinygrad.codegen.ast import ASTKernel
from tinygrad.helpers import DType

class Compiled:
  def __init__(self, buffer: Type[RawBuffer], codegen: Type[ASTKernel], runtime):
    self.buffer, self.codegen, self.runtime = buffer, codegen, runtime
    self.method_cache: Dict[str, ASTRunner] = {}

  def exec_ast(self, ast:LazyOp, output):
    # all movementops do nothing in a Compiled buffer!
    if ast.op in MovementOps and not isinstance(ast.src[0], LazyOp) and ast.src[0].realized is not None: return ast.src[0].realized

    k = self.codegen(ast, output)

    # this is the default now
    if getenv("ENABLE_METHOD_CACHE", 1):
      if k.key not in self.method_cache: self.method_cache[k.key] = k.codegen().build(self.runtime)
      elif DEBUG >= 4: print(f"method cache hit : {k.key}")
      prg = self.method_cache[k.key]
    else:
      prg = k.codegen().build(self.runtime)

    if getenv("PRINT_AST", "") == prg.name or getenv("PRINT_AST", "") == "1":
      k.print()
      print(prg.prg)

    # check if we can reuse the output buffer
    # if it's aliased, don't use it
    # NOTE: this is pretty wrong actually, who knows where else this buffer is used?
    output.realized = output.output_buffer
    if output.realized is not None:
      for a in get_buffers(ast):
        if a.realized == output.realized and not a.st.contiguous:
          output.realized = None
          break

    # we don't have an output buffer, we have to create it
    if output.realized is None:
      output.realized = self.buffer(prod(output.shape), output.dtype)

    prg.exec(k.bufs)
    return output.realized
