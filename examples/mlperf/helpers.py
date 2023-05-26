import numpy as np
import collections
import unicodedata

def _get_best_indices(logits, n_best_size):
  index_and_score = sorted(enumerate(logits), key=lambda x: x[1], reverse=True)
  best_indices = []
  for i in range(len(index_and_score)):
    if i >= n_best_size:
      break
    best_indices.append(index_and_score[i][0])
  return best_indices

def _is_punctuation(char):
  cp = ord(char)
  if ((cp >= 33 and cp <= 47) or (cp >= 58 and cp <= 64) or (cp >= 91 and cp <= 96) or (cp >= 123 and cp <= 126)):
    return True
  cat = unicodedata.category(char)
  if cat.startswith("P"):
    return True
  return False

def _run_split_on_punc(text):
  if text in ("[UNK]", "[SEP]", "[PAD]", "[CLS]", "[MASK]"):
    return [text]
  chars = list(text)
  i = 0
  start_new_word = True
  output = []
  while i < len(chars):
    char = chars[i]
    if _is_punctuation(char):
      output.append([char])
      start_new_word = True
    else:
      if start_new_word:
        output.append([])
      start_new_word = False
      output[-1].append(char)
    i += 1

  return ["".join(x) for x in output]

def get_final_text(pred_text, orig_text, do_lower_case):
  def _strip_spaces(text):
    ns_chars = []
    ns_to_s_map = collections.OrderedDict()
    for (i, c) in enumerate(text):
      if c == " ":
        continue
      ns_to_s_map[len(ns_chars)] = i
      ns_chars.append(c)
    ns_text = "".join(ns_chars)
    return (ns_text, ns_to_s_map)

  orig_tokens = orig_text.strip().split()
  split_tokens = []
  for token in orig_tokens:
    if token not in ("[UNK]", "[SEP]", "[PAD]", "[CLS]", "[MASK]"):
      token = token.lower()
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
    ns_start_position = tok_s_to_ns_map[start_position]
    if ns_start_position in orig_ns_to_s_map:
      orig_start_position = orig_ns_to_s_map[ns_start_position]

  if orig_start_position is None:
    return orig_text

  orig_end_position = None
  if end_position in tok_s_to_ns_map:
    ns_end_position = tok_s_to_ns_map[end_position]
    if ns_end_position in orig_ns_to_s_map:
      orig_end_position = orig_ns_to_s_map[ns_end_position]

  if orig_end_position is None:
    return orig_text

  output_text = orig_text[orig_start_position:(orig_end_position + 1)]
  return output_text

def get_bert_qa_prediction(feature, example, start_logits, end_logits):
  prelim_predictions = []
  start_indices = _get_best_indices(start_logits, 20)
  end_indices = _get_best_indices(end_logits, 20)
  for start_index in start_indices:
    for end_index in end_indices:
      if start_index >= len(feature["tokens"]):
        continue
      if end_index >= len(feature["tokens"]):
        continue
      if start_index not in feature["token_to_orig_map"]:
        continue
      if end_index not in feature["token_to_orig_map"]:
        continue
      if not feature["token_is_max_context"].get(start_index, False):
        continue
      if end_index < start_index:
        continue
      length = end_index - start_index + 1
      if length > 64:
        continue

      prelim_predictions.append({
        "start_index": start_index,
        "end_index": end_index,
        "start_logit": start_logits[start_index],
        "end_logit": end_logits[end_index]
      })
  prelim_predictions = sorted(prelim_predictions, key=lambda x: (x["start_logit"] + x["end_logit"]), reverse=True)

  for pred in prelim_predictions:
    tok_tokens = feature["tokens"][pred["start_index"]:(pred["end_index"] + 1)]
    orig_doc_start = feature["token_to_orig_map"][pred["start_index"]]
    orig_doc_end = feature["token_to_orig_map"][pred["end_index"]]
    orig_tokens = example["context"][orig_doc_start:(orig_doc_end + 1)]
    tok_text = " ".join(tok_tokens)
    tok_text = tok_text.replace(" ##", "")
    tok_text = tok_text.replace("##", "")
    tok_text = tok_text.strip()
    tok_text = " ".join(tok_text.split())
    orig_text = " ".join(orig_tokens)

    return get_final_text(tok_text, orig_text, True)
  return "empty"
