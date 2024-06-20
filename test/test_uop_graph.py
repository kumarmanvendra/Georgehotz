import unittest
from tinygrad import dtypes, Variable
from tinygrad.dtype import PtrDType
from tinygrad.ops import BinaryOps, TernaryOps, UnaryOps
from tinygrad.codegen.uops import UOpGraph, UOps, UOp

class TestUOpGraph(unittest.TestCase):
  # TODO: move to test.helpers
  def assert_equiv_uops(self, uop1:UOp, uop2:UOp):
    # NOTE: direct UOps __eq__ is comparing object reference, use this function to compare two uops
    self.assertEqual(uop1.op, uop2.op)
    self.assertEqual(uop1.dtype, uop2.dtype)
    self.assertEqual(uop1.arg, uop2.arg)

  def test_add_constant_fold(self):
    c1 = UOp(UOps.CONST, dtypes.float, arg=1.0)
    c2 = UOp(UOps.CONST, dtypes.float, arg=2.0)
    out = UOp(UOps.ALU, dtypes.float, (c1, c2), BinaryOps.ADD)
    g = UOpGraph([out])
    self.assertEqual(len(g.uops), 1)
    out = g.uops[-1]
    self.assertEqual(out.op, UOps.CONST)
    self.assertEqual(out.arg, 3.0)

  def test_where_same_fold(self):
    v = UOp(UOps.DEFINE_VAR, dtypes.int, arg=Variable('tmp', 0, 1))
    c0 = UOp(UOps.CONST, dtypes.int, arg=0)
    vc = UOp(UOps.ALU, dtypes.bool, (v, c0), BinaryOps.CMPNE)
    c1 = UOp(UOps.CONST, dtypes.float, arg=1.0)
    out = UOp(UOps.ALU, dtypes.float, (vc, c1, c1), TernaryOps.WHERE)
    g = UOpGraph([out])
    self.assertEqual(len(g.uops), 1)
    out = g.uops[-1]
    self.assertEqual(out.op, UOps.CONST)
    self.assertEqual(out.arg, 1.0)

  def test_where_const_fold(self):
    bf = UOp(UOps.CONST, dtypes.bool, arg=False)
    c1 = UOp(UOps.CONST, dtypes.float, arg=1.0)
    c2 = UOp(UOps.CONST, dtypes.float, arg=2.0)
    out = UOp(UOps.ALU, dtypes.float, (bf, c1, c2), TernaryOps.WHERE)
    g = UOpGraph([out])
    self.assertEqual(len(g.uops), 1)
    out = g.uops[-1]
    self.assertEqual(out.op, UOps.CONST)
    self.assertEqual(out.arg, 2.0)

  def test_const_cast(self):
    bf = UOp(UOps.CONST, dtypes.bool, arg=False)
    out = UOp(UOps.CAST, dtypes.int, (bf,))
    g = UOpGraph([out])
    self.assertEqual(len(g.uops), 1)
    out = g.uops[-1]
    self.assertEqual(out.op, UOps.CONST)
    self.assertEqual(out.arg, 0)

  def test_cast_vectorized_fold(self):
    d0 = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.float), arg=(0, True))
    idx = UOp(UOps.CONST, dtypes.int, arg=0)
    ld = UOp(UOps.LOAD, dtypes.float.vec(2), (d0, idx))
    cast = UOp(UOps.CAST, dtypes.float.vec(2), (ld,))
    x = UOp(UOps.GEP, dtypes.float, (cast, ), arg=0)
    alu = UOp(UOps.ALU, dtypes.float, (x, ), UnaryOps.SQRT)
    out = UOp(UOps.STORE, dtypes.float, (d0, idx, alu))
    g = UOpGraph([out])
    self.assertEqual(len([x for x in g.uops if x.op is UOps.CAST]), 0)

  def test_depth_2_const_fold(self):
    v = UOp(UOps.DEFINE_VAR, dtypes.int, arg=Variable('tmp', 0, 1))
    c2 = UOp(UOps.CONST, dtypes.int, arg=2)
    c4 = UOp(UOps.CONST, dtypes.int, arg=4)
    vc = UOp(UOps.ALU, dtypes.int, (v, c2), BinaryOps.ADD)
    out = UOp(UOps.ALU, dtypes.int, (vc, c4), BinaryOps.ADD)
    g = UOpGraph([out])
    self.assertEqual(len(g.uops), 3)
    out = g.uops[-1]
    self.assertEqual(out.op, UOps.ALU)
    self.assertEqual(out.arg, BinaryOps.ADD)
    self.assertEqual(out.src[1].op, UOps.CONST)
    self.assertEqual(out.src[1].arg, 6)

  def test_fold_gated_load(self):
    glbl0 = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.int), (), (0, True))
    glbl1 = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.int), (), (1, False))
    glbl2 = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.int), (), (2, False))
    idx = UOp.const(dtypes.int, 0)
    ld0 = UOp(UOps.LOAD, dtypes.int, (glbl1, idx, UOp.const(dtypes.bool, False), UOp.const(dtypes.int, 2)))
    ld1 = UOp(UOps.LOAD, dtypes.int, (glbl2, idx, UOp.const(dtypes.bool, True), UOp.const(dtypes.int, 3)))
    uops = UOpGraph([UOp(UOps.STORE, None, (glbl0, idx, ld0+ld1))])
    ld0, ld1 = uops[-1].src[2].src
    # ld0 folds to the invalid value
    self.assert_equiv_uops(ld0, UOp.const(dtypes.int, 2))
    # ld1 folds to the valid value
    self.assert_equiv_uops(ld1, UOp.load(glbl1, idx, dtype=dtypes.int))

  def test_fold_gated_load_local(self):
    glbl0 = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.int), (), (0, True))
    smem = UOp(UOps.DEFINE_LOCAL, PtrDType(dtypes.int), (), ("temp", 16))
    idx0 = UOp.const(dtypes.int, 0)
    idx1 = UOp.const(dtypes.int, 1)
    ld0 = UOp(UOps.LOAD, dtypes.int, (smem, idx0, UOp.const(dtypes.bool, False), UOp.const(dtypes.int, 2)))
    ld1 = UOp(UOps.LOAD, dtypes.int, (smem, idx1, UOp.const(dtypes.bool, True), UOp.const(dtypes.int, 3)))
    uops = UOpGraph([UOp(UOps.STORE, None, (glbl0, idx0, ld0+ld1))])
    ld0, ld1 = uops[-1].src[2].src
    # ld0 folds to the invalid value
    self.assert_equiv_uops(ld0, UOp.const(dtypes.int, 2))
    # ld1 folds to the valid value
    self.assert_equiv_uops(ld1, UOp.load(smem, idx1, dtype=dtypes.int))

  def test_fold_gated_store(self):
    glbl = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.int), (), (0, True))
    idx = UOp.const(dtypes.int, 0)
    val = UOp.const(dtypes.int, 42)
    st0 = UOp(UOps.STORE, None, (glbl, idx, val, UOp.const(dtypes.bool, False)))
    st1 = UOp(UOps.STORE, None, (glbl, idx+1, val, UOp.const(dtypes.bool, True)))
    uops = UOpGraph([st0, st1])
    self.assertEqual(len(uops.uops), 4)
    self.assert_equiv_uops(uops[-1], UOp.store(glbl, idx+1, val))

if __name__ == '__main__':
  unittest.main(verbosity=2)
