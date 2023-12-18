import unittest
from tinygrad.shape.shapetracker import ShapeTracker

class TestShapeTrackerAdd(unittest.TestCase):
  def test_simple_add_reshape(self):
    a = ShapeTracker.from_shape((10, 10))
    a = a.reshape((100,))
    b = ShapeTracker.from_shape((100,))
    assert a+b == b

  def test_simple_add_permute(self):
    a = ShapeTracker.from_shape((10, 10))
    a = a.permute((1,0))
    b = ShapeTracker.from_shape((10, 10))
    b = b.permute((1,0))
    assert a+b == ShapeTracker.from_shape((10, 10))

class TestShapeTrackerInvert(unittest.TestCase):
  def test_invert_reshape(self):
    a = ShapeTracker.from_shape((10, 10))
    x = a.reshape((5, 20))
    ap = ShapeTracker.from_shape(x.shape) + x.invert(a.shape)
    assert ap == a, f"{ap} != {a}"

  def test_invert_permute(self):
    a = ShapeTracker.from_shape((5, 20))
    x = a.permute((1,0))
    ap = ShapeTracker.from_shape(x.shape) + x.invert(a.shape)
    assert ap == a, f"{ap} != {a}"

  def test_invert_permute_3(self):
    a = ShapeTracker.from_shape((8, 4, 5))
    x = a.permute((1,2,0))
    ap = ShapeTracker.from_shape(x.shape) + x.invert(a.shape)
    assert ap == a, f"{ap} != {a}"

  def test_cant_permute_expand(self):
    a = ShapeTracker.from_shape((10, 1))
    x = a.expand((10,10))
    assert x.invert(a.shape) is None

if __name__ == '__main__':
  unittest.main()

