from tinygrad.tensor import Tensor
from tinygrad.helpers import getenv, DEBUG

def train_resnet():
  # TODO: Resnet50-v1.5
  pass

def train_retinanet():
  # TODO: Retinanet
  pass

def train_unet3d():
  from examples.mlperf.losses import dice_ce_loss
  from examples.mlperf.metrics import dice_score
  from extra.models.unet3d import UNet3D
  from extra.datasets.kits19 import iterate, get_train_files, get_val_files, sliding_window_inference
  from tinygrad import dtypes, Device, TinyJit, Tensor
  from tinygrad.nn.optim import SGD
  from tinygrad.nn.state import get_parameters, get_state_dict, safe_save, safe_load, load_state_dict
  from tqdm import tqdm

  import numpy as np
  import random

  TARGET_METRIC = 0.908
  NUM_EPOCHS = getenv("NUM_EPOCHS", 4000)
  BS = getenv("BS", 2)
  LR = getenv("LR", 0.8)
  MOMENTUM = getenv("MOMENTUM", 0.9)
  LR_WARMUP_EPOCHS = getenv("LR_WARMUP_EPOCHS", 200)
  LR_WARMUP_INIT_LR = getenv("LR_WARMUP_INIT_LR", 0.0001)
  EVAL_AT = getenv("EVAL_AT", 20)
  CHECKPOINT_EVERY = getenv("CHECKPOINT_EVERY", 10)
  CHECKPOINT_FN = getenv("CHECKPOINT_FN")
  WANDB = getenv("WANDB")
  PROJ_NAME = getenv("PROJ_NAME", "tinygrad_unet3d_mlperf")
  SIZE = (64, 64, 64) if getenv("SMALL") else (128, 128, 128)
  SEED = getenv("SEED")

  if getenv("FLOAT16"):
    dtypes.default_float = dtypes.float16

  if SEED:
    assert 1 <= SEED <= 9, "seed must be between 1-9"
    Tensor.manual_seed(SEED)
    np.random.seed(SEED)
    random.seed(SEED)

  if WANDB:
    try:
      import wandb
    except ImportError:
      raise "Need to install wandb to use it"

  GPUS = tuple([Device.canonicalize(f'{Device.DEFAULT}:{i}') for i in range(getenv("GPUS", 1))])
  assert BS % len(GPUS) == 0, f"{BS=} is not a multiple of {len(GPUS)=}"
  for x in GPUS: Device[x]

  model = UNet3D()

  if CHECKPOINT_FN:
    state_dict = safe_load(CHECKPOINT_FN)
    load_state_dict(model, state_dict)
    if DEBUG >= 1: print(f"Loaded checkpoint {CHECKPOINT_FN} into model")

  if len(GPUS) > 1:
    for p in get_parameters(model):
      p.to_(GPUS)

  optim = SGD(get_parameters(model), lr=LR, momentum=MOMENTUM, nesterov=True)

  def _lr_warm_up(optim, init_lr, lr, current_epoch, warmup_epochs):
    scale = current_epoch / warmup_epochs
    optim.lr.assign(Tensor([init_lr + (lr - init_lr) * scale], device=GPUS if len(GPUS) > 1 else None))

  @TinyJit
  def _train_step(model, x, y):
    y_hat = model(x)
    loss = dice_ce_loss(y_hat, y, gpus=GPUS)

    optim.zero_grad()
    loss.backward()
    optim.step()

    return loss
  
  def _eval_step(model, x, y):
    y_hat, y = sliding_window_inference(model, x, y)
    score = dice_score(Tensor(y_hat), Tensor(y)).mean().item()
    return score

  if WANDB: wandb.init(project=PROJ_NAME)

  for epoch in range(1, NUM_EPOCHS + 1):
    if epoch <= LR_WARMUP_EPOCHS and LR_WARMUP_EPOCHS > 0:
      _lr_warm_up(optim, LR_WARMUP_INIT_LR, LR, epoch, LR_WARMUP_EPOCHS)

    for x, y in (t:=tqdm(iterate(val=False, shuffle=True, bs=BS, size=SIZE), desc=f"[Training][Epoch: {epoch}/{NUM_EPOCHS}]", total=len(get_train_files()) // BS)):
      x, y = Tensor(x, requires_grad=False), Tensor(y, requires_grad=False)
      if len(GPUS) > 1: x, y = x.shard(GPUS, axis=0), y.shard(GPUS, axis=0)
      loss = _train_step(model, x, y)
      t.set_description(f"[Training][Epoch: {epoch}/{NUM_EPOCHS}][Loss: {loss.item():.3f}]")
      if WANDB: wandb.log({"train_loss": loss.item()})

    if epoch % CHECKPOINT_EVERY == 0:
      state_dict = get_state_dict(model)
      safe_save(state_dict, f"unet3d_mlperf_epoch_{epoch}.safetensors")
      print(f"Saved checkpoint at epoch {epoch}")

    if epoch % EVAL_AT == 0:
      with Tensor.train(val=False):
        scores = 0

        for i, (x, y) in enumerate((t:=tqdm(iterate(), desc=f"[Validation][Epoch: {epoch}/{NUM_EPOCHS}]", total=len(get_val_files()))), start=1):
          scores += _eval_step(model , x, y) # NOTE: passing in model instead since it is jitted
          t.set_description(f"[Validation][Epoch: {epoch}/{NUM_EPOCHS}][Mean DICE: {scores / i:.3f}]")

        scores /= i

        if WANDB: wandb.log({"val_mean_dice": scores})

        if scores >= TARGET_METRIC:
          print(f"Target metric ({TARGET_METRIC:.3f}) has been reached with validation metric of {scores:.3f}")
          print("Training complete")
          break
        else:
          print(f"Target metric ({TARGET_METRIC:.3f}) has not been reached with validation metric of {scores:.3f}")

def train_rnnt():
  # TODO: RNN-T
  pass

def train_bert():
  # TODO: BERT
  pass

def train_maskrcnn():
  # TODO: Mask RCNN
  pass

if __name__ == "__main__":
  with Tensor.train():
    for m in getenv("MODEL", "resnet,retinanet,unet3d,rnnt,bert,maskrcnn").split(","):
      nm = f"train_{m}"
      if nm in globals():
        print(f"training {m}")
        globals()[nm]()


