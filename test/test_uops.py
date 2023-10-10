from typing import Optional, Tuple, Any, List
import unittest, math
import numpy as np
from tinygrad.helpers import dtypes, getenv, DType, PtrDType
from tinygrad.tensor import Device
from tinygrad.ops import UnaryOps, BinaryOps, TernaryOps, ASTRunner, Compiled
from tinygrad.codegen.linearizer import UOps, UOp

def _uops_to_prg(uops):
  src = Device[Device.DEFAULT].renderer("test", uops)
  return ASTRunner("test", src[0] if getenv("TRITON") else src, [1], [1], runtime_args={"binary": getenv("TRITON")}).build(Device[Device.DEFAULT].runtime)

def uop(uops:List[UOp], uop:UOps, dtype:Optional[DType], vin:Tuple[UOp, ...], arg:Any=None) -> UOp:
  uops.append(UOp(uop, dtype, tuple(vin), arg, len(uops)))
  return uops[-1]

def _test_single_value(vals, op, dtype):
  uops = []
  buf_store = uop(uops, UOps.DEFINE_GLOBAL, PtrDType(dtype), (), ('data0', dtype))
  buf_loads = [uop(uops, UOps.DEFINE_GLOBAL, PtrDType(dtype), (), (f'data{i+1}', dtype)) for i in range(len(vals))]
  loads = (uop(uops, UOps.LOAD, dtype, [buf_loads[i], uop(uops, UOps.CONST, dtypes.int32, (), 0)]) for i in range(len(vals)))
  alu = uop(uops, UOps.ALU, dtype, loads, op)
  uop(uops, UOps.STORE, None, (buf_store, uop(uops, UOps.CONST, dtypes.int32, (), 0), alu))
  buf = Device[Device.DEFAULT].buffer(1, dtype)
  buf2 = [Device[Device.DEFAULT].buffer.fromCPU(np.array([a], dtype=dtype.np)) for a in vals]
  prg = _uops_to_prg(uops)
  prg([buf]+buf2)
  return buf.toCPU()[0]

def _test_single_value_const(vals, op, dtype):
  uops = []
  buf_store = uop(uops, UOps.DEFINE_GLOBAL, PtrDType(dtype), (), ('data0', dtype))
  loads = (uop(uops, UOps.CONST, dtype, [], a) for a in vals)
  alu = uop(uops, UOps.ALU, dtype, loads, op)
  uop(uops, UOps.STORE, None, (buf_store, uop(uops, UOps.CONST, dtypes.int32, (), 0), alu))
  buf = Device[Device.DEFAULT].buffer(1, dtype)
  prg = _uops_to_prg(uops)
  prg([buf])
  return buf.toCPU()[0]

class TestUOps(unittest.TestCase):
  def _equal(self, v1, v2, places):
    if not (math.isnan(v1) and math.isnan(v2)): self.assertAlmostEqual(v1, v2, places=places)

  def _test_uop_fxn(self, bop, fxn, dt=dtypes.float32):
    for f in [_test_single_value, _test_single_value_const]:
      for a in [-2.0, 0.0, 1.0, 2.0]:
        self._equal(f([a], bop, dt), fxn(a), places=3 if dt==dtypes.half else 5)

  def _test_bop_fxn(self, bop, fxn, dt=dtypes.float32, no_b_zero=False):
    for f in [_test_single_value, _test_single_value_const]:
      for a in [-2.0, 0.0, 1.0, 2.0]:
        for b in [-3.0, 1.0, 3.0] + ([] if no_b_zero else [0.0]):
          self._equal(f([a,b], bop, dt), fxn(a,b), places=3 if dt==dtypes.half else 5)

  def _test_top_fxn(self, bop, fxn, dt=dtypes.float32):
    for f in [_test_single_value, _test_single_value_const]:
      for a in [-2.0, 0, 1, 2.0]:
        for b in [-3.0, 3.0]:
          for c in [-4.0, 4.0]:
            self._equal(f([a,b,c], bop, dt), fxn(a,b,c), places=3 if dt==dtypes.half else 5)

