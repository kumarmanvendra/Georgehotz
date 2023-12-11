import re
import math
import os
import numpy as np
import torch
import pycocotools.mask as mask_utils
from pathlib import Path
from tinygrad import nn
from tinygrad.tensor import Tensor
from tinygrad.helpers import dtypes, get_child, fetch
from tinygrad.nn.state import torch_load
from tinygrad.shape.symbolic import Node
from typing import Tuple
from extra.models.resnet import ResNet
from extra.models.retinanet import nms as _box_nms

USE_NP_GATHER = os.getenv('FULL_TINYGRAD', '0') == '0'

def rint(tensor):
  x = (tensor*2).cast(dtypes.int32).contiguous().cast(dtypes.float32)/2
  return (x<0).where(x.floor(), x.ceil())

def nearest_interpolate(tensor, scale_factor):
  bs, c, py, px = tensor.shape
  return tensor.reshape(bs, c, py, 1, px, 1).expand(bs, c, py, scale_factor, px, scale_factor).reshape(bs, c, py * scale_factor, px * scale_factor)

def meshgrid(x, y):
  grid_x = Tensor.cat(*[x[idx:idx+1].expand(y.shape).unsqueeze(0) for idx in range(x.shape[0])])
  grid_y = Tensor.cat(*[y.unsqueeze(0)]*x.shape[0])
  return grid_x.reshape(-1, 1), grid_y.reshape(-1, 1)

def topk(input_, k, dim=-1, largest=True, sorted=False):
  k = min(k, input_.shape[dim]-1)
  input_ = input_.numpy()
  if largest: input_ *= -1
  ind = np.argpartition(input_, k, axis=dim)
  if largest: input_ *= -1
  ind = np.take(ind, np.arange(k), axis=dim) # k non-sorted indices
  input_ = np.take_along_axis(input_, ind, axis=dim) # k non-sorted values
  if not sorted: return Tensor(input_), ind
  if largest: input_ *= -1
  ind_part = np.argsort(input_, axis=dim)
  ind = np.take_along_axis(ind, ind_part, axis=dim)
  if largest: input_ *= -1
  val = np.take_along_axis(input_, ind_part, axis=dim)
  return Tensor(val), ind

# This is very slow for large arrays, or indices
def _gather(array, indices):
  indices = indices.float().to(array.device)
  reshape_arg = [1]*array.ndim + [array.shape[-1]]
  return Tensor.where(
    indices.unsqueeze(indices.ndim).expand(*indices.shape, array.shape[-1]) == Tensor.arange(array.shape[-1]).reshape(*reshape_arg).expand(*indices.shape, array.shape[-1]),
    array, 0,
  ).sum(indices.ndim)

# TODO: replace npgather with a faster gather using tinygrad only
# NOTE: this blocks the gradient
def npgather(array,indices):
  if isinstance(array, Tensor): array = array.numpy()
  if isinstance(indices, Tensor): indices = indices.numpy()
  if isinstance(indices, list): indices = np.asarray(indices)
  return Tensor(array[indices.astype(int)])

def get_strides(shape):
  prod = [1]
  for idx in range(len(shape)-1, -1, -1): prod.append(prod[-1] * shape[idx])
  # something about ints is broken with gpu, cuda
  return Tensor(prod[::-1][1:], dtype=dtypes.int32).unsqueeze(0).cpu()

# with keys as integer array for all axes
def tensor_getitem(tensor, *keys):
  # something about ints is broken with gpu, cuda
  flat_keys = Tensor.stack([key.expand((sum(keys)).shape).reshape(-1) for key in keys], dim=1).cpu().cast(dtypes.int32)
  strides = get_strides(tensor.shape)
  idxs = (flat_keys * strides).sum(1)
  gatherer = npgather if USE_NP_GATHER else _gather
  return gatherer(tensor.reshape(-1), idxs).reshape(sum(keys).shape)


# for gather with indicies only on axis=0
def tensor_gather(tensor, indices):
  if not isinstance(indices, Tensor):
    indices = Tensor(indices, requires_grad=False)
  if len(tensor.shape) > 2:
    rem_shape = list(tensor.shape)[1:]
    tensor = tensor.reshape(tensor.shape[0], -1)
  else:
    rem_shape = None
  if len(tensor.shape) > 1:
    tensor = tensor.T
    repeat_arg = [1]*(tensor.ndim-1) + [tensor.shape[-2]]
    indices = indices.unsqueeze(indices.ndim).repeat(repeat_arg)
    ret = _gather(tensor, indices)
    if rem_shape:
      ret = ret.reshape([indices.shape[0]] + rem_shape)
  else:
    ret = _gather(tensor, indices)
  del indices
  return ret


class LastLevelMaxPool:
  def __call__(self, x): return [Tensor.max_pool2d(x, 1, 2)]


# transpose
FLIP_LEFT_RIGHT = 0
FLIP_TOP_BOTTOM = 1


def permute_and_flatten(layer:Tensor, N, A, C, H, W):
  layer = layer.reshape(N, -1, C, H, W)
  layer = layer.permute(0, 3, 4, 1, 2)
  layer = layer.reshape(N, -1, C)
  return layer


class BoxList:
  def __init__(self, bbox, image_size, mode="xyxy"):
    if not isinstance(bbox, Tensor):
      bbox = Tensor(bbox)
    if bbox.ndim != 2:
      raise ValueError(
        "bbox should have 2 dimensions, got {}".format(bbox.ndim)
      )
    if bbox.shape[-1] != 4:
      raise ValueError(
        "last dimenion of bbox should have a "
        "size of 4, got {}".format(bbox.shape[-1])
      )
    if mode not in ("xyxy", "xywh"):
      raise ValueError("mode should be 'xyxy' or 'xywh'")

    self.bbox = bbox
    self.size = image_size  # (image_width, image_height)
    self.mode = mode
    self.extra_fields = {}

  def __repr__(self):
    s = self.__class__.__name__ + "("
    s += "num_boxes={}, ".format(len(self))
    s += "image_width={}, ".format(self.size[0])
    s += "image_height={}, ".format(self.size[1])
    s += "mode={})".format(self.mode)
    return s

  def area(self):
    box = self.bbox
    if self.mode == "xyxy":
      TO_REMOVE = 1
      area = (box[:, 2] - box[:, 0] + TO_REMOVE) * (box[:, 3] - box[:, 1] + TO_REMOVE)
    elif self.mode == "xywh":
      area = box[:, 2] * box[:, 3]
    return area

  def add_field(self, field, field_data):
    self.extra_fields[field] = field_data

  def get_field(self, field):
    return self.extra_fields[field]

  def has_field(self, field):
    return field in self.extra_fields

  def fields(self):
    return list(self.extra_fields.keys())

  def _copy_extra_fields(self, bbox):
    for k, v in bbox.extra_fields.items():
      self.extra_fields[k] = v

  def convert(self, mode):
    if mode == self.mode:
      return self
    xmin, ymin, xmax, ymax = self._split_into_xyxy()
    if mode == "xyxy":
      bbox = Tensor.cat(*(xmin, ymin, xmax, ymax), dim=-1)
      bbox = BoxList(bbox, self.size, mode=mode)
    else:
      TO_REMOVE = 1
      bbox = Tensor.cat(
        *(xmin, ymin, xmax - xmin + TO_REMOVE, ymax - ymin + TO_REMOVE), dim=-1
      )
      bbox = BoxList(bbox, self.size, mode=mode)
    bbox._copy_extra_fields(self)
    return bbox

  def _split_into_xyxy(self):
    if self.mode == "xyxy":
      xmin, ymin, xmax, ymax = self.bbox.chunk(4, dim=-1)
      return xmin, ymin, xmax, ymax
    if self.mode == "xywh":
      TO_REMOVE = 1
      xmin, ymin, w, h = self.bbox.chunk(4, dim=-1)
      return (
        xmin,
        ymin,
        xmin + (w - TO_REMOVE).maximum(0),
        ymin + (h - TO_REMOVE).maximum(0),
      )

  def resize(self, size, *args, **kwargs):
    ratios = tuple(float(s) / float(s_orig) for s, s_orig in zip(size, self.size))
    if ratios[0] == ratios[1]:
      ratio = ratios[0]
      scaled_box = self.bbox * ratio
      bbox = BoxList(scaled_box, size, mode=self.mode)
      for k, v in self.extra_fields.items():
        if not isinstance(v, Tensor):
          v = v.resize(size, *args, **kwargs)
        bbox.add_field(k, v)
      return bbox

    ratio_width, ratio_height = ratios
    xmin, ymin, xmax, ymax = self._split_into_xyxy()
    scaled_xmin = xmin * ratio_width
    scaled_xmax = xmax * ratio_width
    scaled_ymin = ymin * ratio_height
    scaled_ymax = ymax * ratio_height
    scaled_box = Tensor.cat(
      *(scaled_xmin, scaled_ymin, scaled_xmax, scaled_ymax), dim=-1
    )
    bbox = BoxList(scaled_box, size, mode="xyxy")
    for k, v in self.extra_fields.items():
      if not isinstance(v, Tensor):
        v = v.resize(size, *args, **kwargs)
      bbox.add_field(k, v)

    return bbox.convert(self.mode)

  def transpose(self, method):
    image_width, image_height = self.size
    xmin, ymin, xmax, ymax = self._split_into_xyxy()
    if method == FLIP_LEFT_RIGHT:
      TO_REMOVE = 1
      transposed_xmin = image_width - xmax - TO_REMOVE
      transposed_xmax = image_width - xmin - TO_REMOVE
      transposed_ymin = ymin
      transposed_ymax = ymax
    elif method == FLIP_TOP_BOTTOM:
      transposed_xmin = xmin
      transposed_xmax = xmax
      transposed_ymin = image_height - ymax
      transposed_ymax = image_height - ymin

    transposed_boxes = Tensor.cat(
      *(transposed_xmin, transposed_ymin, transposed_xmax, transposed_ymax), dim=-1
    )
    bbox = BoxList(transposed_boxes, self.size, mode="xyxy")
    for k, v in self.extra_fields.items():
      if not isinstance(v, Tensor):
        v = v.transpose(method)
      bbox.add_field(k, v)
    return bbox.convert(self.mode)

  def clip_to_image(self, remove_empty=True):
    TO_REMOVE = 1
    bb1 = self.bbox.clip(min_=0, max_=self.size[0] - TO_REMOVE)[:, 0]
    bb2 = self.bbox.clip(min_=0, max_=self.size[1] - TO_REMOVE)[:, 1]
    bb3 = self.bbox.clip(min_=0, max_=self.size[0] - TO_REMOVE)[:, 2]
    bb4 = self.bbox.clip(min_=0, max_=self.size[1] - TO_REMOVE)[:, 3]
    self.bbox = Tensor.stack((bb1, bb2, bb3, bb4), dim=1)
    # TODO: avoid using np
    if remove_empty:
      box = self.bbox.numpy()
      keep = (box[:, 3] > box[:, 1]) & (box[:, 2] > box[:, 0])
      return self[keep.tolist()]
    return self

  def __getitem__(self, item):
    if isinstance(item, list):
      if len(item) == 0:
        return []
      if sum(item) == len(item) and isinstance(item[0], bool):
        return self
    bbox = BoxList(tensor_gather(self.bbox, item), self.size, self.mode)
    try:
      for k, v in self.extra_fields.items():
        bbox.add_field(k, tensor_gather(v, item))
    except:
      return bbox
    return bbox

  def __len__(self):
    return self.bbox.shape[0]
  
  def copy_with_fields(self, fields, skip_missing=False):
    bbox = BoxList(self.bbox, self.size, self.mode)
    if not isinstance(fields, (list, tuple)):
      fields = [fields]
    for field in fields:
      if self.has_field(field):
        bbox.add_field(field, self.get_field(field))
      elif not skip_missing:
        raise KeyError("Field '{}' not found in {}".format(field, self))
    return bbox
  
  def crop(self, box):
    """
    Cropss a rectangular region from this bounding box. The box is a
    4-tuple defining the left, upper, right, and lower pixel
    coordinate.
    """
    xmin, ymin, xmax, ymax = self._split_into_xyxy()
    w, h = box[2] - box[0], box[3] - box[1]
    cropped_xmin = (xmin - box[0]).clamp(min=0, max=w)
    cropped_ymin = (ymin - box[1]).clamp(min=0, max=h)
    cropped_xmax = (xmax - box[0]).clamp(min=0, max=w)
    cropped_ymax = (ymax - box[1]).clamp(min=0, max=h)

    # TODO should I filter empty boxes here?
    if False:
        is_empty = (cropped_xmin == cropped_xmax) | (cropped_ymin == cropped_ymax)

    cropped_box = torch.cat(
        (cropped_xmin, cropped_ymin, cropped_xmax, cropped_ymax), dim=-1
    )
    bbox = BoxList(cropped_box, (w, h), mode="xyxy")
    # bbox._copy_extra_fields(self)
    for k, v in self.extra_fields.items():
        if not isinstance(v, torch.Tensor):
            v = v.crop(box)
        bbox.add_field(k, v)
    return bbox.convert(self.mode)
  
