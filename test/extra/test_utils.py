#!/usr/bin/env python
import io
import unittest
from tinygrad.helpers import getenv, dtypes
from extra.utils import fetch, fake_torch_load_zipped
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
  def test_fake_torch_load_zipped(self):
    import torch
    import numpy as np
    import tempfile
    class LayerWithOffset(torch.nn.Module):
      def __init__(self):
        super(LayerWithOffset, self).__init__()
        d = torch.randn(16)
        self.param1 = torch.nn.Parameter(
          d.as_strided([2, 2], [2, 3], storage_offset=5)
        )
        self.param2 = torch.nn.Parameter(
          d.as_strided([2, 2], [2, 3], storage_offset=4)
        )

    for isfloat16 in [True, False]:
      for loaded_is_float16, load_dtype in [(False, dtypes.float32), (True, dtypes.float16), (False, None)]:
        if isfloat16 == loaded_is_float16:
          print(f"Saving with float32 and loading with isfloat16={isfloat16}")
          print(f"Loading with dtype={load_dtype}")
          model = torch.nn.Sequential(
            torch.nn.Linear(4, 8),
            torch.nn.Linear(8, 3),
            LayerWithOffset()
          )

          with tempfile.TemporaryDirectory() as tmpdirname:
            path = tmpdirname + '/testloadmodel.pth'
            torch.save(model.state_dict(), path)
            model2 = fake_torch_load_zipped(path, load_dtype=load_dtype)

          if isfloat16: model = model.half()
          for name, a in model.state_dict().items():
            b = model2[name]
            a, b = a.numpy(), b.numpy()
            assert a.shape == b.shape
            assert a.dtype == b.dtype
            assert np.array_equal(a, b)

if __name__ == '__main__':
  unittest.main()