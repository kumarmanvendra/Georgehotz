# this will be the new test_ops for the next level
# schedule confirms the right things are capable of fusing
# NOTE: this has overlap with external_test_opt.py

import unittest
from test.helpers import assert_jit_cache_len
from tinygrad.engine.jit import TinyJit
import torch
import numpy as np
from typing import List, Optional
from tinygrad.device import Device
from tinygrad.engine.realize import run_schedule
from tinygrad.tensor import Tensor
from tinygrad.ops import LoadOps
from tinygrad.helpers import DEBUG, GRAPH
from tinygrad.codegen.linearizer import Linearizer
from tinygrad.features.graph import print_tree, realized_lazybuffer
from tinygrad.engine.schedule import create_schedule
from tinygrad import nn, dtypes

def check_schedule(t:Tensor, allowed:int, to_prerealize:Optional[List[Tensor]]=None, filter_loadops=True):
  seen = set()
  if to_prerealize:
    for pre in to_prerealize:
      for s in create_schedule([pre.lazydata], seen.copy()):
        for i,out in enumerate(s.outputs):
          if GRAPH: realized_lazybuffer(out, 0)
          seen.add(out)
  sched = create_schedule([t.lazydata], seen)
  if GRAPH:
    for i,s in enumerate(sched):
      for out in s.outputs: realized_lazybuffer(out, i+1)
  if filter_loadops: sched = [s for s in sched if s.ast[0].op not in LoadOps]
  if len(sched) != allowed: print(f"SCHEDULE ISSUE, expecting {allowed} got {len(sched)}")
  if len(sched) != allowed or DEBUG >= 3:
    for i, s in enumerate(sched):
      print("kernel", i+1)
      for op in s.ast: print_tree(op)
  assert len(sched) == allowed
  # test the (non loadops) ops linearize
  for s in sched:
    if s.ast[0].op in LoadOps: continue
    l = Linearizer(*s.ast)
    l.hand_coded_optimizations()
    l.linearize()

