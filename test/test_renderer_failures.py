import unittest
from typing import List, cast
import numpy as np
from tinygrad.codegen.uopgraph import full_graph_rewrite, linearize_uop
from tinygrad.device import Buffer, Device
from tinygrad.dtype import PtrDType, DType, dtypes
from tinygrad.engine.realize import CompiledRunner
from tinygrad.helpers import dedup, flatten, getenv, prod
from tinygrad.renderer.cstyle import CStyleLanguage
from tinygrad.ops import BinaryOps, UOp, UOps
from tinygrad.renderer import Program
from tinygrad.tensor import Tensor, _to_np_dtype
from tinygrad.lazy import LazyBuffer

def _test_uop_result(inputs:List[Tensor], stores:List[UOp], global_size=(1, 1, 1)):
  for x in inputs: x.realize()
  # NOTE: we only toposort the stores
  uops: List[UOp] = []
  def _recursive_add(uop:UOp) -> List[UOp]: return flatten([_recursive_add(x) for x in uop.src])+[uop]
  uops = dedup(flatten(_recursive_add(st) for st in stores))
  outbufs = [Buffer(Device.DEFAULT, prod(global_size), cast(DType,u.src[2].dtype), \
      initial_value=np.zeros(prod(global_size), dtype=_to_np_dtype(cast(DType,u.src[2].dtype))).data) for u in uops if u.op is UOps.STORE]
  inbufs = [cast(LazyBuffer,x.lazydata).base.buffer for x in inputs]
  src = Device[Device.DEFAULT].renderer.render("test", uops)
  ei = CompiledRunner(Program("test", src, Device.DEFAULT, uops=uops, global_size=list(global_size)))
  ei.exec(outbufs+inbufs)
  return [np.frombuffer(x.as_buffer(), _to_np_dtype(x.dtype)) for x in outbufs]

@unittest.skipIf(not isinstance(Device[Device.DEFAULT].renderer, CStyleLanguage), "uops are for cstyle")
class TestCStyleFailures(unittest.TestCase):
  def test_inline_const_alu(self):
    a = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.int), (), 0)
    b = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.int), (), 1)
    idx = UOp.const(dtypes.int, 0)
    ld = UOp(UOps.LOAD, dtypes.int, (b, idx))
    alu = ld.alu(BinaryOps.MAX, UOp.const(dtypes.int, dtypes.min(dtypes.int)))
    store = UOp.store(a, idx, alu)
    # CLANG doesn't use the max function
    ret = _test_uop_result([Tensor([1])], [store])[0]
    self.assertEqual(ret[0], 1)

class TestPTXFailures(unittest.TestCase):
  def test_gated_store_with_alu(self):
    a = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.int), (), 0)
    gate_alu = (gidx0:=UOp(UOps.SPECIAL, dtypes.int, (), ('gidx0', 4))).ne(0)
    gated_alu_store = UOp(UOps.STORE, None, (a, gidx0, UOp.const(dtypes.int, 1), gate_alu))
    sink = UOp(UOps.SINK, None, (gated_alu_store,))
    uops = linearize_uop(full_graph_rewrite(sink, Device[Device.DEFAULT].renderer))
    ret = _test_uop_result([], uops, global_size=(4, 1, 1))[0]
    np.testing.assert_equal(ret, [0, 1, 1, 1])

  def test_gated_store_with_if(self):
    a = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.int), (), 0)
    gate_alu = (gidx0:=UOp(UOps.SPECIAL, dtypes.int, (), ('gidx0', 4))).ne(0)
    val = UOp.const(dtypes.int, 1)
    if_uop = UOp(UOps.IF, None, (gate_alu, val))
    gated_alu_store = UOp(UOps.STORE, None, (a, gidx0, val, if_uop))
    sink = UOp(UOps.SINK, None, (gated_alu_store,))
    uops = linearize_uop(full_graph_rewrite(sink, Device[Device.DEFAULT].renderer))
    ret = _test_uop_result([], uops, global_size=(4, 1, 1))[0]

    if getenv("PTX"):
      with self.assertRaises(AssertionError):
        np.testing.assert_equal(ret, [0, 1, 1, 1])
    else: np.testing.assert_equal(ret, [0, 1, 1, 1])

if __name__ == '__main__':
  unittest.main()
