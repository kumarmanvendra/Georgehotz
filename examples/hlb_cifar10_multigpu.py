#!/usr/bin/env python3
from extra import dist
if __name__ == "__main__":
  dist.preinit() # setup everything for DDP

# tinygrad implementation of https://github.com/tysam-code/hlb-CIFAR10/blob/main/main.py
# https://myrtle.ai/learn/how-to-train-your-resnet-8-bag-of-tricks/
# https://siboehm.com/articles/22/CUDA-MMM
import time
import numpy as np
from extra.datasets import fetch_cifar
from tinygrad import nn
from tinygrad.state import get_parameters, get_state_dict
from tinygrad.nn import optim
from tinygrad.tensor import Tensor
from tinygrad.helpers import getenv
from tinygrad.ops import GlobalCounters
from extra.lr_scheduler import OneCycleLR
from tinygrad.jit import TinyJit
from extra.dist import collectives
import wandb


def set_seed(seed):
  Tensor.manual_seed(getenv('SEED', seed)) # Deterministic
  np.random.seed(getenv('SEED', seed))

num_classes = 10

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
    mult = self.se2((self.se1(residual.mean((2,3)))).relu()).sigmoid().reshape(x.shape[0], x.shape[1], 1, 1) if self.se else 1.0
    x = self.norm[1](self.conv[1](x)).relu()
    x = self.norm[2](self.conv[2](x) * mult).relu()
    return x + residual