class Polygons(object):
  """
  This class holds a set of polygons that represents a single instance
  of an object mask. The object can be represented as a set of
  polygons
  """

  def __init__(self, polygons, size, mode):
    # assert isinstance(polygons, list), '{}'.format(polygons)
    if isinstance(polygons, list):
      polygons = [torch.as_tensor(p, dtype=torch.float32) for p in polygons]
    elif isinstance(polygons, Polygons):
      polygons = polygons.polygons

    self.polygons = polygons
    self.size = size
    self.mode = mode

  def transpose(self, method):
    if method not in (FLIP_LEFT_RIGHT, FLIP_TOP_BOTTOM):
      raise NotImplementedError(
          "Only FLIP_LEFT_RIGHT and FLIP_TOP_BOTTOM implemented"
      )

    flipped_polygons = []
    width, height = self.size
    if method == FLIP_LEFT_RIGHT:
      dim = width
      idx = 0
    elif method == FLIP_TOP_BOTTOM:
      dim = height
      idx = 1

    for poly in self.polygons:
      p = poly.clone()
      TO_REMOVE = 1
      p[idx::2] = dim - poly[idx::2] - TO_REMOVE
      flipped_polygons.append(p)

    return Polygons(flipped_polygons, size=self.size, mode=self.mode)

  def crop(self, box):
    w, h = box[2] - box[0], box[3] - box[1]

    # TODO chck if necessary
    w = max(w, 1)
    h = max(h, 1)

    cropped_polygons = []
    for poly in self.polygons:
      p = poly.clone()
      p[0::2] = p[0::2] - box[0]  # .clamp(min=0, max=w)
      p[1::2] = p[1::2] - box[1]  # .clamp(min=0, max=h)
      cropped_polygons.append(p)

    return Polygons(cropped_polygons, size=(w, h), mode=self.mode)

  def resize(self, size, *args, **kwargs):
    ratios = tuple(float(s) / float(s_orig) for s, s_orig in zip(size, self.size))
    if ratios[0] == ratios[1]:
        ratio = ratios[0]
        scaled_polys = [p * ratio for p in self.polygons]
        return Polygons(scaled_polys, size, mode=self.mode)

    ratio_w, ratio_h = ratios
    scaled_polygons = []
    for poly in self.polygons:
        p = poly.clone()
        p[0::2] *= ratio_w
        p[1::2] *= ratio_h
        scaled_polygons.append(p)

    return Polygons(scaled_polygons, size=size, mode=self.mode)

  def convert(self, mode):
    width, height = self.size
    if mode == "mask":
      rles = mask_utils.frPyObjects(
          [p.numpy() for p in self.polygons], height, width
      )
      rle = mask_utils.merge(rles)
      mask = mask_utils.decode(rle)
      mask = torch.from_numpy(mask)
      # TODO add squeeze?
      return mask

  def __repr__(self):
    s = self.__class__.__name__ + "("
    s += "num_polygons={}, ".format(len(self.polygons))
    s += "image_width={}, ".format(self.size[0])
    s += "image_height={}, ".format(self.size[1])
    s += "mode={})".format(self.mode)
    return s

class SegmentationMask:
  """
  This class stores the segmentations for all objects in the image
  """

  def __init__(self, polygons, size, mode=None):
    """
    Arguments:
      polygons: a list of list of lists of numbers. The first
          level of the list correspond to individual instances,
          the second level to all the polygons that compose the
          object, and the third level to the polygon coordinates.
    """
    assert isinstance(polygons, list)

    self.polygons = [Polygons(p, size, mode) for p in polygons]
    self.size = size
    self.mode = mode

  def transpose(self, method):
    if method not in (FLIP_LEFT_RIGHT, FLIP_TOP_BOTTOM):
        raise NotImplementedError(
            "Only FLIP_LEFT_RIGHT and FLIP_TOP_BOTTOM implemented"
        )

    flipped = []
    for polygon in self.polygons:
        flipped.append(polygon.transpose(method))
    return SegmentationMask(flipped, size=self.size, mode=self.mode)

  def crop(self, box):
    w, h = box[2] - box[0], box[3] - box[1]
    cropped = []
    for polygon in self.polygons:
      cropped.append(polygon.crop(box))
    return SegmentationMask(cropped, size=(w, h), mode=self.mode)

  def resize(self, size, *args, **kwargs):
    scaled = []
    for polygon in self.polygons:
      scaled.append(polygon.resize(size, *args, **kwargs))
    return SegmentationMask(scaled, size=size, mode=self.mode)

  def to(self, *args, **kwargs):
    return self

  def __getitem__(self, item):
    if isinstance(item, (int, slice)):
      selected_polygons = [self.polygons[item]]
    else:
      # advanced indexing on a single dimension
      selected_polygons = []
      if isinstance(item, torch.Tensor) and \
              (item.dtype == torch.uint8 or item.dtype == torch.bool):
        item = item.nonzero()
        item = item.squeeze(1) if item.numel() > 0 else item
        item = item.tolist()
      for i in item:
        selected_polygons.append(self.polygons[i])
    return SegmentationMask(selected_polygons, size=self.size, mode=self.mode)

  def __iter__(self):
    return iter(self.polygons)

  def __repr__(self):
    s = self.__class__.__name__ + "("
    s += "num_instances={}, ".format(len(self.polygons))
    s += "image_width={}, ".format(self.size[0])
    s += "image_height={})".format(self.size[1])
    return s

def cat_boxlist(bboxes):
  size = bboxes[0].size
  mode = bboxes[0].mode
  fields = set(bboxes[0].fields())
  cat_box_list = [bbox.bbox for bbox in bboxes if bbox.bbox.shape[0] > 0]

  if len(cat_box_list) > 0:
    cat_boxes = BoxList(Tensor.cat(*cat_box_list, dim=0), size, mode)
  else:
    cat_boxes = BoxList(bboxes[0].bbox, size, mode)
  for field in fields:
    cat_field_list = [bbox.get_field(field) for bbox in bboxes if bbox.get_field(field).shape[0] > 0]

    if len(cat_box_list) > 0:
      data = Tensor.cat(*cat_field_list, dim=0)
    else:
      data = bboxes[0].get_field(field)

    cat_boxes.add_field(field, data)

  return cat_boxes


class FPN:
  def __init__(self, in_channels_list, out_channels):
    self.inner_blocks, self.layer_blocks = [], []
    for in_channels in in_channels_list:
      self.inner_blocks.append(nn.Conv2d(in_channels, out_channels, kernel_size=1))
      self.layer_blocks.append(nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1))
    self.top_block = LastLevelMaxPool()

  def __call__(self, x: Tensor):
    last_inner = self.inner_blocks[-1](x[-1])
    results = []
    results.append(self.layer_blocks[-1](last_inner))
    for feature, inner_block, layer_block in zip(
            x[:-1][::-1], self.inner_blocks[:-1][::-1], self.layer_blocks[:-1][::-1]
    ):
      if not inner_block:
        continue
      inner_top_down = nearest_interpolate(last_inner, scale_factor=2)
      inner_lateral = inner_block(feature)
      last_inner = inner_lateral + inner_top_down
      layer_result = layer_block(last_inner)
      results.insert(0, layer_result)
    last_results = self.top_block(results[-1])
    results.extend(last_results)

    return tuple(results)

