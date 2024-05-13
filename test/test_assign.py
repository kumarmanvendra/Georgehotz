#!/usr/bin/env python
import unittest
import numpy as np
from tinygrad import dtypes, Tensor, TinyJit, GlobalCounters, Variable

N = 200  # has to be bigger than the cache to fail

class TestAssign(unittest.TestCase):
  def test_simple_assignment(self):
    a = Tensor(np.arange(N*N, dtype=np.float32)).reshape(N,N)
    b = Tensor(np.arange(N*N, dtype=np.float32)).reshape(N,N)
    a.realize()
    b.realize()
    ba1 = a.lazydata.base.realized
    bb1 = b.lazydata.base.realized
    a += b
    a.realize()
    ba2 = a.lazydata.base.realized
    assert ba1 == ba2 and ba1 != bb1
    np.testing.assert_allclose(a.numpy(), (np.arange(N*N)*2).reshape((N,N)))

  def test_assign_zeros_good(self):
    a = Tensor.zeros(10,10).contiguous()
    a.assign(Tensor.ones(10,10))
    b = Tensor.zeros(10,10).contiguous()
    a.realize()
    np.testing.assert_allclose(b.numpy(), 0)

  def test_assign_zeros(self):
    a = Tensor.zeros(10,10).contiguous()
    b = Tensor.zeros(10,10).contiguous()
    #with self.assertRaises(RuntimeError):
    a.assign(Tensor.ones(10,10))
    a.realize()
    np.testing.assert_allclose(b.numpy(), 0)

  def test_assign_add(self):
    def f(x):
      x += 1
      x.realize()
    x = Tensor([0])
    f(x)
    assert x.item() == 1

  def test_assign_add_twice(self):
    # NOTE: this has two kernels
    def f(x):
      x += 1
      x += 1
      x.realize()
    x = Tensor([0])
    f(x)
    assert x.item() == 2

  def test_assign_add_double(self):
    def f(x):
      x += 1
      x.realize()
    x = Tensor([0])
    f(x)
    assert (out:=x.item()) == 1, f"expected 1, got {out}"
    x = Tensor([0])
    f(x)
    assert (out:=x.item()) == 1, f"expected 1, got {out}"

  def test_assign_add_jit(self):
    @TinyJit
    def f(x):
      x += 1
      x.realize()
    x = Tensor([0])
    for _ in range(5): f(x)
    assert x.item() == 5

  def test_assign_add_jit_other(self):
    @TinyJit
    def f(x):
      x += 1
      x.realize()
    x = Tensor([0])
    for _ in range(5): f(x)
    assert x.item() == 5

    y = Tensor([0])
    for _ in range(4): f(y)
    assert y.item() == 4

  def test_assign_other_jit(self):
    @TinyJit
    def f(x, a):
      x.assign(a)
      x.realize()
    x = Tensor([0])
    for i in range(1, 6):
      f(x, x.full_like(i).contiguous())  # const would be implicitly folded without contiguous
      assert x.item() == i

  def test_assign_add_other_jit(self):
    @TinyJit
    def f(x, a):
      x += a
      x.realize()
    x = Tensor([0])
    a = 0
    for i in range(1, 6):
      a += i
      f(x, x.full_like(i).contiguous())
      assert x.item() == a

  def test_assign_changes(self):
    a = Tensor.ones(4).contiguous().realize()
    old_a = a
    a.assign(Tensor.full((4,), 2.).contiguous())
    # NOTE: old_a is now 2, and this would match the behavior of pytorch
    new = a + old_a
    np.testing.assert_allclose(new.numpy(), 4)

  def test_assign_diamond_cycle(self):
    # NOTE: should *not* raise AssertionError from numpy
    with self.assertRaises(RuntimeError):
      a = Tensor.ones(4).contiguous().realize()
      times_a = a*3
      a.assign(Tensor.full((4,), 2.).contiguous())
      new = a + (times_a-1)
      np.testing.assert_allclose(new.numpy(), 4)

  def test_assign_diamond_contiguous_cycle(self):
    with self.assertRaises(RuntimeError):
      a = Tensor.ones(4).contiguous().realize()
      times_a = a*3
      a.assign(Tensor.full((4,), 2.))
      new = a.contiguous() + times_a-1
      np.testing.assert_allclose(new.numpy(), 4)

  def test_assign_diamond_possible(self):
    a = Tensor.ones(4).contiguous().realize()
    times_a = a*3
    a.assign(Tensor.full((4,), 2.))
    new = a + (times_a-1).contiguous()
    np.testing.assert_allclose(new.numpy(), 4)

  def test_assign_diamond_possible_contiguous(self):
    a = Tensor.ones(4).contiguous().realize()
    times_a = a*3
    a.assign(Tensor.full((4,), 2.).contiguous())
    new = a + (times_a-1).contiguous()
    np.testing.assert_allclose(new.numpy(), 4)

  def test_assign_diamond_both_contiguous(self):
    a = Tensor.ones(4).contiguous().realize()
    times_a = a*3
    a.assign(Tensor.full((4,), 2.))
    new = a.contiguous() + (times_a-1).contiguous()
    np.testing.assert_allclose(new.numpy(), 4)

  def test_assign_diamond_alt(self):
    a = Tensor.ones(4).contiguous().realize()
    a.assign(Tensor.full((4,), 2.).contiguous())
    times_a = a*3
    new = a + times_a
    np.testing.assert_allclose(new.numpy(), 8)

  def test_double_assign(self):
    a = Tensor.ones(4).contiguous().realize()
    a += 1
    a += 1
    np.testing.assert_allclose(a.numpy(), 3)

  def test_crossover_assign(self):
    a = Tensor.full((4,), 2).contiguous().realize()
    b = Tensor.full((4,), 3).contiguous().realize()
    a += b
    b += a
    Tensor.realize(a,b)
    np.testing.assert_allclose(a.numpy(), 5)
    np.testing.assert_allclose(b.numpy(), 8)

  def test_assign_double_diamond(self):
    a = Tensor.full((4,), 2).contiguous().realize()
    b = Tensor.full((4,), 3).contiguous().realize()
    a_prev = a*4
    b_prev = b+3
    b += a_prev.contiguous()
    a += b_prev.contiguous()
    Tensor.realize(a, b)
    np.testing.assert_equal(b.numpy(), 11)
    np.testing.assert_equal(a.numpy(), 8)

  def test_assign_double_diamond_reduce(self):
    a0 = Tensor.full((16, 16), 10).contiguous().realize()
    a1 = Tensor.full((16, 16), 20).contiguous().realize()
    b0 = Tensor.full((16, ), 1).contiguous().realize()
    b1 = Tensor.full((16, ), 2).contiguous().realize()

    r0 = (a0 - b1.contiguous()).sum(1)
    r1 = (a1 - b0.contiguous()).sum(1)
    b0.assign(r0 * b0)
    b1.assign(r1 * b1)
    Tensor.realize(b0, b1)
    np.testing.assert_equal(b0.numpy(), 128)
    np.testing.assert_equal(b1.numpy(), 608)

  def test_crossunder_assign(self):
    # NOTE: should *not* raise AssertionError from numpy
    with self.assertRaises(RuntimeError):
      a = Tensor.full((4,), 2).contiguous().realize()
      b = Tensor.full((4,), 3).contiguous().realize()
      c = a+9
      a += b
      b += c
      Tensor.realize(a,b)
      np.testing.assert_allclose(a.numpy(), 2+3)
      np.testing.assert_allclose(b.numpy(), 3+2+9)

  def test_assign_kv_cache(self):
    bsz, max_context = 2, 8

    class Attn:
      @TinyJit
      def __call__(self, xk:Tensor, start_pos:Variable):
        seqlen = xk.shape[1]
        if not hasattr(self, "cache_k"):
          self.cache_k = Tensor.zeros(bsz, max_context, 1, 1).contiguous()
        keys = self.cache_k.shrink((None, (0, start_pos), None, None)).cat(xk, dim=1).contiguous() if start_pos > 0 else xk
        self.cache_k.assign(keys.pad((None,(0,max_context-start_pos-seqlen),None,None)).contiguous()).realize()

    attn = Attn()
    xk = Tensor.ones(bsz, 3, 1, 1).contiguous()
    attn(xk, 0)
    for i in range(3,6):
      # copied from LLaMA
      start_pos = Variable("start_pos", 1, max_context).bind(i)
      xk = Tensor.ones(bsz, 1, 1, 1).contiguous()
      attn(xk, start_pos)

    out = attn.cache_k.flatten().numpy()
    np.testing.assert_allclose(out, [1.,1.,1.,1.,1.,1.,0.,0.,1.,1.,1.,1.,1.,1.,0.,0.])

  def test_assign_contiguous(self):
    b = Tensor.rand(4,4).realize()
    a = (Tensor.rand(4,4).realize() + 1)
    kc = GlobalCounters.kernel_count
    b.assign(a.contiguous()).realize()
    assert GlobalCounters.kernel_count - kc == 2

  def test_assign_contiguous_permute(self):
    b = Tensor.rand(4,4).realize()
    a = (Tensor.rand(4,4).realize() + 1).permute((1,0))
    kc = GlobalCounters.kernel_count
    b.assign(a.contiguous()).realize()
    assert GlobalCounters.kernel_count - kc == 2

  def test_permuted_assignment_possible(self):
    a = Tensor.arange(4 * 4).reshape(4, 4).contiguous().realize()
    b = Tensor.arange(4 * 4).reshape(4, 4).contiguous().realize()
    a = a.permute(1, 0)
    new_val = a + b
    a.assign(new_val)
    np.testing.assert_equal(a.numpy(), np.arange(4 * 4).reshape(4, 4).transpose(1, 0) + np.arange(4 * 4).reshape(4, 4))

  def test_permuted_assignment(self):
    a = Tensor(np.arange(N*N, dtype=np.float32)).reshape(N,N)
    b = Tensor(np.arange(N*N, dtype=np.float32)).reshape(N,N)
    a.realize()
    b.realize()
    ba1 = a.lazydata.base.realized
    bb1 = b.lazydata.base.realized
    with self.assertRaises((RuntimeError, AssertionError)):
      a = a.permute(1,0)
      a += b
      a.realize()
      ba2 = a.lazydata.base.realized
      assert ba1 != ba2 and ba1 != bb1
      np.testing.assert_allclose(a.numpy(), np.arange(N*N).reshape((N,N)) + np.arange(N*N).reshape((N,N)).transpose(1,0))

  def test_post_permuted_assignment(self):
    a = Tensor(np.arange(N*N, dtype=np.float32)).reshape(N,N)
    b = Tensor(np.arange(N*N, dtype=np.float32)).reshape(N,N)
    a.realize()
    b.realize()
    #GlobalCounters.cache = []
    ba1 = a.lazydata.base.realized # noqa: F841
    bb1 = b.lazydata.base.realized # noqa: F841
    with self.assertRaises(RuntimeError):
      a.assign(a.permute(1,0) + b)   # this should not work!
      a.realize()
      ba2 = a.lazydata.base.realized # noqa: F841
      # NOTE: don't test that it's assigned
      #assert ba1 == ba2 and ba1 != bb1
      np.testing.assert_allclose(a.numpy(), np.arange(N*N).reshape((N,N)) + np.arange(N*N).reshape((N,N)).transpose(1,0))

  def test_simple_assignment_multioutput(self):
    a = Tensor.randn(32, 32).realize()
    b = Tensor.full((32, ), 1.).contiguous().realize()
    c = Tensor.full((32, ), 2.).contiguous().realize()
    d = Tensor.full((32, ), 3.).contiguous().realize()

    r = a.sum(axis=1)
    b.assign(r + b)
    c.assign(r + c)
    d.assign(r + d)

    kc = GlobalCounters.kernel_count
    Tensor.realize(b, c, d)
    assert GlobalCounters.kernel_count - kc == 1
    np.testing.assert_allclose(b.numpy(), a.sum(1).numpy()+1)
    np.testing.assert_allclose(c.numpy(), a.sum(1).numpy()+2)
    np.testing.assert_allclose(d.numpy(), a.sum(1).numpy()+3)

  # TODO: is there a way to sneak in a permute such that it returns the wrong answer?

  @unittest.skip("don't use output buffer, and mismatch dtype no longer supported")
  def test_cast_assignment(self):
    a = Tensor(np.arange(N*N, dtype=np.float32)).reshape(N,N)
    a.realize()
    oba1 = a.lazydata.base.output_buffer
    a.assign(a.cast(dtypes.int32).realize())
    a.realize()
    oba2 = a.lazydata.base.output_buffer
    assert oba1 is None and oba2 is None
    np.testing.assert_allclose(a.numpy(), np.arange(N*N,dtype=np.int32).reshape((N,N)))

if __name__ == "__main__":
  unittest.main()
