from typing import Final, Dict, Callable, ClassVar, List, Optional, NamedTuple, DefaultDict, Tuple, Set, Union
import math, collections
from tinygrad.codegen.linearizer import Linearizer, UOps, UOp, LocalBuffer
from tinygrad.ops import ASTRunner, Op, UnaryOps, BinaryOps, FusedOps
from tinygrad.helpers import partition, ImageDType, DEBUG, dtypes, colored, getenv, prod
from tinygrad.runtime.lib import RawConst
from tinygrad.shape.symbolic import DivNode, AndNode, render_python, NumNode, Variable, Node, SumNode, MulNode
from tinygrad.lazy import LazyBuffer

# div is different in cl than python
render_cl = render_python.copy()
render_cl[DivNode] = lambda self,ops,ctx: f"({self.a.render(ops, ctx)}/{self.b})"
render_cl[AndNode] = lambda self,ops,ctx: f"({'&&'.join(sorted([x.render(ops,ctx) for x in self.nodes]))})"

class CStyleLanguage(NamedTuple):
  kernel_prefix: str = ""
  buffer_prefix: str = ""
  buffer_suffix: str = ""
  smem_prefix: str = ""
  barrier: str = ""
  gid: List[str] = []
  lid: List[str] = []
  extra_args: List[str] = []
  float4: Optional[str] = None
  half_prekernel: Optional[str] = None
  double_prekernel: Optional[str] = None
  uses_vload: bool = False

