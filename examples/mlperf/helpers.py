from typing import List
from collections import OrderedDict
import unicodedata
import numpy as np
from tinygrad.nn import state
from tinygrad.tensor import Tensor, dtypes
from tinygrad.helpers import getenv

#
# checkpointing utils
#

def invert_dict(d): return {v: k for k, v in reversed(d.items())}
def dedup_dict(d): return invert_dict(invert_dict(d))
# store each tensor into the first key it appears in
def get_training_state(model, optimizer, scheduler):
  # hack: let get_state_dict walk the tree starting with model, so that the checkpoint keys are
  # readable and can be loaded as a model for eval
  train_state = {'model': model, 'optimizer': optimizer, 'scheduler': scheduler}
  return dedup_dict(state.get_state_dict(train_state))
def load_training_state(model, optimizer, scheduler, state_dict):
  # use fresh model to restore duplicate keys
  train_state = {'model': model, 'optimizer': optimizer, 'scheduler': scheduler}
  big_dict = state.get_state_dict(train_state)
  # hack: put back the dupes
  dupe_names = {}
  for k, v in big_dict.items():
    if v not in dupe_names:
      dupe_names[v] = k
      assert k in state_dict
    state_dict[k] = state_dict[dupe_names[v]]
  # scheduler contains optimizer and all params, load each weight only once
  scheduler_state = {'scheduler': scheduler}
  state.load_state_dict(scheduler_state, state_dict)

def gaussian_kernel(n, std):
  from scipy import signal
  gaussian_1d = signal.windows.gaussian(n, std)
  gaussian_2d = np.outer(gaussian_1d, gaussian_1d)
  gaussian_3d = np.outer(gaussian_2d, gaussian_1d)
  gaussian_3d = gaussian_3d.reshape(n, n, n)
  gaussian_3d = np.cbrt(gaussian_3d)
  gaussian_3d /= gaussian_3d.max()
  return gaussian_3d

def prepare_arrays(image, roi_shape=(128, 128, 128)):
  assert len(roi_shape) == 3 and any(roi_shape)
  image_shape = list(image.shape[2:])
  result = np.zeros((1, 3, *image_shape), dtype=image.dtype)
  norm_map = np.zeros_like(result)
  norm_patch = gaussian_kernel(roi_shape[0], 0.125 * roi_shape[0]).astype(norm_map.dtype)
  return result, norm_map, norm_patch