@unittest.skipIf(not isinstance(Device[Device.DEFAULT], Compiled), "only test for compiled backends")
class TestFloatUOps(TestUOps):
  def test_neg(self): self._test_uop_fxn(UnaryOps.NEG, lambda a: -a)
  def test_exp2(self): self._test_uop_fxn(UnaryOps.EXP2, lambda a: np.exp2(a))
  def test_log2(self): self._test_uop_fxn(UnaryOps.LOG2, lambda a: math.log2(a) if a > 0 else float('-inf' if a==0 else 'nan'))
  def test_sin(self): self._test_uop_fxn(UnaryOps.SIN, lambda a: math.sin(a))
  def test_sqrt(self): self._test_uop_fxn(UnaryOps.SQRT, lambda a: math.sqrt(a) if a >= 0 else float('nan'))
  # this is not on most backends
  #def test_recip(self): self._test_uop_fxn(UnaryOps.RECIP, lambda a: 1.0/a if a != 0 else float('inf'))

  def test_add(self): self._test_bop_fxn(BinaryOps.ADD, lambda a,b: a+b)
  def test_sub(self): self._test_bop_fxn(BinaryOps.SUB, lambda a,b: a-b)
  def test_mul(self): self._test_bop_fxn(BinaryOps.MUL, lambda a,b: a*b)
  def test_div(self): self._test_bop_fxn(BinaryOps.DIV, lambda a,b: a/b if b != 0 else a*float('inf'))
  def test_max(self): self._test_bop_fxn(BinaryOps.MAX, lambda a,b: max(a,b))
  def test_cmplt(self): self._test_bop_fxn(BinaryOps.CMPLT, lambda a,b: float(a<b))
  # MOD isn't tested on floats

  def test_mulacc(self): self._test_top_fxn(TernaryOps.MULACC, lambda a,b,c: (a*b)+c)
  def test_where(self): self._test_top_fxn(TernaryOps.WHERE, lambda a,b,c: b if a!=0 else c)

# TODO: fix this on all the backends
@unittest.skipIf(not isinstance(Device[Device.DEFAULT], Compiled) or getenv('ARM64', False) or getenv('LLVM', False), "only test for compiled backends, broken on some")
class TestHalfUOps(TestUOps):
  # 6 tests below are broken on Nvidia OpenCL, CUDA
  def test_exp2_half(self): self._test_uop_fxn(UnaryOps.EXP2, lambda a: np.exp2(a), dtypes.half)
  def test_log2_half(self): self._test_uop_fxn(UnaryOps.LOG2, lambda a: math.log2(a) if a > 0 else float('-inf' if a==0 else 'nan'), dtypes.half)
  def test_sin_half(self): self._test_uop_fxn(UnaryOps.SIN, lambda a: math.sin(a), dtypes.half)
  def test_sqrt_half(self): self._test_uop_fxn(UnaryOps.SQRT, lambda a: math.sqrt(a) if a >= 0 else float('nan'), dtypes.half)
  def test_max_half(self): self._test_bop_fxn(BinaryOps.MAX, lambda a,b: max(a,b), dtypes.half)
  def test_where_half(self): self._test_top_fxn(TernaryOps.WHERE, lambda a,b,c: b if a!=0 else c, dtypes.half)

  def test_add_half(self): self._test_bop_fxn(BinaryOps.ADD, lambda a,b: a+b, dtypes.half)
  def test_sub_half(self): self._test_bop_fxn(BinaryOps.SUB, lambda a,b: a-b, dtypes.half)
  def test_mul_half(self): self._test_bop_fxn(BinaryOps.MUL, lambda a,b: a*b, dtypes.half)
  def test_div_half(self): self._test_bop_fxn(BinaryOps.DIV, lambda a,b: a/b if b != 0 else a*float('inf'), dtypes.half)
  def test_cmplt_half(self): self._test_bop_fxn(BinaryOps.CMPLT, lambda a,b: float(a<b), dtypes.half)

  def test_mulacc_half(self): self._test_top_fxn(TernaryOps.MULACC, lambda a,b,c: (a*b)+c, dtypes.half)

# TODO: fix this on all the backends
@unittest.skipIf(not isinstance(Device[Device.DEFAULT], Compiled) or getenv('ARM64', False), "only test for compiled backends, broken on some")
class TestNonFloatUOps(TestUOps):
  def test_neg_int32(self): self._test_uop_fxn(UnaryOps.NEG, lambda a: -a, dtypes.int32)
  def test_add_int32(self): self._test_bop_fxn(BinaryOps.ADD, lambda a,b: int(a)+int(b), dtypes.int32)
  def test_sub_int32(self): self._test_bop_fxn(BinaryOps.SUB, lambda a,b: int(a)-int(b), dtypes.int32)
  def test_mul_int32(self): self._test_bop_fxn(BinaryOps.MUL, lambda a,b: int(a)*int(b), dtypes.int32)
  def test_div_int32(self): self._test_bop_fxn(BinaryOps.DIV, lambda a,b: int(a/b), dtypes.int32, no_b_zero=True)
  def test_mod_int32(self): self._test_bop_fxn(BinaryOps.MOD, lambda a,b: abs(int(a))%abs(int(b))*(1,-1)[a<0], dtypes.int32, no_b_zero=True)
  def test_cmplt_int32(self): self._test_bop_fxn(BinaryOps.CMPLT, lambda a,b: float(a<b), dtypes.int32)
  def test_mul_bool(self): self._test_bop_fxn(BinaryOps.MUL, lambda a,b: bool(a) and bool(b), dtypes.bool)

if __name__ == '__main__':
  unittest.main(verbosity=2)