def to_image_idx(base_shape:Tuple[int, ...], idxy:Node, valid:Node, validhacks=False) -> Tuple[Node, Node]:
  idy = (idxy//(4*base_shape[1]))
  if validhacks and valid.min == 0:
    idx = (idxy//4) + (idy*-base_shape[1])
    # find the ones in idx that didn't factorize and remove them (TODO: this is not universal)
    if isinstance(idx, SumNode):
      unfactored, idx_nodes = partition(idx.nodes, lambda x: isinstance(x, MulNode) and x.b == -base_shape[1])
      assert len(unfactored) <= 1
      idx = Variable.sum(idx_nodes)
      unfactored = (Variable.sum(unfactored) // base_shape[1])
      idy += unfactored
      # ugh really...handtuned garbage
      if idx.min >= (base_shape[1]*3)//4:
        idx -= base_shape[1]
        idy += 1
  else:
    idx = (idxy//4)%base_shape[1]
  if DEBUG >= 5: print("to_image_idx", base_shape, idx.min, idx.max, idy.min, idy.max, idx, idy)
  return idx, idy

code_for_op: Final[Dict[Op, Callable]] = {
  UnaryOps.EXP2: lambda x: f"exp2({x})",
  UnaryOps.LOG2: lambda x: f"log2({x})",
  UnaryOps.SIN: lambda x: f"sin({x})",
  UnaryOps.SQRT: lambda x: f"sqrt({x})",
  BinaryOps.ADD: lambda a,b: f"({a}+{b})", BinaryOps.SUB: lambda a,b: f"({a}-{b})",
  BinaryOps.MUL: lambda a,b: f"({a}*{b})", BinaryOps.DIV: lambda a,b: f"({a}/{b})",
  BinaryOps.MAX: lambda a,b: f"max({a},{b})",
  BinaryOps.CMPEQ: lambda a,b: f"({a}=={b})", FusedOps.MULACC: lambda a,b,c: f"(({a}*{b})+{c})"
}

def add_gl_dimension(args, i, var, local_size, xid):
  # for M1 tensor core stuff, support > 3 dims
  if i >= 2 and len(args[0]) > len(xid):
    # do this on the x dim for warps
    if len(local_size) == 2: local_size.append(1)
    local_size[-1] *= var.max+1
    lidx = Variable(xid[0], 0, prod(x.max+1 for x in args[0][2:])-1)
    lidx = (lidx//((lidx.max+1)//local_size[-1]))%(var.max+1)
    assert lidx.max == var.max and lidx.min == var.min
    return f"{{ int {var.expr} = {lidx.render(render_cl)};  /* {var.max+1} */"
  else:
    local_size.append(var.max+1)
    return f"{{ int {var.expr} = {xid[min(len(xid), len(args[0]))-1-i]};  /* {var.max+1} */"

def uops_to_cstyle(uops:List[UOp], bufs:List[Union[LocalBuffer,LazyBuffer]], lang:CStyleLanguage) -> Tuple[str, List[int], List[int]]:
  prekernel: Set[str] = set()
  kernel = []
  global_size = []
  local_size = []
  pend_close = None

  bufnames = [b.name if isinstance(b, LocalBuffer) else f"data{i}" for i,b in enumerate(bufs)]

  depth = 0
  def kk(s): kernel.append("  "*depth+s)

  for uop,newvar,vin,args in uops:
    if uop == UOps.LOOP:
      for i,var in enumerate(args[0]):
        if isinstance(var, NumNode):
          if args[1] == "global" and lang.gid: global_size.append(1)
          if args[1] == "local" and lang.lid: local_size.append(1)
          # one number, not an index
          kk("{")
        else:
          if args[1] == "global" and lang.gid:
            kk(add_gl_dimension(args, i, var, global_size, lang.gid))
          elif args[1] == "local" and lang.lid:
            kk(add_gl_dimension(args, i, var, local_size, lang.lid))
          else:
            if getenv("NOUNROLL"): kk("#pragma unroll(1)")   # prevent loop unrolling
            kk(f"for (int {var.expr} = {var.min}; {var.expr} <= {var.max}; ++{var.expr}) {{")
      depth += 1
    elif uop == UOps.BARRIER:
      kk(lang.barrier)
    elif uop == UOps.ENDLOOP:
      if args[1] == "local" and len(lang.lid):
        # TODO: this is a bit of a hack. the local loop isn't real on the GPU
        kk(f"if ({Variable.sum(args[0]).render(render_cl)} == 0) {{")
        pend_close = "}"*(len(args[0])+1) + f" /* {args[1]} */"
      else:
        if args[1] == "global" and pend_close:
          depth -= 1
          kk(pend_close)
          pend_close = None
        depth -= 1
        kk("}"*len(args[0]) + f" /* {args[1]} */")
    elif uop == UOps.WMMA:
      # ((lidx2*32)+(lidx3*4)+(lidx4*16)+(lidx5*8)+(lidx6*2))
      kk("{ simdgroup_float8x8 a,b,c;")
      kk(f"a.thread_elements()[0] = {vin[0].render()}; a.thread_elements()[1] = {vin[1].render()};")
      kk(f"b.thread_elements()[0] = {vin[2].render()}; b.thread_elements()[1] = {vin[3].render()};")
      kk(f"c.thread_elements()[0] = {vin[4].render()}; c.thread_elements()[1] = {vin[5].render()};")
      kk("simdgroup_multiply_accumulate(c, a, b, c);")
      #kk("acc0_0 = simdidx*2;")
      kk(f"{vin[4].render()} = c.thread_elements()[0]; {vin[5].render()} = c.thread_elements()[1]; }}")
    elif uop == UOps.CONST:
      assert newvar is not None
      if args == -math.inf:
        kk(f"{newvar.render(True)} = -INFINITY;")
      elif newvar.dtype == dtypes._float4:
        kk(f"{newvar.render(True)} = {{ {args}f, {args}f, {args}f, {args}f }};")
      else:
        kk(f"{newvar.render(True)} = {args}f;")
    elif uop == UOps.ALU:
      assert newvar is not None
      if newvar in vin:
        kk(f"{newvar.render()} = {code_for_op[args](*[x.render() for x in vin])};")
      else:
        kk(f"{newvar.render(True)} = {code_for_op[args](*[x.render() for x in vin])};")
    elif uop == UOps.LOAD and newvar is not None:
      # TODO: merge with CONST?
      if bufs[args.i] is not None and isinstance(bufs[args.i].realized, RawConst):
        assert newvar.dtype == dtypes.float, "const can't be float4"
        x = bufs[args.i].realized._buf
        if math.isnan(x): val = "NAN"
        elif math.isinf(x): val = ("-" if x < 0 else "") + "INFINITY"
        else: val = f"{x}" +  ("f" if not dtypes.is_int(bufs[args.i].dtype) else "")
      elif isinstance(bufs[args.i].dtype, ImageDType):
        assert newvar.dtype == dtypes._float4, f"image must be float4 {newvar}"
        prekernel.add("const sampler_t smp = CLK_NORMALIZED_COORDS_FALSE | CLK_ADDRESS_CLAMP | CLK_FILTER_NEAREST;\n")
        idx, idy = to_image_idx(bufs[args.i].dtype.shape, args.idx, args.valid)
        val = f"read_imagef({bufnames[args.i]}, smp, (int2)({idx.render(render_cl)}, {idy.render(render_cl)}))"
      else:
        if lang.uses_vload and bufs[args.i].dtype == dtypes.float16:
          if newvar.dtype == dtypes._float4:
            val = f"vload_half4(0, {bufnames[args.i]}+{(args.idx).render(render_cl)})"
          else:
            val = f"vload_half({args.idx.render(render_cl)}, {bufnames[args.i]})"
        else:
          if newvar.dtype == dtypes._float4:
            val = f"({newvar.dtype.name})(*(({lang.smem_prefix if isinstance(bufs[args.i], LocalBuffer) else lang.buffer_prefix}{bufs[args.i].dtype.name}4*)({bufnames[args.i]}+{args.idx.render(render_cl)})))"
          else:
            val = f"*({bufnames[args.i]}+{args.idx.render(render_cl, strip_parens=True)})"
      # NOTE: if min and max are both 0, it should be a CONST in the Linearizer
      if args.valid.min == 1: kk(f"{newvar.render(True)} = {val};")
      else:
        casts = {dtypes._float4: ("", f"{lang.float4}(0.0f, 0.0f, 0.0f, 0.0f)"), dtypes.half: ("(half)", "(half)(0.0f)"), dtypes.float: ("(float)", "0.0f")}[newvar.dtype]
        kk(f"{newvar.render(True)} = ({args.valid.render(render_cl)}) ? {casts[0]}({val}) : {casts[1]};")
    elif uop == UOps.STORE and (vin[0].dtype == dtypes.float or (vin[0].dtype == dtypes._float4 and vin[0].offset is not None)):
      assert not isinstance(bufs[args.i].dtype, ImageDType), "image store must be float4"
      assert args.valid.min == 1, "store must be valid"
      if lang.uses_vload and bufs[args.i].dtype == dtypes.float16:
        kk(f"vstore_half({vin[0].render()}, {args.idx.render(render_cl)}, {bufnames[args.i]});")
      else:
        kk(f"*({bufnames[args.i]}+{args.idx.render(render_cl, strip_parens=True)}) = {vin[0].render()};")
    elif uop == UOps.CAST and newvar is not None and newvar.dtype == dtypes._float4:
      kk(f"{newvar.render(True)} = {lang.float4}({','.join([x.render() for x in vin])});")
    elif uop == UOps.STORE and len(vin) != 0 and vin[0].dtype == dtypes._float4 and vin[0].offset is None:
      assert args.valid.min == 1, "store must be valid"
      if isinstance(bufs[args[0]].dtype, ImageDType):
        idx, idy = to_image_idx(bufs[args.i].dtype.shape, args[1], args[2])
        kk(f"write_imagef({bufnames[args.i]}, (int2)({idx.render(render_cl)}, {idy.render(render_cl)}), {vin[0].render()});")
      elif lang.uses_vload and bufs[args.i].dtype == dtypes.float16:
        kk(f"vstore_half4({vin[0].render()}, {args.idx.render(render_cl)}, {bufnames[args.i]});")
      else:
        kk(f"*(({lang.smem_prefix if isinstance(bufs[args.i], LocalBuffer) else lang.buffer_prefix}{bufs[args.i].dtype.name}4*)({bufnames[args.i]}+{args.idx.render(render_cl)})) = ({bufs[args.i].dtype.name}4){vin[0].render()};")
    elif uop == UOps.DEFINE_LOCAL:
      kk(lang.smem_prefix + f"float {args[0]}[{args[1]}];")
    else:
      raise RuntimeError(f"failed to render {uop}")

  buftypes = [(i,f"{'read_only' if i > 0 else 'write_only'} image2d_t" if x.dtype.name.startswith('image') else
               ("const " if i > 0 else "")+lang.buffer_prefix+x.dtype.name+"*"+lang.buffer_suffix) for i,x in enumerate(bufs)
               if not isinstance(x, LocalBuffer) and not isinstance(x.realized, RawConst)]
  prg = ''.join([f"{lang.kernel_prefix} void KERNEL_NAME_PLACEHOLDER(",] +
    [', '.join([f'{t} {bufnames[i]}' for i,t in buftypes] + lang.extra_args)] +
    [") {\n"] + list(prekernel) + ['\n'.join(kernel), "\n}"])

  if lang.half_prekernel and any(x.dtype == dtypes.float16 for x in bufs): prg = ''.join([f"{lang.half_prekernel}", "\n", prg])
  return prg, global_size, local_size

class CStyleCodegen(Linearizer):
  lang: ClassVar[CStyleLanguage] = CStyleLanguage()
  supports_constant_folding: bool = True
  supports_float4: bool = True
  supports_float4_alu: bool = True

  # for renaming
  kernel_cnt: Final[DefaultDict[str, int]] = collections.defaultdict(int)
  kernel_name_cache: Final[Dict[str, Tuple[str, str]]] = {}

  def codegen(self):
    self.process()
    self.hand_coded_optimizations()
    #self.limit_global_dims(len(self.lang.gid))
    self.linearize()

    prg, global_size, local_size = uops_to_cstyle(self.uops, self.bufs, self.lang)

    # painfully name the function something unique
    if prg in CStyleCodegen.kernel_name_cache: function_name, display_name = CStyleCodegen.kernel_name_cache[prg]
    else:
      CStyleCodegen.kernel_cnt[self.function_name] += 1
      suffix = f"{'n'+str(CStyleCodegen.kernel_cnt[self.function_name]-1)}" if CStyleCodegen.kernel_cnt[self.function_name] > 1 else ""
      CStyleCodegen.kernel_name_cache[prg] = function_name, display_name = self.function_name+suffix, self.display_name+colored(suffix, 'BLACK')

    return ASTRunner(function_name, prg.replace("KERNEL_NAME_PLACEHOLDER", function_name),
      global_size[::-1], local_size[::-1],
      op_estimate=self.info.flops, mem_estimate=self.mem_estimate, display_name=display_name)
