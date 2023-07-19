# sorted in order of increasing complexity
from typing import List
from tinygrad.helpers import dedup, getenv, dtypes
from tinygrad.tensor import Tensor

global_steps = 0

def log_grad(i,g):
  import numpy as np
  global global_steps
  if global_steps == 0:
    with open('/tmp/grad_logs.csv', 'w') as f:
      f.write('step,param_dtype,param_number,param_shape,grad_min,grad_10,grad_25,grad_50,grad_75,grad_90,grad_max\n')
  with open('/tmp/grad_logs.csv', 'a') as f:
    grad = g.numpy()
    row = f"{str(global_steps)},{grad.dtype},{i},{'-'.join(map(str,grad.shape))},{grad.min()},{np.percentile(grad, 10)},{np.percentile(grad, 25)},{np.percentile(grad, 50)},{np.percentile(grad, 75)},{np.percentile(grad, 90)},{grad.max()}"
    f.write(f"{row}\n")  

class Optimizer:
  def __init__(self, params: List[Tensor], lr: float):
    self.init_params(params)
    self.lr = Tensor([lr], requires_grad=False, dtype=dtypes.float16 if getenv('HALF') else None).contiguous()

  def zero_grad(self):
    for param in self.params: param.grad = None

  def init_params(self, params):
    # if it's None, but being put into an optimizer, set it to True
    for x in params:
      if x.requires_grad is None: x.requires_grad = True
    self.params: List[Tensor] = dedup([x for x in params if x.requires_grad])
    self.buffers: List[Tensor] = dedup([x for x in params if not x.requires_grad])   # buffers are still realized

  def realize(self, extra=None):
    # TODO: corealize
    # NOTE: in extra is too late for most of the params due to issues with assign
    for p in extra + self.params + self.buffers if extra is not None else self.params + self.buffers:
      p.realize()

class SGD(Optimizer):
  def __init__(self, params: List[Tensor], lr=0.001, momentum=0, weight_decay=0.0, nesterov=False):
    super().__init__(params, lr)
    self.momentum, self.wd, self.nesterov = momentum, weight_decay, nesterov
    self.b = [Tensor.zeros(*t.shape, device=t.device, requires_grad=False, dtype=t.dtype) for t in self.params] if self.momentum else []

  # https://pytorch.org/docs/stable/generated/torch.optim.SGD.html
  def step(self) -> None:
    global global_steps
    for i, t in enumerate(self.params):
      assert t.grad is not None
      if getenv('LOG_GRADS', 0) == 1: log_grad(i, t.grad)
      g = t.grad.realize() + self.wd * t.detach()
      if self.momentum:
        self.b[i].assign(self.momentum * self.b[i] + g).realize()  # NOTE: self.b[i] is zero on the first run, no if required
        g = (g + self.momentum * self.b[i]) if self.nesterov else self.b[i]
      g = g.cast(t.dtype) if g.dtype != t.dtype else g
      t.assign(t.detach() - g * self.lr)
    self.realize(self.b)
    global_steps += 1

# LAMB is essentially just the trust ratio part of LARS applied to Adam/W so if we just set the trust ratio to 1.0 its just Adam/W.
def AdamW(params: List[Tensor], lr=0.001, b1=0.9, b2=0.999, eps=1e-8, wd=0.01): return LAMB(params, lr, b1, b2, eps, wd, adam=True)
def Adam(params: List[Tensor], lr=0.001, b1=0.9, b2=0.999, eps=1e-8): return LAMB(params, lr, b1, b2, eps, 0.0, adam=True)

class LAMB(Optimizer):
  def __init__(self, params: List[Tensor], lr=0.001, b1=0.9, b2=0.999, eps=1e-6, wd=0.0, adam=False):
    super().__init__(params, lr)
    self.b1, self.b2, self.eps, self.wd, self.adam, self.t = b1, b2, eps, wd, adam, Tensor([0], requires_grad=False).realize()
    self.m = [Tensor.zeros(*t.shape, device=t.device, requires_grad=False) for t in self.params]
    self.v = [Tensor.zeros(*t.shape, device=t.device, requires_grad=False) for t in self.params]

  def step(self) -> None:
    self.t.assign(self.t + 1).realize()
    for i, t in enumerate(self.params):
      assert t.grad is not None
      g = t.grad.realize()
      self.m[i].assign(self.b1 * self.m[i] + (1.0 - self.b1) * g).realize()
      self.v[i].assign(self.b2 * self.v[i] + (1.0 - self.b2) * (g * g)).realize()
      m_hat = self.m[i] / (1.0 - self.b1**self.t)
      v_hat = self.v[i] / (1.0 - self.b2**self.t)
      up = (m_hat / (v_hat.sqrt() + self.eps)) + self.wd * t.detach()
      if not self.adam:
        r1 = t.detach().square().sum().sqrt()
        r2 = up.square().sum().sqrt()
        r = Tensor.where(r1 > 0, Tensor.where(r2 > 0, r1 / r2, 1.0), 1.0)
      else:
        r = 1.0
      t.assign(t.detach() - self.lr * r * up)
    self.realize([self.t] + self.m + self.v)
