"""This is where the forwards and backwards passes live."""
import math
from typing import Tuple, Optional
from tinygrad.helpers import argsort
from tinygrad.dtype import dtypes, DType, sum_acc_dtype
from tinygrad.ops import UnaryOps, BinaryOps, TernaryOps, ReduceOps
from tinygrad.tensor import Function
from tinygrad.lazy import LazyBuffer
from tinygrad.shape.symbolic import sint
import numpy as np

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

class Sin(Function):

  # def taylor_sin(self, x:LazyBuffer) -> LazyBuffer:
  #   # Reduce to [0, 2pi]
  #   old_dtype = x.dtype
  #   # x = x.e(BinaryOps.SUB, x.e(BinaryOps.DIV, x.const(2 * math.pi)).cast(dtypes.int32).cast(old_dtype).e(BinaryOps.MUL, x.const(2 * math.pi)))
  #   # x = x.e(BinaryOps.SUB, x.e(BinaryOps.DIV, x.const(math.pi)).cast(dtypes.int32).cast(old_dtype).e(BinaryOps.MUL, x.const(math.pi)))
  #
  #   # x = x.cast(dtypes.float64)
  #   TWOPI = 6.2831853071795864769252867665590057683943387987502
  #   print(TWOPI)
  #   # q = x.e(BinaryOps.DIV, x.const(2 * math.pi))
  #   q = x.e(BinaryOps.DIV, x.const(TWOPI))
  #   # # q = x.e(BinaryOps.DIV, x.const(math.pi))
  #   print("q: ")
  #   print(__import__('tinygrad').Tensor(q).numpy()[0])
  #   # q = q.cast(dtypes.float32)
  #   # print("q: ")
  #   # print(__import__('tinygrad').Tensor(q).numpy()[0])
  #   # q_floor = q.cast(dtypes.int32).cast(old_dtype)
  #   # print("q_floor: ")
  #   # print(__import__('tinygrad').Tensor(q_floor).numpy()[0])
  #   # diff = q.e(BinaryOps.SUB, q_floor)
  #   # print("diff: ")
  #   # print(__import__('tinygrad').Tensor(diff).numpy())
  #   # # x = diff.e(BinaryOps.MUL, x.const(2 * math.pi))
  #   # x = diff.e(BinaryOps.MUL, x.const(math.pi))
  #
  #   # Import Tensor from tinygrad
  #   print("x: ")
  #   print(__import__('tinygrad').Tensor(x).numpy())
  #   # q = q.e(BinaryOps.MUL, x.const(2 * math.pi))
  #   # x = x.e(BinaryOps.SUB, q)
  #
  #   no_terms = 20
  #   # no_terms = 16
  #   res = x.const(0)
  #   term = x
  #   for i in range(no_terms):
  #     if i % 2 == 0:
  #       res = res.e(BinaryOps.ADD, term)
  #     else:
  #       res = res.e(BinaryOps.SUB, term)
  #     # term = term.e(BinaryOps.MUL, x).e(BinaryOps.DIV, x.const(2 * i + 2)).e(BinaryOps.MUL, x).e(BinaryOps.DIV, x.const(2 * i + 3))
  #     term = term.e(BinaryOps.MUL, x).e(BinaryOps.MUL, x).e(BinaryOps.DIV, x.const((2 * i + 2)*(2 * i + 3)))
  #   return res
  def sin_approx(self, buf:LazyBuffer) -> LazyBuffer:
    lookup_table = [0.7853981633974483, 0.4636476090008061, 0.24497866312686414, 0.12435499454676144, 0.06241880999595735, 0.031239833430268277, 0.015623728620476831, 0.007812341060101111, 0.0039062301319669718, 0.0019531225164788188, 0.0009765621895593195, 0.0004882812111948983, 0.00024414062014936177, 0.00012207031189367021, 6.103515617420877e-05, 3.0517578115526096e-05, 1.5258789061315762e-05, 7.62939453110197e-06, 3.814697265606496e-06, 1.907348632810187e-06, 9.536743164059608e-07, 4.7683715820308884e-07, 2.3841857910155797e-07, 1.1920928955078068e-07, 5.960464477539055e-08, 2.9802322387695303e-08, 1.4901161193847655e-08, 7.450580596923828e-09, 3.725290298461914e-09, 1.862645149230957e-09]
    two_neg_pow = [1, 0.5, 0.25, 0.125, 0.0625, 0.03125, 0.015625, 0.0078125, 0.00390625, 0.001953125, 0.0009765625, 0.00048828125, 0.000244140625, 0.0001220703125, 6.103515625e-05, 3.0517578125e-05, 1.52587890625e-05, 7.62939453125e-06, 3.814697265625e-06, 1.9073486328125e-06, 9.5367431640625e-07, 4.76837158203125e-07, 2.384185791015625e-07, 1.1920928955078125e-07, 5.960464477539063e-08, 2.9802322387695312e-08, 1.4901161193847656e-08, 7.450580596923828e-09, 3.725290298461914e-09, 1.862645149230957e-09]

    old_dtype = buf.dtype
    buf = buf.cast(dtypes.float32)
    # final_sign = buf.const(-1)
    whole_pi = buf.e(BinaryOps.DIV, buf.const(math.pi)).cast(dtypes.int32).cast(dtypes.float32)
    print("whole_pi: ")
    print(__import__('tinygrad').Tensor(whole_pi).numpy())
    whole_pi_mod_2 = whole_pi.e(BinaryOps.SUB, whole_pi.e(BinaryOps.DIV, buf.const(2.0)).cast(dtypes.int32).cast(dtypes.float32).e(BinaryOps.MUL, buf.const(2.0)))
    # whole_pi_mod_2 = whole_pi.e(BinaryOps.MOD, buf.const(2))
    print("whole_pi_mod_2: ")
    print(__import__('tinygrad').Tensor(whole_pi_mod_2).numpy())
    # if whole_pi % 2 == 0:
    # whole_pi_mod_2_is_even = whole_pi_mod_2.e(BinaryOps.CMPEQ, buf.const(0))
    whole_pi_mod_2_is_even = whole_pi_mod_2.e(BinaryOps.CMPEQ, buf.const(0))

    print(__import__('tinygrad').Tensor(whole_pi_mod_2_is_even).numpy())
    # exit()

    # CHECK TERNARY ORDER IF NOT WORKING
    # final_sign = whole_pi_mod_2.e(TernaryOps.WHERE, whole_pi_mod_2_is_even, buf.const(1), buf.const(-1))
    final_sign =  whole_pi_mod_2_is_even.e(TernaryOps.WHERE, buf.const(1), buf.const(-1))

    # angle_rad = angle % (math.pi / 2)
    angle_rad = buf.e(BinaryOps.SUB, buf.e(BinaryOps.DIV, buf.const(math.pi / 2)).cast(dtypes.int32).cast(dtypes.float32).e(BinaryOps.MUL, buf.const(math.pi / 2)))

    print("angle rad: ")
    print(__import__('tinygrad').Tensor(angle_rad).numpy())
    # angle_rad = angle_rad.e(BinaryOps.SUB, angle_rad.cast(dtypes.int32).cast(dtypes.float32))
    print("angle rad: ")
    print(__import__('tinygrad').Tensor(angle_rad).numpy())



    whole_halfpi = buf.e(BinaryOps.DIV, buf.const(math.pi / 2)).cast(dtypes.int32).cast(dtypes.float32)
    # whole_halfpi_mod_2 = whole_halfpi.e(BinaryOps.MOD, buf.const(2))
    whole_halfpi_mod_2 = whole_halfpi.e(BinaryOps.DIV, buf.const(2)).cast(dtypes.int32).cast(dtypes.float32)
    # if whole_halfpi % 2 == 0:
    whole_halfpi_mod_2_is_even = whole_halfpi_mod_2.e(BinaryOps.CMPEQ, buf.const(0))

    # CHECK TERNARY ORDER IF NOT WORKING
    angle_rad = whole_halfpi_mod_2_is_even.e(TernaryOps.WHERE, angle_rad, buf.const(math.pi / 2).e(BinaryOps.SUB, angle_rad))
    print("angle rad: ")
    print(__import__('tinygrad').Tensor(angle_rad).numpy())
    # angle_rad = whole_halfpi_mod_2_is_even.e(TernaryOps.WHERE, buf.const(math.pi / 2).e(BinaryOps.SUB, angle_rad), angle_rad)

    # Initialize variables
    K = 0.607252935
    angle_i = angle_rad
    # x = 1.0
    # y = 0.0
    # z = 0.0
    x = buf.const(1.0)
    y = buf.const(0.0)
    z = buf.const(0.0)

    # for i in range(30):
    for i in range(30):
        # if angle_i < 0:
        #     d = -1
        # else:
        #     d = 1

        is_positive = angle_i.e(BinaryOps.CMPLT, buf.const(0))
        d = is_positive.e(TernaryOps.WHERE, buf.const(-1), buf.const(1))
        # d = is_positive.e(TernaryOps.WHERE, buf.const(1), buf.const(-1))

        # x_new = x - d * y * two_neg_pow[i]
        x_new = x.e(BinaryOps.SUB, d.e(BinaryOps.MUL, y).e(BinaryOps.MUL, buf.const(two_neg_pow[i])))
        # y = y + d * x * two_neg_pow[i]
        y = y.e(BinaryOps.ADD, d.e(BinaryOps.MUL, x).e(BinaryOps.MUL, buf.const(two_neg_pow[i])))
        # z = z - d * lookup_table[i]
        z = z.e(BinaryOps.SUB, d.e(BinaryOps.MUL, buf.const(lookup_table[i])))

        x = x_new
        # angle_i -= d * lookup_table[i]
        angle_i = angle_i.e(BinaryOps.SUB, d.e(BinaryOps.MUL, buf.const(lookup_table[i])))

    # return y * K
    return y.e(BinaryOps.MUL, buf.const(K)).e(BinaryOps.MUL, final_sign).cast(old_dtype)
    # return y.e(BinaryOps.MUL, buf.const(K)).e(BinaryOps.MUL, final_sign)


  def forward(self, x:LazyBuffer) -> LazyBuffer:
    self.x = x
    return self.sin_approx(x)

    # return x.e(UnaryOps.SIN)

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    # return self.x.const(math.pi / 2).e(BinaryOps.SUB, self.x).e(UnaryOps.SIN).e(BinaryOps.MUL, grad_output)
    return self.taylor_sin(self.x.const(math.pi / 2).e(BinaryOps.SUB, self.x)).e(BinaryOps.MUL, grad_output)

# NOTE: maximum(x, 0) behaves differently where x=0
class Relu(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer:
    self.ret = x.e(BinaryOps.MAX, x.const(0))
    return self.ret

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer:
    return self.ret.const(0).e(BinaryOps.CMPLT, self.ret).cast(grad_output.dtype).e(BinaryOps.MUL, grad_output)

class Log(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer:
    self.x = x
    return x.e(UnaryOps.LOG2).e(BinaryOps.MUL, x.const(math.log(2)))

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer: return grad_output.e(BinaryOps.DIV, self.x)

class Exp(Function):
  def forward(self, x:LazyBuffer) -> LazyBuffer:
    self.ret = x.e(BinaryOps.MUL, x.const(1/math.log(2))).e(UnaryOps.EXP2)
    return self.ret

  def backward(self, grad_output:LazyBuffer) -> LazyBuffer: return self.ret.e(BinaryOps.MUL, grad_output)

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
