#!/usr/bin/env python3
# tinygrad implementation of https://github.com/tysam-code/hlb-CIFAR10/blob/main/main.py
# https://myrtle.ai/learn/how-to-train-your-resnet-8-bag-of-tricks/
# https://siboehm.com/articles/22/CUDA-MMM
# TODO: gelu is causing nans!
import time
import numpy as np
from datasets import fetch_cifar
from tinygrad import nn
from tinygrad.nn import optim
from tinygrad.tensor import Tensor
from tinygrad.helpers import getenv
from tinygrad.ops import GlobalCounters
from extra.lr_scheduler import OneCycleLR

def set_seed(seed):
  Tensor.manual_seed(getenv('SEED', seed)) # Deterministic
  np.random.seed(getenv('SEED', seed))

num_classes = 10

# TODO: eval won't work with track_running_stats=False
class ConvGroup:
  def __init__(self, channels_in, channels_out, short, se=True):
    self.short, self.se = short, se and not short
    self.conv = [nn.Conv2d(channels_in if i == 0 else channels_out, channels_out, kernel_size=3, padding=1, bias=False) for i in range(1 if short else 3)]
    self.norm = [nn.BatchNorm2d(channels_out, track_running_stats=False, eps=1e-12, momentum=0.8) for _ in range(1 if short else 3)]
    if self.se: self.se1, self.se2 = nn.Linear(channels_out, channels_out//16), nn.Linear(channels_out//16, channels_out)

  def __call__(self, x):
    x = self.conv[0](x).max_pool2d(2)
    x = self.norm[0](x).relu()
    if self.short: return x
    residual = x
    mult = self.se2(self.se1(residual.mean((2,3))).relu()).sigmoid().reshape(x.shape[0], x.shape[1], 1, 1) if self.se else 1.0
    x = self.norm[1](self.conv[1](x)).relu()
    x = self.norm[2](self.conv[2](x) * mult).relu()
    return x + residual

class SpeedyResNet:
  def __init__(self):
    # TODO: add whitening
    self.net = [
      nn.Conv2d(3, 64, kernel_size=1),
      nn.BatchNorm2d(64, track_running_stats=False, eps=1e-12, momentum=0.8),
      lambda x: x.gelu(), # may not converge with high batch size (512 didnt work)
      ConvGroup(64, 128, short=False),
      ConvGroup(128, 256, short=True),
      ConvGroup(256, 512, short=False),
      lambda x: x.max((2,3)),
      nn.Linear(512, num_classes, bias=False)
    ]

  # note, pytorch just uses https://pytorch.org/docs/stable/generated/torch.nn.CrossEntropyLoss.html instead of log_softmax
  def __call__(self, x, training=True): 
    if not training and getenv('TTA', 0)==1: return ((x.sequential(self.net) * 0.5) + (x[..., ::-1].sequential(self.net) * 0.5)).log_softmax()
    return x.sequential(self.net).log_softmax()

def fetch_batches(X_train, Y_train, BS, is_train=False, flip_chance=0.5):
  if not is_train:
    ind = np.arange(Y_train.shape[0])
    np.random.shuffle(ind)
    X_train, Y_train = X_train[ind, ...], Y_train[ind, ...]
  while True:
    for batch_start in range(0, Y_train.shape[0], BS):
      batch_end = min(batch_start+BS, Y_train.shape[0])
      X = Tensor(X_train[batch_end-BS:batch_end]) # batch_end-BS for padding
      if is_train: X = Tensor.where(Tensor.rand(X.shape[0],1,1,1) < flip_chance, X[..., ::-1], X) # flip augmentation 
      Y = np.zeros((BS, num_classes), np.float32)
      Y[range(BS),Y_train[batch_end-BS:batch_end]] = -1.0*num_classes
      Y = Tensor(Y.reshape(BS, num_classes))
      yield X, Y
    if not is_train: break

def train_cifar(bs=256, eval_bs=500, steps=1500, div_factor=1e16, final_lr_ratio=0.001, max_lr=0.01, pct_start=0.2, momentum=0.8, wd=0.14, label_smoothing=0., seed=6,
                lr_scheduler_pause_step=1200, lr_scheduler_resume_step=1450):
  set_seed(seed)
  Tensor.training = True

  BS, EVAL_BS, STEPS = getenv("BS", bs), getenv('EVAL_BS', eval_bs), getenv("STEPS", steps)
  MAX_LR, PCT_START, MOMENTUM, WD = getenv("MAX_LR", max_lr), getenv('PCT_START', pct_start), getenv('MOMENTUM', momentum), getenv("WD", wd)
  DIV_FACTOR, LABEL_SMOOTHING = getenv('DIV_FACTOR', div_factor), getenv('LABEL_SMOOTHING', label_smoothing)
  FINAL_DIV_FACTOR, LR_PAUSE, LR_RESUME = 1./(DIV_FACTOR*getenv('FINAL_LR_RATIO', final_lr_ratio)), getenv('LR_PAUSE', lr_scheduler_pause_step), getenv('LR_RESUME', lr_scheduler_resume_step)

  if getenv("FAKEDATA"):
    N = 2048
    X_train = np.random.default_rng().standard_normal(size=(N, 3, 32, 32), dtype=np.float32)
    Y_train = np.random.randint(0,10,size=(N), dtype=np.int32)
    X_test, Y_test = X_train, Y_train
  else:
    X_train, Y_train = fetch_cifar(train=True)
    X_test, Y_test = fetch_cifar(train=False)
  model = SpeedyResNet()
  optimizer = optim.SGD(optim.get_parameters(model), lr=0.01, momentum=MOMENTUM, nesterov=True, weight_decay=WD)
  lr_scheduler = OneCycleLR(optimizer, max_lr=MAX_LR, initial_div_factor=DIV_FACTOR, final_div_factor=FINAL_DIV_FACTOR, 
                            total_steps=STEPS - (LR_RESUME-LR_PAUSE), pause_range=[LR_PAUSE, LR_RESUME], pct_start=PCT_START)

  # JIT at every run
  from tinygrad.jit import TinyJit
  @TinyJit
  def train_step_jitted(model, optimizer, lr_scheduler, X, Y):
    out = model(X)
    loss = (1 - LABEL_SMOOTHING) * out.mul(Y).mean() + (-1 * LABEL_SMOOTHING * out.mean())
    if not getenv("DISABLE_BACKWARD"):
      optimizer.zero_grad()
      loss.backward()
      optimizer.step()
      lr_scheduler.step()
    return loss.realize()

  @TinyJit
  def eval_step_jitted(model, X, Y):
    out = model(X, training=False)
    loss = out.mul(Y).mean()
    return out.realize(), loss.realize()
  # 97 steps in 2 seconds = 20ms / step
  # step is 1163.42 GOPS = 56 TFLOPS!!!, 41% of max 136
  # 4 seconds for tfloat32 ~ 28 TFLOPS, 41% of max 68
  # 6.4 seconds for float32 ~ 17 TFLOPS, 50% of max 34.1
  # 4.7 seconds for float32 w/o channels last. 24 TFLOPS. we get 50ms then i'll be happy. only 64x off

  # https://www.anandtech.com/show/16727/nvidia-announces-geforce-rtx-3080-ti-3070-ti-upgraded-cards-coming-in-june
  # 136 TFLOPS is the theoretical max w float16 on 3080 Ti
  best_eval = -1
  i = 0
  for X, Y in fetch_batches(X_train, Y_train, BS=BS, is_train=True):
    if i > STEPS: break
    if i%50 == 0 and i > 1:
      # use training batchnorm (and no_grad would change the kernels)
      corrects = []
      losses = []
      for Xt, Yt in fetch_batches(X_test, Y_test, BS=EVAL_BS):
        out, loss = eval_step_jitted(model, Xt, Yt)
        outs = out.numpy().argmax(axis=1)
        correct = outs == Yt.numpy().argmin(axis=1)
        losses.append(loss.numpy().tolist())
        corrects.extend(correct.tolist())
      acc = sum(corrects)/len(corrects)*100.0
      if acc > best_eval:
        best_eval = acc
        print(f"eval {sum(corrects)}/{len(corrects)} {acc:.2f}%, {(sum(losses)/len(losses)):7.2f} val_loss STEP={i}")
    if STEPS == 0: break
    GlobalCounters.reset()
    st = time.monotonic()
    loss = train_step_jitted(model, optimizer, lr_scheduler, X, Y)
    et = time.monotonic()
    loss_cpu = loss.numpy()
    cl = time.monotonic()
    print(f"{i:3d} {(cl-st)*1000.0:7.2f} ms run, {(et-st)*1000.0:7.2f} ms python, {(cl-et)*1000.0:7.2f} ms CL, {loss_cpu:7.2f} loss, {optimizer.lr.numpy()[0]:.6f} LR, {GlobalCounters.mem_used/1e9:.2f} GB used, {GlobalCounters.global_ops*1e-9/(cl-st):9.2f} GFLOPS")
    i += 1

if __name__ == "__main__":
  train_cifar()
