#!/usr/bin/env python
import unittest
import numpy as np
from tinygrad.helpers import prod
from tinygrad.shapetracker import ShapeTracker

def flatten(obj):
  x = []
  # lazy
  if len(obj.shape) == 3:
    for i in range(obj.shape[0]):
      for j in range(obj.shape[1]):
        for k in range(obj.shape[2]):
          x.append(obj[i, j, k])
  elif len(obj.shape) == 2:
    for i in range(obj.shape[0]):
      for j in range(obj.shape[1]):
        x.append(obj[i, j])
  else:
    for i in range(obj.shape[0]):
      x.append(obj[i])
  return x

class DumbShapeTracker:
  def __init__(self, *shape):
    self.t = np.arange(prod(shape)).reshape(shape)

  @property
  def shape(self):
    return self.t.shape

  def reshape(self, *new_shape):
    self.t = self.t.reshape(new_shape)

  def permute(self, *axis):
    self.t = np.transpose(self.t, axis)

  def expand(self, *new_shape):
    self.t = np.broadcast_to(self.t, new_shape)

  def flip(self, *axis):
    self.t = np.flip(self.t, axis)

  def slice(self, arg):
    # TODO: negative means pad with 0s, not negative indexing like in numpy
    # Use -1 to represent index of 0
    pass

  def __getitem__(self, val):
    return self.t[val]

class TestShapeTracker(unittest.TestCase):
  def setUp(self):
    self.buf = np.arange(2*4).reshape(2, 4)
    self.st = DumbShapeTracker(2,4)

  def tearDown(self):
    x = flatten(self.buf)
    y = flatten(self.st)
    print(x,y)
    assert self.buf.shape == self.st.shape
    assert x == y

  def test_noop(self):
    pass

  def test_simple_split(self):
    self.test_permute()
    self.buf = self.buf.reshape(8)
    self.st.reshape(8)

  def test_reshape(self):
    assert self.buf.shape == self.st.shape
    new_shape = self.buf.shape[::-1]
    self.buf = self.buf.reshape(*new_shape)
    self.st.reshape(*new_shape)

  def test_permute(self):
    self.buf = self.buf.transpose(1,0)
    self.st.permute(1,0)

  def test_expand(self):
    assert self.buf.shape == self.st.shape
    new_shape = [self.buf.shape[0], 1, self.buf.shape[1]]
    self.buf = self.buf.reshape(*new_shape)
    self.st.reshape(*new_shape)

    new_shape[1] = 2
    self.buf = np.broadcast_to(self.buf, new_shape)
    self.st.expand(*new_shape)

  def test_reshape_then_permute(self):
    self.test_reshape()
    self.test_permute()

  def test_reshape_then_expand(self):
    self.test_reshape()
    self.test_expand()

  def test_permute_then_reshape(self):
    self.test_permute()
    self.test_reshape()

  def test_expand_then_reshape(self):
    self.test_expand()
    self.test_reshape()

if __name__ == '__main__':
  unittest.main()
