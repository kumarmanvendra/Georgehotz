import functools
import numpy as np
import pyopencl as cl
from typing import List, Tuple
from tinygrad.helpers import prod
from tinygrad.llops.ops_cpu import unary_op
from tinygrad.ops import UnaryOps, BinaryOps, ReduceOps, MovementOps, ProcessingOps
from tinygrad.shapetracker import ShapeTracker, View, strides_for_shape
from collections import defaultdict

cl_ctx, cl_queue = None, None
def get_cl_ctx(): return cl_ctx
def get_cl_queue(): return cl_queue
def require_init_gpu():
  global cl_ctx, cl_queue
  if cl_ctx is None:
    devices = cl.get_platforms()[0].get_devices(device_type=cl.device_type.GPU)
    if len(devices) == 0:  # settle for CPU
      devices = cl.get_platforms()[0].get_devices(device_type=cl.device_type.CPU)
    cl_ctx = cl.Context(devices=devices)
    cl_queue = cl.CommandQueue(cl_ctx)  # this is an in-order command queue

def roundup(x, n=4): return (x+(n-1))//n * n

class GPUBuffer:
  def __init__(self, shape, hostbuf=None):
    require_init_gpu()
    self.st = ShapeTracker(shape)
    self.shape = self.st.shape
    self.cl = hostbuf.cl if hostbuf is not None else cl.Buffer(cl_ctx, cl.mem_flags.READ_WRITE, 4*roundup(prod(self.shape)))  # padding

  def __repr__(self):
    return f"<GPUBuffer with shape {self.shape!r}>"

  @staticmethod
  def fromCPU(x):
    ret = GPUBuffer(x.shape)
    # TODO: this is blocking even though we told it not to
    cl.enqueue_copy(cl_queue, ret.cl, x.view(np.ndarray).astype(np.float32).ravel(), is_blocking=False)
    return ret

  def toCPU(self):
    data = np.empty(self.shape, dtype=np.float32)
    cl.enqueue_copy(cl_queue, data, contiguous(self).cl, is_blocking=True)
    return data

class CLProgram:
  def __init__(self, name, prg, options, argdtypes):
    self.name = name
    self.built = cl.Program(cl_ctx, prg).build(options=options)
    self.clprg = self.built.__getattr__(name)
    if argdtypes is not None: self.clprg.set_scalar_arg_dtypes(argdtypes)
  def __call__(self, *args):
    #print(f"running {self.name} with {args[0]} count {len(args)-2}")
    self.clprg(cl_queue, *args)

@functools.lru_cache(maxsize=None)
def clbuild(name, prg, options=tuple(), argdtypes=None):
  #print("cache miss", prg[0:100])
  return CLProgram(name, prg, options, argdtypes)

def contiguous_view(x:GPUBuffer, name:str) -> str:
  return f"inline float get_{name}(__global const float *x, int gid) {{ int valid = 1; int idx = gid; {x.st.expr().replace('//', '/')}; return valid ? x[idx] : 0.0;}}"

def elementwise_op_compile(bufs: List[Tuple[str, GPUBuffer]], code:str) -> str:
  return '\n'.join([contiguous_view(buf, name) for name, buf in bufs])+ \
    "inline float _ewop("+','.join(["int gid", "float acc"]+[f"__global const float *{name}_g" for name, _ in bufs])+") {"+ \
    '\n'.join([f"float {name} = get_{name}({name}_g, gid);" for name, _ in bufs])+ \
    f"return {code}; }}"

