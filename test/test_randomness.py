import unittest, math
from functools import partial

import numpy as np
import torch
from tinygrad import nn, dtypes, Tensor, Device, TinyJit
from tinygrad.helpers import getenv, CI
from test.helpers import is_dtype_supported
from hypothesis import given, settings, strategies as strat

settings.register_profile("my_profile", max_examples=200, deadline=None, derandomize=getenv("DERANDOMIZE_CI", False))
settings.load_profile("my_profile")

# https://gist.github.com/devries/11405101
def ksprob(a):
  fac, total, termbf = 2.0, 0.0, 0.0
  a2 = -2.0 * a * a
  for j in range(1, 101):
    term = fac * math.exp(a2 * j * j)
    total += term
    if math.fabs(term) <= 0.001 * termbf or math.fabs(term) <= 1e-8 * total:
      return total
    fac = -fac
    termbf = math.fabs(term)
  return 1.0

def kstest(l1, l2):
  n1, n2 = len(l1), len(l2)
  l1.sort()
  l2.sort()
  j1, j2, d, fn1, fn2 = 0, 0, 0.0, 0.0, 0.0
  while j1 < n1 and j2 < n2:
    d1, d2 = l1[j1], l2[j2]
    if d1 <= d2:
      fn1 = (float(j1) + 1.0) / float(n1)
      j1 += 1
    if d2 <= d1:
      fn2 = (float(j2) + 1.0) / float(n2)
      j2 += 1
    dtemp = math.fabs(fn2 - fn1)
    if dtemp > d:
      d = dtemp
  ne = float(n1 * n2) / float(n1 + n2)
  nesq = math.sqrt(ne)
  prob = ksprob((nesq + 0.12 + 0.11 / nesq) * d)
  return prob

def equal_distribution(tiny_func, torch_func=None, numpy_func=None, shape=(40, 43), alpha=0.04):
  Tensor.manual_seed(1337)
  torch.manual_seed(1337)
  np.random.seed(1337)
  assert not (torch_func is None and numpy_func is None), "no function to compare with"
  x1 = tiny_func(*shape).numpy().flatten()
  x2 = tiny_func(shape).numpy().flatten()
  if numpy_func is not None: y = numpy_func(shape).flatten()
  if torch_func is not None: z = torch_func(shape).numpy().flatten()
  return (numpy_func is None or (kstest(x1, y) >= alpha and kstest(x2, y) >= alpha)) and \
    (torch_func is None or (kstest(x1, z) >= alpha and kstest(x2, z) >= alpha))

def normal_test(func, shape=(20, 23), alpha=0.05): return equal_distribution(func, numpy_func=lambda x: np.random.randn(*x), shape=shape, alpha=alpha)

