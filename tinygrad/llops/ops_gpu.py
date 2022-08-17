from __future__ import annotations
import os, functools
import numpy as np
import pyopencl as cl  # type: ignore
from collections import defaultdict
from typing import List, Tuple, Optional, Dict, Union, Set, Tuple
from tinygrad.helpers import prod, ConvArgs
from tinygrad.ops import DEBUG, UnaryOps, BinaryOps, ReduceOps, MovementOps, ProcessingOps
from tinygrad.shapetracker import ShapeTracker, View, strides_for_shape

CLCACHE = int(os.getenv("CLCACHE", "1"))
class CLBuffer:
  def __init__(self, size):
    if len(CL.BUFFER_CACHE[size]) > 0: self.cl = CL.BUFFER_CACHE[size].pop()
    else:
      CL.mem_used += size
      # TODO: on GPU OOM, clear the cache
      self.cl = cl.Buffer(CL().cl_ctx, cl.mem_flags.READ_WRITE, size)

  def __del__(self):
    if CLCACHE: CL.BUFFER_CACHE[self.cl.size].append(self.cl)
    else: CL.mem_used -= self.cl.size

class CL:
  CACHE, kernel_count, mem_used, time_sum, ops_sum = None, -1, 0, 0.0, 0.0
  BUFFER_CACHE : Dict[int, List[cl.Buffer]] = defaultdict(list)
  cl_ctx : Optional[cl.Context] = None
  cl_queue : Optional[cl.CommandQueue] = None
  def __init__(self):
    if CL.cl_queue is not None: return
    devices = sum([x.get_devices(device_type=cl.device_type.GPU) for x in cl.get_platforms()], [])
    if len(devices) == 0:  # settle for CPU
      devices = sum([x.get_devices(device_type=cl.device_type.CPU) for x in cl.get_platforms()], [])
    CL.cl_ctx = cl.Context(devices=[devices[int(os.getenv("CL_DEVICE", "0"))]])
    if len(devices) > 1 or DEBUG >= 1: print(f"using {CL.cl_ctx.devices}")
    CL.cl_queue = cl.CommandQueue(self.cl_ctx, properties=cl.command_queue_properties.PROFILING_ENABLE)  # this is an in-order command queue

  @staticmethod
  def enqueue_copy(a, b, is_blocking=False):
    if CL.CACHE is not None: assert False, "can't copy while caching"
    if DEBUG >= 1: print(f"**CL**        copy in {b.shape}" if isinstance(b, np.ndarray) else f"**CL**        copy OUT {a.shape}")
    cl.enqueue_copy(CL().cl_queue, a, b, is_blocking=is_blocking)

@functools.lru_cache(maxsize=None)
class CLProgram:
  kernel_cnt = 0
  def __init__(self, name:str, prg:str, options:Tuple[str, ...]=tuple(), argdtypes=None):
    self.name, self.prg, self.options, self.argdtypes = f"{name}_{CLProgram.kernel_cnt}", prg.replace(f"{name}(", f"{name}_{CLProgram.kernel_cnt}("), options, argdtypes
    self.clprogram = cl.Program(CL().cl_ctx, self.prg)
    self.clprg = self.clprogram.build(options=list(self.options)).__getattr__(self.name)
    if self.argdtypes is not None: self.clprg.set_scalar_arg_dtypes(self.argdtypes)
    CLProgram.kernel_cnt += 1
  def __call__(self, *args, op_estimate=0):
    CL.kernel_count += 1
    if CL.CACHE is not None: CL.CACHE.append((self, args))
    else: e = self.clprg(CL().cl_queue, *args)
    if DEBUG >= 2: CL.cl_queue.finish()
    if DEBUG >= 1:
      CL.time_sum += 0 if DEBUG <= 1 or CL.CACHE is not None else (e.profile.end - e.profile.start)
      CL.ops_sum += op_estimate
      print(f"**CL** {CL.kernel_count:6d} {self.name:20s} args {len(args[2:]):5d}  kernels {str(args[0]):18s} {str(args[1]):12s} OPs {op_estimate/1e6:6.1f}M/{CL.ops_sum/1e9:7.2f}G  " + \
            ("" if DEBUG <= 1 or CL.CACHE is not None else f"tm {(e.profile.end - e.profile.start)/1e3:9.2f}us/{CL.time_sum/1e6:9.2f}ms ({op_estimate/(e.profile.end - e.profile.start):8.2f} GFLOPS)"))
    if DEBUG >= 4: print(self.prg)

# **** end CL wrappers ****

