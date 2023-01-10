from __future__ import annotations
import os, functools
import numpy as np
import pyopencl as cl  # type: ignore
from collections import defaultdict
from typing import List, Tuple, Optional, Dict, Union, Set
from tinygrad.helpers import prod
from tinygrad.ops import DEBUG, ASTKernel, UnaryOps, BinaryOps, ReduceOps, LazyOp, Op, ExplicitExecAST, GlobalCounters
from tinygrad.shapetracker import ShapeTracker

CLCACHE = int(os.getenv("CLCACHE", "1"))
class CLBuffer:
  def __init__(self, size):
    if len(CL.BUFFER_CACHE[size]) > 0:
      self.cl = CL.BUFFER_CACHE[size].pop()
    else:
      # TODO: on GPU OOM, clear the cache
      self.cl = cl.Buffer(CL().cl_ctx, cl.mem_flags.READ_WRITE, size)
      CL.mem_used += self.cl.size

  def __del__(self):
    if CLCACHE:
      CL.BUFFER_CACHE[self.cl.size].append(self.cl)
    else:
      CL.mem_used -= self.cl.size

class CL:
  CACHE, kernel_count, mem_used, time_sum, ops_sum = None, -1, 0, 0.0, 0.0
  BUFFER_CACHE : Dict[int, List[cl.Buffer]] = defaultdict(list)
  cl_ctx : Optional[cl.Context] = None
  cl_queue : Optional[cl.CommandQueue] = None
  def __init__(self):
    if CL.cl_queue is not None:  # already initted
      return
    devices = sum([x.get_devices(device_type=cl.device_type.GPU) for x in cl.get_platforms()], [])
    if len(devices) == 0:  # settle for CPU
      devices = sum([x.get_devices(device_type=cl.device_type.CPU) for x in cl.get_platforms()], [])
    CL.cl_ctx = cl.Context(devices=[devices[int(os.getenv("CL_DEVICE", "0"))]])
    if len(devices) > 1 or DEBUG >= 1:
      print(f"using {CL.cl_ctx.devices}")
    CL.cl_queue = cl.CommandQueue(self.cl_ctx, properties=cl.command_queue_properties.PROFILING_ENABLE)  # this is an in-order command queue

  @staticmethod
  def enqueue_copy(a, b, is_blocking=False):
    if CL.CACHE is not None:
      assert False, f"can't copy {a} -> {b} while caching"
    if DEBUG >= 1:
      print(f"**CL**        copy in {b.shape}" if isinstance(b, np.ndarray) else f"**CL**        copy OUT {a.shape}")
    cl.enqueue_copy(CL().cl_queue, a, b, is_blocking=is_blocking)

