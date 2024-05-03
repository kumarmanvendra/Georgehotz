import unittest
from tinygrad import Device, dtypes, Tensor
from tinygrad.buffer import Buffer

class TestSubBuffer(unittest.TestCase):
  def setUp(self):
    self.buf = Buffer(Device.DEFAULT, 10, dtypes.uint8).ensure_allocated()
    self.buf.copyin(memoryview(bytearray(range(10))))

  def test_subbuffer(self):
    vbuf = self.buf.view(2, dtypes.uint8, offset=3).ensure_allocated()
    tst = vbuf.as_buffer().tolist()
    assert tst == [3, 4]

  def test_subbuffer_cast(self):
    # NOTE: bitcast depends on endianness
    vbuf = self.buf.view(2, dtypes.uint16, offset=3).ensure_allocated()
    tst = vbuf.as_buffer().cast("H").tolist()
    assert tst == [3|(4<<8), 5|(6<<8)]

  def test_subbuffer_double(self):
    vbuf = self.buf.view(4, dtypes.uint8, offset=3).ensure_allocated()
    vvbuf = vbuf.view(2, dtypes.uint8, offset=1).ensure_allocated()
    tst = vvbuf.as_buffer().tolist()
    assert tst == [4, 5]

  def test_subbuffer_len(self):
    vbuf = self.buf.view(5, dtypes.uint8, 2).ensure_allocated()
    mv = vbuf.as_buffer()
    assert len(mv) == 5
    mv = vbuf.as_buffer(allow_zero_copy=True)
    assert len(mv) == 5

  def test_subbuffer_used(self):
    t = Tensor.arange(0, 10, dtype=dtypes.uint8).realize()
    # TODO: why does it needs contiguous
    vt = t[2:4].contiguous().realize()
    out = (vt + 100).tolist()
    assert out == [102, 103]

if __name__ == '__main__':
  unittest.main()
