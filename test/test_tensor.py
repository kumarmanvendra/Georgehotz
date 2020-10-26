import numpy as np
import torch
import unittest
from tinygrad.tensor import Tensor
from tinygrad.optim import Adam
from tinygrad.gradcheck import numerical_jacobian, jacobian, gradcheck

x_init = np.random.randn(1,3).astype(np.float32)
W_init = np.random.randn(3,3).astype(np.float32)
m_init = np.random.randn(1,3).astype(np.float32)

class OldAdam(Adam):
  def __init__(self, params, lr=0.001, b1=0.9, b2=0.999, eps=1e-8):
    super(OldAdam, self).__init__(params, lr, b1, b2, eps)

  def step(self):
    self.t += 1
    for i,t in enumerate(self.params):
      self.m[i] = self.b1 * self.m[i] + (1 - self.b1) * t.grad
      self.v[i] = self.b2 * self.v[i] + (1 - self.b2) * np.square(t.grad)
      mhat = self.m[i] / (1. - self.b1**self.t)
      vhat = self.v[i] / (1. - self.b2**self.t)
      t.data -= self.lr * mhat / (np.sqrt(vhat) + self.eps)


class TestTinygrad(unittest.TestCase):
  def test_backward_pass(self):
    def test_tinygrad():
      x = Tensor(x_init)
      W = Tensor(W_init)
      m = Tensor(m_init)
      out = x.dot(W).relu()
      out = out.logsoftmax()
      out = out.mul(m).add(m).sum()
      out.backward()
      return out.data, x.grad, W.grad

    def test_pytorch():
      x = torch.tensor(x_init, requires_grad=True)
      W = torch.tensor(W_init, requires_grad=True)
      m = torch.tensor(m_init)
      out = x.matmul(W).relu()
      out = torch.nn.functional.log_softmax(out, dim=1)
      out = out.mul(m).add(m).sum()
      out.backward()
      return out.detach().numpy(), x.grad, W.grad

    for x,y in zip(test_tinygrad(), test_pytorch()):
      np.testing.assert_allclose(x, y, atol=1e-5)

  def test_adam_step(self):
    n_step = 10
    def test_new_adam():
      x = Tensor(x_init)
      W = Tensor(W_init)
      optim = Adam([W, x])
      m = Tensor(m_init)
      for _ in range(n_step):
        out = x.dot(W).relu()
        out = out.logsoftmax()
        out = out.mul(m).add(m).sum()
        out.backward()
        optim.step()
      return x.data, W.data

    def test_old_adam():
      x = Tensor(x_init)
      W = Tensor(W_init)
      optim = OldAdam([W, x])
      m = Tensor(m_init)
      for _ in range(n_step):
        out = x.dot(W).relu()
        out = out.logsoftmax()
        out = out.mul(m).add(m).sum()
        out.backward()
        optim.step()
      return x.data, W.data

    for x,y in zip(test_new_adam(), test_old_adam()):
      np.testing.assert_allclose(x, y, atol=1e-5)

  def test_jacobian(self):
    W = np.random.RandomState(1337).random((10, 5))
    x = np.random.RandomState(7331).random((1, 10)) - 0.5

    torch_x = torch.tensor(x, requires_grad=True)
    torch_W = torch.tensor(W, requires_grad=True)
    torch_func = lambda x: torch.nn.functional.log_softmax(x.matmul(torch_W).relu(), dim=1)
    PJ = torch.autograd.functional.jacobian(torch_func, torch_x).squeeze().numpy()

    tiny_x = Tensor(x)
    tiny_W = Tensor(W)
    tiny_func = lambda x: x.dot(tiny_W).relu().logsoftmax()
    J = jacobian(tiny_func, tiny_x)
    NJ = numerical_jacobian(tiny_func, tiny_x)

    np.testing.assert_allclose(PJ, J, atol = 1e-5)
    np.testing.assert_allclose(PJ, NJ, atol = 1e-5)

  def test_gradcheck(self):
    W = np.random.RandomState(1337).random((10, 5))
    x = np.random.RandomState(7331).random((1, 10)) - 0.5

    tiny_x = Tensor(x)
    tiny_W = Tensor(W)
    tiny_func = lambda x: x.dot(tiny_W).relu().logsoftmax()

    self.assertTrue(gradcheck(tiny_func, tiny_x))

    # coarse approx. since a "big" eps and the non-linearities of the model
    self.assertFalse(gradcheck(tiny_func, tiny_x, eps = 0.1))

if __name__ == '__main__':
  unittest.main()