class ResNetFPN:
  def __init__(self, resnet, out_channels=256, training=True):
    self.out_channels = out_channels
    self.training = training
    self.body = resnet
    in_channels_stage2 = 256
    in_channels_list = [
      in_channels_stage2,
      in_channels_stage2 * 2,
      in_channels_stage2 * 4,
      in_channels_stage2 * 8,
    ]
    self.fpn = FPN(in_channels_list, out_channels)

  def __call__(self, x):
    # NOTE: this ensures that `BatchNorm2d` behaves similar to mlperf's implementation of FrozenBatchNorm2d
    with Tensor.train(val=not self.training): x = self.body(x)
    return self.fpn(x)

class AnchorGenerator:
  def __init__(
          self,
          sizes=(32, 64, 128, 256, 512),
          aspect_ratios=(0.5, 1.0, 2.0),
          anchor_strides=(4, 8, 16, 32, 64),
          straddle_thresh=0,
  ):
    if len(anchor_strides) == 1:
      anchor_stride = anchor_strides[0]
      cell_anchors = [
        generate_anchors(anchor_stride, sizes, aspect_ratios)
      ]
    else:
      if len(anchor_strides) != len(sizes):
        raise RuntimeError("FPN should have #anchor_strides == #sizes")

      cell_anchors = [
        generate_anchors(
          anchor_stride,
          size if isinstance(size, (tuple, list)) else (size,),
          aspect_ratios
        )
        for anchor_stride, size in zip(anchor_strides, sizes)
      ]
    self.strides = anchor_strides
    self.cell_anchors = cell_anchors
    self.straddle_thresh = straddle_thresh

  def num_anchors_per_location(self):
    return [cell_anchors.shape[0] for cell_anchors in self.cell_anchors]

  def grid_anchors(self, grid_sizes):
    anchors = []
    for size, stride, base_anchors in zip(
            grid_sizes, self.strides, self.cell_anchors
    ):
      grid_height, grid_width = size
      device = base_anchors.device
      shifts_x = Tensor.arange(
        start=0, stop=grid_width * stride, step=stride, dtype=dtypes.float32, device=device
      )
      shifts_y = Tensor.arange(
        start=0, stop=grid_height * stride, step=stride, dtype=dtypes.float32, device=device
      )
      shift_y, shift_x = meshgrid(shifts_y, shifts_x)
      shift_x = shift_x.reshape(-1)
      shift_y = shift_y.reshape(-1)
      shifts = Tensor.stack((shift_x, shift_y, shift_x, shift_y), dim=1)

      anchors.append(
        (shifts.reshape(-1, 1, 4) + base_anchors.reshape(1, -1, 4)).reshape(-1, 4)
      )

    return anchors

  def add_visibility_to(self, boxlist):
    image_width, image_height = boxlist.size
    anchors = boxlist.bbox
    if self.straddle_thresh >= 0:
      inds_inside = (
              (anchors[:, 0] >= -self.straddle_thresh)
              * (anchors[:, 1] >= -self.straddle_thresh)
              * (anchors[:, 2] < image_width + self.straddle_thresh)
              * (anchors[:, 3] < image_height + self.straddle_thresh)
      )
    else:
      device = anchors.device
      inds_inside = Tensor.ones(anchors.shape[0], dtype=dtypes.uint8, device=device)
    boxlist.add_field("visibility", inds_inside)

  def __call__(self, image_list, feature_maps):
    grid_sizes = [feature_map.shape[-2:] for feature_map in feature_maps]
    anchors_over_all_feature_maps = self.grid_anchors(grid_sizes)
    anchors = []
    for (image_height, image_width) in image_list.image_sizes:
      anchors_in_image = []
      for anchors_per_feature_map in anchors_over_all_feature_maps:
        boxlist = BoxList(
          anchors_per_feature_map, (image_width, image_height), mode="xyxy"
        )
        self.add_visibility_to(boxlist)
        anchors_in_image.append(boxlist)
      anchors.append(anchors_in_image)
    return anchors


def generate_anchors(
    stride=16, sizes=(32, 64, 128, 256, 512), aspect_ratios=(0.5, 1, 2)
):
  return _generate_anchors(stride, Tensor(list(sizes)) / stride, Tensor(list(aspect_ratios)))


def _generate_anchors(base_size, scales, aspect_ratios):
  anchor = Tensor([1, 1, base_size, base_size]) - 1
  anchors = _ratio_enum(anchor, aspect_ratios)
  anchors = Tensor.cat(
    *[_scale_enum(anchors[i, :], scales).reshape(-1, 4) for i in range(anchors.shape[0])]
  )
  return anchors


def _whctrs(anchor):
  w = anchor[2] - anchor[0] + 1
  h = anchor[3] - anchor[1] + 1
  x_ctr = anchor[0] + 0.5 * (w - 1)
  y_ctr = anchor[1] + 0.5 * (h - 1)
  return w, h, x_ctr, y_ctr


def _mkanchors(ws, hs, x_ctr, y_ctr):
  ws = ws[:, None]
  hs = hs[:, None]
  anchors = Tensor.cat(*(
    x_ctr - 0.5 * (ws - 1),
    y_ctr - 0.5 * (hs - 1),
    x_ctr + 0.5 * (ws - 1),
    y_ctr + 0.5 * (hs - 1),
  ), dim=1)
  return anchors


def _ratio_enum(anchor, ratios):
  w, h, x_ctr, y_ctr = _whctrs(anchor)
  size = w * h
  size_ratios = size / ratios
  ws = rint(Tensor.sqrt(size_ratios))
  hs = rint(ws * ratios)
  anchors = _mkanchors(ws, hs, x_ctr, y_ctr)
  return anchors


def _scale_enum(anchor, scales):
  w, h, x_ctr, y_ctr = _whctrs(anchor)
  ws = w * scales
  hs = h * scales
  anchors = _mkanchors(ws, hs, x_ctr, y_ctr)
  return anchors


class RPNHead:
  def __init__(self, in_channels, num_anchors):
    self.conv = nn.Conv2d(in_channels, 256, kernel_size=3, padding=1)
    self.cls_logits = nn.Conv2d(256, num_anchors, kernel_size=1)
    self.bbox_pred = nn.Conv2d(256, num_anchors * 4, kernel_size=1)

  def __call__(self, x):
    logits = []
    bbox_reg = []
    for feature in x:
      t = Tensor.relu(self.conv(feature))
      logits.append(self.cls_logits(t))
      bbox_reg.append(self.bbox_pred(t))
    return logits, bbox_reg

class BoxCoder(object):
  def __init__(self, weights, bbox_xform_clip=math.log(1000. / 16)):
    self.weights = weights
    self.bbox_xform_clip = bbox_xform_clip

  def encode(self, reference_boxes, proposals):
    TO_REMOVE = 1  # TODO remove
    ex_widths = proposals[:, 2] - proposals[:, 0] + TO_REMOVE
    ex_heights = proposals[:, 3] - proposals[:, 1] + TO_REMOVE
    ex_ctr_x = proposals[:, 0] + 0.5 * ex_widths
    ex_ctr_y = proposals[:, 1] + 0.5 * ex_heights

    gt_widths = reference_boxes[:, 2] - reference_boxes[:, 0] + TO_REMOVE
    gt_heights = reference_boxes[:, 3] - reference_boxes[:, 1] + TO_REMOVE
    gt_ctr_x = reference_boxes[:, 0] + 0.5 * gt_widths
    gt_ctr_y = reference_boxes[:, 1] + 0.5 * gt_heights

    wx, wy, ww, wh = self.weights
    targets_dx = wx * (gt_ctr_x - ex_ctr_x) / ex_widths
    targets_dy = wy * (gt_ctr_y - ex_ctr_y) / ex_heights
    targets_dw = ww * Tensor.log(gt_widths / ex_widths)
    targets_dh = wh * Tensor.log(gt_heights / ex_heights)

    targets = Tensor.stack((targets_dx, targets_dy, targets_dw, targets_dh), dim=1)
    return targets

  def decode(self, rel_codes, boxes):
    boxes = boxes.cast(rel_codes.dtype)
    rel_codes = rel_codes

    TO_REMOVE = 1  # TODO remove
    widths = boxes[:, 2] - boxes[:, 0] + TO_REMOVE
    heights = boxes[:, 3] - boxes[:, 1] + TO_REMOVE
    ctr_x = boxes[:, 0] + 0.5 * widths
    ctr_y = boxes[:, 1] + 0.5 * heights

    wx, wy, ww, wh = self.weights
    dx = rel_codes[:, 0::4] / wx
    dy = rel_codes[:, 1::4] / wy
    dw = rel_codes[:, 2::4] / ww
    dh = rel_codes[:, 3::4] / wh

    # Prevent sending too large values into Tensor.exp()
    dw = dw.clip(min_=dw.min(), max_=self.bbox_xform_clip)
    dh = dh.clip(min_=dh.min(), max_=self.bbox_xform_clip)

    pred_ctr_x = dx * widths[:, None] + ctr_x[:, None]
    pred_ctr_y = dy * heights[:, None] + ctr_y[:, None]
    pred_w = dw.exp() * widths[:, None]
    pred_h = dh.exp() * heights[:, None]
    x = pred_ctr_x - 0.5 * pred_w
    y = pred_ctr_y - 0.5 * pred_h
    w = pred_ctr_x + 0.5 * pred_w - 1
    h = pred_ctr_y + 0.5 * pred_h - 1
    pred_boxes = Tensor.stack([x, y, w, h]).permute(1,2,0).reshape(rel_codes.shape[0], rel_codes.shape[1])
    return pred_boxes


def boxlist_nms(boxlist, nms_thresh, max_proposals=-1, score_field="scores"):
  if nms_thresh <= 0:
    return boxlist
  mode = boxlist.mode
  boxlist = boxlist.convert("xyxy")
  boxes = boxlist.bbox
  score = boxlist.get_field(score_field)
  keep = _box_nms(boxes.numpy(), score.numpy(), nms_thresh)
  if max_proposals > 0:
    keep = keep[:max_proposals]
  boxlist = boxlist[keep]
  return boxlist.convert(mode)


