#!/usr/bin/env python
import os
if "OPT" not in os.environ:
  os.environ["OPT"] = "2"

import gc
import numpy as np

import unittest
from tinygrad.tensor import Tensor, Device
from tinygrad import nn
from tinygrad.nn import optim
from tinygrad.ops import GlobalCounters, MovementOps, ReduceOps
from tinygrad.lazy import PUSH_PERMUTES

class CLCache():
  def __enter__(self):
    gc.collect()
    for x in [x for x in gc.get_objects() if isinstance(x, Tensor)]:
      x.realize()
    GlobalCounters.cache = []
    print("cache: entering")
  def __exit__(self, type, value, traceback):
    print(f"cache: exiting with size {len(GlobalCounters.cache)}")
    GlobalCounters.cache = None

@unittest.skipUnless(Device.DEFAULT == "GPU", "Not Implemented")
class TestOpt(unittest.TestCase):
  def test_muladd(self):
    a,b,c = [Tensor.ones(2,2) for _ in range(3)]
    with CLCache():
      d = a * b + c
      d.realize()
      assert len(GlobalCounters.cache) == 1, "optimizer didn't fold muladd"
    np.testing.assert_allclose(d.numpy(), np.ones((2,2))*2, rtol=1e-5)

  def test_fold_reduce_elementwise(self):
    img = Tensor.ones(32)
    addme = Tensor.ones(1)
    with CLCache():
      ret = img.sum() + addme
      ret.realize()
      assert len(GlobalCounters.cache) == 1, "optimizer didn't fold reduce/elementwise"
    assert ret.numpy()[0] == 33

  def test_fold_batchnorm(self):
    # TODO: with Tensor.training
    Tensor.training = True
    img = Tensor.ones(1,32,4,4)
    bn = nn.BatchNorm2d(32, track_running_stats=False)
    with CLCache():
      img_bn = bn(img).realize()
      print(img_bn)
      assert len(GlobalCounters.cache) == 3, "optimizer didn't fold batchnorm"
    Tensor.training = False

  def test_fold_conv_sgd(self):
    # TODO: with Tensor.training
    Tensor.training = True
    img = Tensor.ones(1,3,4,4)
    c1 = nn.Conv2d(3,32,3)
    opt = optim.SGD(optim.get_parameters(c1))
    with CLCache():
      opt.zero_grad()
      c1(img).relu().sum().backward()
      opt.step()
      # TODO: this should be 4, but the sum output child stays around
      # with pushing_permutes it can be 3
      assert len(GlobalCounters.cache) in [4,5], "optimizer didn't fold conv-backward SGD"
    Tensor.training = False

  def test_fold_conv_batchnorm_sgd(self):
    # TODO: with Tensor.training
    Tensor.training = True
    img = Tensor.ones(1,3,4,4)
    c1 = nn.Conv2d(3,32,3)
    bn = nn.BatchNorm2d(32, track_running_stats=False)
    opt = optim.SGD(optim.get_parameters([c1, bn]))
    with CLCache():
      img_bn = bn(c1(img)).elu().sum()
      opt.zero_grad()
      img_bn.backward()
      opt.step()
      assert len(GlobalCounters.cache) in [9,10], "optimizer didn't fold conv-backward batchnorm"
    Tensor.training = False

  def test_fold_conv_batchnorm_notrain(self):
    img = Tensor.ones(1,3,8,8)
    c1 = nn.Conv2d(3,32,3)
    bn = nn.BatchNorm2d(32, track_running_stats=False)
    # precache the bn
    img_conv = bn(c1(img)).relu().realize()
    with CLCache():
      img_conv = bn(c1(img)).relu().realize()
      assert len(GlobalCounters.cache) == 1, "optimizer didn't fold conv-batchnorm at test time"

  def test_fold_conv_batchnorm(self):
    Tensor.training = True
    img = Tensor.ones(1,3,8,8)
    c1 = nn.Conv2d(3,32,3)
    bn = nn.BatchNorm2d(32, track_running_stats=False)
    with CLCache():
      img_conv = bn(c1(img)).relu().realize()
      print(img_conv)
      assert len(GlobalCounters.cache) == 4, "optimizer didn't fold conv-batchnorm"
    Tensor.training = False

  def test_fold_conv_elu(self):
    img = Tensor.ones(1,4,8,8)
    c1 = nn.Conv2d(4, 4, kernel_size=3)
    c2 = nn.Conv2d(4, 4, kernel_size=3)
    with CLCache():
      img_conv = img.sequential([c1, Tensor.elu, c2, Tensor.elu]).realize()
      print(img_conv)
      assert len(GlobalCounters.cache) == 2, "optimizer didn't fold conv/elu"

  def test_fold_conv_relu(self):
    img = Tensor.ones(1,4,8,8)
    c1 = nn.Conv2d(4, 4, kernel_size=3)
    c2 = nn.Conv2d(4, 4, kernel_size=3)
    with CLCache():
      img_conv = img.sequential([c1, Tensor.relu, c2, Tensor.relu]).realize()
      print(img_conv)
      assert len(GlobalCounters.cache) == 2, "optimizer didn't fold conv/relu"

  def test_fold_conv_relu_nobias(self):
    img = Tensor.ones(1,4,8,8)
    c1 = nn.Conv2d(4, 4, kernel_size=3, bias=False)
    c2 = nn.Conv2d(4, 4, kernel_size=3, bias=False)
    with CLCache():
      img_conv = img.sequential([c1, Tensor.relu, c2, Tensor.relu]).realize()
      print(img_conv)
      assert len(GlobalCounters.cache) == 2, "optimizer didn't fold conv/relu"

  def test_no_binop_rerun(self):
    a = Tensor.randn(16, 16)
    b = Tensor.randn(16, 16)
    with CLCache():
      c = a*b
      d = (a*b).reshape(16, 16, 1)
      c.realize()
      d.realize()
      assert len(GlobalCounters.cache) == 1, "binop was rerun!"

  def test_no_binop_rerun_alt(self):
    a = Tensor.randn(16, 16)
    b = Tensor.randn(16, 16)
    with CLCache():
      c = (a*b).reshape(16, 16, 1)
      d = a*b
      c.realize()
      d.realize()
      assert len(GlobalCounters.cache) == 1, "binop was rerun!"

  def test_no_reduceop_rerun(self):
    a = Tensor.randn(16, 16, 16)
    with CLCache():
      c = a.sum(2)
      d = a.sum(2).permute(1,0)
      c.realize()
      d.realize()
      cache_len = len(GlobalCounters.cache)
    np.testing.assert_allclose(c.numpy().transpose(1,0), d.numpy())
    assert cache_len == 1, "reduceop was rerun!"

  def test_no_reduceop_rerun_alt(self):
    a = Tensor.randn(16, 16, 16)
    with CLCache():
      c = a.sum(2).permute(1,0)
      d = a.sum(2)
      c.realize()
      d.realize()
      cache_len = len(GlobalCounters.cache)
    np.testing.assert_allclose(c.numpy(), d.numpy().transpose(1,0))
    assert cache_len == 1, "reduceop was rerun!"

  def test_permute_was_pushed(self):
    if not PUSH_PERMUTES: return
    a = Tensor.randn(16, 16, 16)
    with CLCache():
      c = a.sum(2)
      d = c.permute(1,0).contiguous()
      d.realize()
      cache_len = len(GlobalCounters.cache)
    np.testing.assert_allclose(a.numpy().sum(2).transpose(1,0), d.numpy(), rtol=1e-3)
    assert cache_len == 1, "permute wasn't pushed!"

  @unittest.skip("expansion can't push permute yet")
  def test_permute_was_pushed_through_reshape(self):
    if not PUSH_PERMUTES: return
    a = Tensor.randn(16, 16, 16)
    with CLCache():
      c = a.sum(2)
      d = c.reshape(4,4,4,4).permute(2,3,0,1).contiguous()
      d.realize()
      cache_len = len(GlobalCounters.cache)
    np.testing.assert_allclose(a.numpy().sum(2).transpose(1,0).reshape(4,4,4,4), d.numpy(), rtol=1e-3)
    assert cache_len == 1, "permute wasn't pushed!"

  def test_layernorm(self):
    a = Tensor.randn(16, 16, 16)
    ln = nn.LayerNorm(16)
    with CLCache():
      ln(a).realize()
      cache_len = len(GlobalCounters.cache)
    assert cache_len == 3, "layernorm too many ops"

  def test_layernorm_permute(self):
    dim = 16
    a = Tensor.randn(1, 16, 8, 8)
    dwconv = nn.Conv2d(dim, dim, kernel_size=7, padding=3, groups=dim)
    ln = nn.LayerNorm(dim, eps=1e-6)
    with CLCache():
      ln(dwconv(a).permute(0, 2, 3, 1)).realize()
      cache_len = len(GlobalCounters.cache)
    assert cache_len == 4, "conv/permute/layernorm too many ops"

  def test_layernorm_permute_linear(self):
    dim = 16
    a = Tensor.randn(1, 16, 8, 8)
    dwconv = nn.Conv2d(dim, dim, kernel_size=7, padding=3, groups=dim)  # why don't i need avoid here?
    ln = nn.LayerNorm(dim, eps=1e-6)
    linear = nn.Linear(dim, dim, bias=False)
    linear.weight.assign(Tensor.ones(*linear.weight.shape))  # avoid random init op (why do i need this!)
    with CLCache():
      linear(ln(dwconv(a).permute(0, 2, 3, 1))).realize()
      cache_len = len(GlobalCounters.cache)
    assert cache_len == 5, "conv/permute/layernorm too many ops"

  # TODO: these permute tests should really test desired behavior, not outcomes. see test_permute_was_pushed

  """
  def helper_push_permute_before_reshape(self, t, should_push=True, desired_reshape_arg=None, desired_permute_arg=None):
    if PUSH_PERMUTES and should_push:
      assert t.lazydata.op.src[0].op.op == MovementOps.PERMUTE, 'Permute should be pushed before reshape'
      assert t.lazydata.op.src[0].op.arg == desired_permute_arg, f'Pushed permute arg should be {desired_permute_arg}'
      assert t.lazydata.op.op == MovementOps.RESHAPE, 'Reshape should be after permute'
      assert t.lazydata.op.arg == desired_reshape_arg, f'Reshape arg should be {desired_reshape_arg}'
    else:
      assert t.lazydata.op.src[0].op.op == MovementOps.RESHAPE, 'Reshape should before permute'
      assert t.lazydata.op.op == MovementOps.PERMUTE, 'Permute should be after reshape'

  def test_push_permute_before_reshape(self):
    t = Tensor.ones(1,2,3,4)
    t = t.reshape(1,2,3*4).permute(2,1,0)
    self.helper_push_permute_before_reshape(t, should_push=True, desired_reshape_arg=(12,2,1), desired_permute_arg=(2,3,1,0))

    t = Tensor.ones(1,2,3,4)
    t = t.reshape(3,1,2,4).permute(3,2,1,0)
    self.helper_push_permute_before_reshape(t, should_push=False)

    t = Tensor.ones(1,2,3,1,4,1)
    t = t.reshape(1,2,3*4).permute(2,1,0)
    self.helper_push_permute_before_reshape(t, should_push=True, desired_reshape_arg=(12,2,1), desired_permute_arg=(2,3,4,5,1,0))

    t = Tensor.ones(1,2,3,4)
    t = t.reshape(1,2,3,1,4).permute(4,3,2,1,0)
    self.helper_push_permute_before_reshape(t, should_push=False)

  def test_push_permute_before_reduce(self):
    t = Tensor.ones(1,2,3,4)
    t = t.sum(axis=2).permute(2,1,0)
    if PUSH_PERMUTES:
      assert t.lazydata.op.src[0].op.src[0].op.op == MovementOps.PERMUTE, 'Permute should be pushed before reduce'
      assert t.lazydata.op.src[0].op.src[0].op.arg == (3,1,0,2), 'Pushed permute arg error'
      assert t.lazydata.op.src[0].op.op == ReduceOps.SUM, 'Sum should be after permute'
      assert t.lazydata.op.src[0].op.arg == (4,2,1,1), 'Sum arg error'
      assert t.lazydata.op.op == MovementOps.RESHAPE, 'Reshape should be after Sum'
      assert t.lazydata.op.arg == (4,2,1), 'Reshape arg error'
    else:
      assert t.lazydata.op.src[0].op.src[0].op.op == ReduceOps.SUM, 'Sum should be the first'
      assert t.lazydata.op.src[0].op.src[0].op.arg == (1,2,4,1), 'Sum arg error'
      assert t.lazydata.op.src[0].op.op == MovementOps.RESHAPE, 'Reshape should be after sum'
      assert t.lazydata.op.src[0].op.arg == (1,2,4), 'Reshape arg error'
      assert t.lazydata.op.op == MovementOps.PERMUTE, 'Permute should be after Reshape'
      assert t.lazydata.op.arg == (2,1,0), 'Permute arg error'

  def test_push_permute_before_expand(self):
    t = Tensor.ones(1,2,3,4)
    t = t.expand(2,2,3,4).permute(3,2,1,0)
    if PUSH_PERMUTES:
      assert t.lazydata.op.src[0].op.op == MovementOps.PERMUTE, 'Permute should be pushed before reduce'
      assert t.lazydata.op.src[0].op.arg == (3,2,1,0), 'Pushed permute arg error'
      assert t.lazydata.op.op == MovementOps.EXPAND, 'Expand should be after permute'
      assert t.lazydata.op.arg == (4,3,2,2), 'Expand arg error'
    else:
      assert t.lazydata.op.src[0].op.op == MovementOps.EXPAND, 'Expand should be the first'
      assert t.lazydata.op.src[0].op.arg == (2,2,3,4), 'Expand arg error'
      assert t.lazydata.op.op == MovementOps.PERMUTE, 'Permute should be after expand'
      assert t.lazydata.op.arg == (3,2,1,0), 'Permute arg error'
  """

if __name__ == '__main__':
  unittest.main()
