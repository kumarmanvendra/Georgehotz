#!/usr/bin/env python
import unittest
import numpy as np
from tinygrad.tensor import Tensor, Device
from tinygrad.nn import Linear, optim, Conv2d, BatchNorm2d
from tinygrad.helpers import getenv
from extra.training import train, evaluate
from datasets import fetch_mnist

# load the mnist dataset
X_train, Y_train, X_test, Y_test = fetch_mnist()

# create a model
class TinyBobNet:
  def __init__(self):
    self.l1 = Linear(784, 128, bias=False)
    self.l2 = Linear(128, 10, bias=False)

  def parameters(self):
    return optim.get_parameters(self)

  def forward(self, x):
    x = self.l1(x).relu()
    return self.l2(x).log_softmax()

# create a model with a conv layer
class TinyConvNet:
  def __init__(self, has_batchnorm=False):
    # https://keras.io/examples/vision/mnist_convnet/
    conv = 3
    #inter_chan, out_chan = 32, 64
    inter_chan, out_chan = 8, 16   # for speed
    self.c1 = Conv2d(1, inter_chan, conv, bias=False)
    self.c2 = Conv2d(inter_chan, out_chan, conv, bias=False)
    self.l1 = Linear(out_chan * 5 * 5, 10, bias=False)
    if has_batchnorm:
      self.bn1 = BatchNorm2d(inter_chan)
      self.bn2 = BatchNorm2d(out_chan)
    else:
      self.bn1, self.bn2 = lambda x: x, lambda x: x

  def parameters(self):
    return optim.get_parameters(self)

  def forward(self, x:Tensor):
    x = x.reshape(shape=(-1, 1, 28, 28)) # hacks
    x = self.bn1(self.c1(x)).relu().max_pool2d()
    x = self.bn2(self.c2(x)).relu().max_pool2d()
    x = x.reshape(shape=[x.shape[0], -1])
    return self.l1(x).log_softmax()

class TestMNIST(unittest.TestCase):
  def test_sgd_onestep(self):
    np.random.seed(1337)
    model = TinyBobNet()
    optimizer = optim.SGD(model.parameters(), lr=0.001)
    train(model, X_train, Y_train, optimizer, BS=69, steps=1)
    for p in model.parameters(): p.realize()

  def test_sgd_threestep(self):
    np.random.seed(1337)
    model = TinyBobNet()
    optimizer = optim.SGD(model.parameters(), lr=0.001)
    train(model, X_train, Y_train, optimizer, BS=69, steps=3)

  def test_sgd_sixstep(self):
    np.random.seed(1337)
    model = TinyBobNet()
    optimizer = optim.SGD(model.parameters(), lr=0.001)
    train(model, X_train, Y_train, optimizer, BS=69, steps=6, noloss=True)

  def test_adam_onestep(self):
    np.random.seed(1337)
    model = TinyBobNet()
    optimizer = optim.Adam(model.parameters(), lr=0.001)
    train(model, X_train, Y_train, optimizer, BS=69, steps=1)
    for p in model.parameters(): p.realize()

  def test_adam_threestep(self):
    np.random.seed(1337)
    model = TinyBobNet()
    optimizer = optim.Adam(model.parameters(), lr=0.001)
    train(model, X_train, Y_train, optimizer, BS=69, steps=3)

  def test_conv_onestep(self):
    np.random.seed(1337)
    model = TinyConvNet()
    optimizer = optim.SGD(model.parameters(), lr=0.001)
    train(model, X_train, Y_train, optimizer, BS=69, steps=1, noloss=True)
    for p in model.parameters(): p.realize()

  def test_conv(self):
    np.random.seed(1337)
    model = TinyConvNet()
    optimizer = optim.Adam(model.parameters(), lr=0.001)
    train(model, X_train, Y_train, optimizer, steps=100)
    assert evaluate(model, X_test, Y_test) > 0.94   # torch gets 0.9415 sometimes

  def test_conv_with_bn(self):
    np.random.seed(1337)
    model = TinyConvNet(has_batchnorm=True)
    optimizer = optim.AdamW(model.parameters(), lr=0.003)
    train(model, X_train, Y_train, optimizer, steps=200)
    assert evaluate(model, X_test, Y_test) > 0.94

  def test_sgd(self):
    np.random.seed(1337)
    model = TinyBobNet()
    optimizer = optim.SGD(model.parameters(), lr=0.001)
    train(model, X_train, Y_train, optimizer, steps=600)
    assert evaluate(model, X_test, Y_test) > 0.94   # CPU gets 0.9494 sometimes

if __name__ == '__main__':
  unittest.main()
