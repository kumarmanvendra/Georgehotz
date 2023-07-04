#!/usr/bin/env python
import io, unittest
import torch
import numpy as np
from tinygrad.helpers import getenv 
from extra.utils import fetch, temp
from tinygrad.state import torch_load
from PIL import Image

@unittest.skipIf(getenv("CI", "") != "", "no internet tests in CI")
class TestFetch(unittest.TestCase):
  def test_fetch_bad_http(self):
    self.assertRaises(AssertionError, fetch, 'http://httpstat.us/500')
    self.assertRaises(AssertionError, fetch, 'http://httpstat.us/404')
    self.assertRaises(AssertionError, fetch, 'http://httpstat.us/400')

  def test_fetch_small(self):
    assert(len(fetch('https://google.com'))>0)

  def test_fetch_img(self):
    img = fetch("https://media.istockphoto.com/photos/hen-picture-id831791190")
    pimg = Image.open(io.BytesIO(img))
    assert pimg.size == (705, 1024)

class TestUtils(unittest.TestCase):
  def test_fake_torch_load_zipped(self): self._test_fake_torch_load_zipped()
  def test_fake_torch_load_zipped_float16(self): self._test_fake_torch_load_zipped(isfloat16=True)
  def _test_fake_torch_load_zipped(self, isfloat16=False):
    class LayerWithOffset(torch.nn.Module):
      def __init__(self):
        super().__init__()
        d = torch.randn(16)
        self.param1 = torch.nn.Parameter(
          d.as_strided([2, 2], [1, 2], storage_offset=5)
        )
        self.param2 = torch.nn.Parameter(
          d.as_strided([2, 2], [1, 2], storage_offset=4)
        )

    model = torch.nn.Sequential(
      torch.nn.Linear(4, 8),
      torch.nn.Linear(8, 3),
      LayerWithOffset()
    )
    if isfloat16: model = model.half()

    path = temp(f"test_load_{isfloat16}.pt") 
    torch.save(model.state_dict(), path)
    model2 = torch_load(path)

    for name, a in model.state_dict().items():
      b = model2[name]
      a, b = a.numpy(), b.numpy()
      assert a.shape == b.shape
      assert a.dtype == b.dtype
      assert np.array_equal(a, b)
if __name__ == '__main__':
  unittest.main()