@functools.lru_cache(maxsize=None)
class CLProgram:
  kernel_cnt : Dict[str, int] = defaultdict(int)
  def __init__(self, name:str, prg:str, options:Tuple[str, ...]=tuple(), argdtypes=None, rename=True, binary=False):
    self.name = f"{name}{('_N'+str(CLProgram.kernel_cnt[name])) if CLProgram.kernel_cnt[name] else ''}" if rename else name
    self.prg, self.options, self.argdtypes = prg.replace(f"{name}(", f"{self.name}(") if rename else prg, options, argdtypes
    self.clprogram = cl.Program(CL().cl_ctx, CL().cl_ctx.devices, [self.prg]) if binary else cl.Program(CL().cl_ctx, self.prg)  # type: ignore
    self.clprg = self.clprogram.build(options=list(self.options)).__getattr__(self.name)
    if self.argdtypes is not None:
      self.clprg.set_scalar_arg_dtypes(self.argdtypes)
    CLProgram.kernel_cnt[name] += 1
  def __call__(self, *args, op_estimate=0):
    CL.kernel_count += 1
    if CL.CACHE is not None:
      CL.CACHE.append((self, args))
    else:
      e = self.clprg(CL().cl_queue, *args)
    if DEBUG >= 4:
      print(self.prg)
    if DEBUG >= 2:
      CL.cl_queue.finish()
    if DEBUG >= 1:
      CL.time_sum += 0 if DEBUG <= 1 or CL.CACHE is not None else (e.profile.end - e.profile.start)
      CL.ops_sum += op_estimate
      print(f"**CL** {CL.kernel_count:6d} {self.name:28s} args {len(args[2:]):5d}  kernels {str(args[0]):18s} {str(args[1]):12s} OPs {op_estimate/1e6:7.1f}M/{CL.ops_sum/1e9:7.2f}G  mem {CL.mem_used/1e9:5.2f} GB " +
            ("" if DEBUG <= 1 or CL.CACHE is not None else f"tm {(e.profile.end - e.profile.start)/1e3:9.2f}us/{CL.time_sum/1e6:9.2f}ms ({op_estimate/(e.profile.end - e.profile.start):8.2f} GFLOPS)"))
    GlobalCounters.global_ops += op_estimate
    GlobalCounters.global_mem += sum([x.size//4 for x in args[2:] if isinstance(x, cl.Buffer)])

# **** end CL wrappers ****

def ast_kernel_codegen(cls, ast:LazyOp, k:ASTKernel):
  # TODO: make sure it stays split on the image boundary, regardless of stride
  k.process()

  first_reduce, last_reduce = k.first_reduce, len(k.shapes[0])

  output_shape = k.shapes[0][:k.first_reduce]
  kernel = [f"int idx{i} = get_global_id({min(3, len(output_shape))-1-i});\n" for i in range(min(3, len(output_shape)))]
  if len(output_shape) > 3:
    # compact all the dimensions into the final one
    for i in range(len(output_shape)-1, 2, -1):
      kernel += [f"int idx{i} = idx2 % {output_shape[i]};", f"idx2 = idx2 / {output_shape[i]};\n"]
    output_shape = list(output_shape[0:2]) + [prod(output_shape[2:])]

  bufs_to_delete : Set[int] = set()
  seen_idx = set()
  def compute_buf_index(st, buf_index, offset=0):
    key = f"{buf_index}_{offset}"
    # add the index if we don't have it
    if key not in seen_idx:
      idx_pieces = [str(st.offset + offset)] + [(f"idx{i}*{st}" if st != 1 else f"idx{i}") for i,(sh,st) in enumerate(zip(k.shapes[buf_index][0:last_reduce], k.strides[buf_index][0:last_reduce])) if sh != 1 and st != 0]
      if st.needs_valid(): kernel.append(f"bool bufvalid{key} = true;")
      kernel.append(f"int bufi{key} = " + '('+' + '.join(idx_pieces)+');\n')
      if len(st.views) > 1:
        extra_idx = ';'.join([v.expr for v in st.views[0:-1][::-1] if v.expr not in ['', 'idx=idx', 'valid=valid']])
        kernel.append(extra_idx.replace("//", "/").replace("idx", f"bufi{key}").replace("valid", f"bufvalid{key}") + ";\n")
      seen_idx.add(key)
    return key

  def store(buf_index, value, offset=0):
    st = k.bufs[buf_index].st
    if offset > 0: assert len(st.views) == 1
    key = compute_buf_index(st, buf_index, offset)
    kernel.append(f"data{buf_index}[bufi{key}] = {value};\n")

  @functools.lru_cache(None)
  def load(buf_index, offset=0):
    st = k.bufs[buf_index].st
    if offset > 0: assert len(st.views) == 1
    key = compute_buf_index(st, buf_index, offset)

    # constant folding
    constant_fold = None
    if k.bufs[buf_index]._base_shape == (1,) and k.bufs[buf_index]._backing:
      bufs_to_delete.add(buf_index)
      constant_fold = f"({k.bufs[buf_index]._backing[0]})"

    ldr = f"data{buf_index}[bufi{key}]" if not constant_fold else constant_fold
    ldr = f"(bufvalid{key} ? {ldr} : 0.0)" if st.needs_valid() else ldr
    kernel.append(f"float val{key} = {ldr};\n")
    return f"val{key}"

  def ast_parse(x, reduce=False) -> str:
    if not isinstance(x, LazyOp): return load(k.bufs.index(x))
    if isinstance(x.op, ReduceOps) and not reduce: return "acc"
    values = [ast_parse(v, reduce) for v in x.src]
    code = GPUBuffer.code_for_op[x.op]  # TODO: replace this with a function
    if isinstance(x.op, ReduceOps): return code.replace("A", values[0])
    if len(values) >= 1: code = code.replace("A", values[0])
    if len(values) >= 2: code = code.replace("B", values[1])
    return code

  # early ast
  if k.reduceop:
    full_shape = [x for x in k.shapes if x != k.shapes[0]]
    full_shape = k.shapes[0] if len(full_shape) == 0 else full_shape[0]

    kernel.append(f"float acc = {cls.start_for_op[k.reduceop.op]};\n")
    for i in range(first_reduce, last_reduce):
      kernel.append(f"for (int idx{i} = 0; idx{i} < {full_shape[i]}; idx{i}++) {{\n")
    kernel.append("  acc = " + ast_parse(k.reduceop, reduce=True) + ";\n")
    kernel += ["}\n"] * (last_reduce - first_reduce)

  # late ast
  process_ast = ast_parse(ast)
  store(0, process_ast)
  kernel.append("}")

  # kernel function definition
  function_name = ("re_S" if k.reduceop else "ew_S") + '_'.join([str(x) for x in k.bufs[0].shape if x != 1])
  kernel = [f"__kernel void {function_name}(",] + [', '.join(f'__global float *data{i}' for i in range(len(k.bufs)) if i not in bufs_to_delete)] + [") {\n"] + kernel

  # compile kernel
  fxn = CLProgram(function_name, ' '.join(kernel))

  def runner(*bufs):
    clbufs = [x.cl for i,x in enumerate(bufs) if i not in bufs_to_delete]
    return fxn(output_shape[::-1] if len(output_shape) > 0 else [1], None, *clbufs, op_estimate=k.info.flops)
  return runner

class GPUBuffer(ExplicitExecAST):
  code_for_op : Dict[Op, str] = {
    UnaryOps.NOOP: "(A)", UnaryOps.NEG: "(-(A))", UnaryOps.RELU: "max(A, (float)0.)",
    UnaryOps.EXP: "exp(A)", UnaryOps.LOG: "log(A)", UnaryOps.SIGN: "sign(A)", UnaryOps.RECIPROCAL: "((float)1.0/A)",
    BinaryOps.ADD: "(A+B)", BinaryOps.SUB: "(A-B)", BinaryOps.MUL: "(A*B)",
    BinaryOps.DIV: "(A/B)", BinaryOps.POW: "pow(A,B)", BinaryOps.CMPEQ: "(A==B)",
    ReduceOps.SUM: "(acc + A)", ReduceOps.MAX: "max(A, acc)"
  }
  start_for_op = {ReduceOps.SUM: "0.0", ReduceOps.MAX: "-INFINITY"}

  def __init__(self, shape:Union[ShapeTracker, Tuple[int, ...]], hostbuf:Optional[GPUBuffer]=None, backing:Optional[np.ndarray]=None):
    super().__init__(shape, hostbuf)
    self._buf : Optional[CLBuffer] = hostbuf._buf if hostbuf is not None else None
    self._base_shape : Tuple[int, ...] = hostbuf._base_shape if hostbuf is not None else self.shape
    self._backing : Optional[np.ndarray] = hostbuf._backing if hostbuf is not None else backing
    # early copy in for large buffers
    if self._backing is not None and self._backing.shape != (1,):
      self.cl
  
  @property
  def cl(self):
    if self._buf is None:
      self._buf = CLBuffer(4*prod(self._base_shape))
    if self._backing is not None:
      CL.enqueue_copy(self._buf.cl, self._backing, is_blocking=False)
      self._backing = None
    return self._buf.cl

  def __repr__(self): return f"<GPUBuffer {str(self.st)}>"

  @staticmethod
  def fromCPU(x): return GPUBuffer(x.shape, backing=x.view(np.ndarray).astype(np.float32).ravel())

  def toCPU(self):
    data = np.empty(self.shape, dtype=np.float32)
    CL.enqueue_copy(data, self.contiguous().cl, is_blocking=True)
    return data

  #func_cache : Dict[str, Any] = {}
  @classmethod
  def exec_ast(cls, ast:LazyOp):
    k = ASTKernel(ast)
    # TODO: can't cache with constant folding
    #if k.key not in GPUBuffer.func_cache:
    #GPUBuffer.func_cache[k.key] = ast_kernel_codegen(cls, ast, k)
    #GPUBuffer.func_cache[k.key](*k.bufs)
    ast_kernel_codegen(cls, ast, k)(*k.bufs)
    return k.ret
