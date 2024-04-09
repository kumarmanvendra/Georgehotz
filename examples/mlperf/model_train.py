import functools
import os, sys
import time
from tqdm import tqdm
import multiprocessing

from tinygrad import Device, GlobalCounters, Tensor, TinyJit, dtypes
from tinygrad.helpers import colored, getenv, BEAM, WINO
from tinygrad.nn.state import get_parameters, get_state_dict, safe_load, safe_save
from tinygrad.nn.optim import LARS, SGD, OptimizerGroup

from extra.lr_scheduler import LRSchedulerGroup
from examples.mlperf.helpers import get_training_state, load_training_state

def train_resnet():
  from extra.models import resnet
  from examples.mlperf.dataloader import batch_load_resnet
  from extra.datasets.imagenet import get_train_files, get_val_files
  from examples.mlperf.lr_schedulers import PolynomialDecayWithWarmup
  from examples.mlperf.initializers import Conv2dHeNormal, Linear
  from examples.hlb_cifar10 import UnsyncedBatchNorm

  config = {}
  seed = config["seed"] = getenv("SEED", 42)
  Tensor.manual_seed(seed)  # seed for weight initialization

  GPUS = config["GPUS"] = [f"{Device.DEFAULT}:{i}" for i in range(getenv("GPUS", 1))]
  print(f"Training on {GPUS}")
  for x in GPUS: Device[x]

  # ** model definition and initializers **
  num_classes = 1000
  resnet.Conv2d = Conv2dHeNormal
  resnet.Linear = Linear
  if not getenv("SYNCBN"): resnet.BatchNorm = functools.partial(UnsyncedBatchNorm, num_devices=len(GPUS))
  model = resnet.ResNet50(num_classes)

  # shard weights and initialize in order
  for k, x in get_state_dict(model).items():
    if not getenv("SYNCBN") and ("running_mean" in k or "running_var" in k):
      x.realize().shard_(GPUS, axis=0)
    else:
      x.realize().to_(GPUS)
  parameters = get_parameters(model)

  # ** hyperparameters **
  epochs            = config["epochs"]            = getenv("EPOCHS", 41)
  BS                = config["BS"]                = getenv("BS", 104 * len(GPUS))  # fp32 GPUS<=6 7900xtx can fit BS=112
  EVAL_BS           = config["EVAL_BS"]           = getenv("EVAL_BS", BS)
  base_lr           = config["base_lr"]           = getenv("LR", 8.5 * (BS/2048))
  lr_warmup_epochs  = config["lr_warmup_epochs"]  = getenv("WARMUP_EPOCHS", 5)
  decay             = config["decay"]             = getenv("DECAY", 2e-4)

  loss_scaler       = config["LOSS_SCALER"]       = getenv("LOSS_SCALER", 128.0 if dtypes.default_float == dtypes.float16 else 1.0)

  target, achieved  = getenv("TARGET", 0.759), False
  eval_start_epoch  = getenv("EVAL_START_EPOCH", 0)
  eval_epochs       = getenv("EVAL_EPOCHS", 1)

  steps_in_train_epoch  = config["steps_in_train_epoch"]  = (len(get_train_files()) // BS)
  steps_in_val_epoch    = config["steps_in_val_epoch"]    = (len(get_val_files()) // EVAL_BS)

  config["DEFAULT_FLOAT"] = dtypes.default_float.name
  config["BEAM"]    = BEAM.value
  config["WINO"]    = WINO.value
  config["SYNCBN"]  = getenv("SYNCBN")

  # ** Optimizer **
  skip_list = [v for k, v in get_state_dict(model).items() if "bn" in k or "bias" in k or "downsample.1" in k]
  parameters = [x for x in parameters if x not in set(skip_list)]
  optimizer = LARS(parameters, base_lr, momentum=.9, weight_decay=decay)
  optimizer_skip = SGD(skip_list, base_lr, momentum=.9, weight_decay=0.0, classic=True)
  optimizer_group = OptimizerGroup(optimizer, optimizer_skip)

  # ** LR scheduler **
  scheduler = PolynomialDecayWithWarmup(optimizer, initial_lr=base_lr, end_lr=1e-4,
                                        train_steps=epochs * steps_in_train_epoch,
                                        warmup=lr_warmup_epochs * steps_in_train_epoch)
  scheduler_skip = PolynomialDecayWithWarmup(optimizer_skip, initial_lr=base_lr, end_lr=1e-4,
                                             train_steps=epochs * steps_in_train_epoch,
                                             warmup=lr_warmup_epochs * steps_in_train_epoch)
  scheduler_group = LRSchedulerGroup(scheduler, scheduler_skip)
  print(f"training with batch size {BS} for {epochs} epochs")

  # ** resume from checkpointing **
  start_epoch = 0
  if ckpt:=getenv("RESUME", ""):
    load_training_state(model, optimizer_group, scheduler_group, safe_load(ckpt))
    start_epoch = int(scheduler.epoch_counter.numpy().item() / steps_in_train_epoch)
    print(f"resuming from {ckpt} at epoch {start_epoch}")

  # ** init wandb **
  WANDB = getenv("WANDB")
  if WANDB:
    import wandb
    wandb_args = {"id": wandb_id, "resume": "must"} if (wandb_id := getenv("WANDB_RESUME", "")) else {}
    wandb.init(config=config, **wandb_args)

  BENCHMARK = getenv("BENCHMARK")

  # ** jitted steps **
  input_mean = Tensor([123.68, 116.78, 103.94], device=GPUS, dtype=dtypes.float32).reshape(1, -1, 1, 1)
  # mlperf reference resnet does not divide by input_std for some reason
  # input_std = Tensor([0.229, 0.224, 0.225], device=GPUS, dtype=dtypes.float32).reshape(1, -1, 1, 1)
  def normalize(x): return (x.permute([0, 3, 1, 2]) - input_mean).cast(dtypes.default_float)
  @TinyJit
  def train_step(X, Y):
    optimizer_group.zero_grad()
    X = normalize(X)
    out = model.forward(X)
    loss = out.cast(dtypes.float32).sparse_categorical_crossentropy(Y, label_smoothing=0.1)
    top_1 = (out.argmax(-1) == Y).sum()
    (loss * loss_scaler).backward()
    for t in optimizer_group.params: t.grad = t.grad.contiguous() / loss_scaler
    optimizer_group.step()
    scheduler_group.step()
    return loss.realize(), top_1.realize()
  @TinyJit
  def eval_step(X, Y):
    X = normalize(X)
    out = model.forward(X)
    loss = out.cast(dtypes.float32).sparse_categorical_crossentropy(Y, label_smoothing=0.1)
    top_1 = (out.argmax(-1) == Y).sum()
    return loss.realize(), top_1.realize()
  def data_get(it):
    x, y, cookie = next(it)
    return x.shard(GPUS, axis=0).realize(), Tensor(y, requires_grad=False).shard(GPUS, axis=0), cookie

  # ** epoch loop **
  step_times = []
  for e in range(start_epoch, epochs):
    # ** train loop **
    Tensor.training = True
    batch_loader = batch_load_resnet(batch_size=BS, val=False, shuffle=True, seed=seed*epochs + e)
    it = iter(tqdm(batch_loader, total=steps_in_train_epoch, desc=f"epoch {e}", disable=BENCHMARK))
    i, proc = 0, data_get(it)
    st = time.perf_counter()
    while proc is not None:
      GlobalCounters.reset()
      (loss, top_1_acc), proc = train_step(proc[0], proc[1]), proc[2]

      pt = time.perf_counter()

      try:
        next_proc = data_get(it)
      except StopIteration:
        next_proc = None

      dt = time.perf_counter()

      device_str = loss.device if isinstance(loss.device, str) else f"{loss.device[0]} * {len(loss.device)}"
      loss, top_1_acc = loss.numpy().item(), top_1_acc.numpy().item() / BS

      cl = time.perf_counter()
      if BENCHMARK:
        step_times.append(cl - st)

      tqdm.write(
        f"{i:5} {((cl - st)) * 1000.0:7.2f} ms run, {(pt - st) * 1000.0:7.2f} ms python, {(dt - pt) * 1000.0:6.2f} ms fetch data, "
        f"{(cl - dt) * 1000.0:7.2f} ms {device_str}, {loss:5.2f} loss, {top_1_acc:3.2f} acc, {optimizer.lr.numpy()[0]:.6f} LR, "
        f"{GlobalCounters.mem_used / 1e9:.2f} GB used, {GlobalCounters.global_ops * 1e-9 / (cl - st):9.2f} GFLOPS")
      if WANDB:
        wandb.log({"lr": optimizer.lr.numpy(), "train/loss": loss, "train/top_1_acc": top_1_acc, "train/step_time": cl - st,
                   "train/python_time": pt - st, "train/data_time": dt - pt, "train/cl_time": cl - dt,
                   "train/GFLOPS": GlobalCounters.global_ops * 1e-9 / (cl - st), "epoch": e + (i + 1) / steps_in_train_epoch})

      st = cl
      proc, next_proc = next_proc, None  # return old cookie
      i += 1

      if i == BENCHMARK:
        median_step_time = sorted(step_times)[(BENCHMARK + 1) // 2]  # in seconds
        estimated_total_hours = median_step_time * steps_in_train_epoch * epochs / 60 / 60
        print(f"Estimated training time: {estimated_total_hours:.0f}h{(estimated_total_hours - int(estimated_total_hours)) * 60:.0f}m")
        # if we are doing beam search, run the first eval too
        if BEAM.value and e == start_epoch: break
        return

    # ** eval loop **
    if (e + 1 - eval_start_epoch) % eval_epochs == 0 and steps_in_val_epoch > 0:
      train_step.reset()  # free the train step memory :(
      eval_loss = []
      eval_times = []
      eval_top_1_acc = []
      Tensor.training = False

      it = iter(tqdm(batch_load_resnet(batch_size=EVAL_BS, val=True, shuffle=False), total=steps_in_val_epoch))
      proc = data_get(it)
      while proc is not None:
        GlobalCounters.reset()
        st = time.time()

        (loss, top_1_acc), proc = eval_step(proc[0], proc[1]), proc[2]  # drop inputs, keep cookie

        try:
          next_proc = data_get(it)
        except StopIteration:
          next_proc = None

        loss, top_1_acc = loss.numpy().item(), top_1_acc.numpy().item() / EVAL_BS
        eval_loss.append(loss)
        eval_top_1_acc.append(top_1_acc)
        proc, next_proc = next_proc, None  # return old cookie

        et = time.time()
        eval_times.append(et - st)

      eval_step.reset()
      total_loss = sum(eval_loss) / len(eval_loss)
      total_top_1 = sum(eval_top_1_acc) / len(eval_top_1_acc)
      total_fw_time = sum(eval_times) / len(eval_times)
      tqdm.write(f"eval loss: {total_loss:.2f}, eval time: {total_fw_time:.2f}, eval top 1 acc: {total_top_1:.3f}")
      if WANDB:
        wandb.log({"eval/loss": total_loss, "eval/top_1_acc": total_top_1, "eval/forward_time": total_fw_time, "epoch": e + 1})

      # save model if achieved target
      if not achieved and total_top_1 >= target:
        if not os.path.exists("./ckpts"): os.mkdir("./ckpts")
        fn = f"./ckpts/resnet50.safe"
        safe_save(get_state_dict(model), fn)
        print(f" *** Model saved to {fn} ***")
        achieved = True

      # checkpoint every time we eval
      if getenv("CKPT"):
        if not os.path.exists("./ckpts"): os.mkdir("./ckpts")
        if WANDB and wandb.run is not None:
          fn = f"./ckpts/{time.strftime('%Y%m%d_%H%M%S')}_{wandb.run.id}_e{e}.safe"
        else:
          fn = f"./ckpts/{time.strftime('%Y%m%d_%H%M%S')}_e{e}.safe"
        print(f"saving ckpt to {fn}")
        safe_save(get_training_state(model, optimizer_group, scheduler_group), fn)

# def train_retinanet():
#   # TODO: Retinanet
#   import sys
#   from extra.models.retinanet import RetinaNet, AnchorGenerator, ImageList
#   from extra.models.resnet import ResNeXt50_32X4D
#   from tinygrad.nn.optim import Adam
#   from extra.datasets.openimages_new import iterate, get_openimages
#   ROOT = 'extra/datasets/open-images-v6TEST'
#   NAME = 'openimages-mlperf'
#   coco = get_openimages(NAME,ROOT, 'train')
#   # for x,y in iterate(coco, 8):
#   #   print(x.shape)
#   #   print(y[0].keys())
#   #   print(y[0]['boxes'].shape, y[0]['image_size'])
#   #   print(y[1]['boxes'].shape, y[1]['image_size'])
#   #   print(y[2]['boxes'].shape, y[2]['image_size'])
#   #   print(x[0])
#   #   sys.exit()
#   # # from extra.datasets.openimages import openimages, iterate
#   from pycocotools.coco import COCO
#   from pycocotools.cocoeval import COCOeval
#   import numpy as np
  
#   anchor_sizes = tuple((x, int(x * 2 ** (1.0 / 3)), int(x * 2 ** (2.0 / 3))) for x in [32, 64, 128, 256, 512])
#   # print(anchor_sizes)
#   # sys.exit()
#   aspect_ratios = ((0.5, 1.0, 2.0),) * len(anchor_sizes)
#   anchor_generator = AnchorGenerator(
#       anchor_sizes, aspect_ratios
#   )
#   # coco = COCO(openimages())
#   # coco_eval = COCOeval(coco, iouType="bbox")
#   # coco_evalimgs, evaluated_imgs, ncats, narea = [], [], len(coco_eval.params.catIds), len(coco_eval.params.areaRng)
#   input_mean = Tensor([0.485, 0.456, 0.406]).reshape(1, -1, 1, 1)
#   input_std = Tensor([0.229, 0.224, 0.225]).reshape(1, -1, 1, 1)
#   def input_fixup(x):
#     x = x.permute([0,3,1,2]) / 255.0
#     # x -= input_mean
#     x = x - input_mean
#     # x /= input_std
#     x = x/input_std
#     return x
#   # from examples.mlperf.dataloader import batch_load_resnet
#   # from extra.datasets.imagenet import get_train_files, get_val_files
#   # from examples.mlperf.lr_schedulers import PolynomialDecayWithWarmup
#   # from examples.mlperf.initializers import Conv2dHeNormal, Linear
#   # from examples.hlb_cifar10 import UnsyncedBatchNorm

#   EPOCHS = 3
#   BS = 4
#   BS = 2

  
#   model = RetinaNet(ResNeXt50_32X4D())
#   parameters = []
#   for k, x in get_state_dict(model).items():
#     # x.requires_grad = True
#     x.realize()
#     # print(k, x.grad, x.shape, x.device)
#     if 'head' in k:
#       parameters.append(x)


#   # parameters = get_parameters(model)
#   optimizer = Adam(parameters)
  

#   # @TinyJit
#   def train_step(X, Y):
#     Tensor.training = True
#     # i_s = [y['image_size'] for y in Y]
#     # https://github.com/mlcommons/training/blob/master/single_stage_detector/ssd/model/transform.py#L96
#     i_s = [(800,800)]*X.shape[0]
#     # print(i_s)
#     # sys.exit()
#     # image_list = ImageList(X, [(800,800)]*X.shape[0])

#     image_list = ImageList(X, i_s)
    
#     # a1 = model.anchor_gen(X.shape[1:3])
#     # a1 = [Tensor(t, dtype=dtypes.float) for t in a1]

#     # anchors = model.anchor_gen(X.shape[1:3])
#     # anchors = [Tensor(t, dtype=dtypes.float) for t in anchors]
#     # for t in anchors:
#     #   print(t.shape)
#     # sys.exit()

#     # optimizer.zero_grad()

#     logits = model(X, Y, anchor_generator)
#     return logits.realize()
#     features, logits_reg, logits_class = model(X)
#     print(colored(f'HEAD TYPES RETURN {len(features)} {type(features)} {features[0].shape} {features[1].shape} {type(logits_reg)} {type(logits_class)}', 'magenta'))
#     del features, logits_reg, logits_class
#     return Tensor([1,2,7])
#     return logits_reg.realize()
#     # features, logits_reg, logits_class = model(input_fixup(X))
#     # features, logits_reg, logits_class = model(X.permute([0,3,1,2]) / 255.0)
#     # print('backbone:', len(backbone_logits))
#     # features = [backbone_logits[i] for i in range(X.shape[0])]
#     anchors = anchor_generator(image_list, features)
    
#     # print('Train_step logits', logits_reg.shape, logits_class.shape)
#     # print(logits_reg.numpy())
#     # logits_reg, logits_class = model(X)
#     # print(anchors)
#     # print(a1)
#     # print(features)
#     # sys.exit()
   
#     # return logits_reg.realize()
#     loss = model.loss(logits_reg, logits_class, Y, anchors)
    
#     # loss = logits_reg.sparse_categorical_crossentropy(Tensor([1,2,3,4,5,6,7,8]).reshape(8,1), label_smoothing=0.1)
#     # print('loss',loss.numpy())
#     # sys.exit()
#     # loss.backward()
#     # optimizer.step()
#     return loss
#     return loss.realize()
#   # @TinyJit
#   def eval_step(X, Y):
#     Tensor.training = False

#     Tensor.training = True

#   for epoch in range(EPOCHS):
#     Tensor.training = True
#     # for X,Y in iterate(coco, BS, True):
#     for X,Y in iterate(coco, BS):
#       X = input_fixup(X).realize()
#       print(colored(f'Input Data Shape: {X.shape} {X.dtype} {Tensor.training}','green'))
#       # print(X.shape)
#       # print(Y[0]['boxes'].shape, Y[0]['labels'].shape )
#       # print(Y[1]['boxes'].shape, Y[1]['labels'].shape)
#       # print(Y[0]['boxes'].shape, Y[0]['image_size'])
#       # print(Y[1]['boxes'].shape, Y[1]['image_size'])
#       # print(Y[2]['boxes'].shape, Y[2]['image_size'])
#       # print(x[0])

#       # sys.exit()
#       # a = model.anchor_gen(X.shape[1:3])
#       # print(a)
#       # sys.exit()
#       # for i in range(len(Y)):
#       #   print(Y[i]['boxes'].numpy())
#       #   print(Y[i]['labels'].numpy())
#       #   print(Y[i]['image_id'])
#       #   print(Y[i]['image_size'])
#       # sys.exit()
#       st = time.monotonic()
#       loss = train_step(X, Y)

#       print(colored(f'Iter done! Time: {time.monotonic()-st} Loss: {loss.numpy()}', 'red'))
#       # print(colored(f'Iter done! Time: {time.monotonic()-st}', 'red'))

#       del loss, X, Y
#       # break
#     # break
#       # sys.exit()
#   pass
def train_retinanet():
  EPOCHS = 10
  BS = 2
  input_mean = Tensor([0.485, 0.456, 0.406]).reshape(1, -1, 1, 1)
  input_std = Tensor([0.229, 0.224, 0.225]).reshape(1, -1, 1, 1)
  def input_fixup(x):
    x = x.permute([0,3,1,2]) / 255.0
    x -= input_mean
    x /= input_std
    return x
  from extra.models.retinanetNew import RetinaNet, sigmoid_focal_loss
  # from extra.models.retinanet import RetinaNet
  from extra.models.retinanet import AnchorGenerator, ImageList
  anchor_sizes = tuple((x, int(x * 2 ** (1.0 / 3)), int(x * 2 ** (2.0 / 3))) for x in [32, 64, 128, 256, 512])
  aspect_ratios = ((0.5, 1.0, 2.0),) * len(anchor_sizes)
  anchor_generator = AnchorGenerator(
      anchor_sizes, aspect_ratios
  )
  from extra.models.resnet import ResNeXt50_32X4D
  from tinygrad.nn.optim import Adam
  from extra.datasets.openimages_new import iterate
  from extra.datasets.openimages_new import get_openimages
  ROOT = 'extra/datasets/open-images-v6TEST'
  NAME = 'openimages-mlperf'
  coco = get_openimages(NAME,ROOT, 'train')

  # from extra.datasets.openimages import openimages, iterate
  # from pycocotools.coco import COCO
  # from pycocotools.cocoeval import COCOeval
  # coco = COCO(openimages())
  # coco_eval = COCOeval(coco, iouType="bbox")


  model = RetinaNet(ResNeXt50_32X4D(), num_anchors=anchor_generator.num_anchors_per_location()[0])
  mdlrun = TinyJit(lambda x: model(x, True))
  # mdlrun_false = TinyJit(lambda x: model(x, False))
  # mdlloss = TinyJit(lambda r, c, Y, a: model.loss(r,c,Y,a))
  # mdlloss_temp = TinyJit(lambda r, c,y,a : model.loss_temp(r,c,y,a))
  parameters = []
  for k, x in get_state_dict(model).items():
    # print(k, x.shape)
    # print(k, x.numpy())
    # x.requires_grad = True
    
    # print(k, x.grad, x.shape, x.device)
    if 'head' in k and ('clas' in k or 'reg' in k ):
    # if 'head' in k and ('reg' in k ):
    # if 'head' in k and ('clas' in k ):
      print(k)
      x.requires_grad = True
      parameters.append(x)
    else:
      x.requires_grad = False
    # x.realize()
    # print(k, x.numpy(), x.grad)
  # p = get_parameters(model)
  # for t in p:
  #   if t.requires_grad:
  #     parameters.append(t)
  optimizer = Adam(parameters)
  # for k, x in get_state_dict(model).items():
  #   if 'head' in k:
  #     x.requires_grad = True
  #   else:
  #     x.requires_grad = False
  #   # print(k, x.requires_grad)
  # @TinyJit
  def train_step(X, Y_b_P, Y_l_P, matched_idxs):
    Tensor.training = True
    # optimizer.zero_grad()

    # mdlloss.reset()
    # _ = model(X)
    # _ = mdlrun(X)
    # Tensor.training = False
    b,r,c = mdlrun(X)
    # b,r,c = model(X, True)
    # return model.loss_dummy(r,c)
    # o = mdlrun_false(X)
    # o = model(X, False)
    # loss = o.max()
    # return loss.realize()

    # b,r,c = model(X, True)
    # _ = model(X, False)
    # r = r.chunk(BS)
    # r = [rr.squeeze(0) for rr in r]
    # c = c.chunk(BS)
    # c = [cc.squeeze(0).contiguous() for cc in c]

    # loss = Tensor(69)
    # loss_reg, loss_class = model.loss(r, c, Y, anchor_generator(X, b))
    # loss_reg, loss_class = model.loss(r, c, Y_b, Y_l, anchors, Y_b_P, Y_l_P)
    # loss_reg = model.head.regression_head.loss(r, Y_b_P, anchors, matched_idxs)
    # loss_class = model.head.classification_head.loss(c, Y_l_P, matched_idxs)
    # loss_reg = mdl_reg_loss_jit(r, Y_b_P, matched_idxs)
    # loss_class = mdl_class_loss_jit(c, Y_l_P, matched_idxs)
    loss_reg = mdl_reg_loss(r, Y_b_P, matched_idxs)
    loss_class = mdl_class_loss(c, Y_l_P, matched_idxs)

    print(colored(f'loss_reg {loss_reg.numpy()}', 'green'))
    print(colored(f'loss_class {loss_class.numpy()}', 'green'))
    loss = loss_reg+loss_class
    # loss = mdlloss_temp(r,c, Y, anchor_generator(X, b))
    # loss = model.loss_temp(r,c, Y, anchor_generator(X, b))
    # loss = mdlloss(r, c, Y, anchor_generator(X, b))
    # loss = model.loss_dummy(r,c)
    # for o in optimizer.params:
    #   print(o.grad)
    # loss.backward()
    # print('******')
    # for o in optimizer.params:
    #   print(o.grad)
    # optimizer.step()
    return loss.realize()

  # print('Yuh', anchor_generator.num_anchors_per_location()[0])
  # sys.exit()
  for epoch in range(EPOCHS):
    print(colored(f'EPOCH {epoch}/{EPOCHS}:', 'cyan'))
    cnt = 0
    # for X,Y in iterate(coco, BS):
    for X, Y_boxes, Y_labels, Y_boxes_p, Y_labels_p in iterate(coco, BS):
      # print('SHAPE_CHECKK', Y_labels_p.shape)
      # print('ITERATE', Y_boxes_p.shape, Y_labels_p.shape)
      # for y1,y2 in zip(Y_boxes_p, Y_labels_p):
      #   print(y1.shape, y2.shape)
      # print('X_REQ_GRADDD', X.requires_grad)
      # train_step.reset()
      # if cnt<5: sigmoid_focal_loss.reset()
      if(cnt==0 and epoch==0):
        b,_,_ = mdlrun(X)
        ANCHORS = anchor_generator(X, b)
        ANCHORS = [a.realize() for a in ANCHORS]
        ANCHORS = Tensor.stack(ANCHORS)
        mdl_reg_loss_jit = TinyJit(lambda r, y, m: model.head.regression_head.loss(r,y,ANCHORS, m).realize())
        mdl_class_loss_jit = TinyJit(lambda c, y,m: model.head.classification_head.loss(c,y,m).realize())
        mdl_reg_loss = lambda r, y, m: model.head.regression_head.loss(r,y,ANCHORS, m)
        mdl_class_loss = lambda c, y,m: model.head.classification_head.loss(c,y,m)

      st = time.time()
      # print('IMAGE DATA', X)
      # for tt in Y:
      #   print(tt['boxes'].shape)
      #   print(tt['boxes'].numpy())
      # # print(X.numpy())
    # for X,Y in iterate(coco, BS, True):
      # print('PRE ZZEERROO OPTIMIZER PARAMS')
      # for pp in optimizer.params:
      #   print(pp.requires_grad, pp.grad)
      # print(colored(f'Image shape PREEEE {X.shape}', 'yellow'))
      # temp = input_fixup(X).realize()
      # print(colored(f'POST KIMAGE SHape {temp.shape} {temp.numpy()}', 'yellow'))
      cnt+=1
      # optimizer.zero_grad()
      # a = anchor_generator(X, b)
      # loss = train_step(X, Y)
      matched_idxs = model.matcher_gen(ANCHORS, Y_boxes).realize()
      loss = train_step(X, Y_boxes_p, Y_labels_p, matched_idxs)
      # loss = Tensor(44)
      
      # print(colored(f'JIT STATE {train_step.cnt} {train_step.jit_cache} {train_step.input_replace}', 'red'))
      
      # loss.backward()
      # optimizer.step()
      print(colored(f'{cnt} STEP {loss.numpy()} || {time.time()-st}', 'magenta'))
      # print('OPTIMIZER PARAMS')
      # for pp in optimizer.params:
      #   print(pp.requires_grad, pp.grad)
      # del loss
  #     if cnt>6:
  #       optimizer.zero_grad()
  #       i_s = []
  #       for t in Y:
  #         i_s.append(t['image_size'])
  #       # image_list = ImageList(X, i_s)
  #       anchors = anchor_generator(X, loss[0])
  #       # anchors = [a.realize() for a in anchors]
  #       print(colored(f'Computed anchor gen', 'red'))
  #       # loss = model.loss(loss[1], loss[2], Y, anchors)
  #       loss = model.loss_temp(loss[2])
  #       # print(colored(f'FOUND LOSS {loss.shape}', 'green'))
  #       loss.realize()
  #       print(colored(f'SUCESS LOSS REAILIZE {loss.shape} {loss.numpy()}', 'cyan'))
  #       # loss.backward()
  #       # print(colored(f'SUCESS LOSS BACKWARDS {loss.shape} {loss.grad}', 'red'))
  #       # # # for t in optimizer.params: t.grad = t.grad.contiguous()
  #       # optimizer.step()
  #       # print(colored(f'SUCESS OPTIMIZER STEP', 'cyan'))


  #       # print(colored(f'{cnt} LOSS SHAPE {loss.shape}', 'green'))
  #       # del image_list
  #     # del loss
  #     if cnt>200:
  #       sys.exit()
  #     del X, Y
  # pass
def train_unet3d():
  # TODO: Unet3d
  pass

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
  multiprocessing.set_start_method('spawn')
  with Tensor.train():
    for m in getenv("MODEL", "resnet,retinanet,unet3d,rnnt,bert,maskrcnn").split(","):
      nm = f"train_{m}"
      if nm in globals():
        print(f"training {m}")
        globals()[nm]()
