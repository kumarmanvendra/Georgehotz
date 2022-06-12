import unittest
from tinygrad.tensor import Tensor

class TestConv(unittest.TestCase):
  def test_simple(self):
    x = Tensor.ones(1,12,128,256)
    w = Tensor.ones(32,12,3,3)
    ret = x.conv2d(w, stride=(2,2), padding=(1,1)).numpy()
    print(ret)
    # it's not 108 around the padding
    assert (ret[:, :, 1:-1, 1:-1] == 108).all()
    assert ret[0,0,0,0] == 48
    assert ret[0,0,0,1] == 72

  def test_first_three(self):
    x = Tensor.ones(1,12,128,256)

    w = Tensor.ones(32,12,3,3)
    x = x.conv2d(w, stride=(2,2), padding=(1,1))

    w = Tensor.ones(32,1,3,3)
    x = x.conv2d(w, padding=(1,1), groups=32)

    w = Tensor.ones(16,32,1,1)
    x = x.conv2d(w)

    x = x.numpy()
    print(x.shape)

if __name__ == '__main__':
  unittest.main()