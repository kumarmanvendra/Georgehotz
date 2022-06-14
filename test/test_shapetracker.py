#!/usr/bin/env python
import unittest
import numpy as np
from tinygrad.helpers import prod
from tinygrad.shapetracker import ShapeTracker

def strides_for_shape(shape):
  strides = [1]
  for d in shape[::-1][:-1]:
    strides = [d*strides[0]] + strides
  return strides

class View:
  def __init__(self, shape, strides, offset=0):
    self.shape = shape
    self.strides = strides
    self.offset = offset
  
  def __getitem__(self, val):
    ret = self.offset
    for d,s in zip(self.shape[::-1], self.strides[::-1]):
      ret += (val%d) * s
      val //= d
    return ret

class StackedViewShapeTracker:
  def __init__(self, *shape):
    self.views = []
    self.views.append(View(shape, strides_for_shape(shape)))

  def __getitem__(self, val):
    for v in self.views[::-1]:
      val = v[val]
    return val

  @property
  def shape(self):
    return tuple(self.views[-1].shape)

  def reshape(self, *new_shape):
    self.views.append(View(new_shape, strides_for_shape(new_shape)))

  def permute(self, *axis):
    assert all([isinstance(x, int) and x >= 0 and x < len(self.shape) for x in axis])
    assert len(set(axis)) == len(axis)
    shape = [self.shape[a] for a in axis]
    strides = strides_for_shape(self.shape)
    strides = [strides[a] for a in axis]
    self.views.append(View(shape, strides))

  def expand(self, *new_shape):
    assert all([isinstance(x, int) for x in new_shape])
    strides = strides_for_shape(self.shape)
    for i,(x,y) in enumerate(zip(self.shape, new_shape)):
      if x != y:
        assert x == 1
        #assert y%x == 0
        strides[i] = 0
    print(self.shape, new_shape, strides)
    self.views.append(View(new_shape, strides))

  def flip(self, *axis):
    assert all([isinstance(x, int) and x >= 0 and x < len(self.shape) for x in axis])
    strides = strides_for_shape(self.shape)
    offset = 0
    for a in axis:
      offset += (self.shape[a]-1) * strides[a]
      strides[a] *= -1
    self.views.append(View(self.shape, strides, offset))

class DumbShapeTracker:
  def __init__(self, *shape):
    self.t = np.arange(prod(shape), dtype=np.uint8).reshape(shape)

  @property
  def shape(self):
    return self.t.shape

  def reshape(self, *new_shape):
    self.t = self.t.reshape(new_shape)
    #print("reshape", self.t.shape, self.t.strides)

  def permute(self, *axis):
    self.t = np.transpose(self.t, axis)
    #print("permute", self.t.shape, self.t.strides)

  def expand(self, *new_shape):
    self.t = np.broadcast_to(self.t, new_shape)
    #print("expand", self.t.shape, self.t.strides)

  def flip(self, *axis):
    self.t = np.flip(self.t, axis)
    #print("flip", self.t.shape, self.t.strides)

  def slice(self, arg):
    # TODO: negative means pad with 0s, not negative indexing like in numpy
    # Use -1 to represent index of 0
    pass

  def __getitem__(self, val):
    return self.t.flatten()[val]

# Tensor.zeros(2, 4).permute(1,0).reshape(2, 4)
# (d1*4 + d0%4), d1=x//4, d0=x%4 = ((x//4)*4) + (x%4)%4

class TestShapeTracker(unittest.TestCase):
  def setUp(self):
    #self.st = ShapeTracker(2,4)
    self.st = StackedViewShapeTracker(2,4)
    self.dt = DumbShapeTracker(2,4)
    self.apply = lambda fxn: [fxn(x) for x in [self.st, self.dt]]

  def tearDown(self):
    x = [self.st[i] for i in range(prod(self.st.shape))]
    y = [self.dt[i] for i in range(prod(self.dt.shape))]
    print(x,y, self.st.shape, self.dt.shape)
    assert self.st.shape == self.dt.shape
    assert x == y

  def test_noop(self):
    pass

  def test_simple_split(self):
    self.test_permute()
    self.apply(lambda x: x.reshape(8))

  def test_reshape(self):
    assert self.st.shape == self.dt.shape
    new_shape = self.st.shape[::-1]
    self.apply(lambda x: x.reshape(*new_shape))

  def test_permute(self):
    self.apply(lambda x: x.permute(1,0))

  # should this work?
  # NOTE: you can write this as a reshape and expand
  """
  def test_simple_expand(self):
    new_shape = [self.st.shape[0]*2, self.st.shape[1]]
    fxn = lambda x: x.expand(*new_shape)
    [fxn(x) for x in [self.st, self.dt]]
  """

  def test_reshape_with_1(self):
    assert self.st.shape == self.dt.shape
    new_shape = [self.st.shape[0], 1, self.st.shape[1]]
    self.apply(lambda x: x.reshape(*new_shape))

  def test_expand(self):
    self.test_reshape_with_1()
    new_shape = list(self.st.shape)
    new_shape[1] = 2
    self.apply(lambda x: x.expand(*new_shape))

  def test_flip_0(self):
    self.apply(lambda x: x.flip(0))

  def test_flip_1(self):
    self.apply(lambda x: x.flip(1))

  def test_flip_01(self):
    self.apply(lambda x: x.flip(0,1))

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