class SpeedyResNet:
  def __init__(self):
    # TODO: add whitening
    self.net = [
      nn.Conv2d(3, 64, kernel_size=1),
      nn.BatchNorm2d(64, track_running_stats=False, eps=1e-12, momentum=0.8),
      lambda x: x.relu(),
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

def fetch_batches(all_X, all_Y, BS, seed, is_train=False, flip_chance=0.5):
  def _shuffle(all_X, all_Y):
    if is_train:
      ind = np.arange(all_Y.shape[0])
      np.random.shuffle(ind)
      all_X, all_Y = all_X[ind, ...], all_Y[ind, ...]
    return all_X, all_Y
  rank, world_size = getenv("RANK"), getenv("WORLD_SIZE")
  while True:
    set_seed(seed)
    all_X, all_Y = _shuffle(all_X, all_Y)
    for batch_start in range(0, all_Y.shape[0], BS):
      batch_end = min(batch_start+BS, all_Y.shape[0])
      X = Tensor(all_X[batch_end-BS:batch_end]) # batch_end-BS for padding
      Y = np.zeros((BS, num_classes), np.float32)
      Y[range(BS),all_Y[batch_end-BS:batch_end]] = -1.0*num_classes
      Y = Tensor(Y.reshape(BS, num_classes))
      # divide into rank subsets
      if is_train:
        X = X[BS*rank//world_size:BS*(rank+1)//world_size]
        Y = Y[BS*rank//world_size:BS*(rank+1)//world_size]
      else:
        world_size = min(world_size, 4)
        rank = min(rank, 3)
        X = X[BS*rank//world_size:BS*(rank+1)//world_size]
        Y = Y[BS*rank//world_size:BS*(rank+1)//world_size]
      yield X, Y
    if not is_train: break
    seed += 1

def train_cifar(config, bs=512, eval_bs=500, steps=1000, div_factor=1e16, final_lr_ratio=0.001, max_lr=0.04, pct_start=0.2, momentum=0.8, wd=0.13, label_smoothing=0., mixup_alpha=0.025, seed=6):
  rank, world_size = getenv("RANK"), getenv("WORLD_SIZE")
  set_seed(seed)
  Tensor.training = True

  BS, EVAL_BS, STEPS = getenv("BS", bs), getenv('EVAL_BS', eval_bs), getenv("STEPS", steps)
  MAX_LR, PCT_START, MOMENTUM, WD = getenv("MAX_LR", config["max_lr"]), getenv('PCT_START', config["pct_start"]), getenv('MOMENTUM', config["momentum"]), getenv("WD", config["wd"])
  DIV_FACTOR, LABEL_SMOOTHING, MIXUP_ALPHA = getenv('DIV_FACTOR', config["div_factor"]), getenv('LABEL_SMOOTHING', config["label_smoothing"]), getenv('MIXUP_ALPHA', config["mixup_alpha"])
  FINAL_DIV_FACTOR = 1./(DIV_FACTOR*getenv('FINAL_LR_RATIO', config["final_lr_ratio"]))
  if getenv("FAKEDATA"):
    N = 2048
    X_train = np.random.default_rng().standard_normal(size=(N, 3, 32, 32), dtype=np.float32)
    Y_train = np.random.randint(0,10,size=(N), dtype=np.int32)
    X_test, Y_test = X_train, Y_train
  else:
    X_train, Y_train = fetch_cifar(train=True)
    X_test, Y_test = fetch_cifar(train=False)
  model = SpeedyResNet()
  optimizer = optim.SGD(get_parameters(model), lr=0.01, momentum=MOMENTUM, nesterov=True, weight_decay=WD)
  lr_scheduler = OneCycleLR(optimizer, max_lr=MAX_LR, div_factor=DIV_FACTOR, final_div_factor=FINAL_DIV_FACTOR,
                            total_steps=STEPS, pct_start=PCT_START)

  state_dict = get_state_dict(model)

  # JIT at every run
  @TinyJit
  def train_step_jitted(model, optimizer, lr_scheduler, Xr, Xl, Yr, Yl, mixup_prob):
    X, Y = Xr*mixup_prob + Xl*(1-mixup_prob), Yr*mixup_prob + Yl*(1-mixup_prob)
    X = Tensor.where(Tensor.rand(X.shape[0],1,1,1) < 0.5, X[..., ::-1], X) # flip augmentation
    out = model(X)
    loss = (1 - LABEL_SMOOTHING) * out.mul(Y).mean() + (-1 * LABEL_SMOOTHING * out.mean())
    if not getenv("DISABLE_BACKWARD"):
      optimizer.zero_grad()
      loss.backward()

      # sync gradients across ranks
      # bucket grads into buckets in order to optimize allreduce performance
      bucket_meta = {}
      bucket = []
      for k, v in state_dict.items():
        if v.grad is not None:
          bucket_meta[k] = (v.numel(), v.shape)
          bucket.append(v.grad.flatten())
        if len(bucket) == getenv("BUCKET_SIZE", 4):
          grads = collectives.allreduce(Tensor.cat(*bucket, Tensor.zeros(2)), cache_id=k)
          offset = 0
          for k in bucket_meta:
            size = bucket_meta[k][0]
            state_dict[k].grad.assign(grads[offset:offset+size].reshape(*bucket_meta[k][1]) / world_size)
            offset += size
          bucket = []
          bucket_meta = {}
      if len(bucket) > 0:
        grads = collectives.allreduce(Tensor.cat(*bucket, Tensor.zeros(2)), cache_id="last")
        offset = 0
        for k in bucket_meta:
          size = bucket_meta[k][0]
          state_dict[k].grad.assign(grads[offset:offset+size].reshape(*bucket_meta[k][1]) / world_size)
          offset += size

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
  from extra.dist import OOB

  # https://www.anandtech.com/show/16727/nvidia-announces-geforce-rtx-3080-ti-3070-ti-upgraded-cards-coming-in-june
  # 136 TFLOPS is the theoretical max w float16 on 3080 Ti
  best_eval = -1
  i = 0
  left_batcher, right_batcher = fetch_batches(X_train, Y_train, BS=BS, seed=seed, is_train=True), fetch_batches(X_train, Y_train, BS=BS, seed=seed+1, is_train=True)
  while i <= STEPS:
    (Xr, Yr), (Xl, Yl) = next(right_batcher), next(left_batcher)
    mixup_prob = Tensor(np.random.beta(MIXUP_ALPHA, MIXUP_ALPHA, (1, )).astype(np.float32)).contiguous() if MIXUP_ALPHA > 0 else Tensor.ones(Xr.shape[0], 1, 1, 1)
    if i%50 == 0 and i > 1:
      # batchnorm is frozen, no need for Tensor.training=False
      corrects = []
      losses = []
      for Xt, Yt in fetch_batches(X_test, Y_test, BS=EVAL_BS, seed=seed):
        out, loss = eval_step_jitted(model, Xt, Yt)
        outs = out.numpy().argmax(axis=1)
        correct = outs == Yt.numpy().argmin(axis=1)
        losses.append(loss.numpy().tolist())
        corrects.extend(correct.tolist())

      correct = sum(corrects)
      # collect accuracy calculations from all ranks
      if rank == 0:
        for j in range(1, min(getenv("WORLD_SIZE"), 4)):
          correct += OOB.recv(j)
      elif rank < 5:
        OOB.send(correct, 0)

      if rank == 0:
        acc = correct/(len(corrects)*min(getenv("WORLD_SIZE"), 4))*100.0
        if acc > best_eval:
          best_eval = acc
          print(f"eval {correct}/{len(corrects)*min(getenv('WORLD_SIZE'), 4)} {acc:.2f}%, {(sum(losses)/len(losses)):7.2f} val_loss STEP={i}")
    if STEPS == 0 or i==STEPS: break
    GlobalCounters.reset()
    st = time.monotonic()
    loss = train_step_jitted(model, optimizer, lr_scheduler, Xr, Xl, Yr, Yl, mixup_prob)
    et = time.monotonic()
    loss_cpu = loss.numpy()
    cl = time.monotonic()
    print(f"{i:3d} {(cl-st)*1000.0:7.2f} ms run, {(et-st)*1000.0:7.2f} ms python, {(cl-et)*1000.0:7.2f} ms CL, {loss_cpu:7.2f} loss, {optimizer.lr.numpy()[0]:.6f} LR, {GlobalCounters.mem_used/1e9:.2f} GB used, {GlobalCounters.global_ops*1e-9/(cl-st):9.2f} GFLOPS")
    # if rank == 0: wandb.log({"loss": loss_cpu, "lr": optimizer.lr.numpy()[0], "gflops": GlobalCounters.global_ops*1e-9/(cl-st), "mem_used": GlobalCounters.mem_used/1e9})
    i += 1
  return best_eval

def rank_0_initiate_train_single_step():
  wandb.init(project="tinygrad-cifar10")

  from extra.dist import OOB
  # send command to all ranks to start a single train iteration
  for i in range(1, getenv("WORLD_SIZE")):
    OOB.send({
      "max_lr": wandb.config.max_lr,
      "pct_start": wandb.config.pct_start,
      "div_factor": wandb.config.div_factor,
      "momentum": wandb.config.momentum,
      "wd": wandb.config.wd,
      "label_smoothing": wandb.config.label_smoothing,
      "final_lr_ratio": wandb.config.final_lr_ratio,
      "mixup_alpha": wandb.config.mixup_alpha,
    }, i)

  acc = train_cifar({
    "max_lr": wandb.config.max_lr,
    "pct_start": wandb.config.pct_start,
    "div_factor": wandb.config.div_factor,
    "momentum": wandb.config.momentum,
    "wd": wandb.config.wd,
    "label_smoothing": wandb.config.label_smoothing,
    "final_lr_ratio": wandb.config.final_lr_ratio,
    "mixup_alpha": wandb.config.mixup_alpha,
  })
  wandb.log({"val_acc": acc})

def run():
  rank = getenv("RANK")
  # setup wandb sweep
  if rank == 0:
    wandb.login()
    metric = {
      "name": "val_acc",
      "goal": "maximize",
      "target": 90.0,
    }
    good = {
      "max_lr": 0.016279556912291174,
      "pct_start": 0.20191245376826575,
      "div_factor": 3098105635814253,
      "momentum": 0.9210648369131892,
      "wd": 0.12201506850368582,
      "label_smoothing": 0.1590394650216007,
      "final_lr_ratio": 0.0015072069793158026,
      "mixup_alpha": 0.020980960594745347,
    }
    parameters_dict = {
      "max_lr": {
        "distribution": "normal",
        "mu": good["max_lr"],
        "sigma": 0.0032,
      },
      "pct_start": {
        "distribution": "normal",
        "mu": good["pct_start"],
        "sigma": 0.04,
      },
      "div_factor": {
        "distribution": "normal",
        "mu": good["div_factor"],
        "sigma": 6e14,
      },
      "momentum": {
        "distribution": "normal",
        "mu": good["momentum"],
        "sigma": 0.184,
      },
      "wd": {
        "distribution": "normal",
        "mu": good["wd"],
        "sigma": 0.0244,
      },
      "label_smoothing": {
        "distribution": "normal",
        "mu": good["label_smoothing"],
        "sigma": 0.032,
      },
      "final_lr_ratio": {
        "distribution": "normal",
        "mu": good["final_lr_ratio"],
        "sigma": 0.0003,
      },
      "mixup_alpha": {
        "distribution": "normal",
        "mu": good["mixup_alpha"],
        "sigma": 0.004,
      },
    }
    sweep_config = {
      "method": "bayes",
      "name": "sweep-5",
      "metric": metric,
      "parameters": parameters_dict,
    }
    sweep_id = wandb.sweep(sweep_config, project="tinygrad-cifar10")
    wandb.agent(sweep_id, function=rank_0_initiate_train_single_step, count=300)
  else:
    # wait for command from rank 0 to start a single train iteration
    from extra.dist import OOB
    while True:
      config = OOB.recv(0)
      if config is not None:
        train_cifar(config)
      else: break

def run2():
  train_cifar({
    "max_lr": 0.01,
    "pct_start": 0.0546875,
    "div_factor": 1e16,
    "momentum": 0.8,
    "wd": 0.15,
    "label_smoothing": 0,
    "final_lr_ratio": 0.001,
    "mixup_alpha": 0.025,
  })

if __name__ == "__main__":
  devices = ["gpu:0", "gpu:1", "gpu:2", "gpu:3", "gpu:4", "gpu:5"]
  # devices = ["hip:0", "hip:1", "hip:2", "hip:3", "hip:4", "hip:5"]
  world_size = len(devices)

  # startup our manager
  dist.init_oob(world_size)

  processes = []
  for rank, device in enumerate(devices):
    processes.append(dist.spawn(rank, device, fn=run2, args=()))
  for p in processes: p.join()
