from tinygrad.nn import Conv2d,BatchNorm2d
from tinygrad.tensor import Tensor
from tinygrad.nn import Conv2d,BatchNorm2d
import numpy as np
import math
from itertools import chain
from extra.utils import download_file, get_child
from pathlib import Path
import torch


#Model architecture from https://github.com/ultralytics/ultralytics/issues/189
#the upsampling class has been taken from this pull request by dc-dc-dc. Now 2 models use upsampling. (retinet and this)


# UTIL FUNCTIONS
def dist2bbox(distance, anchor_points, xywh=True, dim=-1):
  lt, rb = distance.chunk(2, dim)
  x1y1 = anchor_points - lt
  x2y2 = anchor_points + rb
  if xywh:
    c_xy = (x1y1 + x2y2) / 2
    wh = x2y2 - x1y1
    return c_xy.cat(wh, dim=1)  # xywh bbox
  return x1y1.cat(x2y2, dim=1) # xyxy bbox

def make_anchors(feats, strides, grid_cell_offset=0.5):
  anchor_points, stride_tensor = [], []
  assert feats is not None
  for i, stride in enumerate(strides):
    _, _, h, w = feats[i].shape
    sx = np.arange(w, dtype='float32') + grid_cell_offset  # shift x
    sy = np.arange(h, dtype='float32') + grid_cell_offset  # shift y
    sy, sx = np.meshgrid(sx, sy, indexing='ij')
    anchor_points.append(np.stack((sy, sx), -1).reshape(-1, 2))
    stride_tensor.append(np.full((h * w, 1), stride.cpu().numpy()))
  return np.concatenate(anchor_points).reshape(2, -1), np.concatenate(stride_tensor).reshape(1, -1)

# this function is from the original implementation
def autopad(k, p=None, d=1):  # kernel, padding, dilation
  if d > 1:
    k = d * (k - 1) + 1 if isinstance(k, int) else [d * (x - 1) + 1 for x in k]  # actual kernel-size
  if p is None:
    p = k // 2 if isinstance(k, int) else [x // 2 for x in k]  # auto-pad
  return p

#this is taken from https://github.com/geohot/tinygrad/pull/784/files by dc-dc-dc (Now 2 models use upsampling)
class Upsample:
  def __init__(self, scale_factor:int, mode: str = "nearest") -> None:
    assert mode == "nearest" # only mode supported for now
    self.mode = mode
    self.scale_factor = scale_factor

  def __call__(self, x: Tensor) -> Tensor:
    assert len(x.shape) > 2 and len(x.shape) <= 5
    (b, c), _lens = x.shape[:2], len(x.shape[2:])
    tmp = x.reshape([b, c, -1] + [1] * _lens) * Tensor.ones(*[1, 1, 1] + [self.scale_factor] * _lens)
    return tmp.reshape(list(x.shape) + [self.scale_factor] * _lens).permute([0, 1] + list(chain.from_iterable([[y+2, y+2+_lens] for y in range(_lens)]))).reshape([b, c] + [x * self.scale_factor for x in x.shape[2:]])
  
