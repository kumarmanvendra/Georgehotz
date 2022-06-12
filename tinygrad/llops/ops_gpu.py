import functools
import numpy as np
import pyopencl as cl
from tinygrad.helpers import prod
from tinygrad.ops import UnaryOps, BinaryOps, ReduceOps, MovementOps, ProcessingOps

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

i32 = np.int32
def roundup(x, n=4): return (x+(n-1))//n * n
def sync(): cl_queue.finish()

class GPUBuffer:
  def __init__(self, shape, hostbuf=None):
    require_init_gpu()
    self.shape, self.dtype = tuple(shape), np.float32
    self.cl = hostbuf.cl if isinstance(hostbuf, GPUBuffer) else cl.Buffer(cl_ctx, cl.mem_flags.READ_WRITE, 4*roundup(prod(shape)))  # padding
    if hostbuf is not None and not isinstance(hostbuf, GPUBuffer):
      cl.enqueue_copy(cl_queue, self.cl, hostbuf.astype(np.float32).ravel())

  def __repr__(self):
    return f"<GPUBuffer with shape {self.shape!r}>"

  @staticmethod
  def fromCPU(x):
    return GPUBuffer(x.shape, x.view(np.ndarray))

  def toCPU(self):
    data = np.empty(self.shape, dtype=np.float32)
    sync()
    cl.enqueue_copy(cl_queue, data, self.cl, is_blocking=True)
    return data

def buffer_np(x):
  return cl.Buffer(cl_ctx, cl.mem_flags.READ_WRITE | cl.mem_flags.COPY_HOST_PTR, hostbuf=x)

@functools.lru_cache
def clbuild(name, prg):
  clprg = cl.Program(cl_ctx, prg).build().__getattr__(name)
  def run(*args): clprg(cl_queue, *args)
  return run