class GPUBuffer:
  code_for_op = {
    UnaryOps.NOOP: "(A)", UnaryOps.NEG: "(-(A))", UnaryOps.RELU: "max(A, (float)0.)", UnaryOps.EXP: "exp(A)", UnaryOps.LOG: "log(A)", UnaryOps.SIGN: "sign(A)",
    BinaryOps.ADD: "(A+B)", BinaryOps.SUB: "(A-B)", BinaryOps.MUL: "(A*B)", BinaryOps.DIV: "(A/B)", BinaryOps.POW: "pow(A,B)", BinaryOps.CMPEQ: "(A==B)",
    ReduceOps.SUM: "(acc + A)", ReduceOps.MAX: "max(A, acc)"
  }
  start_for_op = {ReduceOps.SUM: "0.0", ReduceOps.MAX: "-INFINITY"}

  def __init__(self, shape:Union[ShapeTracker, Tuple[int, ...]], hostbuf:Optional[GPUBuffer]=None, backing:Optional[np.ndarray]=None):
    self.st = shape if isinstance(shape, ShapeTracker) else ShapeTracker(tuple(shape))
    self.shape = self.st.shape
    self._buf : Optional[CLBuffer] = hostbuf._buf if hostbuf is not None else None
    self._base_shape : Tuple[int, ...] = hostbuf._base_shape if hostbuf is not None else self.shape
    self._backing : Optional[np.ndarray] = hostbuf._backing if hostbuf is not None else backing
    # early copy in for large buffers
    if self._backing is not None and self._backing.shape != (1,): self.cl
  
  @property
  def cl(self):
    if self._buf is None: self._buf = CLBuffer(4*prod(self._base_shape))
    if self._backing is not None:
      CL.enqueue_copy(self._buf.cl, self._backing, is_blocking=False)
      self._backing = None
    return self._buf.cl

  def __repr__(self): return f"<GPUBuffer with shape {self.shape!r}>"

  @staticmethod
  def fromCPU(x): return GPUBuffer(x.shape, backing=x.view(np.ndarray).astype(np.float32).ravel())

  def toCPU(self):
    data = np.empty(self.shape, dtype=np.float32)
    CL.enqueue_copy(data, self.contiguous_op().cl, is_blocking=True)
    return data

  def contiguous_view(x, name:str) -> str:
    return f"inline float get_{name}(__global const float *x, int gid) {{ int valid = 1; int idx = gid; {x.st.expr().replace('//', '/')}; return valid ? x[idx] : 0.0;}}"

  def contiguous_view_constant_fold(x, name:str) -> Tuple[str, bool]:
    if x._base_shape == (1,) and x._backing is not None:
      return f"inline float get_{name}(int gid) {{ int valid = 1; int idx = gid; {x.st.expr().replace('//', '/')}; return valid ? {x._backing[0]} : 0.0;}}", False
    else:
      return x.contiguous_view(name), True

  def unary_op(x, op:UnaryOps): return type(x)(x.shape)._processing_op([("A", x)], GPUBuffer.code_for_op[op])
  def binary_op(x, op:BinaryOps, y:GPUBuffer): return type(x)(x.shape)._processing_op([("A", x), ("B", y)], GPUBuffer.code_for_op[op])
  def contiguous_op(x): return x if x.st.contiguous else x.unary_op(UnaryOps.NOOP)
  def movement_op(x, op:MovementOps, arg) -> GPUBuffer: return type(x)(ShapeTracker(x.st).movement_op(op, arg), x)
  def reduce_op(x, op:ReduceOps, new_shape:Tuple[int, ...]): return type(x)(new_shape)._processing_op([("A", x)], code="acc", earlycode=GPUBuffer.code_for_op[op], earlybufs=set("A"), start=GPUBuffer.start_for_op[op])

  #REQUIRES_SIMPLE_REDUCE = True
  # is there a downside to REQUIRES_SIMPLE_REDUCE always?
  def _processing_op(ret, bufs: List[Tuple[str, GPUBuffer]]=[], code:str="acc", C:Optional[ConvArgs]=None, start="0.0", reduce_shape=None, earlybufs:Set[str]=set(), earlycode:str="acc") -> GPUBuffer:
    assert C is None

    # this takes a ret index to an inp index, indexing 0 on the reduced strides
    # if it's not a reduce, this should be a NOOP
    reduce_shape = (bufs[0][1].shape, ret.shape) if reduce_shape is None else reduce_shape
    view = View(reduce_shape[1], strides_for_shape(reduce_shape[0]))
    loop : List[Tuple[str, str]] = []
    if reduce_shape[1] != reduce_shape[0]:   # this is a reduce
      # reverse operation of expand, this validates inputs
      # generate loops with combined adjacent reduce axis
      acc = 1
      for shp,stride in ShapeTracker(reduce_shape[1]).movement_op(MovementOps.EXPAND, reduce_shape[0]).views[-1].shape_strides[::-1]:
        if stride == 0: loop.append((f"for (int axis_{len(loop)} = 0; axis_{len(loop)} < {shp}; axis_{len(loop)}++) {{", f"idx += {acc}; }} idx -= {shp*acc};"))
        acc *= shp

    kernel_name = "reduce" if len(loop) > 0 else "elementwise"
    views = {name:buf.contiguous_view_constant_fold(name) for name, buf in bufs}
    buf_types = [f"__global const float *{name}_g" for name, _ in bufs if name not in views or views[name][1]] 
    conv_prg = CLProgram(kernel_name, f"""{chr(10).join([x[0] for x in views.values()])}
    __kernel void {kernel_name}({','.join(["__global float* restrict output"] + buf_types)}) {{ 
      float acc = {start}; int gid = get_global_id(0); int idx = gid; {view.expr.replace('//', '/')};
      {' '.join([ls for ls, _ in loop[::-1]])}
{chr(10).join([f'        float {name} = ' + (f'get_{name}({name}_g, idx);' if views[name][1] else f'get_{name}(idx);') for name, _ in bufs if name in earlybufs])}
        acc = {earlycode};
      {' '.join([le for _, le in loop])} idx = gid;
{chr(10).join([f'      float {name} = ' + (f'get_{name}({name}_g, idx);' if views[name][1] else f'get_{name}(idx);') for name, _ in bufs if name not in earlybufs])}
      output[gid] = {code};
    }}""", argdtypes=tuple(None if i < 1+len(buf_types) else np.int32 for i in range(1+len(buf_types))))
    conv_prg([prod(ret.shape), 1, 1], None, ret.cl, *[buf.cl for name, buf in bufs if name not in views or views[name][1]], op_estimate=prod(reduce_shape[0])*len(earlybufs) + prod(reduce_shape[1])*len(bufs))
    return ret
