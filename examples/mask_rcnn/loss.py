# RCNN-specific loss functions

from models.mask_rcnn import BoxList, BoxCoder, cat_boxlist
from tinygrad.tensor import Tensor
from tinygrad.tensor import dtypes
import numpy as np
from typing import List, Callable, Tuple
from torch.nn import functional as F

# implementation from https://github.com/kuangliu/torchcv/blob/master/torchcv/utils/box.py
# with slight modifications

def test_boxlist_iou():
  a = boxlist_iou(BoxList(Tensor([[0, 0, 10, 10]]), image_size = (50, 50)), BoxList(Tensor([[0, 0, 5, 5]]), image_size = (50, 50)))
  assert all(((a == .25)[0]).numpy())


def boxlist_iou(boxlist1: BoxList, boxlist2: BoxList) -> Tensor:
  # Compute the intersection over union of two set of boxes.
  assert boxlist1.size == boxlist2.size, f"boxlists should have same image size, got {boxlist1}, {boxlist2}"
  N, M = len(boxlist1), len(boxlist2)
  area1, area2 = boxlist1.area(), boxlist2.area()
  box1, box2 = boxlist1.bbox, boxlist2.bbox
  lt = Tensor.maximum(box1[:, None, :2], box2[:, :2])  # [N,M,2]
  rb = Tensor.minimum(box1[:, None, 2:], box2[:, 2:])  # [N,M,2]
  wh = (rb - lt).maximum(0)  # [N,M,2]
  inter = wh[:, :, 0] * wh[:, :, 1]  # [N,M]

  iou = inter / (area1[:, None] + area2 - inter)
  return iou


def test_match_eval():
  fn1 = make_match_evaluation_fn(0.7, 0.4)

  match_quality_matrix = Tensor([[0.9, 0.7, 0.8, 0.9], # gt 1, .9
                                [0.1, 0.5, 0.1, 0.2],  # gt 2, .5
                                [0.1, 0.2, 0.2, 0.3]]) # gt 3, .3
  # 1. test that it works
  a = fn1(match_quality_matrix)
  assert all(((a == Tensor([[ 0.9],[-2.],[-1.]]))[:, 0]).numpy())
   

