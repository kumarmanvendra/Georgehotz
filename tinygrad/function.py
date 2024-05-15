"""This is where the forwards and backwards passes live."""
import math
from typing import Tuple, List, Optional

from six import binary_type
from tinygrad.helpers import argsort
from tinygrad.dtype import dtypes, DType, sum_acc_dtype
from tinygrad.ops import UnaryOps, BinaryOps, TernaryOps, ReduceOps
from tinygrad.tensor import Function
from tinygrad.lazy import LazyBuffer
from tinygrad.shape.symbolic import sint

class Contiguous(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer: return x.contiguous()
  def backward(self, grad_output:LazyBuffer) -> LazyBuffer: return grad_output

class ContiguousBackward(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer: return x
  def backward(self, grad_output:LazyBuffer) -> LazyBuffer: return grad_output.contiguous()

class Cast(Function):
  def forward(self, x:LazyBuffer, dtype:DType, bitcast:bool=False) -> LazyBuffer:
    self.input_dtype, self.bitcast = x.dtype, bitcast
    return x.cast(dtype, bitcast)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer: return grad_output.cast(self.input_dtype, self.bitcast)

# ************* unary ops *************

class Neg(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer: return x.e(UnaryOps.NEG)
  def backward(self, grad_output:LazyBuffer) -> LazyBuffer: return grad_output.e(UnaryOps.NEG)

class Reciprocal(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer:
    self.ret = x.const(1).e(BinaryOps.DIV, x)
    return self.ret
  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return grad_output.e(UnaryOps.NEG).e(BinaryOps.MUL, self.ret).e(BinaryOps.MUL, self.ret)

class OldSin(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer:
    self.x = x
    return x.e(UnaryOps.SIN)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return self.x.const(math.pi / 2).e(BinaryOps.SUB, self.x).e(UnaryOps.SIN).e(BinaryOps.MUL, grad_output)

def _taylor(self, x:LazyBuffer, coefficients:List[float]) -> LazyBuffer:
  current_term = x.const(1)
  result = x.const(0)
  for i, coef in enumerate(coefficients):
    if i > 0: current_term = current_term.e(BinaryOps.MUL, x)
    result = result.e(BinaryOps.ADD, current_term.e(BinaryOps.MUL, x.const(coef)))
  return result

class Sin(Function):
  coefficients =  [0, 9.9999999999993782751e-01, 0, -1.6666666666432553012e-01, 0, 8.3333333187754141114e-03, 0, -1.9841266413256992626e-04,
                   0, 2.7556932047318987798e-06, 0, -2.5029522966723734896e-08, 0, 1.5401222741012872935e-10]

  def floor(self, x:LazyBuffer) -> LazyBuffer:
    x_dtype = x.dtype
    return x.cast(dtypes.ulong).cast(x_dtype)

  def two_pi_mod(self, x:LazyBuffer) -> LazyBuffer:
    x_dtype = x.dtype
    if self.device != "METAL": x = x.cast(dtypes.double)
    two_pi = x.const(math.pi * 2)
    div = x.e(BinaryOps.DIV, two_pi)
    floor_div = self.floor(div)
    subtraction = x.e(BinaryOps.SUB, two_pi.e(BinaryOps.MUL, floor_div))
    return subtraction.cast(x_dtype)

  def normalized_sin(self, x:LazyBuffer) -> Tuple[LazyBuffer, LazyBuffer]:
    pi = x.const(math.pi)
    signs = x.e(BinaryOps.CMPLT, x.const(0))
    abs_x = signs.e(TernaryOps.WHERE, x.e(UnaryOps.NEG), x)
    two_pi_x = self.two_pi_mod(abs_x)
    less_than_pi = two_pi_x.e(BinaryOps.CMPLT, pi)
    pi_x = less_than_pi.e(TernaryOps.WHERE, two_pi_x, two_pi_x.e(BinaryOps.SUB, pi))
    less_than_pi_half = pi_x.e(BinaryOps.CMPLT, pi.e(BinaryOps.DIV, pi_x.const(2)))
    pi_half_x = less_than_pi_half.e(TernaryOps.WHERE, pi_x, pi.e(BinaryOps.SUB, pi_x))
    signs = less_than_pi.e(BinaryOps.XOR, less_than_pi.const(True)).e(BinaryOps.XOR, signs)
    return (pi_half_x, signs)

  def taylor(self, x:LazyBuffer, coefficients:List[float]) -> LazyBuffer:
    current_term = x.const(1)
    result = x.const(0)
    for i, coef in enumerate(coefficients):
      if i > 0: current_term = current_term.e(BinaryOps.MUL, x)
      result = result.e(BinaryOps.ADD, current_term.e(BinaryOps.MUL, x.const(coef)))
    return result

  def forward(self, x:LazyBuffer) -> LazyBuffer:
    self.x = x
    normalized_x, signs = self.normalized_sin(x)
    result = self.taylor(normalized_x, self.coefficients)
    signed_result = signs.e(TernaryOps.WHERE, result.e(UnaryOps.NEG), result)
    return signed_result

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    x = self.x.e(BinaryOps.ADD, self.x.const(math.pi / 2))
    normalized_x, signs = self.normalized_sin(x)
    result = self.taylor(normalized_x, self.coefficients)
    signed_result = signs.e(TernaryOps.WHERE, result.e(UnaryOps.NEG), result)
    return signed_result.e(BinaryOps.MUL, grad_output)

# NOTE: maximum(x, 0) behaves differently where x=0
class Relu(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer:
    self.ret = x.e(BinaryOps.MAX, x.const(0))
    return self.ret

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return self.ret.const(0).e(BinaryOps.CMPLT, self.ret).cast(grad_output.dtype).e(BinaryOps.MUL, grad_output)

class Log2(Function):
  coefficients = [-3.72162108e+00, 1.01438705e+01, -1.59554068e+01, 1.97155445e+01, -1.78832735e+01, 1.17975216e+01,
                  -5.59830547e+00, 1.86329583e+00, -4.13182982e-01, 5.48583264e-02, -3.30087854e-03]

  def get_info(self, x:LazyBuffer) -> Tuple[LazyBuffer, LazyBuffer, LazyBuffer]:
    if x.dtype is dtypes.double:
      b = x.cast(dtypes.ulong, bitcast=True)
      int_repr = dtypes.long
      pow_shift = 2**52
      sig_shift = 2**9
      fix = 4607182418800017408
      bias = 1023
    elif x.dtype is dtypes.float:
      b = x.cast(dtypes.uint, bitcast=True)
      int_repr = dtypes.int
      pow_shift = 2**23
      sig_shift = 2**9
      fix = 1065353216
      bias = 127

    bpow = b.e(BinaryOps.DIV, b.const(pow_shift)).cast(int_repr, bitcast=True)
    pow = bpow.e(BinaryOps.SUB, b.const(bias).cast(int_repr))
    bsig = b.e(BinaryOps.MUL, b.const(sig_shift)).e(BinaryOps.DIV, b.const(sig_shift))
    sig = bsig.e(BinaryOps.ADD, bsig.const(fix)).cast(x.dtype, bitcast=True)
    nan = bpow.e(BinaryOps.CMPLT, bpow.const(sig_shift/2 - 1))
    return (pow, sig, nan)

  def forward(self, x:LazyBuffer) -> LazyBuffer:
    x_dtype = x.dtype
    self.x = x
    if x_dtype in (dtypes.float16, dtypes.bfloat16): x = x.cast(dtypes.float32)
    pow, sig, nan = self.get_info(x)
    t = _taylor(self, sig, self.coefficients).e(BinaryOps.ADD, pow.cast(x.dtype))
    handle_nan = nan.e(TernaryOps.WHERE, t, x.const(math.nan))
    handle_neg = x.e(BinaryOps.CMPLT, x.const(0.0)).e(TernaryOps.WHERE, handle_nan.const(math.nan), handle_nan)
    handle_0 = x.e(BinaryOps.CMPEQ, x.const(0.0)).e(TernaryOps.WHERE, handle_neg.const(-math.inf), handle_neg)
    handle_1 = x.e(BinaryOps.CMPEQ, x.const(1.0)).e(TernaryOps.WHERE, handle_0.const(0.0), handle_0)
    handle_inf = x.e(BinaryOps.CMPEQ, x.const(math.inf)).e(TernaryOps.WHERE, handle_1.const(math.inf), handle_1)
    return handle_inf.cast(x_dtype)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return grad_output.e(BinaryOps.DIV, self.x.e(BinaryOps.MUL, self.x.const(math.log(2))))

class Exp(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer:
    self.ret = x.e(BinaryOps.MUL, x.const(1/math.log(2))).e(UnaryOps.EXP2)
    return self.ret

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer: return self.ret.e(BinaryOps.MUL, grad_output)

class Exp2(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer:
    COEFFICIENTS = [1.00000000081861939449e+00, 6.93147182520959859175e-01, 2.40226495713301319013e-01, 5.55040941978386520583e-02,
                    9.61815259200062347422e-03, 1.33338111576375667293e-03, 1.54018907490696861651e-04, 1.52360250041314857400e-05,
                    1.32587007737466412782e-06, 1.06391894003459590264e-07, 6.77776413810625168015e-09]

    var = x.const(1)
    acc = x.const(0)
    for i, coef in enumerate(COEFFICIENTS):
      if i > 0: var = var.e(BinaryOps.MUL, x)
      term = var.e(BinaryOps.MUL, x.const(coef))
      acc = acc.e(BinaryOps.ADD, term)

    self.ret = acc
    return self.ret

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return self.ret.e(BinaryOps.MUL, self.ret.const(math.log(2))).e(BinaryOps.MUL, grad_output)

class Sqrt(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer:
    self.ret = x.e(UnaryOps.SQRT)
    return self.ret

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return grad_output.e(BinaryOps.DIV, self.ret.e(BinaryOps.MUL, self.ret.const(2)))

# NOTE: the implicit derivative of sigmoid is not stable
# https://towardsdatascience.com/derivative-of-the-sigmoid-function-536880cf918e
# TODO: have the backend automatically find this
class Sigmoid(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer:
    self.ret = x.const(1).e(BinaryOps.DIV, x.const(1).e(BinaryOps.ADD, x.e(BinaryOps.MUL, x.const(-1/math.log(2))).e(UnaryOps.EXP2)))
    return self.ret

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return self.ret.e(BinaryOps.MUL, self.ret.const(1).e(BinaryOps.SUB, self.ret)).e(BinaryOps.MUL, grad_output)

# ************* binary ops *************

class Less(Function):
  def forward(self, x:LazyBuffer, y:LazyBuffer) -> LazyBuffer: return x.e(BinaryOps.CMPLT, y)
  def backward(self, grad_output:LazyBuffer) -> Tuple[Optional[LazyBuffer], Optional[LazyBuffer]]: return None, None

class Eq(Function):
  def forward(self, x:LazyBuffer, y:LazyBuffer) -> LazyBuffer: return x.e(BinaryOps.CMPEQ, y)
  def backward(self, grad_output:LazyBuffer) -> Tuple[Optional[LazyBuffer], Optional[LazyBuffer]]: return None, None

class Xor(Function):
  def forward(self, x:LazyBuffer, y:LazyBuffer) -> LazyBuffer: return x.e(BinaryOps.XOR, y)

class Add(Function):
  def forward(self, x:LazyBuffer, y:LazyBuffer) -> LazyBuffer: return x.e(BinaryOps.ADD, y)

  def backward(self, grad_output:LazyBuffer) -> Tuple[Optional[LazyBuffer], Optional[LazyBuffer]]:
    return grad_output if self.needs_input_grad[0] else None, \
           grad_output if self.needs_input_grad[1] else None

class Sub(Function):
  def forward(self, x:LazyBuffer, y:LazyBuffer) -> LazyBuffer: return x.e(BinaryOps.SUB, y)

  def backward(self, grad_output:LazyBuffer) -> Tuple[Optional[LazyBuffer], Optional[LazyBuffer]]:
    return grad_output if self.needs_input_grad[0] else None, \
           grad_output.e(UnaryOps.NEG) if self.needs_input_grad[1] else None

class Mul(Function):
  def forward(self, x:LazyBuffer, y:LazyBuffer) -> LazyBuffer:
    self.x, self.y = x, y
    return x.e(BinaryOps.MUL, y)

  def backward(self, grad_output:LazyBuffer) -> Tuple[Optional[LazyBuffer], Optional[LazyBuffer]]:
    return self.y.e(BinaryOps.MUL, grad_output) if self.needs_input_grad[0] else None, \
           self.x.e(BinaryOps.MUL, grad_output) if self.needs_input_grad[1] else None

class Div(Function):
  def forward(self, x:LazyBuffer, y:LazyBuffer) -> LazyBuffer:
    self.x, self.y = x, y
    return x.e(BinaryOps.DIV, y)

  def backward(self, grad_output:LazyBuffer) -> Tuple[Optional[LazyBuffer], Optional[LazyBuffer]]:
    return grad_output.e(BinaryOps.DIV, self.y) if self.needs_input_grad[0] else None, \
           grad_output.e(UnaryOps.NEG).e(BinaryOps.MUL, self.x).e(BinaryOps.DIV, self.y.e(BinaryOps.MUL, self.y)) if self.needs_input_grad[1] else None  # noqa: E501

# ************* ternary ops *************

class Where(Function):
  def forward(self, x:LazyBuffer, y:LazyBuffer, z:LazyBuffer) -> LazyBuffer:
    self.x = x
    return self.x.e(TernaryOps.WHERE, y, z)

  def backward(self, grad_output:LazyBuffer) -> Tuple[None, Optional[LazyBuffer], Optional[LazyBuffer]]:
    return None, \
      self.x.e(TernaryOps.WHERE, grad_output, grad_output.const(0)) if self.needs_input_grad[1] else None, \
      self.x.e(TernaryOps.WHERE, grad_output.const(0), grad_output) if self.needs_input_grad[2] else None

# ************* reduce ops *************

class Sum(Function):
  def forward(self, x:LazyBuffer, axis:Tuple[int, ...]) -> LazyBuffer:
    self.input_shape = x.shape
    return x.r(ReduceOps.SUM, axis)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer: return grad_output.expand(self.input_shape)

class Max(Function):
  def forward(self, x:LazyBuffer, axis:Tuple[int, ...]) -> LazyBuffer:
    self.x, self.ret, self.axis = x, x.r(ReduceOps.MAX, axis), axis
    return self.ret

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    # 1s in locations where the max was chosen (can be two locations)
    max_is_1s = self.x.e(BinaryOps.CMPEQ, self.ret.expand(self.x.shape)).cast(dtypes.float)
    div = max_is_1s.r(ReduceOps.SUM, self.axis).expand(self.x.shape)
    return max_is_1s.e(BinaryOps.DIV, div).cast(grad_output.dtype).e(BinaryOps.MUL, grad_output.expand(self.x.shape))

# ************* movement ops *************

# NOTE: this is sum in reverse
class Expand(Function):
  def forward(self, x:LazyBuffer, shape:Tuple[int, ...]) -> LazyBuffer:
    self.expanded_axis = tuple(i for i, (si, so) in enumerate(zip(x.shape, shape)) if si != so)
    return x.expand(shape)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return grad_output.cast(sum_acc_dtype(grad_output.dtype)).r(ReduceOps.SUM, self.expanded_axis).cast(grad_output.dtype)

class Reshape(Function):
  def forward(self, x:LazyBuffer, shape:Tuple[int, ...]) -> LazyBuffer:
    self.input_shape = x.shape
    return x.reshape(shape)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer: return grad_output.reshape(self.input_shape)

class Permute(Function):
  def forward(self, x:LazyBuffer, order:Tuple[int, ...]) -> LazyBuffer:
    self.input_order = order
    return x.permute(order)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer: return grad_output.permute(argsort(self.input_order))

class Pad(Function):
  def forward(self, x:LazyBuffer, arg:Tuple[Tuple[int, int], ...]) -> LazyBuffer:
    self.narg = tuple([(p[0], s+p[0]) for s,p in zip(x.shape, arg)])
    return x.pad(arg)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer: return grad_output.shrink(self.narg)

class Shrink(Function):
  def forward(self, x:LazyBuffer, arg:Tuple[Tuple[sint, sint], ...]) -> LazyBuffer:
    self.narg = tuple([(p[0], s-p[1]) for s,p in zip(x.shape, arg)])
    return x.shrink(arg)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer: return grad_output.pad(self.narg)

class Flip(Function):
  def forward(self, x:LazyBuffer, axis:Tuple[int, ...]) -> LazyBuffer:
    self.arg = tuple([-1 if i in set(axis) else 1 for i in range(len(x.shape))])
    return x.stride(self.arg)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer: return grad_output.stride(self.arg)
