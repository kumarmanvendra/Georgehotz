import math
import unittest
import numpy as np
import torch
from tinygrad.tensor import Tensor

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

def normal_test(func, shape=(20, 23), alpha=0.05):
  x = func(*shape).cpu().numpy().flatten()
  y = np.random.randn(*shape).flatten()
  return kstest(x, y) >= alpha

def equal_distribution(tiny_func, torch_func, numpy_func, shape=(20, 23), alpha=0.05):
  Tensor.manual_seed(1337)
  torch.manual_seed(1337)
  np.random.seed(1337)
  x = tiny_func(*shape).cpu().numpy().flatten()
  y = numpy_func(shape).flatten()
  z = torch_func(shape).numpy().flatten()
  return kstest(x, y) >= alpha and kstest(x, z) >= alpha

class TestRandomness(unittest.TestCase):
  def test_rand(self):
    self.assertFalse(normal_test(Tensor.rand))
    self.assertTrue(equal_distribution(Tensor.rand, torch.rand, lambda x: np.random.rand(*x)))

  def test_randn(self):
    self.assertTrue(normal_test(Tensor.randn))
    self.assertTrue(equal_distribution(Tensor.randn, torch.randn, lambda x: np.random.randn(*x)))

  def test_uniform(self):
    self.assertFalse(normal_test(Tensor.uniform))
    self.assertTrue(equal_distribution(Tensor.uniform, lambda x: torch.nn.init.uniform_(torch.empty(x), a=-1, b=1), lambda x: np.random.rand(*x) * 2 - 1))

  def test_scaled_uniform(self):
    self.assertFalse(normal_test(Tensor.scaled_uniform))
    self.assertTrue(equal_distribution(Tensor.scaled_uniform, lambda x: torch.nn.init.uniform_(torch.empty(x), a=-1, b=1) / math.sqrt(math.prod(x)), lambda x: (np.random.rand(*x) * 2 - 1) / math.sqrt(math.prod(x))))

  def test_glorot_uniform(self):
    self.assertFalse(normal_test(Tensor.glorot_uniform))
    self.assertTrue(equal_distribution(Tensor.glorot_uniform, lambda x: torch.nn.init.xavier_uniform_(torch.empty(x)), lambda x: (np.random.rand(*x) * 2 - 1) * math.sqrt(6 / (x[0] + math.prod(x[1:])))))

  def test_kaiming_uniform(self):
    self.assertFalse(normal_test(Tensor.kaiming_uniform))
    self.assertTrue(equal_distribution(Tensor.kaiming_uniform, lambda x: torch.nn.init.kaiming_uniform_(torch.empty(x)), lambda x: (np.random.rand(*x) * 2 - 1) * math.sqrt(3.0) * math.sqrt(2.0) / math.sqrt(x[0])))

if __name__ == "__main__":
  unittest.main()