def make_match_evaluation_fn(high: float, low: float, allow_low_qual: bool = False) -> Callable[[Tensor], Tensor]:
  # TODO this is a bit of a mess but I guess it helps get out of holes
  def set_low_quality_matches_(preds: Tensor):
    """
    Produce additional matches for predictions that have only low-quality matches.
    Specifically, for each ground-truth find the set of predictions that have
    maximum overlap with it (including ties); for each prediction in that set, if
    it is unmatched, then match it to the ground-truth with which it has the highest
    quality value.
    """
    # For each gt, find the prediction with which it has highest quality
    highest_quality_foreach_gt, _ = match_quality_matrix.max(axis=1)
    # Find highest quality match available, even if it is low, including ties
    gt_pred_pairs_of_highest_quality = Tensor.nonzero(
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

  def loss_eval_fn(match_quality_matrix: Tensor) -> Tensor:
    if match_quality_matrix.numel() == 0:
      if match_quality_matrix.shape[0] == 0:
        raise ValueError(
          "No ground-truth boxes available for one of the images "
          "during training")
      else:
        raise ValueError(
          "No proposal boxes available for one of the images "
          "during training")

    # find best gt candidate for each prediction
    preds = match_quality_matrix.max(axis=1, keepdim=True)

    above_high_threshold = preds >= high
    below_low_threshold = preds < low
    between_thresholds = (preds >= low) * (preds < high)
    
    if allow_low_qual:
      set_low_quality_matches_(preds)

    return above_high_threshold*preds - between_thresholds*2 - below_low_threshold*1
  return loss_eval_fn

def test_rind():
  x = rind(Tensor([1, 1, 1, 1, 0, 0, 0, 0, 1, 1]).numpy(), 3)
  assert x.ndim == 1
  assert x.shape[0] == 3
  import numpy as np
  assert np.isin(x, [0, 1, 2, 3, 8, 9]).all()

# TODO perf
def rind(mask: np.ndarray, take: int) -> Tensor:
  assert mask.ndim == 1 and mask.shape[0] >= take
  masked = (np.arange(mask.shape[0]) * mask)[mask.astype(bool)]
  stacked = np.stack([masked,np.random.rand(masked.shape[0])],axis=0)
  return stacked[0, stacked[1].argsort()[:take]]

def test_balanced_sampler():
  fn1 = make_balanced_sampler_fn(10, 0.5)
  t1 = Tensor([1, 0, 1, 1, 1, 1, 0, 1, 1, 0])
  a1 = np.arange(t1.shape[0])
  a, b = fn1([t1])
  assert np.isin(a[0] * a1, t1.numpy() * a1).all()
  assert np.isin(b[0] * a1, (t1 == 0).numpy() * a1).all()

# returns a random mask of positive and negative examples
def make_balanced_sampler_fn(batch_size_per_image: int, positive_fraction: float) -> Callable[[Tensor], Tuple[List[Tensor], List[Tensor]]]:
  def sampler_fn(image_matches: List[Tensor]) -> (Tensor, Tensor):
    pos_masks = []
    neg_masks = []
    for matches in image_matches:
      # TODO this was >= 1 in the example, docs say > 0
      positive, negative = matches >= 1, matches == 0 
      num_pos = int(batch_size_per_image * positive_fraction)
      
      # protect against not enough positive examples
      pos_numel, neg_numel = positive.sum().numpy().item(), negative.sum().numpy().item()
      num_pos = int(min(pos_numel, num_pos))
      num_neg = int(min(neg_numel, batch_size_per_image - num_pos))
      
      # option .. return a mask or return gather indices, which is more efficient?
      pos_mask = np.zeros_like(matches.numpy())
      pos_mask[rind(positive.numpy(), num_pos).astype(int)] = 1 # scatter 1s into the mask
      pos_masks.append(pos_mask)

      neg_mask = np.zeros_like(pos_mask)
      neg_mask[rind(negative.numpy(), num_neg).astype(int)] = 1
      neg_masks.append(neg_mask)

    return pos_masks, neg_masks
  return sampler_fn

# This function should be overwritten in RetinaNet
def generate_rpn_labels(matched_idxs):
    labels_per_image = matched_idxs >= 0
    return labels_per_image

def make_rpn_loss_evaluator(box_coder):
  matcher = make_match_evaluation_fn(.7, .3)
  fg_bg_sampler = make_balanced_sampler_fn(256, .5)

  loss_evaluator = RPNLossComputation(
      proposal_matcher=matcher,
      fg_bg_sampler=fg_bg_sampler,
      box_coder=box_coder,
      generate_labels_func=generate_rpn_labels
  )
  return loss_evaluator

def permute_and_flatten(layer, N, A, C, H, W):
  layer = layer.view(N, -1, C, H, W)
  layer = layer.permute(0, 3, 4, 1, 2)
  layer = layer.reshape(N, -1, C)
  return layer

def concat_box_prediction_layers(box_cls, box_regression):
  box_cls_flattened = []
  box_regression_flattened = []
  # for each feature level, permute the outputs to make them be in the
  # same format as the labels. Note that the labels are computed for
  # all feature levels concatenated, so we keep the same representation
  # for the objectness and the box_regression
  for box_cls_per_level, box_regression_per_level in zip(
      box_cls, box_regression
  ):
      N, AxC, H, W = box_cls_per_level.shape
      Ax4 = box_regression_per_level.shape[1]
      A = Ax4 // 4
      C = AxC // A
      box_cls_per_level = permute_and_flatten(
          box_cls_per_level, N, A, C, H, W
      )
      box_cls_flattened.append(box_cls_per_level)

      box_regression_per_level = permute_and_flatten(
          box_regression_per_level, N, A, 4, H, W
      )
      box_regression_flattened.append(box_regression_per_level)
  # concatenate on the first dimension (representing the feature levels), to
  # take into account the way the labels were generated (with all feature maps
  # being concatenated as well)
  box_cls = Tensor.cat(box_cls_flattened, dim=1).reshape(-1, C)
  box_regression = Tensor.cat(box_regression_flattened, dim=1).reshape(-1, 4)
  return box_cls, box_regression

# TODO maybe push this to nn?
def smooth_l1_loss(input, target, beta=1. / 9, size_average=True):
  """
  very similar to the smooth_l1_loss from pytorch, but with
  the extra beta parameter
  """
  n = Tensor.abs(input - target)
  cond = n < beta
  loss = Tensor.where(cond, 0.5 * n ** 2 / beta, n - 0.5 * beta)
  if size_average:
      return loss.mean()
  return loss.sum()


# one way this differs from reference is it doesn't rely on boxlist mutables
class RPNLossComputation:
  def __init__(self, proposal_matcher, fg_bg_sampler, box_coder,
              generate_labels_func):
    """
    Arguments:
        proposal_matcher (Matcher)
        fg_bg_sampler (BalancedPositiveNegativeSampler)
        box_coder (BoxCoder)
    """
    # self.target_preparator = target_preparator
    self.proposal_matcher = proposal_matcher
    self.fg_bg_sampler = fg_bg_sampler
    self.box_coder = box_coder
    self.generate_labels_func = generate_labels_func
    self.discard_cases = ['not_visibility', 'between_thresholds']

  def match_targets_to_anchors(self, anchor, target):
    match_quality_matrix = boxlist_iou(target, anchor)
    matched_idxs = self.proposal_matcher(match_quality_matrix)
    matched_targets = target[matched_idxs.maximum(0)]
    return matched_targets, matched_idxs

  def prepare_targets(self, anchors, targets):
    labels = []
    regression_targets = []
    #
    for anchors_per_image, targets_per_image in zip(anchors, targets):
      matched_targets, matched_idxs = self.match_targets_to_anchors(
          anchors_per_image, targets_per_image, self.copied_fields
      )
      labels_per_image = self.generate_labels_func(matched_idxs)
      labels_per_image = labels_per_image.to(dtype=dtypes.float32)

      # Background (negative examples)
      bg_indices = matched_idxs == -1 # TODO: magic number
      labels_per_image[bg_indices] = 0

      # discard anchors that go out of the boundaries of the image
      labels_per_image[~anchors_per_image.get_field("visibility")] = -1

      inds_to_discard = matched_idxs == -2 # TODO: magic number
      labels_per_image[inds_to_discard] = -1

      # compute regression targets
      regression_targets_per_image = self.box_coder.encode(
          matched_targets.bbox, anchors_per_image.bbox
      )

      labels.append(labels_per_image)
      regression_targets.append(regression_targets_per_image)

    return labels, regression_targets


  def __call__(self, anchors, objectness, box_regression, targets):
    """
    Arguments:
        anchors (list[BoxList])
        objectness (list[Tensor])
        box_regression (list[Tensor])
        targets (list[BoxList])

    Returns:
        objectness_loss (Tensor)
        box_loss (Tensor
    """
    anchors = [cat_boxlist(anchors_per_image) for anchors_per_image in anchors]
    labels, regression_targets = self.prepare_targets(anchors, targets)
    sampled_pos_inds, sampled_neg_inds = self.fg_bg_sampler(labels)
    sampled_pos_inds = Tensor.nonzero(Tensor.cat(sampled_pos_inds, dim=0)).squeeze(1)
    sampled_neg_inds = Tensor.nonzero(Tensor.cat(sampled_neg_inds, dim=0)).squeeze(1)

    sampled_inds = Tensor.cat([sampled_pos_inds, sampled_neg_inds], dim=0)

    objectness, box_regression = \
            concat_box_prediction_layers(objectness, box_regression)

    objectness = objectness.squeeze()

    labels = Tensor.cat(labels, dim=0)
    regression_targets = Tensor.cat(regression_targets, dim=0)

    box_loss = smooth_l1_loss(
        box_regression[sampled_pos_inds],
        regression_targets[sampled_pos_inds],
        beta=1.0 / 9,
        size_average=False,
    ) / (sampled_inds.numel())

    objectness_loss = F.binary_cross_entropy_with_logits(
        objectness[sampled_inds], labels[sampled_inds]
    )

    return objectness_loss, box_loss

if __name__ == "__main__":
  #PLAYGROUND
  data = Tensor([[1, 2, 3],
               [4, 5, 6],
               [7, 8, 9],
               [10, 11, 12]])
  idx = Tensor([0, 2, 1, 1]).reshape(4, 1)
  result = data.gather(idx, dim=1)
  t = Tensor(1)
  print('ldata')
  print(t.lazydata)
  print('realize')
  print(t.lazydata.realize())
  print('lazydata')
  print(t.lazydata)
  print('realized')
  print(type(t.lazydata.realized))
  print(t.lazydata.realized)
  print(t[0])
  test_rind()
  test_boxlist_iou()
  test_match_eval()
  test_balanced_sampler()
  

    # ind = Tensor.arange(mask.shape[0])
    # nz = mask.sum().numpy().item()
    # mask = mask * ind
    # idx = mask.numpy().argsort()[-int(nz):]