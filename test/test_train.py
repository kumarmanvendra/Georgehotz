import os
import unittest
import time
import tinygrad.optim as optim
import numpy as np
from tinygrad.tensor import Tensor
from extra.training import train
from extra.utils import get_parameters
from models.efficientnet import EfficientNet
from models.transformer import Transformer
from models.resnet import ResNet18, ResNet34, ResNet50

BS = int(os.getenv("BS", "4"))

def train_one_step(model,X,Y):
  params = get_parameters(model)
  pcount = 0
  for p in params:
    pcount += np.prod(p.shape)
  optimizer = optim.Adam(params, lr=0.001)
  print("stepping %r with %.1fM params bs %d" % (type(model), pcount/1e6, BS))
  st = time.time()
  train(model, X, Y, optimizer, steps=1, BS=BS)
  et = time.time()-st
  print("done in %.2f ms" % (et*1000.))

class TestTrain(unittest.TestCase):
  def test_efficientnet(self):
    model = EfficientNet(0)
    X = np.zeros((BS,3,224,224), dtype=np.float32)
    Y = np.zeros((BS), dtype=np.int32)
    train_one_step(model,X,Y)

  def test_transformer(self):
    # this should be small GPT-2, but the param count is wrong
    model = Transformer(syms=10, maxlen=6, layers=12, embed_dim=768, num_heads=12)
    X = np.zeros((BS,6), dtype=np.float32)
    Y = np.zeros((BS,6), dtype=np.int32)
    train_one_step(model,X,Y)

  def test_resnet(self):
    X = np.zeros((BS, 3, 224, 224), dtype=np.float32)
    Y = np.zeros((BS), dtype=np.int32)
    for resnet_v in [ResNet18, ResNet34, ResNet50]:
      model = resnet_v(num_classes=1000, pretrained=True)
      train_one_step(model, X, Y)

  def test_bert(self):
    # TODO: write this
    pass

if __name__ == '__main__':
  unittest.main()
