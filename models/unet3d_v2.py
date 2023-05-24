from pathlib import Path
import torch
from tinygrad import nn
from tinygrad.tensor import Tensor
from extra.utils import download_file, get_child

class ConvTranspose:
  ...

class InputBlock:
  def __init__(self, c0, c1):
    self.conv1 = [nn.Conv2d(c0, c1, kernel_size=(3, 3, 3), padding=(1, 1, 1, 1, 1, 1), bias=False), nn.GroupNorm(16, c1), Tensor.relu]
    self.conv2 = [nn.Conv2d(c1, c1, kernel_size=(3, 3, 3), padding=(1, 1, 1, 1, 1, 1), bias=False), nn.GroupNorm(16, c1), Tensor.relu]

  def __call__(self, x):
    return x.sequential(self.conv1).sequential(self.conv2)

class DownsampleBlock:
  def __init__(self, c0, c1):
    self.conv1 = [nn.Conv2d(c0, c1, kernel_size=(3, 3, 3), stride=2, padding=(1, 1, 1, 1, 1, 1), bias=False), nn.GroupNorm(16, c1), Tensor.relu]
    self.conv2 = [nn.Conv2d(c1, c1, kernel_size=(3, 3, 3), padding=(1, 1, 1, 1, 1, 1), bias=False), nn.GroupNorm(16, c1), Tensor.relu]

  def __call__(self, x):
    return x.sequential(self.conv1).sequential(self.conv2)

class UpsampleBlock:
  def __init__(self, c0, c1):
    # TODO: this is also cheating!
    self.upsample_conv = [torch.nn.ConvTranspose3d(c0, c1, kernel_size=2, stride=2)]
    self.conv1 = [nn.Conv2d(c1 * 2, c1, kernel_size=(3, 3, 3), padding=(1, 1, 1, 1, 1, 1), bias=False), nn.GroupNorm(16, c1), Tensor.relu]
    self.conv2 = [nn.Conv2d(c1, c1, kernel_size=(3, 3, 3), padding=(1, 1, 1, 1, 1, 1), bias=False), nn.GroupNorm(16, c1), Tensor.relu]

  def __call__(self, x, skip):
    x = Tensor(self.upsample_conv[0](torch.tensor(x.numpy())).detach().numpy())
    x = Tensor.cat(x, skip, dim=1)
    return x.sequential(self.conv1).sequential(self.conv2)

class UNet3D:
  def __init__(self, in_channels=1, n_class=3):
    filters = [32, 64, 128, 256, 320]
    inp, out = filters[:-1], filters[1:]
    self.input_block = InputBlock(in_channels, filters[0])
    self.downsample = [DownsampleBlock(i, o) for i, o in zip(inp, out)]
    self.bottleneck = DownsampleBlock(filters[-1], filters[-1])
    self.upsample = [UpsampleBlock(filters[-1], filters[-1])] + [UpsampleBlock(i, o) for i, o in zip(out[::-1], inp[::-1])] 
    self.output = {"conv": nn.Conv2d(filters[0], n_class, kernel_size=(1, 1, 1))}

  def __call__(self, x):
    x = self.input_block(x)
    outputs = [x]
    for downsample in self.downsample:
      x = downsample(x)
      outputs.append(x)
    x = self.bottleneck(x)
    for upsample, skip in zip(self.upsample, outputs[::-1]):
      x = upsample(x, skip)
    x = self.output["conv"](x)
    return x
    
  def load_from_pretrained(self):
    fn = Path(__file__).parent.parent / "weights" / "unet-3d.ckpt"
    download_file("https://zenodo.org/record/5597155/files/3dunet_kits19_pytorch.ptc?download=1", fn)
    # TODO: replace with fake_torch_load
    state_dict = torch.jit.load(fn, map_location=torch.device("cpu")).state_dict()
    for k, v in state_dict.items():
      obj = get_child(self, k)
      assert obj.shape == v.shape, (k, obj.shape, v.shape)
      if hasattr(obj, "assign"):
        obj.assign(v.numpy())
      else:
        obj[0].data = v

if __name__ == "__main__":
  mdl = UNet3D()
  mdl.load_from_pretrained()