class TestSchedule(unittest.TestCase):
  def test_basic_binop_fusion(self):
    a = Tensor.empty(10)
    b = Tensor.empty(10)
    c = Tensor.empty(10)
    d = a+b+c
    check_schedule(d, 1)

  def test_basic_binop_fusion_deep(self):
    a = Tensor.empty(10)
    b = Tensor.empty(10)
    c = Tensor.empty(10)
    d = Tensor.empty(10)
    e = a+b+c+d
    check_schedule(e, 1)

  def test_mulacc_fusion(self):
    a = Tensor.empty(10)
    b = Tensor.empty(10)
    c = (a*b).sum()
    check_schedule(c, 1)

  def test_mulacc_relu_fusion(self):
    a = Tensor.empty(10)
    b = Tensor.empty(10)
    c = (a*b).sum().relu()
    check_schedule(c, 1)

  def test_binop_reshape_fusion(self):
    a = Tensor.empty(10)
    b = Tensor.empty(10)
    c = Tensor.empty(5,2)
    d = (a+b).reshape(5,2)+c
    check_schedule(d, 1)

  def test_binop_permute_fusion(self):
    a = Tensor.empty(2,5)
    b = Tensor.empty(2,5)
    c = Tensor.empty(5,2)
    d = (a+b).permute(1,0)+c
    check_schedule(d, 1)

  def test_constants_are_embedded(self):
    a = Tensor.empty(3,3) * 2
    check_schedule(a, 2, filter_loadops=False)

  def test_binop_elu_fusion(self):
    a = Tensor.empty(10)
    b = a.elu()
    check_schedule(b, 1)

  def test_binop_reshape_reduce_fusion(self):
    a = Tensor.empty(100)
    b = Tensor.empty(100)
    c = (a+b).reshape(10, 10).sum(axis=0, keepdim=True)
    check_schedule(c, 1)

  def test_reduce_reshape_binop_fusion(self):
    a = Tensor.empty(10,10)
    b = Tensor.empty(10)
    c = a.sum(axis=0) + b
    check_schedule(c, 1)

  @unittest.skip("not pushing permutes through reduces")
  def test_reduce_permute_binop_fusion(self):
    a = Tensor.empty(10,10,10)
    b = Tensor.empty(10,10,1)
    c = a.sum(axis=0, keepdim=True).permute(2,1,0) + b
    check_schedule(c, 1)

  def test_binop_early_reshape_reduce_fusion(self):
    a = Tensor.empty(100)
    b = Tensor.empty(100)
    c = Tensor.empty(10,10)
    d = ((a+b).reshape(10,10) + c).sum(axis=0)
    check_schedule(d, 1)

  def test_diamond_folded(self):
    a = Tensor.empty(10)
    b = Tensor.empty(10)
    c = Tensor.empty(10)
    d = Tensor.empty(10)
    ab = a+b
    e = (ab+c) + (ab+d)
    check_schedule(e, 1)

  def test_cache_binaryop(self):
    a = Tensor.empty(10)
    b = Tensor.empty(10)
    c = a+b
    d = a+b
    check_schedule(d, 0, [c])

  @unittest.skip("failing in old lazy")
  def test_cache_binaryop_reshaped(self):
    a = Tensor.empty(10)
    b = Tensor.empty(10)
    c = a+b
    d = a.reshape(10,1)+b.reshape(10,1)
    check_schedule(d, 0, [c])

  @unittest.skip("failing in new lazy")
  def test_cache_binaryop_transpose(self):
    a = Tensor.empty(10,10)
    b = Tensor.empty(10,10)
    c = (a.T*b.T).T #.contiguous()
    d = a*b
    check_schedule(d, 0, [c])

  def test_cache_two_reduceops(self):
    a = Tensor.empty(10)
    b = a.sum()
    c = a.sum()
    bc = b+c
    check_schedule(bc, 1)

  def test_fold_double_unary(self):
    y = Tensor.empty(2)
    out = y.sum(keepdim=True).sqrt().__neg__()
    check_schedule(out, 1)

  #@unittest.skip("may want to reconsider this")
  def test_fold_batchnorm(self):
    with Tensor.train():
      img = Tensor.empty(1,32,4,4)
      bn = nn.BatchNorm2d(32, track_running_stats=False)
      out = bn(img)
      check_schedule(out, 3)

  def test_fold_conv_relu(self):
    c1 = nn.Conv2d(3,16,3)

    # run
    img = Tensor.ones(2,3,64,64)
    out = c1(img).relu()
    check_schedule(out, 1, [c1.weight, c1.bias])

  def test_fold_conv_elu(self):
    c1 = nn.Conv2d(3,16,3)

    # run
    img = Tensor.rand(2,3,64,64)
    out = c1(img).elu()
    check_schedule(out, 1, [c1.weight, c1.bias, img])

  def test_two_sum(self):
    img = Tensor.empty(64,64)
    x = (img.sum(0) + img.sum(1))
    out = x.relu()
    del x    # is 3 without this
    check_schedule(out, 2)

  #@unittest.skip("failing in old lazy")
  def test_push_permute_through_reshape(self):
    a = Tensor.empty(16,16)
    b = Tensor.empty(16,16)
    c = (a+b).reshape(4,4,4,4).permute(2,3,0,1).contiguous()
    check_schedule(c, 1)

  #@unittest.skip("failing in old lazy")
  def test_push_permute_through_reshape_alt(self):
    a = Tensor.empty(4,4,4,4)
    b = Tensor.empty(4,4,4,4)
    c = (a+b).reshape(16,16).permute(1,0).contiguous()
    check_schedule(c, 1)

  def test_no_binop_rerun(self):
    a = Tensor.empty(16)
    b = Tensor.empty(16)
    c = a+b
    d = (a+b).reshape(16,1)
    check_schedule(d, 0, [c])

  def test_multi_permute_should_collapse(self):
    a = Tensor.empty(4,4,4,4)
    b = Tensor.empty(16)
    c = a.sum((0,1)).cast(dtypes.float16).permute(1,0).reshape(4,4,1).permute(1,0,2).reshape(16) + b
    check_schedule(c, 1)

  @unittest.skip("failing in old lazy")
  def test_fancy_reshape_fusion(self):
    a = Tensor.empty(10)
    b = Tensor.empty(10)
    c = a+b
    d = a.reshape(10,1)+b.reshape(10,1)
    out = c.sum() + d.sum()
    check_schedule(out, 1)

  # NOTE: for this to pass, LazyViews must be children of LazyBuffers so the (a+b) runs first
  @unittest.skip("not real world")
  def test_children_dont_push(self):
    a = Tensor.empty(10, 10, 1)
    b = Tensor.empty(10, 10, 1)
    d = (a+b).expand(10, 10, 10)
    e = (a+b).permute(2,1,0)
    f = d+e
    check_schedule(f, 2)

  @unittest.skip("failing in new lazy")
  def test_dont_fuse_binops_with_children(self):
    a = Tensor.empty(10)
    b = Tensor.empty(10)
    c = Tensor.empty(10)
    keep_me = a+b
    e = keep_me.sum() # noqa: F841 give keep_me a child (NOTE: BinaryOps won't be a child since it will instant fuse)
    d = keep_me+c
    check_schedule(d, 2)
    check_schedule(keep_me, 0, [d])

  #@unittest.skip("failing in old lazy")
  def test_permute_breaks_fusion(self):
    a = Tensor.empty(10, 10, 10)
    b = Tensor.empty(10, 10)
    c = (a.sum(axis=2) + b).permute(1,0)
    d = c.permute(1,0)
    check_schedule(d, 1)

  def test_some_permute_fusion(self):
    a = Tensor.empty(8192, 16)
    b = Tensor.empty(1, 16)
    d = (a.T + b.expand(8192, 16).T)
    c = a + b.expand(8192, 16)
    e = d.T
    check_schedule(c, 1)
    check_schedule(e, 1)

  def test_shrink_fuse(self):
    a = Tensor.empty(8192, 16)
    b = Tensor.empty(8192, 16)
    c = a * b
    d = Tensor.empty(1, 16)
    e = c[0] * d
    check_schedule(e, 1)

  def test_expand_nofuse(self):
    a = Tensor.empty(1, 16)
    b = Tensor.empty(1, 16)
    c = a * b
    d = Tensor.empty(8192, 16)
    e = c * d
    check_schedule(e, 2)

  # this is the failing case in openpilot...it's very simple like this
  @unittest.skip("failing in old lazy")
  def test_image_conv_fusion(self):
    from tinygrad.features.image import image_conv2d
    w1 = Tensor.empty(16, 16, 1, 1)
    b1 = Tensor.empty(16)
    w2 = Tensor.empty(16, 16, 1, 1)
    b2 = Tensor.empty(16)
    w3 = Tensor.empty(16, 16, 1, 1)
    b3 = Tensor.empty(16)

    x = Tensor.empty(1, 16, 32, 32)
    x = base = image_conv2d(x, w1, b1)
    x = image_conv2d(x, w2, b2) + base
    x = image_conv2d(x, w3, b3)

    # NOOP, 3 convs, contiguous
    check_schedule(x, 5)

  def test_image_conv_fusion_minimal(self):
    b1 = Tensor.empty(16)
    b2 = Tensor.empty(16)
    def p(x): return x.permute(1,0).contiguous().reshape(32,16,1).expand(32,16,16).sum(axis=2).permute(1,0)

    x = Tensor.empty(16, 32)
    x = base = p(x) + b1.reshape(16,1)
    x = p(x)
    x = x + b2.reshape(16,1)
    x = x + base
    del base
    x = p(x)
    check_schedule(x, 4)

  def test_image_conv_fusion_more_minimal(self):
    b1 = Tensor.empty(16)
    def p(x): return x.permute(1,0).contiguous().reshape(32,16,1).expand(32,16,16).sum(axis=2).permute(1,0)

    x = Tensor.empty(16, 32)
    x = base = p(x) + b1.reshape(16,1)
    x = p(x)
    del base
    check_schedule(x, 3)

  def test_resnet_block(self):
    Tensor.training = False

    in_planes, planes = 64, 64
    conv1 = nn.Conv2d(in_planes, planes, kernel_size=3, stride=1, padding=1, bias=False)
    bn1 = nn.BatchNorm2d(planes)
    conv2 = nn.Conv2d(planes, planes, kernel_size=3, padding=1, stride=1, bias=False)
    bn2 = nn.BatchNorm2d(planes)

    x = Tensor.empty(1, 64, 32, 32)
    out = bn1(conv1(x)).relu()
    out = bn2(conv2(out))
    out = (out + x).relu()
    check_schedule(out, 2, [conv1.weight, conv2.weight])

  def test_contiguous_while_contiguous(self):
    x = Tensor.empty(1, 64, 32, 32)
    out = x.contiguous()
    check_schedule(out, 1, filter_loadops=False)

  def test_contiguous_while_not_contiguous(self):
    x = Tensor.empty(1, 64, 32, 32)
    out = x.permute(0,2,3,1).contiguous()
    check_schedule(out, 2, filter_loadops=False)

  def test_double_from(self):
    x = Tensor([1,2,3,4])
    out = x.to('ext')
    check_schedule(out, 0, filter_loadops=False)

  def test_pow_const_tensor_simplified(self):
    x = Tensor([1,2,3,4])
    # NOTE: this does not test ** Tensor(2) is simpler in ast than ** Tensor(2.5)
    out = x ** Tensor(2)
    check_schedule(out, 1)

  def test_pow_const_tensor_to_zero(self):
    x = Tensor([1,2,3,4])
    out = x ** Tensor(0)
    # NOTE: this is ConstBuffer 0 + ConstBuffer 1
    check_schedule(out, 1)

  def test_zero_size(self):
    x = Tensor.empty(2, 3, 0)
    out = x + 1
    check_schedule(out, 0, filter_loadops=False)

  def test_reduce_permute_nofuse(self):
    x = Tensor.empty(32, 32, 32)
    y = Tensor.empty(32, 32)
    out = x.sum(axis=2).T+y
    check_schedule(out, 2)

  def test_two_elus_sum(self):
    x = Tensor.empty(32, 32)
    y = Tensor.empty(32, 32)
    out = x.sum(1).relu().elu() + y.sum(1).relu().elu()
    check_schedule(out, 2)

  def test_multistage_reduce(self):
    x = Tensor.empty(32, 32, 32)
    out = x.sum(2).relu().sum(1)
    check_schedule(out, 2)

  def test_multistage_reduce_fork(self):
    x = Tensor.empty(32, 32, 32)
    x = x.sum(2)
    out2 = x + 1
    out = x.relu().sum(1) + out2[0]
    check_schedule(out, 2)

  def test_example_matmul(self):
    x = Tensor.eye(64, requires_grad=True)
    y = Tensor.eye(64, requires_grad=True)
    z = y.matmul(x).sum()
    z.backward()
    out = x.grad.contiguous()
    check_schedule(out, 2)

  def test_contiguous_add(self):
    x = Tensor.empty(32)
    y = Tensor.empty(32)
    z = Tensor.empty(32)
    out = (x+y).contiguous()+z
    check_schedule(out, 2)

  def test_double_sum_ref(self):
    x = Tensor.empty(32, 32, 32)
    x = x.sum(2)
    out = x + x[:, 4]
    check_schedule(out, 2)

  def test_reduce_shrink(self):
    x = Tensor.empty(32, 32)
    y = Tensor.empty(16)
    x = x.sum(1)
    x = x[:16]
    out = x + y
    check_schedule(out, 2)  # TODO: this should be 1

  def test_const_no_recompute(self):
    x = Tensor(2) + Tensor(2)
    y = Tensor(2) + Tensor(2)
    out = x.contiguous() + y.contiguous()
    check_schedule(out, 2)


