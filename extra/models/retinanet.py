import math
import time
import sys
from typing import List, Dict
from tinygrad import Tensor, dtypes, TinyJit
from tinygrad.helpers import colored, flatten, get_child
from tinygrad.nn.state import safe_load, load_state_dict
import tinygrad.nn as nn
from extra.models.resnet import ResNet
import numpy as np

Conv2dNormal = nn.Conv2d
Conv2dNormal_prior_prob = nn.Conv2d
Conv2dKaiming = nn.Conv2d

def cust_bin_cross_logits(inputs, targets):
  return inputs.maximum(0) - targets * inputs + (1 + inputs.abs().neg().exp()).log()
# @TinyJit
def sigmoid_focal_loss(
    inputs: Tensor,
    targets: Tensor,
    mask: Tensor,
    alpha: float = 0.25,
    gamma: float = 2,
):

  p = Tensor.sigmoid(inputs) * mask

  ce_loss = cust_bin_cross_logits(inputs, targets) #* mask
  
  # print('ce_loss', ce_loss.shape)
  p_t = p * targets + (1 - p) * (1 - targets)
  # print('P_T', p_t.shape)
  loss = ce_loss * ((1 - p_t) ** gamma)
  # loss = ce_loss * ((1 - p_t).exp2())
  
  # print(f'SIg_LOSS_POS_EXP {loss.shape}')
  if alpha >= 0:
    alpha_t = alpha * targets + (1 - alpha) * (1 - targets)
    loss = alpha_t * loss
  loss=loss

  loss = loss * mask

  loss = loss.sum(-1)
  loss = loss.sum(-1)

  # print('sigmoid_focal_loss:', loss.shape)
  return loss

def l1_loss(x1:Tensor, x2:Tensor) -> Tensor:
  # print('l1 loss inputs', x1.numpy(), x2.numpy())
  # https://pytorch.org/docs/stable/_modules/torch/nn/functional.html#l1_loss
  ans = (x1 - x2).abs().sum(-1)
  ans = ans.sum(-1)
  # print('l1 loss', ans.numpy())
  return ans