class TestRandomness(unittest.TestCase):
  def test_rand(self):
    self.assertFalse(normal_test(Tensor.rand))
    self.assertTrue(equal_distribution(Tensor.rand, torch.rand, lambda x: np.random.rand(*x)))

  @unittest.skipUnless(is_dtype_supported(dtypes.float16), "need float16 support")
  def test_rand_float16(self):
    N = 128
    x = Tensor.rand((2, N, N), dtype=dtypes.float16)
    assert x.dtype == dtypes.float16
    nx = x.numpy()
    # seed dependant, check output range is [0, 1)
    assert nx[nx == 1].size == 0
    assert nx[nx == 0].size > 0
    equal_distribution(lambda *x: Tensor.rand(*x, dtype=dtypes.float16), torch.rand, lambda x: np.random.rand(*x), shape=(2, N, N))

  @unittest.skipIf(CI and Device.DEFAULT == "NV", "gpuocelot doesn't support certain ops needed for threefry")
  def test_threefly_against_reference(self):
    Tensor.manual_seed(1337)

    # reference generated using
    """
    key0 = 1337
    key1 = 0
    values = jax.extend.random.threefry_2x32((np.uint32(key1), np.uint32(key0)), np.arange(20, dtype=np.uint32))
    print(f"[{', '.join(f'{v}' for v in values)}]")
    """
    jr = np.array([2221762175, 1752107825, 653745012, 1967534793, 1395205442, 3840423848, 2159346757,
                   603508235, 3319473678, 3363866483, 3544324138, 1436466838, 2169858556, 2570072943,
                   2387150698, 3678370550, 2911697663, 403244401, 2560861638, 1692360114])

    counts = Tensor.arange(20, dtype=dtypes.uint32)
    counts0, counts1 = counts.chunk(2)
    r = Tensor._threefry_random_bits(1337, 0, counts0, counts1).numpy()

    np.testing.assert_allclose(jr, r)

  @unittest.skipUnless(Device.DEFAULT == "GPU", "reference is on GPU")
  def test_threefly_against_reference_full(self):
    Tensor.manual_seed(1337)

    # reference generated using
    """
    key0 = 1337
    key1 = 0
    values = jax.extend.random.threefry_2x32((np.uint32(key1), np.uint32(key0)), np.arange(20, dtype=np.uint32))
    print(f"[{', '.join(f'{v}' for v in values)}]")
    """
    jr = np.array([0.7882130146026611, 0.0680311918258667, 0.6758031845092773, 0.2525523900985718, 0.5712389945983887,
                   0.8758237361907959, 0.13559412956237793, 0.9069793224334717, 0.8781528472900391, 0.7737162113189697,
                   0.050452232360839844, 0.1645597219467163, 0.06776463985443115, 0.09560704231262207, 0.2754603624343872,
                   0.10108339786529541, 0.3488548994064331, 0.7904064655303955, 0.2519160509109497, 0.7925788164138794], dtype=np.float32)

    r = Tensor.rand(20).numpy()

    np.testing.assert_allclose(jr, r, atol=1e-5, rtol=1e-5)

  @unittest.skipIf(CI and Device.DEFAULT in ("GPU", "CUDA", "METAL", "NV"), "no GPU CI")
  def test_threefly_tensors_cnt(self):
    Tensor.manual_seed(1337)

    Tensor.rand(20).realize()

    assert len(Tensor._device_rng_counters) == 1
    assert len(Tensor._device_seeds) == 1

    Tensor.rand(20, device=f"{Device.DEFAULT}:1").realize()

    assert len(Tensor._device_rng_counters) == 2
    assert len(Tensor._device_seeds) == 2

    Tensor.manual_seed(2)

    assert len(Tensor._device_rng_counters) == 0
    assert len(Tensor._device_seeds) == 0

  @unittest.skipUnless(is_dtype_supported(dtypes.bfloat16), "need bfloat16 support")
  def test_rand_bfloat16(self):
    N = 128
    x = Tensor.rand((2, N, N), dtype=dtypes.bfloat16)
    assert x.dtype == dtypes.bfloat16
    nx = x.numpy()
    assert nx[nx == 1].size == 0
    assert nx[nx == 0].size > 0
    equal_distribution(lambda *x: Tensor.rand(*x, dtype=dtypes.bfloat16).float(), torch.rand, lambda x: np.random.rand(*x), shape=(2, N, N))

  def test_rand_like(self):
    empty = Tensor.empty((80, 44))
    rand = Tensor.rand_like(empty)
    assert rand.shape == empty.shape
    assert rand.dtype == empty.dtype
    assert rand.device == empty.device

  def test_rand_like_zero_shape(self):
    empty = Tensor.empty(0, 20)
    rand = Tensor.rand_like(empty)
    assert rand.shape == empty.shape
    assert rand.dtype == empty.dtype
    assert rand.device == empty.device

  def test_rand_like_more_dims(self):
    empty = Tensor.empty((1, 2, 3, 4, 5, 6))
    rand = Tensor.rand_like(empty)
    assert rand.shape == empty.shape
    assert rand.dtype == empty.dtype
    assert rand.device == empty.device

  @unittest.skipUnless(is_dtype_supported(dtypes.float16), "need float16 support")
  def test_rand_like_dtype(self):
    empty = Tensor.empty((80, 44), dtype=dtypes.float16)
    rand = Tensor.rand_like(empty)
    assert rand.shape == empty.shape
    assert rand.dtype == empty.dtype
    assert rand.device == empty.device

    empty = Tensor.empty((80, 44))
    rand = Tensor.rand_like(empty, dtype=dtypes.float16)
    assert rand.shape == empty.shape
    assert rand.dtype == dtypes.float16
    assert rand.device == empty.device

  def test_randn(self):
    self.assertTrue(normal_test(Tensor.randn))
    self.assertTrue(equal_distribution(Tensor.randn, torch.randn, lambda x: np.random.randn(*x)))

  @given(strat.sampled_from([dtypes.float, dtypes.float16, dtypes.bfloat16]))
  @unittest.skipIf(Device.DEFAULT in ["HSA", "AMD"], "bfloat16 local buffer broken in HSA")
  def test_randn_finite(self, default_float):
    if not is_dtype_supported(default_float): return
    old_default_float = dtypes.default_float
    # low precision can result in inf from randn
    dtypes.default_float = default_float
    t = Tensor.randn(1024, 1024)
    mx = t.max().numpy().item()
    mn = t.min().numpy().item()
    print(f"testing with {default_float=}")
    assert math.isfinite(mx), mx
    assert math.isfinite(mn), mn
    dtypes.default_float = old_default_float

  def test_randint(self):
    self.assertFalse(normal_test(Tensor.randint))
    self.assertTrue(equal_distribution(partial(Tensor.randint, low=-2, high=5), numpy_func=lambda x: np.random.randint(low=-2, high=5, size=x)))
    self.assertTrue(Tensor.randint(1, device="CLANG").device=="CLANG")
    # check types of args
    with self.assertRaises(TypeError): Tensor.randint((3, 4), low=0.1, high=3)
    with self.assertRaises(TypeError): Tensor.randint((3, 4), low=0, high=3.5)
    with self.assertRaises(TypeError): Tensor.randint((3, 4), low=0, high=3, dtype=dtypes.float32)

  def test_normal(self):
    self.assertTrue(normal_test(Tensor.normal))
    self.assertTrue(equal_distribution(Tensor.normal, lambda x: torch.nn.init.normal_(torch.empty(x), mean=0, std=1),
                                                      lambda x: np.random.normal(loc=0, scale=1, size=x)))

  def test_uniform(self):
    self.assertFalse(normal_test(Tensor.uniform))
    self.assertTrue(equal_distribution(Tensor.uniform, lambda x: torch.nn.init.uniform_(torch.empty(x)), lambda x: np.random.uniform(size=x)))
    self.assertTrue(equal_distribution(partial(Tensor.uniform, low=-100, high=100, dtype=dtypes.int32),
                                       numpy_func=lambda x: np.random.randint(low=-100, high=100, size=x)))

  def test_scaled_uniform(self):
    self.assertFalse(normal_test(Tensor.scaled_uniform))
    self.assertTrue(equal_distribution(Tensor.scaled_uniform, lambda x: torch.nn.init.uniform_(torch.empty(x), a=-1, b=1) / math.sqrt(math.prod(x)),
                                                              lambda x: np.random.uniform(-1, 1, size=x) / math.sqrt(math.prod(x))))

  def test_glorot_uniform(self):
    self.assertFalse(normal_test(Tensor.glorot_uniform))
    self.assertTrue(equal_distribution(Tensor.glorot_uniform, lambda x: torch.nn.init.xavier_uniform_(torch.empty(x)),
                                                              lambda x: np.random.uniform(-1, 1, size=x) * math.sqrt(6 / (x[0] + math.prod(x[1:])))))

  def test_kaiming_uniform(self):
    for shape in [(256, 128, 3, 3), (80, 44), (3, 55, 35)]:
      self.assertTrue(equal_distribution(Tensor.kaiming_uniform, lambda x: torch.nn.init.kaiming_uniform_(torch.empty(x)), shape=shape))

  def test_kaiming_normal(self):
    for shape in [(256, 128, 3, 3), (80, 44), (3, 55, 35)]:
      self.assertTrue(equal_distribution(Tensor.kaiming_normal, lambda x: torch.nn.init.kaiming_normal_(torch.empty(x)), shape=shape))

  def test_multinomial(self):
    self.assertRaises(AssertionError, lambda: Tensor(2).multinomial(1, replacement=False))
    self.assertRaises(AssertionError, lambda: Tensor([1, 9]).multinomial(0, replacement=False))
    def _check_with_torch(w, num_samples, replacement):
      tiny_res = Tensor(w).multinomial(num_samples, replacement=replacement)
      torch_res = torch.tensor(w).multinomial(num_samples, replacement=replacement)
      self.assertEqual(tiny_res.shape, torch_res.shape)
      if torch_res.ndim == 1:
        tiny_res = tiny_res.unsqueeze(0)
        torch_res = torch_res.unsqueeze(0)
      for i in range(torch_res.shape[0]):
        self.assertTrue(equal_distribution(lambda *_: tiny_res[i], lambda _: torch_res[i]))
    _check_with_torch(w=[0.231, 0., 1., 0.5], num_samples=2000, replacement=True)
    _check_with_torch(w=[[0.2, 0.8]], num_samples=2000, replacement=True)  # 2D but only 1 row
    _check_with_torch(w=[[0.453, 0., 1., 0.81], [0.1, 0.8, 0., 0.1]], num_samples=2000, replacement=True)
    # no-replacement isn't supported, unless taking only one sample
    w = [0.1, 0.9]
    self.assertRaises(AssertionError, lambda: Tensor(w).multinomial(100, replacement=False))

    @TinyJit
    def sample_one(): return Tensor(w).multinomial(1, replacement=False).realize()

    # TODO: fix mockgpu issue
    if not (CI and Device.DEFAULT == "AMD"):
      tiny_samples = [sample_one().item() for _ in range(1000)]
      torch_samples = [torch.tensor(w).multinomial(1, replacement=False).item() for _ in range(1000)]
      self.assertTrue(equal_distribution(lambda *_: Tensor(tiny_samples), lambda _: torch.tensor(torch_samples)))

  def test_multinomial_counterexample(self):
    tiny_res = Tensor([0.3, 0.6, 0.1]).multinomial(2000, replacement=True)
    torch_res = torch.tensor([0.3, 0.6, 0.1]).multinomial(2000, replacement=True)
    self.assertTrue(equal_distribution(lambda *_: tiny_res, lambda _: torch_res))
    torch_res = torch.tensor([0.2, 0.7, 0.1]).multinomial(2000, replacement=True)
    self.assertFalse(equal_distribution(lambda *_: tiny_res, lambda _: torch_res))

  def test_conv2d_init(self):
    params = (128, 256, (3,3))
    assert equal_distribution(lambda *_: nn.Conv2d(*params).weight, lambda _: torch.nn.Conv2d(*params).weight.detach())
    assert equal_distribution(lambda *_: nn.Conv2d(*params).bias, lambda _: torch.nn.Conv2d(*params).bias.detach())

  def test_linear_init(self):
    params = (64, 256)
    assert equal_distribution(lambda *_: nn.Linear(*params).weight, lambda _: torch.nn.Linear(*params).weight.detach())
    assert equal_distribution(lambda *_: nn.Linear(*params).bias, lambda _: torch.nn.Linear(*params).bias.detach())

  def test_bn_init(self):
    params = (64,)
    assert equal_distribution(lambda *_: nn.BatchNorm2d(*params).weight, lambda _: torch.nn.BatchNorm2d(*params).weight.detach())
    assert equal_distribution(lambda *_: nn.BatchNorm2d(*params).bias, lambda _: torch.nn.BatchNorm2d(*params).bias.detach())

if __name__ == "__main__":
  unittest.main()