def remove_small_boxes(boxlist, min_size):
  xywh_boxes = boxlist.convert("xywh").bbox
  _, _, ws, hs = xywh_boxes.chunk(4, dim=1)
  keep = ((
          (ws >= min_size) * (hs >= min_size)
  ) > 0).reshape(-1)
  if keep.sum().numpy() == len(boxlist):
    return boxlist
  else:
    keep = keep.numpy().nonzero()[0]
  return boxlist[keep]


class RPNPostProcessor:
  # Not used in Loss calculation
  def __init__(
          self,
          pre_nms_top_n,
          post_nms_top_n,
          nms_thresh,
          min_size,
          box_coder=None,
          fpn_post_nms_top_n=None,
  ):
    self.pre_nms_top_n = pre_nms_top_n
    self.post_nms_top_n = post_nms_top_n
    self.nms_thresh = nms_thresh
    self.min_size = min_size

    if box_coder is None:
      box_coder = BoxCoder(weights=(1.0, 1.0, 1.0, 1.0))
    self.box_coder = box_coder

    if fpn_post_nms_top_n is None:
      fpn_post_nms_top_n = post_nms_top_n
    self.fpn_post_nms_top_n = fpn_post_nms_top_n

  def forward_for_single_feature_map(self, anchors, objectness, box_regression):
    device = objectness.device
    N, A, H, W = objectness.shape
    objectness = permute_and_flatten(objectness, N, A, 1, H, W).reshape(N, -1)
    objectness = objectness.sigmoid()

    box_regression = permute_and_flatten(box_regression, N, A, 4, H, W)

    num_anchors = A * H * W

    pre_nms_top_n = min(self.pre_nms_top_n, num_anchors)
    objectness, topk_idx = topk(objectness, pre_nms_top_n, dim=1, sorted=False)
    concat_anchors = Tensor.cat(*[a.bbox for a in anchors], dim=0).reshape(N, -1, 4)
    image_shapes = [box.size for box in anchors]

    box_regression_list = []
    concat_anchors_list = []
    for batch_idx in range(N):
      box_regression_list.append(tensor_gather(box_regression[batch_idx], topk_idx[batch_idx]))
      concat_anchors_list.append(tensor_gather(concat_anchors[batch_idx], topk_idx[batch_idx]))

    box_regression = Tensor.stack(box_regression_list)
    concat_anchors = Tensor.stack(concat_anchors_list)

    proposals = self.box_coder.decode(
      box_regression.reshape(-1, 4), concat_anchors.reshape(-1, 4)
    )

    proposals = proposals.reshape(N, -1, 4)

    result = []
    for proposal, score, im_shape in zip(proposals, objectness, image_shapes):
      boxlist = BoxList(proposal, im_shape, mode="xyxy")
      boxlist.add_field("objectness", score)
      boxlist = boxlist.clip_to_image(remove_empty=False)
      boxlist = remove_small_boxes(boxlist, self.min_size)
      boxlist = boxlist_nms(
        boxlist,
        self.nms_thresh,
        max_proposals=self.post_nms_top_n,
        score_field="objectness",
      )
      result.append(boxlist)
    return result

  def __call__(self, anchors, objectness, box_regression, targets=None):
    sampled_boxes = []
    num_levels = len(objectness)
    anchors = list(zip(*anchors))
    for a, o, b in zip(anchors, objectness, box_regression):
      sampled_boxes.append(self.forward_for_single_feature_map(a, o, b))

    boxlists = list(zip(*sampled_boxes))
    boxlists = [cat_boxlist(boxlist) for boxlist in boxlists]

    if num_levels > 1:
      boxlists = self.select_over_all_levels(boxlists)

    if targets is not None:
      boxlists = self.add_gt_proposals(boxlists, targets)

    return boxlists

  def select_over_all_levels(self, boxlists):
    num_images = len(boxlists)
    for i in range(num_images):
      objectness = boxlists[i].get_field("objectness")
      post_nms_top_n = min(self.fpn_post_nms_top_n, objectness.shape[0])
      _, inds_sorted = topk(objectness,
        post_nms_top_n, dim=0, sorted=False
      )
      boxlists[i] = boxlists[i][inds_sorted]
    return boxlists
 
  def add_gt_proposals(self, proposals, targets):
    gt_boxes = [target.copy_with_fields([]) for target in targets]

    # later cat of bbox requires all fields to be present for all bbox
    # so we need to add a dummy for objectness that's missing
    for gt_box in gt_boxes:
      gt_box.add_field("objectness", Tensor.ones(len(gt_box)))

    proposals = [cat_boxlist((proposal, gt_box)) for proposal, gt_box in zip(proposals, gt_boxes)]
    return proposals


class RPN:
  def __init__(self, in_channels):
    self.anchor_generator = AnchorGenerator()

    in_channels = 256
    head = RPNHead(
      in_channels, self.anchor_generator.num_anchors_per_location()[0]
    )
    rpn_box_coder = BoxCoder(weights=(1.0, 1.0, 1.0, 1.0))
    box_selector_train = RPNPostProcessor(
      pre_nms_top_n=2000,
      post_nms_top_n=2000,
      nms_thresh=0.7,
      min_size=0,
      box_coder=rpn_box_coder,
      fpn_post_nms_top_n=2000
    )
    box_selector_test = RPNPostProcessor(
        pre_nms_top_n=1000,
        post_nms_top_n=1000,
        nms_thresh=0.7,
        min_size=0,
        box_coder=rpn_box_coder,
        fpn_post_nms_top_n=1000
    )
    self.head = head
    self.box_selector_train = box_selector_train
    self.box_selector_test = box_selector_test
    self.loss_evaluator = self.create_loss_evaluator(rpn_box_coder)
    # TODO: create loss_evaluator here

  def __call__(self, images, features, targets=None):
    objectness, rpn_box_regression = self.head(features)
    anchors = self.anchor_generator(images, features)
    if targets is not None:
      with Tensor.train(val=False):
        boxes = self.box_selector_train(anchors, objectness, rpn_box_regression, targets=targets)

      loss_objectness, loss_rpn_box_neg = self.loss_evaluator(anchors, objectness, rpn_box_regression, targets)
      return boxes, {"loss_objectness": loss_objectness, "loss_rpn_box_neg": loss_rpn_box_neg}
    else:
      boxes = self.box_selector_test(anchors, objectness, rpn_box_regression)
      return boxes, {}
    
  def create_loss_evaluator(self, rpn_box_coder):
    matcher = Matcher(0.7, 0.3, allow_low_quality_matches=True)
    fg_bg_sampler = BalancedPositiveNegativeSampler(256, 0.5)
    return RPNLossComputation(matcher, fg_bg_sampler, rpn_box_coder, generate_rpn_labels)


def make_conv3x3(
  in_channels,
  out_channels,
  dilation=1,
  stride=1,
  use_gn=False,
):
  conv = nn.Conv2d(
    in_channels,
    out_channels,
    kernel_size=3,
    stride=stride,
    padding=dilation,
    dilation=dilation,
    bias=False if use_gn else True
  )
  return conv


class MaskRCNNFPNFeatureExtractor:
  def __init__(self):
    resolution = 14
    scales = (0.25, 0.125, 0.0625, 0.03125)
    sampling_ratio = 2
    pooler = Pooler(
      output_size=(resolution, resolution),
      scales=scales,
      sampling_ratio=sampling_ratio,
    )
    input_size = 256
    self.pooler = pooler

    use_gn = False
    layers = (256, 256, 256, 256)
    dilation = 1
    self.mask_fcn1 = make_conv3x3(input_size, layers[0], dilation=dilation, stride=1, use_gn=use_gn)
    self.mask_fcn2 = make_conv3x3(layers[0], layers[1], dilation=dilation, stride=1, use_gn=use_gn)
    self.mask_fcn3 = make_conv3x3(layers[1], layers[2], dilation=dilation, stride=1, use_gn=use_gn)
    self.mask_fcn4 = make_conv3x3(layers[2], layers[3], dilation=dilation, stride=1, use_gn=use_gn)
    self.blocks = [self.mask_fcn1, self.mask_fcn2, self.mask_fcn3, self.mask_fcn4]

  def __call__(self, x, proposals):
    x = self.pooler(x, proposals)
    for layer in self.blocks:
      if x is not None:
        x = Tensor.relu(layer(x))
    return x


class MaskRCNNC4Predictor:
  def __init__(self):
    num_classes = 81
    dim_reduced = 256
    num_inputs = dim_reduced
    self.conv5_mask = nn.ConvTranspose2d(num_inputs, dim_reduced, 2, 2, 0)
    self.mask_fcn_logits = nn.Conv2d(dim_reduced, num_classes, 1, 1, 0)

  def __call__(self, x):
    x = Tensor.relu(self.conv5_mask(x))
    return self.mask_fcn_logits(x)


class FPN2MLPFeatureExtractor:
  def __init__(self, cfg):
    resolution = 7
    scales = (0.25, 0.125, 0.0625, 0.03125)
    sampling_ratio = 2
    pooler = Pooler(
      output_size=(resolution, resolution),
      scales=scales,
      sampling_ratio=sampling_ratio,
    )
    input_size = 256 * resolution ** 2
    representation_size = 1024
    self.pooler = pooler
    self.fc6 = nn.Linear(input_size, representation_size)
    self.fc7 = nn.Linear(representation_size, representation_size)

  def __call__(self, x, proposals):
    x = self.pooler(x, proposals)
    x = x.reshape(x.shape[0], -1)
    x = Tensor.relu(self.fc6(x))
    x = Tensor.relu(self.fc7(x))
    return x