def unary_op(op, x, ret):
  if op == UnaryOps.RELU: code = 'max(a, (float)0.)'
  elif op == UnaryOps.EXP: code = 'exp(a)'
  elif op == UnaryOps.LOG: code = 'log(a)'
  elif op == UnaryOps.NEG: code = '-a'
  elif op == UnaryOps.SIGN: code = 'sign(a)'
  else: raise Exception(f"{op} isn't supported")
  unop = clbuild("unop", """
  __kernel void unop(__global const float4 *a_g, __global float4 *res_g) {
    int gid = get_global_id(0);
    float4 a = a_g[gid];
    res_g[gid] = """+code+""";
  }""")
  unop([roundup(prod(ret.shape))//4], None, x.cl, ret.cl)
  return ret

def binary_op(op, x, y, ret):
  if op == BinaryOps.ADD: code = "a+b"
  elif op == BinaryOps.SUB: code = "a-b"
  elif op == BinaryOps.MUL: code = "a*b"
  elif op == BinaryOps.DIV: code = "b/a"
  elif op == BinaryOps.POW: code = "pow(a,b)"
  elif op == BinaryOps.CMPEQ: code = "(float4)(1.0f*(a.x==b.x), 1.0f*(a.y==b.y), 1.0f*(a.z==b.z), 1.0f*(a.w==b.w))"
  else: raise Exception(f"{op} isn't supported")
  assert x.shape == ret.shape and y.shape == ret.shape
  binop = clbuild("binop", """
  __kernel void binop(__global const float4 *a_g, __global const float4 *b_g, __global float4 *res_g) {
    int gid = get_global_id(0);
    float4 a = a_g[gid];
    float4 b = b_g[gid];
    res_g[gid] = """+code+""";
  }""")
  binop([roundup(prod(ret.shape))//4], None, x.cl, y.cl, ret.cl)
  return ret

def reduce_op(op, inp, ret):
  if op == ReduceOps.SUM:
    code = "out += a"
    start = "0.0"
  elif op == ReduceOps.MAX:
    code = "out = max(a,out)"
    start = "-INFINITY"
  else: raise Exception(f"{op} isn't supported")
  # TODO: this is insanely slow
  # NOTE: ret.shape can be (1,), it's mostly by luck that this works
  reduce = clbuild("reduce", """
  __kernel void reduce(__global const float *a_g, int sz, __global float *res_g, int prod, int n_dims,
                       __global const int *shape_x, __global const int *shape_ret) {
    int gid = get_global_id(0);

    float out = """+start+""";
    for (int x = 0; x < sz; x++) {
      int idx = 0;  // compute index into a_g
      int tprod = prod;
      int tsz = sz;
      for (int dim = 0; dim < n_dims; dim++) {
        idx *= shape_x[dim];
        if (shape_x[dim] == shape_ret[dim]) {   // dim from gid, don't reduce
          tprod /= shape_x[dim];
          idx += (gid / tprod) % shape_x[dim];
        } else {  // dim from x
          tsz /= shape_x[dim];
          idx += (x / tsz) % shape_x[dim];
        }
      }
      float a = a_g[idx];
      """+code+""";
    }
    res_g[gid] = out;
  }""")
  reduce([prod(ret.shape)], None, inp.cl,
    i32(prod(inp.shape)//prod(ret.shape)), ret.cl,
    i32(prod(ret.shape)), i32(len(ret.shape)),
    buffer_np(np.array(inp.shape, dtype=np.int32)),
    buffer_np(np.array(ret.shape, dtype=np.int32)))

def reshape(x, ret):
  cl.enqueue_copy(cl_queue, ret.cl, x.cl)

def perm_axis(inp, order, ret):
  perm = clbuild("perm", """
  __kernel void perm(__global const float *a_g, __global float *res_g, int n_axis,
                       __global const int *shape, __global const int *order) {
    int gid = get_global_id(0);
    int gi = gid;
    int idx = 0;
    for(int i = n_axis-1; i>-1; i--) {
      int stride = 1;
      for(int j=order[i]+1; j<n_axis; j++) stride *= shape[j];
      idx += (gi % shape[order[i]])*stride;
      gi /= shape[order[i]];
    }
    res_g[gid] = a_g[idx];
  }""")
  perm([prod(inp.shape)], None, inp.cl, ret.cl, i32(len(inp.shape)),
    buffer_np(np.array(inp.shape, dtype=np.int32)),
    buffer_np(np.array(order, dtype=np.int32)))

# TODO: merge this with perm axis
def inner_slice(x, arg, ret):
  shift = [y[0] for y in arg]
  gslice = clbuild("gslice", """
  __kernel void gslice(__global const float *input, __global float *output, int prod, int n_dims,
                       __global const int *shape_x, __global const int *shape_ret,
                       __global const int *shift) {
    int gid = get_global_id(0);
    int iptr = 0;
    int zero = 1;
    for (int dim = 0; dim < n_dims; dim++) {
      prod /= shape_ret[dim];
      int sidx = (gid / prod) % shape_ret[dim] + shift[dim];
      zero &= (sidx >= 0 && sidx < shape_x[dim]);
      iptr = (iptr * shape_x[dim]) + sidx;
    }
    output[gid] = zero ? input[iptr] : 0.0;
  }""")
  gslice([prod(ret.shape)], None,
    x.cl, ret.cl, i32(prod(ret.shape)), i32(len(ret.shape)),
    buffer_np(np.array(x.shape, dtype=np.int32)),
    buffer_np(np.array(ret.shape, dtype=np.int32)),
    buffer_np(np.array(shift, dtype=np.int32)))

def expand(x, ret):
  assert len(x.shape) == len(ret.shape)

  dimlist, complist = [], [] # note: len(dimlist) may be less than n_dims
  def push(dim, comp):
    if len(complist) > 0 and complist[-1] == comp:
      dimlist[-1] *= dim
    elif comp != (False, False):
      dimlist.append(dim); complist.append(comp)
  for i,j in zip(x.shape, ret.shape): # group together any adjacent dimensions that we can to simplify broadcasting
    push(np.int32(max(i,j)), (i > 1, j > 1))
  prod_list = np.array(dimlist, dtype=i32)[-1::-1].cumprod(dtype=i32)[-1::-1] # take cumprod from back to front

  ndims = len(complist)
  args = "".join([f", int d{i}" for i in range(ndims)] + [f", int p{i}" for i in range(ndims-1)])
  compute_idx_rets = "".join([f"\n    int idx_ret{i} = (gid0 / {f'p{i}' if i < ndims-1 else '1'}) % d{i};" for i in range(ndims)])

  idx_exprs = ["0", "0"] # [idx_x, idx_y]
  for i in range(ndims):
    for j in range(2):
      if complist[i][j]:
        idx_exprs[j] = "idx_ret%d + d%d*(%s)" % (i, i, idx_exprs[j])

  expandop = clbuild("expandop", """__kernel void expandop(__global const float *x_g, __global float *res_g"""+args+""") {
    int gid0 = get_global_id(0);"""+compute_idx_rets+"""
    res_g[gid0] = x_g["""+idx_exprs[0]+"""];\n}""")
  expandop([prod_list[0] if len(dimlist) > 0 else 1], None, x.cl, ret.cl, *dimlist, *(prod_list[1:]))

def movement_op(op, x, ret, arg=None):
  if op == MovementOps.RESHAPE: reshape(x, ret)
  elif op == MovementOps.PERMUTE: perm_axis(x, arg, ret)
  elif op == MovementOps.SLICE: inner_slice(x, arg, ret)
  elif op == MovementOps.EXPAND: expand(x, ret)

def conv(x,w,ret,C):
  # input  = (bs, groups, cin, iy, ix)
  # weight = (groups, rcout, cin, H, W)
  # output = (bs, groups, rcout, oy, ox)
  conv_prg = clbuild("conv", """
  __kernel void conv(__global const float *input, __global const float *weight, __global float *output,
    int H, int W, int groups, int rcout, int cin, int oy, int ox, int iy, int ix, int ys, int xs, int bs) {

    int B = get_global_id(0)/(groups*rcout);  // range 0-bs
    int g = (get_global_id(0)/rcout)%groups;
    int c = get_global_id(0) % rcout;

    int Y = get_global_id(1);  // range 0-oy
    int X = get_global_id(2);  // range 0-ox
    int IY = Y*ys;
    int IX = X*xs;

    float acc = 0.0;
    for (int ci = 0; ci < cin; ci++) {
      for (int y = IY; y < IY+H; y++) { for (int x = IX; x < IX+W; x++) {
        acc += input[B*groups*cin*iy*ix + g*cin*iy*ix + ci*iy*ix + y*ix + x] * \
          weight[g*rcout*cin*H*W + c*cin*H*W + ci*H*W + (y-IY)*W + (x-IX)];
      } }
    }
    output[B*groups*rcout*oy*ox + g*rcout*oy*ox + c*oy*ox + Y*ox + X] = acc;
  }""")

  conv_prg([C.bs*C.groups*C.rcout, C.oy, C.ox], None, x.cl, w.cl, ret.cl, *[i32(x) for x in C[0:12]])

# tensx = (bs, groups*cin, iy, ix)
# tensw = (groups*rcout, cin, H, W)
# ggg = (bs, groups*rout, oy, ox)

def convdw(x,grad_output,dw,C):
  convdw_prg = clbuild("convdw", """
  __kernel void convdw(__global const float *tensx, __global const float *ggg, __global float *dw,
    int H, int W, int groups, int rcout, int cin, int oy, int ox, int iy, int ix, int ys, int xs, int bs) {

    int g = get_global_id(0)/(rcout*cin) ; // range 0-groups
    int c = (get_global_id(0)/(cin)) %rcout; // range 0-rcout
    int ci = get_global_id(0) % cin;        // range 0-cin
    int y = get_global_id(1);  // range 0-H
    int x = get_global_id(2);  // range 0-W

    float acc = 0.0;
    for (int Y = 0; Y < oy; Y++) { for (int X = 0; X < ox; X++) {
      for (int B = 0; B < bs; B++) {
        acc += ggg[B*groups*rcout*oy*ox + +g*rcout*oy*ox + c*oy*ox + Y*ox + X] * \
          tensx[B*groups*cin*iy*ix + g*cin*iy*ix + ci*iy*ix + (Y*ys+y)*ix + X*xs+x];
        }
    } }
    dw[get_global_id(0)*H*W + y*W + x] = acc;
  }""")
  convdw_prg([C.groups*C.rcout*C.cin, C.H, C.W], None, x.cl, grad_output.cl, dw.cl, *[i32(x) for x in C[0:12]])

def convdx(grad_output,w,dx,C):
  convdx_prg = clbuild("convdx", """
  __kernel void convdx(__global const float *tensw, __global const float *ggg, __global float *dx,
    int H, int W, int groups, int rcout, int cin, int oy, int ox, int iy, int ix, int ys, int xs, int bs) {

    int B = get_global_id(0);
    int g = get_global_id(1);
    int ci = get_global_id(2);

    for (int Y = 0; Y < iy; Y++) { for (int X = 0; X < ix; X++) {
      dx[B*groups*cin*iy*ix + g*cin*iy*ix + ci*iy*ix + Y*ix + X] = 0.0;
    } }

    for (int Y = 0; Y < oy; Y++) { for (int X = 0; X < ox; X++) {
      for (int y = 0; y < H; y++) { for (int x = 0; x < W; x++) {
        float acc = 0.0;
        for (int c = 0; c < rcout; c++) {
          acc += ggg[B*groups*rcout*oy*ox + g*rcout*oy*ox + c*oy*ox + Y*ox + X] * \
            tensw[g*rcout*cin*H*W + c*cin*H*W + ci*H*W + y*W + x];
        }
        dx[B*groups*cin*iy*ix + g*cin*iy*ix + ci*iy*ix + (Y*ys+y)*ix + X*xs+x] += acc;
      } }
    } }
  }
  """)
  convdx_prg([C.bs, C.groups, C.cin], None, w.cl, grad_output.cl, dx.cl, *[i32(x) for x in C[0:12]])

def processing_op(op,a,b,ret,C):
  if op == ProcessingOps.CONV: conv(a,b,ret,C)
  elif op == ProcessingOps.CONVT: convdx(a,b,ret,C)
  elif op == ProcessingOps.CONVDW: convdw(a,b,ret,C)
