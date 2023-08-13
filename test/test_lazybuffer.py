#!/usr/bin/env python
import numpy as np
import unittest
from tinygrad.lazy import LazyBuffer, Device, simplify_ast
from tinygrad.tensor import Tensor
from tinygrad.ops import GlobalCounters
from tinygrad.ops import BinaryOps

def helper_test_patterns(clb):
  N = 8
  a = Tensor.rand(N, N)
  b = Tensor.rand(N, N)
  c = Tensor.rand(N, N)
  z = clb(a, b, c).lazydata
  simplify_ast(z, set())
  return z

class TestLazyBuffer(unittest.TestCase):
  def test_fromcpu_buffer_sharing(self):
    a = np.arange(8)
    assert LazyBuffer.fromCPU(a).realized._buf is a

  def test_fromcpu_shape_tracker(self):
    def helper(a: np.ndarray):
      print(a.shape, a.strides, a.flags.c_contiguous)
      b = LazyBuffer.fromCPU(a).realize()
      assert b.st.contiguous == a.flags.c_contiguous
      assert b.st.shape == a.shape
      np.testing.assert_equal(a, b.toCPU())

    for ndims in range(1, 4):
      a = np.random.randn(*(4,)*ndims).astype(np.float32)
      for stride in [-2, 1, 2]:
        for start in [0, 1]:
          helper(a[(slice(start, None, stride),)*ndims])

  def test_shuffle_pad_ops_cmpeq(self):
    y = Tensor([1]).cat(Tensor([1]) == 0).numpy()
    z = Tensor([1, 0]).numpy()
    np.testing.assert_allclose(y, z)

  def test_shuffle_pad_ops_div(self):
    y = Tensor([1]).cat(Tensor([1]).div(Tensor([2.0]))).numpy()
    z = Tensor([1, 0.5]).numpy()
    np.testing.assert_allclose(y, z)

  def test_shuffle_pad_ops_log(self):
    y = Tensor([1]).cat(Tensor([1]).log()).numpy()
    z = Tensor([1, 0]).numpy()
    np.testing.assert_allclose(y, z)

  def test_shuffle_pad_ops_exp(self):
    y = Tensor([1]).cat(Tensor([1]).exp()).numpy()
    z = Tensor([1, np.e]).numpy()
    np.testing.assert_allclose(y, z)

  @unittest.skipUnless(Device.DEFAULT in ["METAL", "CUDA", "GPU"], "Only GPU backends supports cache")
  def test_children_count(self):
    a = Tensor.rand(8,8,8)
    d1 = a.sum((0))
    d2 = a.sum((0)).reshape(32,2)
    assert len(d1.lazydata.op.src[0].children) == 1
    in1 = d1.reshape(16,4)
    d3 = in1.reshape(8,8)
    assert len(d3.lazydata.op.src[0].children) == 2

    GlobalCounters.cache = []
    l = Tensor.rand(8,8)
    r = Tensor.rand(8,8)
    dd = d1 + l
    dd.realize()
    de = d3 + r
    de.realize()
    assert len(GlobalCounters.cache) == 3
    assert GlobalCounters.cache[0][0].name.startswith("r_") # Reduce should not merged 2 times.
    assert GlobalCounters.cache[1][0].name.startswith("E_")
    assert GlobalCounters.cache[2][0].name.startswith("E_")
    GlobalCounters.cache = None

  def test_simplification_patterns(self):
    assert helper_test_patterns(lambda a,b,_: a.maximum(b)).op.op == BinaryOps.MAX
    assert helper_test_patterns(lambda a,b,_: (a<b).detach().where(b, (a>b).detach().where(a, (a+b)/2))).op.op == BinaryOps.MAX
    assert helper_test_patterns(lambda a,b,_: (a<b).where(b, (a>b).detach().where(a, (a+b)/2))).op.op == BinaryOps.MAX
    assert helper_test_patterns(lambda a,b,_: (a<b).where(b, (a>b).where(a, (a+b)/2))).op.op == BinaryOps.MAX
    assert helper_test_patterns(lambda a,b,_: (a<b).detach().where(b, (a>b).detach().where(a, (a+b)/3))).op.op != BinaryOps.MAX
    assert helper_test_patterns(lambda a,b,_: (a<b).detach().where(b, (a>b).detach().where(b, (a+b)/2))).op.op != BinaryOps.MAX
    assert helper_test_patterns(lambda a,b,_: (a<b).detach().where(b, a)).op.op != BinaryOps.MAX
    assert helper_test_patterns(lambda a,b,_: (a<b).detach().where(b, (b>b).detach().where(a, (a+b)/2))).op.op != BinaryOps.MAX
    assert helper_test_patterns(lambda a,b,_: (b<b).detach().where(b, (a>b).detach().where(a, (a+b)/2))).op.op != BinaryOps.MAX
    assert helper_test_patterns(lambda a,b,c: (a<c).where(c, (a>b).detach().where(a, (a+b)/2))).op.op != BinaryOps.MAX
    assert helper_test_patterns(lambda a,b,c: (c<b).detach().where(b, (a>b).detach().where(a, (a+b)/2))).op.op != BinaryOps.MAX
    assert helper_test_patterns(lambda a,b,c: (a<b).where(b, (a>b).detach().where(a, (c+b)/2))).op.op != BinaryOps.MAX
    assert helper_test_patterns(lambda a,b,c: (a<b).detach().where(b, (a>b).detach().where(c, (a+b)/2))).op.op != BinaryOps.MAX

if __name__ == "__main__":
  unittest.main()