def _bilinear_interpolate(
  input,  # [N, C, H, W]
  roi_batch_ind,  # [K]
  y,  # [K, PH, IY]
  x,  # [K, PW, IX]
  ymask,  # [K, IY]
  xmask,  # [K, IX]
):
  _, channels, height, width = input.shape
  y = y.clip(min_=0.0, max_=float(height-1))
  x = x.clip(min_=0.0, max_=float(width-1))

  # Tensor.where doesnt work well with int32 data so cast to float32
  y_low = y.cast(dtypes.int32).contiguous().float().contiguous()
  x_low = x.cast(dtypes.int32).contiguous().float().contiguous()

  y_high = Tensor.where(y_low >= height - 1, float(height - 1), y_low + 1)
  y_low = Tensor.where(y_low >= height - 1, float(height - 1), y_low)

  x_high = Tensor.where(x_low >= width - 1, float(width - 1), x_low + 1)
  x_low = Tensor.where(x_low >= width - 1, float(width - 1), x_low)

  ly = y - y_low
  lx = x - x_low
  hy = 1.0 - ly
  hx = 1.0 - lx

  def masked_index(
    y,  # [K, PH, IY]
    x,  # [K, PW, IX]
  ):
    if ymask is not None:
      assert xmask is not None
      y = Tensor.where(ymask[:, None, :], y, 0)
      x = Tensor.where(xmask[:, None, :], x, 0)
    key1 = roi_batch_ind[:, None, None, None, None, None]
    key2 = Tensor.arange(channels, device=input.device)[None, :, None, None, None, None]
    key3 = y[:, None, :, None, :, None]
    key4 = x[:, None, None, :, None, :]
    return tensor_getitem(input,key1,key2,key3,key4)  # [K, C, PH, PW, IY, IX]

  v1 = masked_index(y_low, x_low)
  v2 = masked_index(y_low, x_high)
  v3 = masked_index(y_high, x_low)
  v4 = masked_index(y_high, x_high)

  # all ws preemptively [K, C, PH, PW, IY, IX]
  def outer_prod(y, x):
    return y[:, None, :, None, :, None] * x[:, None, None, :, None, :]

  w1 = outer_prod(hy, hx)
  w2 = outer_prod(hy, lx)
  w3 = outer_prod(ly, hx)
  w4 = outer_prod(ly, lx)

  val = w1*v1 + w2*v2 + w3*v3 + w4*v4
  return val

#https://pytorch.org/vision/main/_modules/torchvision/ops/roi_align.html#roi_align
def _roi_align(input, rois, spatial_scale, pooled_height, pooled_width, sampling_ratio, aligned):
  orig_dtype = input.dtype
  _, _, height, width = input.shape
  ph = Tensor.arange(pooled_height, device=input.device)
  pw = Tensor.arange(pooled_width, device=input.device)

  roi_batch_ind = rois[:, 0].cast(dtypes.int32).contiguous()
  offset = 0.5 if aligned else 0.0
  roi_start_w = rois[:, 1] * spatial_scale - offset
  roi_start_h = rois[:, 2] * spatial_scale - offset
  roi_end_w = rois[:, 3] * spatial_scale - offset
  roi_end_h = rois[:, 4] * spatial_scale - offset

  roi_width = roi_end_w - roi_start_w
  roi_height = roi_end_h - roi_start_h
  if not aligned:
    roi_width = roi_width.maximum(1.0)
    roi_height = roi_height.maximum(1.0)

  bin_size_h = roi_height / pooled_height
  bin_size_w = roi_width / pooled_width

  exact_sampling = sampling_ratio > 0
  roi_bin_grid_h = sampling_ratio if exact_sampling else (roi_height / pooled_height).ceil()
  roi_bin_grid_w = sampling_ratio if exact_sampling else (roi_width / pooled_width).ceil()

  if exact_sampling:
    count = max(roi_bin_grid_h * roi_bin_grid_w, 1)
    iy = Tensor.arange(roi_bin_grid_h, device=input.device)
    ix = Tensor.arange(roi_bin_grid_w, device=input.device)
    ymask = None
    xmask = None
  else:
    count = (roi_bin_grid_h * roi_bin_grid_w).maximum(1)
    iy = Tensor.arange(height, device=input.device)
    ix = Tensor.arange(width, device=input.device)
    ymask = iy[None, :] < roi_bin_grid_h[:, None]
    xmask = ix[None, :] < roi_bin_grid_w[:, None]

  def from_K(t):
    return t[:, None, None]

  y = (
    from_K(roi_start_h)
    + ph[None, :, None] * from_K(bin_size_h)
    + (iy[None, None, :] + 0.5) * from_K(bin_size_h / roi_bin_grid_h)
  )
  x = (
    from_K(roi_start_w)
    + pw[None, :, None] * from_K(bin_size_w)
    + (ix[None, None, :] + 0.5) * from_K(bin_size_w / roi_bin_grid_w)
  )

  val = _bilinear_interpolate(input, roi_batch_ind, y, x, ymask, xmask)
  if not exact_sampling:
    val = ymask[:, None, None, None, :, None].where(val, 0)
    val = xmask[:, None, None, None, None, :].where(val, 0)

  output = val.sum((-1, -2))
  if isinstance(count, Tensor):
    output = output / count[:, None, None, None]
  else:
    output = output / count

  output = output.cast(orig_dtype)
  return output

class ROIAlign:
  def __init__(self, output_size, spatial_scale, sampling_ratio):
    self.output_size = output_size
    self.spatial_scale = spatial_scale
    self.sampling_ratio = sampling_ratio

  def __call__(self, input, rois):
    output = _roi_align(
      input, rois, self.spatial_scale, self.output_size[0], self.output_size[1], self.sampling_ratio, aligned=False
    )
    return output


class LevelMapper:
  def __init__(self, k_min, k_max, canonical_scale=224, canonical_level=4, eps=1e-6):
    self.k_min = k_min
    self.k_max = k_max
    self.s0 = canonical_scale
    self.lvl0 = canonical_level
    self.eps = eps

  def __call__(self, boxlists):
    s = Tensor.sqrt(Tensor.cat(*[boxlist.area() for boxlist in boxlists]))
    target_lvls = (self.lvl0 + Tensor.log2(s / self.s0 + self.eps)).floor()
    target_lvls = target_lvls.clip(min_=self.k_min, max_=self.k_max)
    return target_lvls - self.k_min


class Pooler:
  def __init__(self, output_size, scales, sampling_ratio):
    self.output_size = output_size
    self.scales = scales
    self.sampling_ratio = sampling_ratio
    poolers = []
    for scale in scales:
      poolers.append(
        ROIAlign(
          output_size, spatial_scale=scale, sampling_ratio=sampling_ratio
        )
      )
    self.poolers = poolers
    self.output_size = output_size
    lvl_min = -math.log2(scales[0])
    lvl_max = -math.log2(scales[-1])
    self.map_levels = LevelMapper(lvl_min, lvl_max)

  def convert_to_roi_format(self, boxes):
    concat_boxes = Tensor.cat(*[b.bbox for b in boxes], dim=0)
    device, dtype = concat_boxes.device, concat_boxes.dtype
    ids = Tensor.cat(
      *[
        Tensor.full((len(b), 1), i, dtype=dtype, device=device)
        for i, b in enumerate(boxes)
      ],
      dim=0,
    )
    if concat_boxes.shape[0] != 0:
      rois = Tensor.cat(*[ids, concat_boxes], dim=1)
      return rois

  def __call__(self, x, boxes):
    num_levels = len(self.poolers)
    rois = self.convert_to_roi_format(boxes)
    if rois:
      if num_levels == 1:
        return self.poolers[0](x[0], rois)

      levels = self.map_levels(boxes)
      results = []
      all_idxs = []
      for level, (per_level_feature, pooler) in enumerate(zip(x, self.poolers)):
        # this is fine because no grad will flow through index
        idx_in_level = (levels.numpy() == level).nonzero()[0]
        if len(idx_in_level) > 0:
          rois_per_level = tensor_gather(rois, idx_in_level)
          pooler_output = pooler(per_level_feature, rois_per_level)
          all_idxs.extend(idx_in_level)
          results.append(pooler_output)

      return tensor_gather(Tensor.cat(*results), [x[0] for x in sorted({i:idx for i, idx in enumerate(all_idxs)}.items(), key=lambda x: x[1])])


class FPNPredictor:
  def __init__(self):
    num_classes = 81
    representation_size = 1024
    self.cls_score = nn.Linear(representation_size, num_classes)
    num_bbox_reg_classes = num_classes
    self.bbox_pred = nn.Linear(representation_size, num_bbox_reg_classes * 4)

  def __call__(self, x):
    scores = self.cls_score(x)
    bbox_deltas = self.bbox_pred(x)
    return scores, bbox_deltas