@unittest.skipIf(Device.DEFAULT == "METAL", "No multioutput in Metal because of the 32 buffer limit.")
class TestMultiOutputSchedule(unittest.TestCase):
  def _test(self, outs_tiny:List[Tensor], outs_np:List[np.ndarray]=[], allowed:int=1):
    sched = create_schedule([x.lazydata for x in outs_tiny])
    kernels = [si for si in sched if si.ast[0].op not in LoadOps]
    assert len(kernels) == allowed, f"Expected {allowed} kernels, got {len(kernels)}"
    run_schedule(sched)
    for out_tiny, out_np in zip(outs_tiny, outs_np): np.testing.assert_equal(out_tiny.numpy(), out_np)

  def test_simple(self):
    a, b = Tensor([1,2]), Tensor([3,4])
    out0, out1 = a+b, a*b
    out0_np, out1_np = a.numpy()+b.numpy(), a.numpy()*b.numpy()
    self._test([out0, out1], [out0_np, out1_np], 1)

  def test_contiguous_single_kernel(self):
    a, b = Tensor([1,2]), Tensor([3,4])
    out0, out1 = (a+b).contiguous(), (a*b).contiguous()
    out0_np, out1_np = a.numpy()+b.numpy(), a.numpy()*b.numpy()
    self._test([out0, out1], [out0_np, out1_np], 2)

  def test_reduce_unique_kernel(self):
    a, b = Tensor([1,2]), Tensor([3,4])
    out0, out1, out2 = a+b, a*b, a.sum()
    out3 = out2 + a
    out0_np, out1_np, out2_np = a.numpy()+b.numpy(), a.numpy()*b.numpy(), a.numpy().sum()
    out3_np = out2_np+a.numpy()
    self._test([out0, out1, out2, out3], [out0_np, out1_np, out2_np, out3_np], 3)

  def test_reduce_pair_fusion(self):
    a_sum = Tensor([1,2,3,4]).sum()
    out0, out1 = a_sum+2, a_sum+3
    out0_np, out1_np = a_sum.numpy()+2, a_sum.numpy()+3
    self._test([out0, out1], [out0_np, out1_np], 1)

  def test_simple_assign(self):
    a, b = Tensor.ones(4).contiguous().realize(), Tensor.ones(4).contiguous().realize()
    a.assign(Tensor.full((4,), 2.))
    b.assign(Tensor.full((4,), 3.))
    self._test([a, b], [np.full((4,), 2.), np.full((4,), 3.)], 1)

  def test_double_assign_two_kernels(self):
    a = Tensor.ones(4).contiguous().realize()
    a += 1
    a += 1
    self._test([a], [np.full((4,), 3.)], 2)

  def test_fused_diamond(self):
    a, b = Tensor.ones(4).contiguous().realize(), Tensor.ones(4).contiguous().realize()
    times_a, times_b = a*2, b*3

    a.assign(Tensor.full((4,), 4.))
    b.assign(Tensor.full((4,), 5.))

    old_a_add = (times_a+1).contiguous()
    old_b_add = (times_b+1).contiguous()

    new0 = a + old_a_add
    new1 = b + old_b_add

    self._test([new0, new1], [np.full((4,), 7.), np.full((4,), 9.)], 4)

  def test_change_shape(self):
    a, b = Tensor([1,2,3,4]), Tensor([5,6,7,8])
    out0, out1 = a+b, a*b
    out2, out3 = a.reshape((2,2))+b.reshape((2,2)), a.reshape((2,2))*b.reshape((2,2))
    self._test([out0, out1, out2, out3], allowed=2)

  def test_multiple_steps_fusion(self):
    init_x = Tensor.randn((1, 4)).numpy()
    init_W = Tensor.randn((4, 4)).numpy()

    class Model:
      def __init__(self, tensor):
        self.x = tensor(init_x, requires_grad=True)
        self.W = tensor(init_W, requires_grad=True)
      def forward(self): return (self.x * self.W).sum()

    tiny_model = Model(Tensor)
    tiny_adam = nn.optim.Adam([tiny_model.x, tiny_model.W], lr=0.001)
    torch_model = Model(torch.tensor)
    torch_adam = torch.optim.Adam([torch_model.x, torch_model.W], lr=0.001)

    def train_step(model, optimizer):
      out = model.forward()
      optimizer.zero_grad()
      out.backward()
      optimizer.step()
    jitted_step = TinyJit(train_step)

    for _ in range(4):
      jitted_step(tiny_model, tiny_adam)
      train_step(torch_model, torch_adam)
    assert_jit_cache_len(jitted_step, 7)
    np.testing.assert_allclose(tiny_model.x.detach().numpy(), torch_model.x.detach().numpy(), atol=1e-4, rtol=1e-4)

  @unittest.skip("Doesn't yet fuse multilevel")
  def test_multilevel_nodes(self):
    a, b = Tensor([1]), Tensor([2])
    out0, out1 = a+2, b+2
    out2 = out0 + out1
    out0_np, out1_np = a.numpy()+2, b.numpy()+2
    self._test([out0, out1, out2], [out0_np, out1_np, out0_np+out1_np], 1)

  @unittest.skip("Doesn't yet simplify ones in output shape")
  def test_simplified_shape(self):
    a, b = Tensor.randn(4).reshape(4, 1), Tensor.randn(4)
    out0, out1 = a+2, b+2
    out0_np, out1_np = a.numpy()+2, b.numpy()+2
    self._test([out0, out1], [out0_np, out1_np], 1)

  @unittest.skip("TODO: correct reduce pairs")
  def test_reduce_contig_children_with_reduce(self):
    a_sum = Tensor([1,2,3,4]).sum()
    b = Tensor([6])
    c = Tensor([9,8,7,6,5])
    out0 = a_sum+b
    out1 = c.max()+a_sum+b
    self._test([out0, out1], allowed=2)

  @unittest.skip("TODO: correct reduce pairs")
  def test_reduce_pair_different_parents_possible_fusion(self):
    a_sum = Tensor([1,2,3,4]).sum()
    b, c = Tensor([1]), Tensor([2])
    out0 = a_sum+b
    out1 = a_sum+c
    self._test([out0, out1], [np.array([11]), np.array([12])], 1)

  @unittest.skip("TODO: correct reduce pairs")
  def test_reduce_pair_different_reduce_parents(self):
    a_reduce = Tensor.randint((4,)).sum()
    b_reduce = Tensor.randint((4,)).sum()
    c_reduce = Tensor.randint((4,)).sum()

    out0 = a_reduce+b_reduce+c_reduce
    out1 = a_reduce+Tensor([5])
    self._test([out0, out1], allowed=3)

if __name__ == '__main__':
  unittest.main(verbosity=2)
