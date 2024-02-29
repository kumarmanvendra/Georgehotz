from typing import List

from tinygrad import Tensor
from tinygrad.nn.optim import Optimizer

# https://github.com/mlcommons/training/blob/master/image_classification/tensorflow2/lars_optimizer.py
class LARS(Optimizer):
  def __init__(self, params: List[Tensor], lr, momentum=0.9, weight_decay=1e-4, eta=0.001, eps=0.0, skip_list=None, nesterov=False, track_gnorm=False):
    super().__init__(params, lr)
    self.momentum, self.weight_decay, self.eta, self.eps, self.nesterov, self.track_gnorm = momentum, weight_decay, eta, eps, nesterov, track_gnorm
    self.b = [Tensor.zeros(*t.shape, device=t.device, requires_grad=False) for t in self.params]
    self.skip_list = skip_list or set()

  def step(self):
    gnorm = 0
    for i, t in enumerate(self.params):
      assert t.grad is not None
      t.grad.realize()
      t_ = t.detach()
      g_norm = (t.grad * t.grad).sum().sqrt()
      if self.track_gnorm: gnorm = gnorm + g_norm.to("HIP")
      if t not in self.skip_list:
        w_norm = (t_ * t_).sum().sqrt()
        trust_ratio = (w_norm > 0).where(
          (g_norm > 0).where(
            self.eta * w_norm / (g_norm + self.weight_decay * w_norm + self.eps), 1.0
          ), 1.0
        )
        scaled_lr = self.lr * trust_ratio
        g = t.grad + self.weight_decay * t.detach()
      else:
        scaled_lr = self.lr
        g = t.grad

      g = g * scaled_lr
      if self.momentum:
        self.b[i].assign(self.momentum * self.b[i] + g)  # NOTE: self.b[i] is zero on the first run, no if required
        g = (g + self.momentum * self.b[i]) if self.nesterov else self.b[i]
      t.assign(t.detach() - g)
    self.realize(self.b)
    if self.track_gnorm: return gnorm.realize()