class PostProcessor:
  # Not used in training
  def __init__(
          self,
          score_thresh=0.05,
          nms=0.5,
          detections_per_img=100,
          box_coder=None,
          cls_agnostic_bbox_reg=False
  ):
    self.score_thresh = score_thresh
    self.nms = nms
    self.detections_per_img = detections_per_img
    if box_coder is None:
      box_coder = BoxCoder(weights=(10., 10., 5., 5.))
    self.box_coder = box_coder
    self.cls_agnostic_bbox_reg = cls_agnostic_bbox_reg

  def __call__(self, x, boxes):
    class_logits, box_regression = x
    class_prob = Tensor.softmax(class_logits, -1)
    image_shapes = [box.size for box in boxes]
    boxes_per_image = [len(box) for box in boxes]
    concat_boxes = Tensor.cat(*[a.bbox for a in boxes], dim=0)

    if self.cls_agnostic_bbox_reg:
      box_regression = box_regression[:, -4:]
    proposals = self.box_coder.decode(
      box_regression.reshape(sum(boxes_per_image), -1), concat_boxes
    )
    if self.cls_agnostic_bbox_reg:
      proposals = proposals.repeat([1, class_prob.shape[1]])
    num_classes = class_prob.shape[1]
    proposals = proposals.unsqueeze(0)
    class_prob = class_prob.unsqueeze(0)
    results = []
    for prob, boxes_per_img, image_shape in zip(
            class_prob, proposals, image_shapes
    ):
      boxlist = self.prepare_boxlist(boxes_per_img, prob, image_shape)
      boxlist = boxlist.clip_to_image(remove_empty=False)
      boxlist = self.filter_results(boxlist, num_classes)
      results.append(boxlist)
    return results

  def prepare_boxlist(self, boxes, scores, image_shape):
    boxes = boxes.reshape(-1, 4)
    scores = scores.reshape(-1)
    boxlist = BoxList(boxes, image_shape, mode="xyxy")
    boxlist.add_field("scores", scores)
    return boxlist

  def filter_results(self, boxlist, num_classes):
    boxes = boxlist.bbox.reshape(-1, num_classes * 4)
    scores = boxlist.get_field("scores").reshape(-1, num_classes)

    device = scores.device
    result = []
    scores = scores.numpy()
    boxes = boxes.numpy()
    inds_all = scores > self.score_thresh
    for j in range(1, num_classes):
      inds = inds_all[:, j].nonzero()[0]
      # This needs to be done in numpy because it can create empty arrays
      scores_j = scores[inds, j]
      boxes_j = boxes[inds, j * 4: (j + 1) * 4]
      boxes_j = Tensor(boxes_j)
      scores_j = Tensor(scores_j)
      boxlist_for_class = BoxList(boxes_j, boxlist.size, mode="xyxy")
      boxlist_for_class.add_field("scores", scores_j)
      if len(boxlist_for_class):
        boxlist_for_class = boxlist_nms(
          boxlist_for_class, self.nms
        )
      num_labels = len(boxlist_for_class)
      boxlist_for_class.add_field(
        "labels", Tensor.full((num_labels,), j, device=device)
      )
      result.append(boxlist_for_class)

    result = cat_boxlist(result)
    number_of_detections = len(result)

    if number_of_detections > self.detections_per_img > 0:
      cls_scores = result.get_field("scores")
      image_thresh, _ = topk(cls_scores, k=self.detections_per_img)
      image_thresh = image_thresh.numpy()[-1]
      keep = (cls_scores.numpy() >= image_thresh).nonzero()[0]
      result = result[keep]
    return result


class RoIBoxHead:
  def __init__(self, in_channels):
    box_coder = BoxCoder((10., 10., 5., 5.))
    matcher = Matcher(0.5, 0.5, allow_low_quality_matches=False)
    fg_bg_sampler = BalancedPositiveNegativeSampler(512, 0.25)

    self.feature_extractor = FPN2MLPFeatureExtractor(in_channels)
    self.predictor = FPNPredictor()
    self.post_processor = PostProcessor(
        score_thresh=0.05,
        nms=0.5,
        detections_per_img=100,
        box_coder=box_coder,
        cls_agnostic_bbox_reg=False
    )
    self.loss_evaluator = FastRCNNLossComputation(matcher, fg_bg_sampler, box_coder, cls_agnostic_bbox_reg=False)

  def __call__(self, features, proposals, targets=None):
    if targets is not None:
      with Tensor.train(val=False):
        proposals = self.loss_evaluator.subsample(proposals, targets)

    x = self.feature_extractor(features, proposals)
    class_logits, box_regression = self.predictor(x)

    if not Tensor.training:
      result = self.post_processor((class_logits, box_regression), proposals)
      return x, result, {}
    
    loss_classifier, loss_box_reg = self.loss_evaluator([class_logits], [box_regression])
    return x, proposals, dict(loss_classifier=loss_classifier, loss_box_reg=loss_box_reg)


class MaskPostProcessor:
  # Not used in loss calculation
  def __call__(self, x, boxes):
    mask_prob = x.sigmoid().numpy()
    num_masks = x.shape[0]
    labels = [bbox.get_field("labels") for bbox in boxes]
    labels = Tensor.cat(*labels).numpy().astype(np.int32)
    index = np.arange(num_masks)
    mask_prob = mask_prob[index, labels][:, None]
    boxes_per_image, cumsum = [], 0
    for box in boxes:
      cumsum += len(box)
      boxes_per_image.append(cumsum)
    # using numpy here as Tensor.chunk doesnt have custom chunk sizes
    mask_prob = np.split(mask_prob, boxes_per_image, axis=0)
    results = []
    for prob, box in zip(mask_prob, boxes):
      bbox = BoxList(box.bbox, box.size, mode="xyxy")
      for field in box.fields():
        bbox.add_field(field, box.get_field(field))
      prob = Tensor(prob)
      bbox.add_field("mask", prob)
      results.append(bbox)

    return results


class Mask:
  def __init__(self):
    self.feature_extractor = MaskRCNNFPNFeatureExtractor()
    self.predictor = MaskRCNNC4Predictor()
    self.post_processor = MaskPostProcessor()
    self.loss_evaluator = self.create_loss_evaluator()

  def __call__(self, features, proposals, targets=None):
    if targets is not None:
      all_proposals = proposals
      proposals, _ = keep_only_positive_boxes(proposals)

    x = self.feature_extractor(features, proposals)
    if x:
      mask_logits = self.predictor(x)
      # TODO: Fix this issue when we start to introduce SegmentationMasks
      # if targets is not None:
      #   loss_mask = self.loss_evaluator(proposals, mask_logits, targets)
      #   return x, all_proposals, dict(loss_mask=loss_mask)
      # else:
      result = self.post_processor(mask_logits, proposals)
      return x, result, {}
      
  def create_loss_evaluator(self):
    matcher = Matcher(0.5, 0.5, allow_low_quality_matches=False)
    return MaskRCNNLossComputation(matcher, 28)


class RoIHeads:
  def __init__(self, in_channels):
    self.box = RoIBoxHead(in_channels)
    self.mask = Mask()

  def __call__(self, features, proposals, targets=None):
    x, detections, loss_box = self.box(features, proposals, targets)
    x, detections, loss_mask = self.mask(features, detections, targets)
    return x, detections, dict(loss_box=loss_box, loss_mask=loss_mask)


class ImageList(object):
  def __init__(self, tensors, image_sizes):
    self.tensors = tensors
    self.image_sizes = image_sizes

  def to(self, *args, **kwargs):
    cast_tensor = self.tensors.to(*args, **kwargs)
    return ImageList(cast_tensor, self.image_sizes)


def to_image_list(tensors, size_divisible=32):
  # Preprocessing
  if isinstance(tensors, Tensor) and size_divisible > 0:
    tensors = [tensors]

  if isinstance(tensors, ImageList):
    return tensors
  elif isinstance(tensors, Tensor):
    # single tensor shape can be inferred
    assert tensors.ndim == 4
    image_sizes = [tensor.shape[-2:] for tensor in tensors]
    return ImageList(tensors, image_sizes)
  elif isinstance(tensors, (tuple, list)):
    max_size = tuple(max(s) for s in zip(*[img.shape for img in tensors]))
    if size_divisible > 0:

      stride = size_divisible
      max_size = list(max_size)
      max_size[1] = int(math.ceil(max_size[1] / stride) * stride)
      max_size[2] = int(math.ceil(max_size[2] / stride) * stride)
      max_size = tuple(max_size)

    batch_shape = (len(tensors),) + max_size
    batched_imgs = np.zeros(batch_shape, dtype=tensors[0].numpy().dtype)
    for img, pad_img in zip(tensors, batched_imgs):
      pad_img[: img.shape[0], : img.shape[1], : img.shape[2]] += img.numpy()

    batched_imgs = Tensor(batched_imgs)
    image_sizes = [im.shape[-2:] for im in tensors]

    return ImageList(batched_imgs, image_sizes)
  else:
    raise TypeError("Unsupported type for to_image_list: {}".format(type(tensors)))
  
class Matcher(object):
  """
  This class assigns to each predicted "element" (e.g., a box) a ground-truth
  element. Each predicted element will have exactly zero or one matches; each
  ground-truth element may be assigned to zero or more predicted elements.

  Matching is based on the MxN match_quality_matrix, that characterizes how well
  each (ground-truth, predicted)-pair match. For example, if the elements are
  boxes, the matrix may contain box IoU overlap values.

  The matcher returns a tensor of size N containing the index of the ground-truth
  element m that matches to prediction n. If there is no match, a negative value
  is returned.
  """

  BELOW_LOW_THRESHOLD = -1
  BETWEEN_THRESHOLDS = -2

  def __init__(self, high_threshold, low_threshold, allow_low_quality_matches=False):
    """
    Args:
        high_threshold (float): quality values greater than or equal to
            this value are candidate matches.
        low_threshold (float): a lower quality threshold used to stratify
            matches into three levels:
            1) matches >= high_threshold
            2) BETWEEN_THRESHOLDS matches in [low_threshold, high_threshold)
            3) BELOW_LOW_THRESHOLD matches in [0, low_threshold)
        allow_low_quality_matches (bool): if True, produce additional matches
            for predictions that have only low-quality match candidates. See
            set_low_quality_matches_ for more details.
    """
    assert low_threshold <= high_threshold
    self.high_threshold = high_threshold
    self.low_threshold = low_threshold
    self.allow_low_quality_matches = allow_low_quality_matches

  def __call__(self, match_quality_matrix):
    """
    Args:
        match_quality_matrix (Tensor[float]): an MxN tensor, containing the
        pairwise quality between M ground-truth elements and N predicted elements.

    Returns:
        matches (Tensor[int64]): an N tensor where N[i] is a matched gt in
        [0, M - 1] or a negative value indicating that prediction i could not
        be matched.
    """
    match_quality_matrix = torch.from_numpy(match_quality_matrix.numpy())
    if match_quality_matrix.numel() == 0:
      # empty targets or proposals not supported during training
      if match_quality_matrix.shape[0] == 0:
          raise ValueError(
              "No ground-truth boxes available for one of the images "
              "during training")
      else:
          raise ValueError(
              "No proposal boxes available for one of the images "
              "during training")

    # match_quality_matrix is M (gt) x N (predicted)
    # Max over gt elements (dim 0) to find best gt candidate for each prediction
    matched_vals, matches = match_quality_matrix.max(dim=0)
    # matched_vals, matches = Tensor(matched_vals.numpy()), Tensor(matches.numpy())
    if self.allow_low_quality_matches:
      all_matches = matches.clone()

    # Assign candidate matches with low quality to negative (unassigned) values
    below_low_threshold = matched_vals < self.low_threshold
    between_thresholds = (matched_vals >= self.low_threshold) & (
        matched_vals < self.high_threshold
    )
    matches[below_low_threshold] = Matcher.BELOW_LOW_THRESHOLD
    matches[between_thresholds] = Matcher.BETWEEN_THRESHOLDS

    if self.allow_low_quality_matches:
        self.set_low_quality_matches_(matches, all_matches, match_quality_matrix)

    return Tensor(matches.numpy())

  def set_low_quality_matches_(self, matches, all_matches, match_quality_matrix):
    """
    Produce additional matches for predictions that have only low-quality matches.
    Specifically, for each ground-truth find the set of predictions that have
    maximum overlap with it (including ties); for each prediction in that set, if
    it is unmatched, then match it to the ground-truth with which it has the highest
    quality value.
    """
    # For each gt, find the prediction with which it has highest quality
    highest_quality_foreach_gt, _ = match_quality_matrix.max(dim=1)
    # Find highest quality match available, even if it is low, including ties
    gt_pred_pairs_of_highest_quality = torch.nonzero(
        match_quality_matrix == highest_quality_foreach_gt[:, None]
    )
    # Example gt_pred_pairs_of_highest_quality:
    #   tensor([[    0, 39796],
    #           [    1, 32055],
    #           [    1, 32070],
    #           [    2, 39190],
    #           [    2, 40255],
    #           [    3, 40390],
    #           [    3, 41455],
    #           [    4, 45470],
    #           [    5, 45325],
    #           [    5, 46390]])
    # Each row is a (gt index, prediction index)
    # Note how gt items 1, 2, 3, and 5 each have two ties

    pred_inds_to_update = gt_pred_pairs_of_highest_quality[:, 1]
    matches[pred_inds_to_update] = all_matches[pred_inds_to_update]