def _processing_op(bufs: List[Tuple[str, GPUBuffer]]=[], code:str="acc", C=None):
  if C is not None:
    ret = GPUBuffer(C.out_shape)
    ints = ''.join(f"int {x} = {getattr(C, x)};" for x in ["H", "W", "ys", "xs", "dx", "dy", "px", "py"] + (["cin"] if C.cin == 1 else []))
    params = [(f"int {x}", getattr(C, x)) for x in ["groups", "rcout", "oy", "ox", "iy", "ix"] + (["cin"] if C.cin > 1 else [])]
    options = []
    if C.px == 0 and C.py == 0: options.append("-DALLVALID")
    if C.oy == 1 and C.ox == 1: options.append("-DONEBYONE")
    global_size = [C.bs*C.cout, C.oy, C.ox]
    ewbufs = bufs[2:]
  else:
    ret = GPUBuffer(bufs[0][1].shape)
    ints = ''
    params = []
    options = ["-DNOCONV"]
    global_size = [prod(ret.shape), 1, 1]
    ewbufs = bufs

  conv_params = ["__global float* restrict output"] + \
                [f"__global const float *{name}_g" for name, _ in bufs] + \
                [x[0] for x in params]
  conv_prg = clbuild("conv", elementwise_op_compile(ewbufs, code)+"""
  __kernel void conv("""+','.join(conv_params)+""") {
    float acc = 0.0;
    int gid = get_global_id(0);
    """+ints+"""

#ifndef NOCONV
    int B = get_global_id(0)/(groups*rcout);  // range 0-bs
    int g = (get_global_id(0)/rcout)%groups;
    int c = get_global_id(0) % rcout;

#ifdef ONEBYONE
    int Y = 0;
    int X = 0;
#else
    int Y = get_global_id(1);  // range 0-oy
    int X = get_global_id(2);  // range 0-ox
    gid = gid*oy*ox + Y*ox + X;
#endif

    int IY = Y*ys;
    int IX = X*xs;

    for (int ci = 0; ci < cin; ci++) {
      for (int y = 0; y < H; y++) { for (int x = 0; x < W; x++) {
        int idx_y = y*dy + IY - py;
        int idx_x = x*dx + IX - px;
#ifdef ALLVALID
        acc += input_g[B*groups*cin*iy*ix + g*cin*iy*ix + ci*iy*ix + idx_y*ix + idx_x] * \
          weight_g[g*rcout*cin*H*W + c*cin*H*W + ci*H*W + y*W + x];
#else
        int valid = (idx_y >= 0 && idx_y < iy && idx_x >= 0 && idx_x < ix);
        acc += valid * input_g[B*groups*cin*iy*ix + g*cin*iy*ix + ci*iy*ix + clamp(idx_y, 0, iy-1)*ix + clamp(idx_x, 0, ix-1)] * \
          weight_g[g*rcout*cin*H*W + c*cin*H*W + ci*H*W + y*W + x];
#endif
      } }
    }
#endif

    output[gid] = _ewop("""+','.join(["gid", "acc"]+[f"{name}_g" for name, _ in ewbufs])+""");
  }""", options=tuple(options),
  argdtypes=tuple([None]*(1+len(bufs)) + [np.int32]*len(params)))
  conv_prg(global_size, None, ret.cl, *[buf.cl for _, buf in bufs], *[x[1] for x in params])
  return ret

def movement_op(op, x, arg):
  ret = GPUBuffer(x.st, x)
  ret.shape = ret.st.movement_op(op, arg).shape
  return ret

def processing_op(op,x,w,C):
  assert op == ProcessingOps.CONV, f"{op} isn't supported"
  return _processing_op([("input", contiguous(x)), ("weight", contiguous(w))], "acc", C)

code_for_op = {
  UnaryOps.NOOP: "(A)", UnaryOps.RELU: "max(A, (float)0.)", UnaryOps.EXP: "exp(A)", UnaryOps.LOG: "log(A)", UnaryOps.NEG: "(-(A))", UnaryOps.SIGN: "sign(A)",
  BinaryOps.ADD: "(A+B)", BinaryOps.SUB: "(A-B)", BinaryOps.MUL: "(A*B)", BinaryOps.DIV: "(B/A)", BinaryOps.POW: "pow(A,B)", BinaryOps.CMPEQ: "(A==B)",
}

def unary_op(op, x): return _processing_op([("A", x)], code_for_op[op])
def binary_op(op, x, y): return _processing_op([("A", x), ("B", y)], code_for_op[op])
def contiguous(x:GPUBuffer): return x if x.st.contiguous else unary_op(UnaryOps.NOOP, x)

def reduce_op(op, inp, new_shape):
  ret = GPUBuffer(new_shape)
  if op == ReduceOps.SUM: code, start = "out += a", "0.0"
  elif op == ReduceOps.MAX: code, start = "out = max(a,out)", "-INFINITY"
  else: raise Exception(f"{op} isn't supported")

  # reverse operation of expand, this validates inputs
  st = ShapeTracker(ret.shape).movement_op(MovementOps.EXPAND, inp.shape)
  # this takes a ret index to an inp index, indexing 0 on the reduced strides
  view = View(ret.shape, strides_for_shape(inp.shape))

  # generate loops with combined adjacent reduce axis
  acc = 1
  loop_start, loop_end = [], []
  for shp,stride in st.views[-1].shape_strides[::-1]:
    if stride == 0:
      loop_start.append(f"for (int axis_{len(loop_start)} = 0; axis_{len(loop_start)} < {shp}; axis_{len(loop_start)}++) {{")
      loop_end.append(f"idx += {acc}; }} idx -= {shp*acc};")
    acc *= shp

  # TODO: support multistage reduces
  prg = contiguous_view(inp, 'A')+"""
  __kernel void reduce(__global const float *a_g, __global float *res_g) {
    int gid = get_global_id(0); int idx = gid;"""+view.expr.replace('//', '/')+""";
    float out = """+start+""";\n"""+ \
      '\n'.join(loop_start[::-1])+"""
        float a = get_A(a_g, idx);
        """+code+""";\n"""+ \
      '\n'.join(loop_end)+"""
    res_g[gid] = out;
  }"""
  clbuild("reduce", prg)([prod(ret.shape)], None, inp.cl, ret.cl)
  return ret