def box_iou(boxes1: Tensor, boxes2: Tensor) -> Tensor:
  # print('BOX_IOU Arguements', boxes1.shape, boxes2.shape)

  def box_area(boxes): return (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
  area1 = box_area(boxes1)
  area2 = box_area(boxes2)
  lt = Tensor.maximum(boxes1[:, :2].unsqueeze(1), boxes2[:, :2])  # [N,M,2]
  rb = Tensor.minimum(boxes1[:, 2:].unsqueeze(1), boxes2[:, 2:])  # [N,M,2]
  # wh = (rb - lt).clip(min_=0)  # [N,M,2]
  wh = (rb - lt).maximum(0)  # [N,M,2]
  inter = wh[:, :, 0] * wh[:, :, 1]  # [N,M]
  # union = area1[:, None] + area2 - inter
  union = area1.unsqueeze(1) + area2 - inter
  iou = inter / union
  # print('BOx_IOU Ret Shape:', iou.shape, boxes1.shape, boxes2.shape)
  # print(iou[-1].numpy(), iou[-1].sum().numpy())

  return iou

def nms(boxes, scores, thresh=0.5):
  x1, y1, x2, y2 = np.rollaxis(boxes, 1)
  areas = (x2 - x1 + 1) * (y2 - y1 + 1)
  to_process, keep = scores.argsort()[::-1], []
  while to_process.size > 0:
    cur, to_process = to_process[0], to_process[1:]
    keep.append(cur)
    inter_x1 = np.maximum(x1[cur], x1[to_process])
    inter_y1 = np.maximum(y1[cur], y1[to_process])
    inter_x2 = np.minimum(x2[cur], x2[to_process])
    inter_y2 = np.minimum(y2[cur], y2[to_process])
    inter_area = np.maximum(0, inter_x2 - inter_x1 + 1) * np.maximum(0, inter_y2 - inter_y1 + 1)
    iou = inter_area / (areas[cur] + areas[to_process] - inter_area)
    to_process = to_process[np.where(iou <= thresh)[0]]
  return keep

def decode_bbox(offsets, anchors):
  dx, dy, dw, dh = np.rollaxis(offsets, 1)
  widths, heights = anchors[:, 2] - anchors[:, 0], anchors[:, 3] - anchors[:, 1]
  cx, cy = anchors[:, 0] + 0.5 * widths, anchors[:, 1] + 0.5 * heights
  pred_cx, pred_cy = dx * widths + cx, dy * heights + cy
  pred_w, pred_h = np.exp(dw) * widths, np.exp(dh) * heights
  pred_x1, pred_y1 = pred_cx - 0.5 * pred_w, pred_cy - 0.5 * pred_h
  pred_x2, pred_y2 = pred_cx + 0.5 * pred_w, pred_cy + 0.5 * pred_h
  return np.stack([pred_x1, pred_y1, pred_x2, pred_y2], axis=1, dtype=np.float32)
def encode_boxes(reference_boxes, proposals, weights = (1.0,1.0,1.0,1.0)):

  # perform some unpacking to make it JIT-fusion friendly
  wx = weights[0]
  wy = weights[1]
  ww = weights[2]
  wh = weights[3]

  proposals_x1 = proposals[:,:, 0].unsqueeze(-1)
  proposals_y1 = proposals[:,:, 1].unsqueeze(-1)
  proposals_x2 = proposals[:,:, 2].unsqueeze(-1)
  proposals_y2 = proposals[:,:, 3].unsqueeze(-1)
  # print('proposals:', proposals_x1.numpy(), proposals_x2.numpy(), proposals_y1.numpy(),
  #       proposals_y2.numpy())

  reference_boxes_x1 = reference_boxes[:,:, 0].unsqueeze(-1)
  reference_boxes_y1 = reference_boxes[:,:, 1].unsqueeze(-1)
  reference_boxes_x2 = reference_boxes[:,:, 2].unsqueeze(-1)
  reference_boxes_y2 = reference_boxes[:,:, 3].unsqueeze(-1)
  # print('reference boxes:', reference_boxes_x1.numpy(), reference_boxes_x2.numpy(),
        # reference_boxes_y1.numpy(), reference_boxes_y2.numpy())

  # implementation starts here
  ex_widths = proposals_x2 - proposals_x1
  ex_heights = proposals_y2 - proposals_y1
  ex_ctr_x = proposals_x1 + 0.5 * ex_widths
  ex_ctr_y = proposals_y1 + 0.5 * ex_heights
  # print('exw,exh,excx,excy:',ex_widths.numpy(),ex_heights.numpy(),ex_ctr_x.numpy(),ex_ctr_y.numpy())

  gt_widths = reference_boxes_x2 - reference_boxes_x1
  gt_heights = reference_boxes_y2 - reference_boxes_y1
  gt_ctr_x = reference_boxes_x1 + 0.5 * gt_widths
  gt_ctr_y = reference_boxes_y1 + 0.5 * gt_heights

  targets_dx = wx * (gt_ctr_x - ex_ctr_x) / ex_widths
  targets_dy = wy * (gt_ctr_y - ex_ctr_y) / ex_heights
  targets_dw = ww * (gt_widths / ex_widths).log()
  targets_dh = wh * (gt_heights / ex_heights).log()

  targets = Tensor.cat(*(targets_dx, targets_dy, targets_dw, targets_dh), dim=2)#.realize()
  # print('Encode BOX RETURN', targets.shape)
  return targets
def generate_anchors(input_size, grid_sizes, scales, aspect_ratios):
  assert len(scales) == len(aspect_ratios) == len(grid_sizes)
  anchors = []
  for s, ar, gs in zip(scales, aspect_ratios, grid_sizes):
    s, ar = np.array(s), np.array(ar)
    h_ratios = np.sqrt(ar)
    w_ratios = 1 / h_ratios
    ws = (w_ratios[:, None] * s[None, :]).reshape(-1)
    hs = (h_ratios[:, None] * s[None, :]).reshape(-1)
    base_anchors = (np.stack([-ws, -hs, ws, hs], axis=1) / 2).round()
    stride_h, stride_w = input_size[0] // gs[0], input_size[1] // gs[1]
    shifts_x, shifts_y = np.meshgrid(np.arange(gs[1]) * stride_w, np.arange(gs[0]) * stride_h)
    shifts_x = shifts_x.reshape(-1)
    shifts_y = shifts_y.reshape(-1)
    shifts = np.stack([shifts_x, shifts_y, shifts_x, shifts_y], axis=1, dtype=np.float32)
    anchors.append((shifts[:, None] + base_anchors[None, :]).reshape(-1, 4))
  return anchors
def cust_meshgrid(x:Tensor, y:Tensor):
  xs = x.shape[0]
  ys = y.shape[0]
  y = Tensor.stack([y]*xs)
  x = x.reshape(xs, 1).expand((xs,ys))
  return x, y
class AnchorGenerator:

  def __init__(
    self,
    sizes=((128, 256, 512),),
    aspect_ratios=((0.5, 1.0, 2.0),),
  ):
    if not isinstance(sizes[0], (list, tuple)):
      # TODO change this
      sizes = tuple((s,) for s in sizes)
    if not isinstance(aspect_ratios[0], (list, tuple)):
      aspect_ratios = (aspect_ratios,) * len(sizes)

    assert len(sizes) == len(aspect_ratios)

    self.sizes = sizes
    self.aspect_ratios = aspect_ratios
    self.cell_anchors = [self.generate_anchors(size, aspect_ratio)
                          for size, aspect_ratio in zip(sizes, aspect_ratios)]

  # TODO: https://github.com/pytorch/pytorch/issues/26792
  # For every (aspect_ratios, scales) combination, output a zero-centered anchor with those values.
  # (scales, aspect_ratios) are usually an element of zip(self.scales, self.aspect_ratios)
  # This method assumes aspect ratio = height / width for an anchor.
  def generate_anchors(self, scales: List[int], aspect_ratios: List[float], dtype=dtypes.float):
    scales = Tensor(list(scales), dtype=dtype)
    aspect_ratios = Tensor(list(aspect_ratios), dtype=dtype)
    h_ratios = aspect_ratios.sqrt()
    w_ratios = 1 / h_ratios
    ws = (w_ratios.unsqueeze(1) * scales.unsqueeze(0)).reshape(-1)  #.view(-1)
    hs = (h_ratios.unsqueeze(1) * scales.unsqueeze(0)).reshape(-1)  #.view(-1)
    base_anchors = Tensor.stack([-ws, -hs, ws, hs], dim=1) / 2
    return base_anchors.round()

  def set_cell_anchors(self, dtype):
    self.cell_anchors = [cell_anchor.cast(dtype)
                          for cell_anchor in self.cell_anchors]

  def num_anchors_per_location(self):
    return [len(s) * len(a) for s, a in zip(self.sizes, self.aspect_ratios)]

  # For every combination of (a, (g, s), i) in (self.cell_anchors, zip(grid_sizes, strides), 0:2),
  # output g[i] anchors that are s[i] distance apart in direction i, with the same dimensions as a.
  def grid_anchors(self, grid_sizes: List[List[int]], strides: List[List[Tensor]]) -> List[Tensor]:
    anchors = []
    cell_anchors = self.cell_anchors
    assert cell_anchors is not None

    if not (len(grid_sizes) == len(strides) == len(cell_anchors)):
      raise ValueError("Anchors should be Tuple[Tuple[int]] because each feature "
                        "map could potentially have different sizes and aspect ratios. "
                        "There needs to be a match between the number of "
                        "feature maps passed and the number of sizes / aspect ratios specified.")

    for size, stride, base_anchors in zip(
        grid_sizes, strides, cell_anchors
    ):
      grid_height, grid_width = size
      stride_height, stride_width = stride
      # device = base_anchors.device
      # For output anchor, compute [x_center, y_center, x_center, y_center]
      shifts_x = Tensor.arange(
          0, grid_width, dtype=dtypes.float) * stride_width
      shifts_y = Tensor.arange(
          0, grid_height, dtype=dtypes.float) * stride_height
      shift_y, shift_x = cust_meshgrid(shifts_y, shifts_x)
      shift_x = shift_x.reshape(-1)
      shift_y = shift_y.reshape(-1)
      shifts = Tensor.stack((shift_x, shift_y, shift_x, shift_y), dim=1)

      # For every (base anchor, output anchor) pair,
      # offset each zero-centered base anchor by the center of the output anchor.
      # print('trying anchor append')
      anchors.append(
        (shifts.reshape(-1, 1, 4) + base_anchors.reshape(1, -1, 4)).reshape(-1, 4)
      )
    return anchors

  def forward(self, image_list_shape, feature_maps) -> List[Tensor]:
    grid_sizes = [feature_map for feature_map in feature_maps]
    image_size = image_list_shape[-2:]
    # dtype = feature_maps[0].dtype
    strides = [[Tensor(image_size[0] // g[0], dtype=dtypes.int64),
                Tensor(image_size[1] // g[1], dtype=dtypes.int64)] for g in grid_sizes]
    # self.set_cell_anchors(dtype)
    anchors_over_all_feature_maps = self.grid_anchors(grid_sizes, strides)
    anchors: List[List[Tensor]] = []
    for _ in range(image_list_shape[0]):
      anchors_in_image = [anchors_per_feature_map for anchors_per_feature_map in anchors_over_all_feature_maps]
      anchors.append(anchors_in_image)
    anchors = [Tensor.cat(*anchors_per_image) for anchors_per_image in anchors]
    return anchors
  def __call__(self, image_list, feature_maps):
    return self.forward(image_list, feature_maps)


class RetinaNet:
  def __init__(self, backbone: ResNet, num_classes=264, num_anchors=9, scales=None, aspect_ratios=None,
               fg_iou_thresh=0.5, bg_iou_thresh=0.4):
    assert isinstance(backbone, ResNet)
    scales = tuple((i, int(i*2**(1/3)), int(i*2**(2/3))) for i in [32,  64, 128, 256, 512]) if scales is None else scales
    aspect_ratios = ((0.5, 1.0, 2.0),) * len(scales) if aspect_ratios is None else aspect_ratios
    self.num_anchors, self.num_classes = num_anchors, num_classes
    assert len(scales) == len(aspect_ratios) and all(self.num_anchors == len(s) * len(ar) for s, ar in zip(scales, aspect_ratios))

    self.backbone = ResNetFPN(backbone)
    self.head = RetinaHead(self.backbone.out_channels, num_anchors=num_anchors, num_classes=num_classes)
    self.anchor_gen = lambda input_size: generate_anchors(input_size, self.backbone.compute_grid_sizes(input_size), scales, aspect_ratios)

  def __call__(self, x, train=False):
    
    b = self.backbone(x)
    r, c = self.head(b)
    if train:
      return b, r, c
    else: 
      c = c.sigmoid().realize()
      return r.cat(c, dim=-1)
  def forward(self, x):
    return self.head(self.backbone(x))

  def load_from_pretrained(self):
    model_urls = {
      (50, 1, 64): "https://download.pytorch.org/models/retinanet_resnet50_fpn_coco-eeacb38b.pth",
      (50, 32, 4): "https://zenodo.org/record/6605272/files/retinanet_model_10.zip",
    }
    self.url = model_urls[(self.backbone.body.num, self.backbone.body.groups, self.backbone.body.base_width)]
    from torch.hub import load_state_dict_from_url
    state_dict = load_state_dict_from_url(self.url, progress=True, map_location='cpu')
    state_dict = state_dict['model'] if 'model' in state_dict.keys() else state_dict
    for k, v in state_dict.items():
      obj = get_child(self, k)
      dat = v.detach().numpy()
      assert obj.shape == dat.shape, (k, obj.shape, dat.shape)
      obj.assign(dat)
  def load_checkpoint(self, fn):
    d = safe_load(fn)
    load_state_dict(self, d)

  # predictions: (BS, (H1W1+...+HmWm)A, 4 + K)
  def postprocess_detections(self, predictions, input_size=(800, 800), image_sizes=None, orig_image_sizes=None, score_thresh=0.05, topk_candidates=1000, nms_thresh=0.5):
    anchors = self.anchor_gen(input_size)
    grid_sizes = self.backbone.compute_grid_sizes(input_size)
    split_idx = np.cumsum([int(self.num_anchors * sz[0] * sz[1]) for sz in grid_sizes[:-1]])
    detections = []
    for i, predictions_per_image in enumerate(predictions):
      h, w = input_size if image_sizes is None else image_sizes[i]

      predictions_per_image = np.split(predictions_per_image, split_idx)
      offsets_per_image = [br[:, :4] for br in predictions_per_image]
      scores_per_image = [cl[:, 4:] for cl in predictions_per_image]

      image_boxes, image_scores, image_labels = [], [], []
      for offsets_per_level, scores_per_level, anchors_per_level in zip(offsets_per_image, scores_per_image, anchors):
        # remove low scoring boxes
        scores_per_level = scores_per_level.flatten()
        keep_idxs = scores_per_level > score_thresh
        scores_per_level = scores_per_level[keep_idxs]

        # keep topk
        topk_idxs = np.where(keep_idxs)[0]
        num_topk = min(len(topk_idxs), topk_candidates)
        sort_idxs = scores_per_level.argsort()[-num_topk:][::-1]
        topk_idxs, scores_per_level = topk_idxs[sort_idxs], scores_per_level[sort_idxs]

        # bbox coords from offsets
        anchor_idxs = topk_idxs // self.num_classes
        labels_per_level = topk_idxs % self.num_classes
        boxes_per_level = decode_bbox(offsets_per_level[anchor_idxs], anchors_per_level[anchor_idxs])
        # clip to image size
        clipped_x = boxes_per_level[:, 0::2].clip(0, w)
        clipped_y = boxes_per_level[:, 1::2].clip(0, h)
        boxes_per_level = np.stack([clipped_x, clipped_y], axis=2).reshape(-1, 4)

        image_boxes.append(boxes_per_level)
        image_scores.append(scores_per_level)
        image_labels.append(labels_per_level)

      image_boxes = np.concatenate(image_boxes)
      image_scores = np.concatenate(image_scores)
      image_labels = np.concatenate(image_labels)

      # nms for each class
      keep_mask = np.zeros_like(image_scores, dtype=bool)
      for class_id in np.unique(image_labels):
        curr_indices = np.where(image_labels == class_id)[0]
        curr_keep_indices = nms(image_boxes[curr_indices], image_scores[curr_indices], nms_thresh)
        keep_mask[curr_indices[curr_keep_indices]] = True
      keep = np.where(keep_mask)[0]
      keep = keep[image_scores[keep].argsort()[::-1]]

      # resize bboxes back to original size
      image_boxes = image_boxes[keep]
      if orig_image_sizes is not None:
        resized_x = image_boxes[:, 0::2] * orig_image_sizes[i][1] / w
        resized_y = image_boxes[:, 1::2] * orig_image_sizes[i][0] / h
        image_boxes = np.stack([resized_x, resized_y], axis=2).reshape(-1, 4)
      # xywh format
      image_boxes = np.concatenate([image_boxes[:, :2], image_boxes[:, 2:] - image_boxes[:, :2]], axis=1)

      detections.append({"boxes":image_boxes, "scores":image_scores[keep], "labels":image_labels[keep]})
    return detections

class ClassificationHead:
  def __init__(self, in_channels, num_anchors, num_classes, prior_probability=0.01):
    self.num_classes = num_classes
    self.conv = []
    for _ in range(4):
      c = Conv2dNormal(in_channels, in_channels, kernel_size=3, padding=1)
      self.conv.append(c)
      self.conv.append(Tensor.relu)
    self.cls_logits = Conv2dNormal_prior_prob(in_channels, num_anchors * num_classes, kernel_size=3, padding=1)


  def __call__(self, x):
    out = [self.cls_logits(feat.sequential(self.conv)).permute(0, 2, 3, 1).reshape(feat.shape[0], -1, self.num_classes) for feat in x]
    return out[0].cat(*out[1:], dim=1)#.sigmoid()
  # @TinyJit
  def loss(self, logits_class, matched_idxs, labels_temp):
    batch_size = logits_class.shape[0]
    foreground_idxs = matched_idxs >= 0
    num_foreground = foreground_idxs.sum(-1)

    labels_temp = (labels_temp+1)*foreground_idxs-1
    # print('LAbels_temp:', labels_temp.shape)
    gt_classes_target = labels_temp.one_hot(logits_class.shape[-1])
    # print('gt_classes_target', gt_classes_target.shape)
    valid_idxs = matched_idxs != -2
    s = sigmoid_focal_loss(logits_class, 
                                       gt_classes_target, 
                                       valid_idxs.reshape(batch_size,-1,1)) #.realize()
    # print('sig_loss:', s.shape)
    a = s/num_foreground
    return a.mean()

class RegressionHead:
  def __init__(self, in_channels, num_anchors):
    self.conv = []
    for _ in range(4):
      c = Conv2dNormal(in_channels, in_channels, kernel_size=3, padding=1)
      self.conv.append(c)
      self.conv.append(Tensor.relu)
    self.bbox_reg = Conv2dNormal(in_channels, num_anchors * 4, kernel_size=3, padding=1)

    
  def __call__(self, x):
    out = [self.bbox_reg(feat.sequential(self.conv)).permute(0, 2, 3, 1).reshape(feat.shape[0], -1, 4) for feat in x]
    return out[0].cat(*out[1:], dim=1)

  def loss(self, logits_reg, anchors, matched_idxs,boxes_temp):
    batch_size = logits_reg.shape[0]
    foreground_idxs = matched_idxs >= 0
    num_foreground = foreground_idxs.sum(-1)

    matched_gt_boxes = boxes_temp * foreground_idxs.reshape(batch_size,-1,1)
    bbox_reg = logits_reg * foreground_idxs.reshape(batch_size,-1,1)
    anchors = anchors*foreground_idxs.reshape(batch_size,-1,1)
    target_regression = encode_boxes(matched_gt_boxes, anchors)
    target_regression = target_regression * foreground_idxs.reshape(batch_size,-1,1)
    # print('l1_LOSS', bbox_reg.shape, target_regression.shape, num_foreground.shape)
    l = l1_loss(
        bbox_reg,
        target_regression,
      ) / num_foreground
    # print('l1_POST',l.shape, l.numpy())
    return l.mean()

class RetinaHead:
  def __init__(self, in_channels, num_anchors, num_classes):
    self.classification_head = ClassificationHead(in_channels, num_anchors, num_classes)
    self.regression_head = RegressionHead(in_channels, num_anchors)
  def __call__(self, x):
    pred_bbox, pred_class = self.regression_head(x), self.classification_head(x)
    return pred_bbox, pred_class

class ResNetFPN:
  def __init__(self, resnet, out_channels=256, returned_layers=[2, 3, 4]):
    self.out_channels = out_channels
    self.body = resnet
    in_channels_list = [(self.body.in_planes // 8) * 2 ** (i - 1) for i in returned_layers]
    self.fpn = FPN(in_channels_list, out_channels)

  # this is needed to decouple inference from postprocessing (anchors generation)
  def compute_grid_sizes(self, input_size):
    return np.ceil(np.array(input_size)[None, :] / 2 ** np.arange(3, 8)[:, None])

  def __call__(self, x):
    out = self.body.bn1(self.body.conv1(x)).relu()
    out = out.pad2d([1,1,1,1]).max_pool2d((3,3), 2)
    out = out.sequential(self.body.layer1)
    p3 = out.sequential(self.body.layer2)
    p4 = p3.sequential(self.body.layer3)
    p5 = p4.sequential(self.body.layer4)
    return self.fpn([p3, p4, p5])

class ExtraFPNBlock:
  def __init__(self, in_channels, out_channels):
    self.p6 = Conv2dKaiming(in_channels, out_channels, kernel_size=3, stride=2, padding=1)
    self.p7 = Conv2dKaiming(out_channels, out_channels, kernel_size=3, stride=2, padding=1)
    self.use_P5 = in_channels == out_channels

  def __call__(self, p, c):
    p5, c5 = p[-1], c[-1]
    x = p5 if self.use_P5 else c5
    p6 = self.p6(x)
    p7 = self.p7(p6.relu())
    p.extend([p6, p7])
    return p

class FPN:
  def __init__(self, in_channels_list, out_channels, extra_blocks=None):
    self.inner_blocks, self.layer_blocks = [], []
    for in_channels in in_channels_list:
      c1 = Conv2dKaiming(in_channels, out_channels, kernel_size=1)
      self.inner_blocks.append(c1)
      c2 = Conv2dKaiming(out_channels, out_channels, kernel_size=3, padding=1)
      self.layer_blocks.append(c2)
    self.extra_blocks = ExtraFPNBlock(256, 256) if extra_blocks is None else extra_blocks

  def __call__(self, x):
    last_inner = self.inner_blocks[-1](x[-1])
    results = [self.layer_blocks[-1](last_inner)]
    for idx in range(len(x) - 2, -1, -1):
      inner_lateral = self.inner_blocks[idx](x[idx])

      # upsample to inner_lateral's shape
      (ih, iw), (oh, ow), prefix = last_inner.shape[-2:], inner_lateral.shape[-2:], last_inner.shape[:-2]
      eh, ew = math.ceil(oh / ih), math.ceil(ow / iw)
      inner_top_down = last_inner.reshape(*prefix, ih, 1, iw, 1).expand(*prefix, ih, eh, iw, ew).reshape(*prefix, ih*eh, iw*ew)[:, :, :oh, :ow]

      last_inner = inner_lateral + inner_top_down
      results.insert(0, self.layer_blocks[idx](last_inner))
    if self.extra_blocks is not None:
      results = self.extra_blocks(results, x)
    return results

if __name__ == "__main__":
  from extra.models.resnet import ResNeXt50_32X4D
  backbone = ResNeXt50_32X4D()
  retina = RetinaNet(backbone)
  retina.load_from_pretrained()