class BalancedPositiveNegativeSampler(object):
  """
  This class samples batches, ensuring that they contain a fixed proportion of positives
  """

  def __init__(self, batch_size_per_image, positive_fraction):
    """
    Arguments:
        batch_size_per_image (int): number of elements to be selected per image
        positive_fraction (float): percentage of positive elements per batch
    """
    self.batch_size_per_image = batch_size_per_image
    self.positive_fraction = positive_fraction

  def __call__(self, matched_idxs):
    """
    Arguments:
        matched idxs: list of tensors containing -1, 0 or positive values.
            Each tensor corresponds to a specific image.
            -1 values are ignored, 0 are considered as negatives and > 0 as
            positives.

    Returns:
        pos_idx (list[tensor])
        neg_idx (list[tensor])

    Returns two lists of binary masks for each image.
    The first list contains the positive elements that were selected,
    and the second list the negative example.
    """
    pos_idx = []
    neg_idx = []
    # TODO: optimize some of the ops to be tinygrad native
    for matched_idxs_per_image in matched_idxs:
      positive = torch.nonzero(torch.from_numpy(matched_idxs_per_image.numpy()) >= 1).squeeze(1)
      negative = torch.nonzero(torch.from_numpy(matched_idxs_per_image.numpy()) == 0).squeeze(1)

      num_pos = int(self.batch_size_per_image * self.positive_fraction)
      # protect against not enough positive examples
      num_pos = min(positive.numel(), num_pos)
      num_neg = self.batch_size_per_image - num_pos
      # protect against not enough negative examples
      num_neg = min(negative.numel(), num_neg)

      # randomly select positive and negative examples
      perm1 = torch.randperm(positive.numel(), device=positive.device)[:num_pos]
      perm2 = torch.randperm(negative.numel(), device=negative.device)[:num_neg]

      pos_idx_per_image = positive[perm1]
      neg_idx_per_image = negative[perm2]

      # create binary mask from indices
      pos_idx_per_image_mask = torch.zeros_like(
          torch.from_numpy(matched_idxs_per_image.numpy()), dtype=torch.bool
      )
      neg_idx_per_image_mask = torch.zeros_like(
          torch.from_numpy(matched_idxs_per_image.numpy()), dtype=torch.bool
      )
      pos_idx_per_image_mask[pos_idx_per_image] = 1
      neg_idx_per_image_mask[neg_idx_per_image] = 1

      pos_idx.append(Tensor(pos_idx_per_image_mask.numpy()))
      neg_idx.append(Tensor(neg_idx_per_image_mask.numpy()))

    return pos_idx, neg_idx

class FastRCNNLossComputation:
  def __init__(self, proposal_matcher, fg_bg_sampler, box_coder, cls_agnostic_bbox_reg=False):
    self.proposal_matcher = proposal_matcher
    self.fg_bg_sampler = fg_bg_sampler
    self.box_coder = box_coder
    self.cls_agnostic_bbox_reg = cls_agnostic_bbox_reg

  def match_targets_to_proposals(self, proposal, target):
    match_quality_matrix = boxlist_iou(target, proposal)
    matched_idxs = self.proposal_matcher(match_quality_matrix)
    # Fast RCNN only need "labels" field for selecting the targets
    target = target.copy_with_fields("labels")
    # get the targets corresponding GT for each proposal
    # NB: need to clamp the indices because we can have a single
    # GT in the image, and matched_idxs can be -2, which goes
    # out of bounds
    matched_targets = target[matched_idxs.maximum(0)]
    return matched_targets, matched_idxs
  
  def prepare_targets(self, proposals, targets):
    labels, regression_targets = [], []
    for proposals_per_image, targets_per_image in zip(proposals, targets):
      matched_targets, matched_idxs = self.match_targets_to_proposals(proposals_per_image, targets_per_image)

      labels_per_images = matched_targets.get_field("labels").cast(dtypes.int64)

      # Label background (below the low threshold)
      labels_per_images = (matched_idxs == -1).where(0, labels_per_images)

      # Label ignore proposals (between low and high thresholds)
      labels_per_images = (matched_idxs == -2).where(-1, labels_per_images)

      # compute regression targets
      regression_targets_per_image = self.box_coder.encode(matched_targets.bbox, proposals_per_image.bbox)

      labels.append(labels_per_images)
      regression_targets.append(regression_targets_per_image)

    return labels, regression_targets
  
  def subsample(self, proposals, targets):
    labels, regression_targets = self.prepare_targets(proposals, targets)
    sampled_pos_inds, sampled_neg_inds = self.fg_bg_sampler(labels)

    proposals = list(proposals)
    # add corresponding label and regression_targets information to the bounding boxes
    for labels_per_image, regression_targets_per_image, proposals_per_image in zip(labels, regression_targets, proposals):
      proposals_per_image.add_field("labels", labels_per_image)
      proposals_per_image.add_field("regression_targets", regression_targets_per_image)

    # distributed sampled proposals, that were obtained on all feature maps
    # concatenated via the fg_bg_sampler, into individual feature map levels
    for img_idx, (pos_inds_img, neg_inds_img) in enumerate(zip(sampled_pos_inds, sampled_neg_inds)):
      # TODO: optimize this to be in tinygrad
      pos_inds_img, neg_inds_img = torch.from_numpy(pos_inds_img.numpy()), torch.from_numpy(neg_inds_img.numpy())
      img_sampled_inds = Tensor(torch.nonzero(pos_inds_img | neg_inds_img).squeeze(1).numpy())
      proposals_per_image = proposals[img_idx][img_sampled_inds]
      proposals[img_idx] = proposals_per_image

    self._proposals = proposals
    return proposals
  
  def __call__(self, class_logits, box_regression):
    class_logits = Tensor.cat(*class_logits)
    box_regression = Tensor.cat(*box_regression)
    device = class_logits.device

    if not hasattr(self, "_proposals"):
      raise RuntimeError("subsample needs to be called before")
    
    proposals = self._proposals

    labels = Tensor.cat(*[proposal.get_field("labels") for proposal in proposals])
    regression_targets = Tensor.cat(*[proposal.get_field("regression_targets") for proposal in proposals])

    # TODO: figure this out
    classification_loss = torch.nn.functional.cross_entropy(torch.from_numpy(class_logits.numpy()), torch.from_numpy(labels.numpy()).long())

    # get indices that correspond to the regression targets for
    # the corresponding ground truth labels, to be used with
    # advanced indexing
    sampled_pos_inds_subset = nonzero(labels > 0)
    labels_pos = labels[sampled_pos_inds_subset]
    if self.cls_agnostic_bbox_reg:
      map_inds = Tensor([4, 5, 6, 7], device=device)
    else:
      map_inds = 4 * labels_pos[:, None] + Tensor([0, 1, 2, 3], device=device)

    box_loss = smooth_l1_loss(
      box_regression[sampled_pos_inds_subset[:, None], map_inds],
      regression_targets[sampled_pos_inds_subset],
      beta=1,
      size_average=False
    )
    box_loss = box_loss / labels.numel()
    return classification_loss, box_loss

class MaskRCNNLossComputation:
  def __init__(self, proposal_matcher, discretization_size):
    self.proposal_matcher = proposal_matcher
    self.discretization_size = discretization_size

  def match_targets_to_proposals(self, proposal, target):
    match_quality_matrix = boxlist_iou(target, proposal)
    matched_idxs = self.proposal_matcher(match_quality_matrix)
    # Fast RCNN only need "labels" field for selecting the targets
    target = target.copy_with_fields("labels")
    # get the targets corresponding GT for each proposal
    # NB: need to clamp the indices because we can have a single
    # GT in the image, and matched_idxs can be -2, which goes
    # out of bounds
    matched_targets = target[matched_idxs.maximum(0)]
    return matched_targets, matched_idxs
  
  def prepare_targets(self, proposals, targets):
    labels, masks = [], []
    for proposals_per_image, targets_per_image in zip(proposals, targets):
      matched_targets, matched_idxs = self.match_targets_to_proposals(proposals_per_image, targets_per_image)

      labels_per_image = matched_targets.get_field("labels").cast(dtypes.int64)
      labels_per_image = (matched_idxs == Matcher.BELOW_LOW_THRESHOLD).where(0, labels_per_image)

      positive_inds = nonzero(labels_per_image > 0)

      segmentation_masks = matched_targets.get_field("masks")
      segmentation_masks = segmentation_masks[positive_inds]

      positive_proposals = proposals_per_image[positive_inds]

      masks_per_image = project_masks_on_boxes(segmentation_masks, positive_proposals, self.discretization_size)

      labels.append(labels_per_image)
      masks.append(masks_per_image)

    return labels, masks
  
  def __call__(self, proposals, mask_logits, targets):
    labels, mask_targets = self.prepare_targets(proposals, targets)
    labels, mask_targets = Tensor.cat(*labels), Tensor.cat(*mask_targets)

    positive_inds = nonzero(labels > 0)
    labels_pos = labels[positive_inds]

    if mask_targets.numel() == 0: return mask_logits.sum() * 0

    return Tensor.binary_crossentropy_logits(mask_logits[positive_inds, labels_pos], mask_targets)

