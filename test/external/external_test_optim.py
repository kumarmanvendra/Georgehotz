#!/usr/bin/env python
import unittest

import nodeenv
import numpy as np
import tensorflow as tf
import tensorflow_addons as tfa
from tensorflow.python.ops import math_ops

from tinygrad.tensor import Tensor
from tinygrad.nn.optim import LAMB
from examples.mlperf.optimizers import LARS
from examples.mlperf.lr_schedulers import PolynomialDecayWithWarmup
from test.external.mlperf_resnet.lars_optimizer import LARSOptimizer
from test.external.mlperf_resnet.lars_util import PolynomialDecayWithWarmup as PolynomialDecayWithWarmup_tf

np.random.seed(1337)
x_init = np.random.randn(1,4).astype(np.float32)
W_init = np.random.randn(4,4).astype(np.float32)
m_init = np.random.randn(1,4).astype(np.float32)

class TinyNet:
  def __init__(self):
    self.x = Tensor(x_init.copy(), requires_grad=True)
    self.W = Tensor(W_init.copy(), requires_grad=True)
    self.m = Tensor(m_init.copy())

  def forward(self):
    out = self.x.matmul(self.W).relu()
    out = out.log_softmax(1)
    out = out.mul(self.m).add(self.m).sum()
    return out

class TinyNetTF:
  def __init__(self):
    self.x = tf.Variable(x_init.copy(), trainable=True, name="x")
    self.W = tf.Variable(W_init.copy(), trainable=True, name="W")
    self.m = tf.constant(m_init.copy())

  def forward(self):
    out = tf.matmul(self.x, self.W)
    out = tf.nn.relu(out)
    out = tf.nn.log_softmax(out, axis=1)
    out = tf.multiply(out, self.m) + self.m
    out = tf.reduce_sum(out)
    return out

def step(optim, steps=1, kwargs={}, scheduler=None, schedopts=None):
  net = TinyNet()
  optim = optim([net.x, net.W], **kwargs)
  if scheduler is not None: scheduler = scheduler(optim, **schedopts)
  lrs = []
  for _ in range(steps):
    out = net.forward()
    optim.zero_grad()
    out.backward()
    lrs.append(optim.lr.numpy().item() if isinstance(optim.lr, Tensor) else optim.lr)
    optim.step()
    if scheduler is not None: scheduler.step()
  return lrs, net.x.detach().numpy(), net.W.detach().numpy()

def step_tf(optim, steps=1, kwargs={}, scheduler=None, schedopts=None):
  net = TinyNetTF()
  if scheduler is not None: kwargs['lr'] = scheduler(**schedopts)
  optim = optim(**kwargs)
  lrs = []
  for _ in range(steps):
    with tf.GradientTape() as tape:
      out = net.forward()
    lr_t = optim.learning_rate
    # refer to test/external/mlperf_resnet/lars_optimizer.py:_prepare_local
    if callable(lr_t): lr_t = lr_t(math_ops.cast(optim.iterations, tf.float32))
    lrs.append(lr_t)
    grads = tape.gradient(out, [net.x, net.W])
    optim.apply_gradients(zip(grads, [net.x, net.W]))
    # optim calls scheduler in tf
  return lrs, net.x.numpy(), net.W.numpy()

# skip_list=True -> skip W
def create_tiny_lars(params, lr, skip_list=False): return LARS(params, lr, skip_list=[params[1]] if skip_list else None)
def create_tf_lars(lr, skip_list=False): return LARSOptimizer(lr, skip_list=["W"] if skip_list else None)

def create_tf_polylr(initial_lr, end_lr, train_steps, warmup, power=2):
  assert power == 2
  return PolynomialDecayWithWarmup_tf(1, 1, train_steps,
                                      initial_learning_rate=initial_lr, end_learning_rate=end_lr,
                                      warmup_epochs=warmup)

class ExternalTestOptim(unittest.TestCase):
  def _test_optim(self, tinygrad_optim, tensorflow_optim, steps, opts, atol, rtol, tiny_sched=None, tf_sched=None, schedopts=None):
    for x,y in zip(step(tinygrad_optim, scheduler=tiny_sched, steps=steps, kwargs=opts, schedopts=schedopts),
                   step_tf(tensorflow_optim, scheduler=tf_sched, steps=steps, kwargs=opts, schedopts=schedopts)):
      np.testing.assert_allclose(x, y, atol=atol, rtol=rtol)

  def _test_lamb(self, steps, opts, atol, rtol): self._test_optim(LAMB, tfa.optimizers.LAMB, steps, opts, atol, rtol)
  def _test_lars(self, steps, opts, atol, rtol): self._test_optim(create_tiny_lars, create_tf_lars, steps, opts, atol, rtol)
  def _test_lars_polylr(self, steps, opts, schedopts, atol, rtol):
    self._test_optim(create_tiny_lars, create_tf_lars, steps, opts, atol, rtol,
                     tiny_sched=PolynomialDecayWithWarmup, tf_sched=create_tf_polylr, schedopts=schedopts)

  def test_lamb(self): self._test_lamb(1, {'lr': 0.001}, 1e-5, 0)
  def test_lamb_high_lr(self): self._test_lamb(1, {'lr': 10}, 1e-5, 1e-5)

  def test_multistep_lamb(self): self._test_lamb(10, {'lr': 0.001}, 1e-5, 0)
  def test_multistep_lamb_high_lr(self): self._test_lamb(10, {'lr': 10}, 1e-5, 3e-4)

  def test_lars(self): self._test_lars(1, {'lr': 0.01}, 1e-5, 0)
  def test_lars_high_lr(self): self._test_lars(1, {'lr': 10}, 1e-5, 1e-5)
  def test_multistep_lars(self): self._test_lamb(10, {'lr': 0.001}, 1e-5, 0)
  def test_multistep_lars_high_lr(self): self._test_lamb(10, {'lr': 10}, 1e-5, 3e-4)
  def test_lars_skip_list(self): self._test_lars(1, {'lr': 0.01, 'skip_list': True}, 1e-5, 0)

  def test_lars_polylr(self):
    self._test_lars_polylr(10, {'lr': 1.0}, {
      'initial_lr': 1.0,
      'end_lr': 1e-4,
      'train_steps': 10,
      'warmup': 3
    }, 1e-5, 1e-5)

if __name__ == '__main__':
  unittest.main()
