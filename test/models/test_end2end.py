import torch
from torch import nn
import unittest
import numpy as np
from tinygrad.nn import optim, Linear, Conv2d, BatchNorm2d
from tinygrad.tensor import Tensor
from datasets import fetch_mnist

def compare_tiny_torch(model, model_torch, X, Y):
  Tensor.training = True
  model_torch.train()
  model_state_dict = optim.get_state_dict(model)
  for k,v in model_torch.named_parameters():
    print(f"initting {k} from torch")
    model_state_dict[k].assign(Tensor(v.detach().numpy())).realize()

  optimizer = optim.SGD(optim.get_parameters(model), lr=0.01)
  optimizer_torch = torch.optim.SGD(model_torch.parameters(), lr=0.01)

  Xt = torch.Tensor(X.numpy())
  np.testing.assert_allclose(X.numpy(), Xt.detach().numpy())

  out = model(X)
  loss = (out * Y).mean()
  print(loss.realize().numpy()[0])

  out_torch = model_torch(torch.Tensor(X.numpy()))
  loss_torch = (out_torch * torch.Tensor(Y.numpy())).mean()
  print(loss_torch.detach().numpy())

  # assert losses match
  np.testing.assert_allclose(loss.realize().numpy()[0], loss_torch.detach().numpy(), atol=1e-4)

  # zero and backward
  optimizer.zero_grad()
  loss.backward()
  optimizer_torch.zero_grad()
  loss_torch.backward()

  for k,v in list(model_torch.named_parameters())[::-1]:
    g = model_state_dict[k].grad.numpy()
    gt = v.grad.detach().numpy()
    print("testing grads", k)
    np.testing.assert_allclose(g, gt, atol=1e-3)

  # take the steps
  optimizer.step()
  optimizer_torch.step()

  # assert weights match (they don't!)
  for k,v in model_torch.named_parameters():
    print("testing weight", k)
    np.testing.assert_allclose(model_state_dict[k].numpy(), v.detach().numpy(), atol=1e-3)

def get_mnist_data():
  X_train, Y_train, X_test, Y_test = fetch_mnist()
  BS = 32
  num_classes = 10
  X = Tensor(X_test[0:BS].astype(np.float32))
  Y = np.zeros((BS, num_classes), np.float32)
  Y[range(BS),Y_test[0:BS]] = -1.0*num_classes
  return X, Tensor(Y)

class TestEnd2End(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.X, cls.Y = get_mnist_data()

  def test_linear_mnist(self):
    class LinTiny:
      def __init__(self):
        self.l1 = Linear(784, 128)
        self.l2 = Linear(128, 10)
      def __call__(self, x):
        return self.l2(self.l1(x).relu()).log_softmax()
    class LinTorch(nn.Module):
      def __init__(self):
        super().__init__()
        self.l1 = nn.Linear(784, 128)
        self.l2 = nn.Linear(128, 10)
      def forward(self, x):
        return self.l2(self.l1(x).relu()).log_softmax(-1)
    compare_tiny_torch(LinTiny(), LinTorch(), self.X, self.Y)

  def test_conv_mnist(self):
    class LinTiny:
      def __init__(self, has_batchnorm=False):
        self.c1 = Conv2d(1, 8, 3, stride=2)
        self.c2 = Conv2d(8, 16, 3, stride=2)
        self.l1 = Linear(16*6*6, 10)
        if has_batchnorm:
          self.bn1, self.bn2 = BatchNorm2d(8), BatchNorm2d(16)
        else:
          self.bn1, self.bn2 = lambda x: x, lambda x: x
      def __call__(self, x):
        return self.l1(self.bn2(self.c2(self.bn1(self.c1(x)).relu())).relu().reshape(x.shape[0], -1)).log_softmax(-1)
    class LinTorch(nn.Module):
      def __init__(self, has_batchnorm=False):
        super().__init__()
        self.c1 = nn.Conv2d(1, 8, 3, stride=2)
        self.c2 = nn.Conv2d(8, 16, 3, stride=2)
        self.l1 = nn.Linear(16*6*6, 10)
        if has_batchnorm:
          self.bn1, self.bn2 = nn.BatchNorm2d(8), nn.BatchNorm2d(16)
        else:
          self.bn1, self.bn2 = lambda x: x, lambda x: x
      def forward(self, x):
        return self.l1(self.bn2(self.c2(self.bn1(self.c1(x)).relu())).relu().reshape(x.shape[0], -1)).log_softmax(-1)
    for has_batchnorm in [False, True]:
      with self.subTest(has_batchnorm=has_batchnorm):
        compare_tiny_torch(LinTiny(has_batchnorm), LinTorch(has_batchnorm), self.X.reshape((-1, 1, 28, 28)), self.Y)

if __name__ == "__main__":
  unittest.main()