class RPNLossComputation:
  def __init__(self, proposal_matcher, fg_bg_sampler, box_coder, generate_labels_func):
    self.proposal_matcher = proposal_matcher
    self.fg_bg_sampler = fg_bg_sampler
    self.box_coder = box_coder
    self.generate_labels_func = generate_labels_func
    self.discard_cases = ["not_visibility", "between_thresholds"]

  def __call__(self, anchors, objectness, box_regression, targets) -> Tuple[Tensor, ...]:
    anchors = [cat_boxlist(anchors_per_image) for anchors_per_image in anchors]
    labels, regression_targets = self.prepare_targets(anchors, targets)
    sampled_pos_inds, sampled_neg_inds = self.fg_bg_sampler(labels)
    sampled_pos_inds, sampled_neg_inds = nonzero(Tensor.cat(*sampled_pos_inds)), nonzero(Tensor.cat(*sampled_neg_inds))
    sampled_inds = sampled_pos_inds.cat(sampled_neg_inds)

    objectness, box_regression = concat_box_prediction_layers(objectness, box_regression)
    objectness = objectness.squeeze()
    labels, regression_targets = Tensor.cat(*labels), Tensor.cat(*regression_targets)

    box_loss = smooth_l1_loss(box_regression[sampled_pos_inds], regression_targets[sampled_pos_inds], size_average=False) / (sampled_inds.numel())
    objectness_loss = objectness[sampled_inds].binary_crossentropy_logits(labels[sampled_inds])
    return objectness_loss, box_loss

  def match_targets_to_anchors(self, anchor, target):
    match_quality_matrix = boxlist_iou(target, anchor)
    matched_idxs = self.proposal_matcher(match_quality_matrix)
    matched_targets = target[matched_idxs.maximum(0)]
    return matched_targets, matched_idxs

  def prepare_targets(self, anchors, targets):
    labels, regression_targets = [], []
    for anchors_per_image, targets_per_image in zip(anchors, targets):
      matched_targets, matched_idxs = self.match_targets_to_anchors(anchors_per_image, targets_per_image)

      labels_per_image = self.generate_labels_func(matched_idxs)
      labels_per_image = labels_per_image.cast(dtypes.float32)
      labels_per_image = (matched_idxs == -1).where(0, labels_per_image)

      if "not_visibility" in self.discard_cases:
        mask = tilde(anchors_per_image.get_field("visibility").cast(dtypes.bool))
        labels_per_image = mask.where(-1, labels_per_image)

      if "between_thresholds" in self.discard_cases:
        labels_per_image = (matched_idxs == -2).where(-1, labels_per_image)

      regression_targets_per_image = self.box_coder.encode(matched_targets.bbox, anchors_per_image.bbox)
      labels.append(labels_per_image)
      regression_targets.append(regression_targets_per_image)

    return labels, regression_targets

def smooth_l1_loss(self:Tensor, Y:Tensor, beta:float = 1./9, size_average:bool = True) -> Tensor:
  n = (self-Y).abs()
  cond = n < beta
  loss = cond.where(0.5 * n ** 2 / beta, n - 0.5 * beta)
  if size_average: return loss.mean()
  return loss.sum()

def boxlist_iou(boxlist1:BoxList, boxlist2:BoxList) -> Tensor:
  assert boxlist1.size == boxlist2.size, "boxlists should have the same size"
  area1, area2 = boxlist1.area(), boxlist2.area()
  box1, box2 = boxlist1.bbox, boxlist2.bbox
  lt = Tensor.maximum(box1[:, None, :2], box2[:, :2])
  rb = Tensor.minimum(box1[:, None, 2:], box2[:, 2:])
  TO_REMOVE = 1
  wh = (rb - lt + TO_REMOVE).maximum(0)
  inter = wh[:, :, 0] * wh[:, :, 1]
  return inter / (area1[:, None] + area2 - inter)

def concat_box_prediction_layers(box_cls, box_regression) -> Tuple[Tensor, ...]:
  box_cls_flattened, box_regression_flattened = [], []
  # for each feature level, permute the outputs to make them be in the
  # same format as the labels. Note that the labels are computed for
  # all feature levels concatenated, so we keep the same representation
  # for the objectness and the box_regression
  for box_cls_per_level, box_regression_per_level in zip(box_cls, box_regression):
    N, AxC, H, W = box_cls_per_level.shape
    Ax4 = box_regression_per_level.shape[1]
    A = Ax4 // 4
    C = AxC // A
    box_cls_per_level = permute_and_flatten(box_cls_per_level, N, A, C, H, W)
    box_cls_flattened.append(box_cls_per_level)

    box_regression_per_level = permute_and_flatten(box_regression_per_level, N, A, 4, H, W)
    box_regression_flattened.append(box_regression_per_level)
  # concatenate on the first dimension (representing the feature levels), to
  # take into account the way the labels were generated (with all feature maps
  # being concatenated as well)
  box_cls = Tensor.cat(*box_cls_flattened, dim=1).reshape(-1, C)
  box_regression = Tensor.cat(*box_regression_flattened, dim=1).reshape(-1, 4)
  return box_cls, box_regression

def generate_rpn_labels(matched_idxs): return matched_idxs >= 0

# NOTE: implement this in tinygrad
def nonzero(self:Tensor) -> Tensor: return Tensor(torch.from_numpy(self.numpy()).nonzero().squeeze(1).numpy())

def tilde(x: Tensor) -> Tensor:
  if x.dtype == dtypes.bool: return (1 - x).cast(dtypes.bool)
  return (x + 1) * -1  # this seems to be what the ~ operator does in pytorch for non bool

def project_masks_on_boxes(segmentation_masks, proposals, discretization_size):
  """ Given segmentation masks and the bounding boxes corresponding
  to the location of the masks in the image, this function
  crops and resizes the masks in the position defined by the
  boxes. This prepares the masks for them to be fed to the
  loss computation as the targets.

  Arguments:
      segmentation_masks: an instance of SegmentationMask
      proposals: an instance of BoxList
  """
  masks = []
  M = discretization_size
  device = proposals.bbox.device
  proposals = proposals.convert("xyxy")
  assert segmentation_masks.size == proposals.size, "{}, {}".format(
    segmentation_masks, proposals
  )
  # TODO put the proposals on the CPU, as the representation for the
  # masks is not efficient GPU-wise (possibly several small tensors for
  # representing a single instance mask)
  proposals = proposals.bbox
  for segmentation_mask, proposal in zip(segmentation_masks, proposals):
    # crop the masks, resize them to the desired resolution and
    # then convert them to the tensor representation,
    # instead of the list representation that was used
    cropped_mask = segmentation_mask.crop(proposal)
    scaled_mask = cropped_mask.resize((M, M))
    mask = scaled_mask.convert(mode="mask")
    masks.append(mask)
  if len(masks) == 0:
    return Tensor.empty(0, dtype=dtypes.float32)
  return masks.stack(dim=0).cast(dtypes.float32)

def keep_only_positive_boxes(boxes):
  """
  Given a set of BoxList containing the `labels` field,
  return a set of BoxList for which `labels > 0`.

  Arguments:
      boxes (list of BoxList)
  """
  assert isinstance(boxes, (list, tuple))
  assert isinstance(boxes[0], BoxList)
  assert boxes[0].has_field("labels")
  positive_boxes = []
  positive_inds = []
  num_boxes = 0
  for boxes_per_image in boxes:
    labels = boxes_per_image.get_field("labels")
    inds_mask = labels > 0
    inds = nonzero(inds_mask)
    positive_boxes.append(boxes_per_image[inds])
    positive_inds.append(inds_mask)
  return positive_boxes, positive_inds

class MaskRCNN:
  def __init__(self, backbone: ResNet):
    self.backbone = ResNetFPN(backbone, out_channels=256)
    self.rpn = RPN(self.backbone.out_channels)
    self.roi_heads = RoIHeads(self.backbone.out_channels)

  def load_from_pretrained(self):
    fn = Path('./') / "weights/maskrcnn.pt"
    fetch("https://download.pytorch.org/models/maskrcnn/e2e_mask_rcnn_R_50_FPN_1x.pth", fn)

    state_dict = torch_load(fn)['model']
    loaded_keys = []
    for k, v in state_dict.items():
      if "module." in k:
        k = k.replace("module.", "")
      if "stem." in k:
        k = k.replace("stem.", "")
      if "fpn_inner" in k:
        block_index = int(re.search(r"fpn_inner(\d+)", k).group(1))
        k = re.sub(r"fpn_inner\d+", f"inner_blocks.{block_index - 1}", k)
      if "fpn_layer" in k:
        block_index = int(re.search(r"fpn_layer(\d+)", k).group(1))
        k = re.sub(r"fpn_layer\d+", f"layer_blocks.{block_index - 1}", k)
      loaded_keys.append(k)
      get_child(self, k).assign(v.numpy()).realize()
    return loaded_keys

  def __call__(self, images, targets=None):
    if Tensor.training and targets is None:
      raise ValueError("In training mode, targets should be passed")

    images = to_image_list(images)
    features = self.backbone(images.tensors)
    proposals, proposal_losses = self.rpn(images, features, targets=targets)
    x, result, detector_losses = self.roi_heads(features, proposals, targets=targets)

    if targets is not None: return dict(detector_losses=detector_losses, proposal_losses=proposal_losses)
    return result


if __name__ == '__main__':
  resnet = resnet = ResNet(50, num_classes=None, stride_in_1x1=True)
  model = MaskRCNN(backbone=resnet)
  model.load_from_pretrained()