def get_slice(image, roi_shape=(128, 128, 128), overlap_factor=0.5):
  assert len(roi_shape) == 3 and any(roi_shape)
  assert 0 < overlap_factor < 1
  image_shape, dim = list(image.shape[2:]), len(image.shape[2:])
  strides = [int(roi_shape[i] * (1 - overlap_factor)) for i in range(dim)]
  size = [(image_shape[i] - roi_shape[i]) // strides[i] + 1 for i in range(dim)]
  for i in range(0, strides[0] * size[0], strides[0]):
    for j in range(0, strides[1] * size[1], strides[1]):
      for k in range(0, strides[2] * size[2], strides[2]):
        yield i, j, k

def _get_best_indices(logits, n_best_size):
  index_and_score = sorted(enumerate(logits), key=lambda x: x[1], reverse=True)
  return list(map(lambda x: x[0], index_and_score))[:n_best_size]

def _is_punctuation(char):
  if (cp := ord(char)) in range(33, 48) or cp in range(58, 65) or cp in range(91, 97) or cp in range(123, 127):
    return True
  return unicodedata.category(char).startswith("P")

def _is_whitespace(char):
  if char == " " or char == "\t" or char == "\n" or char == "\r":
    return True
  return unicodedata.category(char) == "Zs"

def _is_control(char):
  if char == "\t" or char == "\n" or char == "\r":
    return False
  return unicodedata.category(char).startswith("C")

def _run_split_on_punc(text):
  if text in ("[UNK]", "[SEP]", "[PAD]", "[CLS]", "[MASK]"):
    return [text]
  start_new_word = True
  output = []
  for i in range(len(text)):
    if _is_punctuation(char := text[i]):
      output.append([char])
      start_new_word = True
    else:
      if start_new_word:
        output.append([])
      start_new_word = False
      output[-1].append(char)
  return ["".join(x) for x in output]

def _run_strip_accents(text):
  output = []
  for char in unicodedata.normalize("NFD", text):
    if unicodedata.category(char) != "Mn":
      output.append(char)
  return "".join(output)

def _clean_text(text):
  output = []
  for char in text:
    if not ((cp := ord(char)) == 0 or cp == 0xfffd or _is_control(char)):
      output.append(" " if _is_whitespace(char) else char)
  return "".join(output)

def _get_final_text(pred_text, orig_text):
  def _strip_spaces(text):
    ns_text = ""
    ns_to_s_map = OrderedDict()
    for i, c in enumerate(text):
      if c == " ":
        continue
      ns_to_s_map[len(ns_text)] = i
      ns_text += c
    return ns_text, ns_to_s_map

  orig_tokens = _clean_text(orig_text).strip().split()
  split_tokens = []
  for token in orig_tokens:
    if token not in ("[UNK]", "[SEP]", "[PAD]", "[CLS]", "[MASK]"):
      token = token.lower()
      token = _run_strip_accents(token)
    split_tokens.extend(_run_split_on_punc(token))

  tok_text = " ".join(" ".join(split_tokens).strip().split())
  start_position = tok_text.find(pred_text)
  if start_position == -1:
    return orig_text
  end_position = start_position + len(pred_text) - 1

  orig_ns_text, orig_ns_to_s_map = _strip_spaces(orig_text)
  tok_ns_text, tok_ns_to_s_map = _strip_spaces(tok_text)
  if len(orig_ns_text) != len(tok_ns_text):
    return orig_text
  tok_s_to_ns_map = {v: k for k, v in tok_ns_to_s_map.items()}

  orig_start_position = None
  if start_position in tok_s_to_ns_map:
    if (ns_start_position := tok_s_to_ns_map[start_position]) in orig_ns_to_s_map:
      orig_start_position = orig_ns_to_s_map[ns_start_position]
  if orig_start_position is None:
    return orig_text

  orig_end_position = None
  if end_position in tok_s_to_ns_map:
    if (ns_end_position := tok_s_to_ns_map[end_position]) in orig_ns_to_s_map:
      orig_end_position = orig_ns_to_s_map[ns_end_position]
  if orig_end_position is None:
    return orig_text

  output_text = orig_text[orig_start_position:(orig_end_position + 1)]
  return output_text

def get_bert_qa_prediction(features, example, start_end_logits):
  prelim_predictions = []
  for i, feature in enumerate(features):
    for start_index in _get_best_indices(start_end_logits[i][0], 20):
      for end_index in _get_best_indices(start_end_logits[i][1], 20):
        if start_index >= len(feature["tokens"]) or end_index >= len(feature["tokens"]):
          continue
        if start_index not in feature["token_to_orig_map"] or end_index not in feature["token_to_orig_map"]:
          continue
        if not feature["token_is_max_context"].get(start_index, False):
          continue
        if end_index < start_index or end_index - start_index + 1 > 30:
          continue

        prelim_predictions.append({
          "feature_index": i,
          "start_index": start_index,
          "end_index": end_index,
          "start_logit": start_end_logits[i][0, start_index],
          "end_logit": start_end_logits[i][1, end_index]
        })
  predictions = sorted(prelim_predictions, key=lambda x: (x["start_logit"] + x["end_logit"]), reverse=True)

  if len(predictions) > 0:
    feature = features[predictions[0]["feature_index"]]
    tok_tokens = feature["tokens"][predictions[0]["start_index"]:(predictions[0]["end_index"] + 1)]
    orig_doc_start = feature["token_to_orig_map"][predictions[0]["start_index"]]
    orig_doc_end = feature["token_to_orig_map"][predictions[0]["end_index"]]
    orig_tokens = example["context"][orig_doc_start:(orig_doc_end + 1)]
    tok_text = " ".join(tok_tokens).replace(" ##", "").replace("##", "")
    tok_text = " ".join(tok_text.strip().split())
    orig_text = " ".join(orig_tokens)
    return _get_final_text(tok_text, orig_text)
  return "empty"

def get_mlperf_bert_config():
  """Config is BERT-large"""
  return {
    "attention_probs_dropout_prob": 0.1,
    "hidden_dropout_prob": 0.1,
    "hidden_size": 1024,
    "intermediate_size": 4096,
    "max_position_embeddings": 512,
    "num_attention_heads": 16,
    "num_hidden_layers": 24,
    "type_vocab_size": 2,
    "vocab_size": 30522
  }

def get_mlperf_bert_model(checkpoint_path:str):
  from extra.models import bert
  from examples.mlperf.initializers import LinearBert, EmbeddingBert, LayerNormBert

  bert.Linear = LinearBert
  bert.Embedding = EmbeddingBert 
  bert.LayerNorm = LayerNormBert

  from extra.models.bert import BertForPretraining
  config = get_mlperf_bert_config()
  if getenv("DISABLE_DROPOUT", 0):
    config["hidden_dropout_prob"] = config["attention_probs_dropout_prob"] = 0.0
  return BertForPretraining(**config).load_from_pretrained(checkpoint_path)

def get_data_bert(GPUS:list[str], it):
  data: dict[str, Tensor] = next(it)
  for key in data.keys(): data[key].shard_(GPUS, axis=0)
  return data

def get_fake_data_bert(GPUS:list[str], BS:int):
  return {
    "input_ids": Tensor.zeros((BS, 512), dtype=dtypes.float32).contiguous().shard_(GPUS, axis=0),
    "input_mask": Tensor.zeros((BS, 512), dtype=dtypes.default_float).contiguous().shard_(GPUS, axis=0),
    "segment_ids": Tensor.zeros((BS, 512), dtype=dtypes.float32).contiguous().shard_(GPUS, axis=0),
    "masked_lm_positions": Tensor.zeros((BS, 512), dtype=dtypes.float32).contiguous().shard_(GPUS, axis=0),
    "masked_lm_ids": Tensor.zeros((BS, 512), dtype=dtypes.float32).contiguous().shard_(GPUS, axis=0),
    "masked_lm_weights": Tensor.zeros((BS, 512), dtype=dtypes.float32).contiguous().shard_(GPUS, axis=0),
    "next_sentence_labels": Tensor.zeros((BS, 1), dtype=dtypes.float32).contiguous().shard_(GPUS, axis=0),
  }

def box_iou_np(boxes1:np.ndarray, boxes2:np.ndarray):
  def box_area(boxes): return (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
  area1 = box_area(boxes1)
  area2 = box_area(boxes2)
  
  lt = np.maximum(boxes1[:, None, :2], boxes2[:, :2])  # [N,M,2]
  rb = np.minimum(boxes1[:, None, 2:], boxes2[:, 2:])  # [N,M,2]
  wh = (rb-lt).clip(min=0)
  inter = wh[:, :, 0] * wh[:, :, 1]
  union = area1[:, None] + area2 - inter
  return inter / union

def matcher_np(match_quality_matrix:np.ndarray):
  assert match_quality_matrix.size != 0, 'Need valid matrix'

  def set_low_quality_matches_(matches:np.ndarray, all_matches:np.ndarray, match_quality_matrix:np.ndarray):
    # For each gt, find the prediction with which it has highest quality
    highest_quality_foreach_gt= match_quality_matrix.max(axis=1)
    # Find highest quality match available, even if it is low, including ties
    gt_pred_pairs_of_highest_quality = np.nonzero(match_quality_matrix == highest_quality_foreach_gt[:, None])
    pred_inds_to_update = gt_pred_pairs_of_highest_quality[1]
    matches[pred_inds_to_update] = all_matches[pred_inds_to_update]

  # match_quality_matrix is M (gt) x N (predicted)
  # Max over gt elements (dim 0) to find best gt candidate for each prediction
  matched_vals, matches = match_quality_matrix.max(axis=0), match_quality_matrix.argmax(axis=0)
  all_matches = np.copy(matches)

  # Assign candidate matches with low quality to negative (unassigned) values
  below_low_threshold = matched_vals < 0.4
  between_thresholds = (matched_vals >= 0.4) & (matched_vals < 0.5)
  matches[below_low_threshold] = -1
  matches[between_thresholds] = -2

  assert all_matches is not None
  set_low_quality_matches_(matches, all_matches, match_quality_matrix)
  return matches

def matcher_iou_func(box, anchor): return matcher_np(box_iou_np(box, anchor))

def resize_boxes_np(boxes:np.ndarray, original_size: List[int], new_size: List[int]) -> np.ndarray:
  rh, rw = [n / o for n, o in zip(new_size, original_size)]
  return boxes * np.tile([rw, rh, rw, rh], (boxes.shape[0], 1))