# MODULE Definitions
class SPPF:
  def __init__(self, c1, c2, k=5):
      c_ = c1 // 2  # hidden channels
      self.cv1 = Conv_Block(c1, c_, k, 1, padding=None)
      self.cv2 = Conv_Block(c_ * 4, c2, k, 1, padding=None)
      self.maxpool = lambda x : x.pad2d((k // 2, k // 2, k // 2, k // 2)).max_pool2d(kernel_size=k, stride=1)
        
  def __call__(self, x):
    x = self.cv1(x)
    x2 = self.maxpool(x)
    x3 = self.maxpool(x2)
    x4 = self.maxpool(x3)
    return self.cv2(x.cat(x2, x3, x4, dim=1))
      
class Conv_Block:
  def __init__(self, c1, c2, kernel_size=1, stride=1, groups=1, dilation=1, padding=None):
    self.conv = Conv2d(c1,c2, kernel_size, stride, padding= autopad(kernel_size, padding, dilation), bias=False, groups=groups, dilation=dilation)
    self.batch = BatchNorm2d(c2)

  def __call__(self, x):
    return self.batch(self.conv(x)).silu()
   
class Bottleneck:
  def __init__(self, c1, c2 , shortcut: bool, g=1, kernels: list = (3,3), channel_factor=0.5):
    c_ = int(c2 * channel_factor)
    self.cv1 = Conv_Block(c1, c_, kernel_size=kernels[0], stride=1, padding=None)
    self.cv2 = Conv_Block(c_, c2, kernel_size=kernels[1], stride=1, padding=None, groups=g)
    self.residual = c1 == c2 and shortcut
    
  def __call__(self, x):
    return x + self.cv2(self.cv1(x)) if self.residual else self.cv2(self.cv1(x))
                  
class C2f:
  def __init__(self, c1, c2, n=1, shortcut=False, g=1, e=0.5):  # ch_in, ch_out, number, shortcut, groups, expansion
    self.c = int(c2 * e)  # hidden channels
    self.cv1 = Conv_Block(c1, 2 * self.c, 1,)
    self.cv2 = Conv_Block((2 + n) * self.c, c2, 1)  # optional act=FReLU(c2)
    self.bottleneck = [Bottleneck(self.c, self.c, shortcut, g, kernels=[(3, 3), (3, 3)], channel_factor=1.0) for _ in range(n)]
   
  def __call__(self, x):
    y= list(self.cv1(x).chunk(2, 1))
    y.extend(m(y[-1]) for m in self.bottleneck)
    z = y[0]
    for i in y[1:]: z = z.cat(i, dim=1)
    return self.cv2(z)

class DFL():
  def __init__(self, c1=16):
    self.conv = Conv2d(c1, 1, 1, bias=False)
    x = Tensor.arange(c1)
    self.conv.weight.assign(x.reshape(1, c1, 1, 1))
    self.c1 = c1

  def __call__(self, x):
    b, c, a = x.shape # batch, channels, anchors
    return self.conv(x.reshape(b, 4, self.c1, a).transpose(2, 1).softmax(1)).reshape(b, 4, a)


# stride = tensor([ 8., 16., 32.])
class DetectionHead():
  def __init__(self, nc=80, filters=()):
    self.ch = 16  # DFL channels
    self.nc = nc  # number of classes
    self.nl = len(filters)  # number of detection layers
    self.no = nc + self.ch * 4  # number of outputs per anchor
    self.stride = Tensor([8, 16, 32])  # strides computed during build #TODO - figure this out
    c1 = max(filters[0], self.nc)
    c2 = max((filters[0] // 4, self.ch * 4))

    self.dfl = DFL(self.ch) 
    self.cls = [[Conv_Block(x, c1, 3), Conv_Block(c1, c1, 3), Conv2d(c1, self.nc, 1)] for x in filters]
    self.box = [[Conv_Block(x, c2, 3), Conv_Block(c2, c2, 3), Conv2d(c2, 4 * self.ch, 1)] for x in filters]
    
  def forward(self, x):
    for i in range(self.nl):
      x[i] = x[i].sequential(self.box[i]).cat(x[i].sequential(self.cls[i]), dim=1)
    self.anchors, self.strides = (x.transpose(0, 1) for x in make_anchors(x, self.stride, 0.5))
    y = [i.reshape(x[0].shape[0], self.no, -1) for i in x]
    x_cat = y[0].cat(y[1], y[2], dim=2)
    split_sizes = [self.ch * 4, self.nc]
    box, cls = [x_cat[:, :split_sizes[0], :], x_cat[:, split_sizes[0]:, :]]
    dbox = dist2bbox(self.dfl(box), Tensor(self.anchors).unsqueeze(0), xywh=True, dim=1) * Tensor(self.strides)
    z = dbox.cat(cls.sigmoid(), dim=1)
    return z
             
                           
class Darknet():
  def __init__(self, w, r, d): #width_multiple, ratio_multiple, depth_multiple
    self.b1 = [Conv_Block(c1=3, c2=64*w, kernel_size=3, stride=2, padding=1), Conv_Block(64*w, 128*w, kernel_size=3, stride=2, padding=1)]
    self.b2 = [C2f(c1=128*w, c2=128*w, n=3*d, shortcut=True), Conv_Block(128*w, 256*w, 3, 2, 1), C2f(256*w, 256*w, 6*d, True)]
    self.b3 = [Conv_Block(256*w, 512*w, kernel_size=3, stride=2, padding=1), C2f(512*w, 512*w, 6*d, True)]
    self.b4 = [Conv_Block(512*w, 512*w*r, kernel_size=3, stride=2, padding=1), C2f(512*w*r, 512*w*r, 3*d, True)]
    self.b5 = [SPPF(512*w*r, 512*w*r, 5)]
    

  def return_modules(self):
    return [*self.b1, *self.b2, *self.b3, *self.b4, *self.b5]
  
  def forward(self, x):
    x1 = x.sequential(self.b1)
    x2 = x1.sequential(self.b2)
    x3 = x2.sequential(self.b3)
    x4 = x3.sequential(self.b4)
    x5 = self.b5[0](x4)
    return (x2, x3, x5)
  
class Yolov8NECK():
  def __init__(self, w, r, d):  #width_multiple, ratio_multiple, depth_multiple
    self.up = Upsample(2, mode='nearest')
    self.n1 = C2f(c1=512*w*(1+r), c2=512*w, n=3*d, shortcut=False)
    self.n2 = C2f(c1=768*w, c2=256*w, n=3*d, shortcut=False)
    self.n3 = Conv_Block(c1=256*w, c2=256*w, kernel_size=3, stride=2, padding=1)
    self.n4 = C2f(c1=768*w, c2=512*w, n=3*d, shortcut=False)
    self.n5 = Conv_Block(c1=512* w, c2=512 * w, kernel_size=3, stride=2, padding=1)
    self.n6 = C2f(c1=512*w*(1+r), c2=512*w*r, n=3*d, shortcut=False)
  
  def forward(self, p3, p4, p5):
    x =  self.n1(p4.cat(self.up(p5), dim=1))
    head_1 = self.n2(p3.cat(self.up(x), dim=1))
    head_2 = self.n4(x.cat(self.n3(head_1), dim=1))
    head_3 = self.n6(p5.cat(self.n5(head_2), dim=1))
    return [head_1, head_2, head_3]

class YOLOv8():
  # confirm filters. 
  def __init__(self, w, r,  d, num_classes): #width_multiple, ratio_multiple, depth_multiple
    self.net = Darknet(w, r, d)
    self.fpn = Yolov8NECK(w, r, d)
    self.head = DetectionHead(num_classes, filters=(256*w, 512*w, 512*w*r))

  def forward(self, x):
    x = self.net.forward(x)
    x = self.fpn.forward(*x)
    return self.head.forward(x)

  def load_weights(self):
    weights_path = Path(__file__).parent.parent / "weights" / "yolov8l.pt"
    state_dict = torch.load(weights_path)
    backbone_parameters = self.net.return_modules()
    weights = state_dict['model'].state_dict().items()
    backbone_parameters[0].conv.weight.assign(x)

test_inferece = Tensor.rand(1 ,3 , 640 , 640)
yolo_infer = YOLOv8(1, 1, 1, 80)  
print(yolo_infer.forward(test_inferece))
yolo_infer.load_weights()


# post processing functions for raw outputs from the head "https://github.com/ultralytics/ultralytics/blob/dada5b73c4340671ac67b99e8c813bf7b16c34ce/ultralytics/yolo/v8/detect/predict.py"
#NMS --> xywh2xyxy + box_iou
#Scale_boxes --> clip_boxes

#Saving --> plotting function - write results. 
#pre_process --> image process

def clip_boxes(boxes, shape):
  if isinstance(boxes, Tensor):  # TODO: maybe tensor.clip can be used here.
   boxes[..., 0] = boxes[..., 0].maximum(0).minimum(shape[1])  # x1
   boxes[..., 1] = boxes[..., 1].maximum(0).minimum(shape[0])  # y1
   boxes[..., 2] = boxes[..., 2].maximum(0).minimum(shape[1])  # x2
   boxes[..., 3] = boxes[..., 3].maximum(0).minimum(shape[0])  # y2
  else:  # np.array 
    boxes[..., [0, 2]] = np.clip(boxes[..., [0, 2]], 0, shape[1])  # x1, x2
    boxes[..., [1, 3]] = np.clip(boxes[..., [1, 3]], 0, shape[0])  # y1, y2

def scale_boxes(img1_shape, boxes, img0_shape, ratio_pad=None):
  if ratio_pad is None:  # calculate from img0_shape
    gain = min(img1_shape[0] / img0_shape[0], img1_shape[1] / img0_shape[1])  # gain  = old / new
    pad = (img1_shape[1] - img0_shape[1] * gain) / 2, (img1_shape[0] - img0_shape[0] * gain) / 2  # wh padding
  else:
    gain = ratio_pad[0][0]
    pad = ratio_pad[1]
  boxes[..., [0, 2]] -= pad[0]  # x padding
  boxes[..., [1, 3]] -= pad[1]  # y padding
  boxes[..., :4] /= gain
  clip_boxes(boxes, img0_shape)
  return boxes

# # TODO: remove clone 
# def xywh2xyxy(x):
#   y = x.clone() if isinstance(x, Tensor) else np.copy(x)
#   y[..., 0] = x[..., 0] - x[..., 2] / 2  # top left x
#   y[..., 1] = x[..., 1] - x[..., 3] / 2  # top left y
#   y[..., 2] = x[..., 0] + x[..., 2] / 2  # bottom right x
#   y[..., 3] = x[..., 1] + x[..., 3] / 2  # bottom right y
#   return y

# # TODO: fix prod and see about clamp
# def box_iou(box1, box2):
#   (a1, a2), (b1, b2) = box1[:, None].chunk(2, 2), box2.chunk(2, 1)
#   intersection = (a2.minimum(b2) - a1.maximum(b1)).maximum(0).prod(2)
#   # IoU = intersection / (area1 + area2 - intersection)
#   box1 = box1.T
#   box2 = box2.T
#   area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
#   area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])
#   return intersection / (area1[:, None] + area2 - intersection)
    


