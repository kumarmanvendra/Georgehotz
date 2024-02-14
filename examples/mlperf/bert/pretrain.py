import json, math, time
from pathlib import Path
import numpy as np
from tinygrad.helpers import getenv
from tinygrad.features.jit import TinyJit
from tinygrad.ops import GlobalCounters
from tinygrad.nn import optim
from tinygrad.nn.state import get_parameters, load_state_dict, safe_load, safe_save, get_state_dict
from tinygrad.tensor import Tensor, dtypes
from extra.lr_scheduler import OneCycleLR
from extra.models.bert import Bert
from extra.datasets.wikipedia import iterate

if getenv('HALF', 0):
  Tensor.default_type = dtypes.float16
  np_dtype = np.float16
else:
  Tensor.default_type = dtypes.float32
  np_dtype = np.float32

BS, EVAL_BS, STEPS, MAX_EVAL_STEPS, WARMUP_STEPS, EPOCH, MAX_LR  = getenv("BS", 32), getenv('EVAL_BS', 8), getenv("STEPS", 100000), getenv("MAX_EVAL_STEPS", 100), getenv("WARMUP_STEPS", 10000), getenv("EPOCHS", 30), getenv('MAX_LR', 2.0)
EVAL_FREQ = math.floor(min(0.05*(230.23 * BS + 3000000), 25000))

def get_model_and_config(path:str):
  with open(path, 'r') as f:
    config = json.load(f)
  model = Bert(
    config["hidden_size"],
    config["intermediate_size"], 
    config["max_position_embeddings"], 
    config["num_attention_heads"], 
    config["num_hidden_layers"], 
    config["type_vocab_size"], 
    config["vocab_size"], 
    config["attention_probs_dropout_prob"], 
    config["hidden_dropout_prob"]
  )
  p_weights = Tensor.uniform(*(config["hidden_size"], config["hidden_size"]), low=-0.1, high=0.1) #TODO: change init range
  s_weights = Tensor.uniform(*(2, config["hidden_size"]), low=-0.1, high=0.1) #TODO: change init range
  s_bias = Tensor.zeros(2)
  m_weights = Tensor.uniform(*(config["hidden_size"], config["hidden_size"]), low=-0.1, high=0.1) #TODO: change init range
  m_bias = Tensor.zeros((config["vocab_size"],))
  if getenv('USE_PRETRAINED'):
    load_state_dict(model, safe_load("/tmp/bert.safetensor"))
    p_weights = load_state_dict(p_weights, safe_load("/tmp/p_weights.safetensor"))
    s_weights = load_state_dict(s_weights, safe_load("/tmp/s_weights.safetensor"))
    s_bias = load_state_dict(s_bias, safe_load("/tmp/s_bias.safetensor"))
    m_weights = load_state_dict(m_weights, safe_load("/tmp/m_weights.safetensor"))
    m_bias = load_state_dict(m_bias, safe_load("/tmp/m_bias.safetensor"))
  embedding_table = model.embeddings.word_embeddings.weight
  return model, embedding_table, s_weights, s_bias, m_weights, m_bias, p_weights 

def pool_output(output:Tensor, weights:Tensor): return Tensor.tanh(output[:, 0].linear(weights))

def gather_indexes(sequence_tensor:Tensor, positions:Tensor):
  assert len(sequence_tensor.shape) == 3, f"Expected tensor to have rank 3, but got {len(sequence_tensor.shape)}"
  sequence_shape = list(sequence_tensor.shape)
  batch_size, seq_length, width = sequence_shape[0], sequence_shape[1], sequence_shape[2]

  flat_offsets = Tensor.arange(0, batch_size, requires_grad=False).reshape([1, -1]) * seq_length
  flat_positions = (positions + flat_offsets.reshape(-1, 1)).reshape([-1])
  flat_sequence_tensor = sequence_tensor.reshape([batch_size * seq_length, width])
  return flat_sequence_tensor[flat_positions]

def get_masked_lm_output(input_tensor:Tensor, output_weights:Tensor, transform_weights:Tensor, transform_bias:Tensor, positions:Tensor, label_ids:Tensor): 
  input_tensor = gather_indexes(input_tensor, positions)
  input_tensor = Tensor.gelu(input_tensor.matmul(transform_weights))
  input_tensor = Tensor.layernorm(input_tensor)
  output = input_tensor.matmul(output_weights.transpose()).add(transform_bias)
  return output.sparse_categorical_crossentropy(label_ids.flatten())

def get_masked_lm_accuracy(input_tensor:Tensor, output_weights:Tensor, transform_weights:Tensor, transform_bias:Tensor, positions:Tensor, label_ids:Tensor):
  input_tensor = gather_indexes(input_tensor, positions)
  input_tensor = Tensor.gelu(input_tensor.matmul(transform_weights))
  input_tensor = Tensor.layernorm(input_tensor)
  logits = input_tensor.matmul(output_weights.transpose()).add(transform_bias)
  log_probs = logits.log_softmax()
  predictions = log_probs.argmax(axis=-1)
  correct_predictions = predictions == label_ids.flatten()
  return correct_predictions.float().mean()

def get_next_sentence_output(input_tensor:Tensor, labels: Tensor, weights:Tensor, bias:Tensor):
  output = input_tensor.matmul(weights.transpose()).add(bias)
  return output.log_softmax().binary_crossentropy_logits(labels)

def pretrain():
  model, embedding_table, s_weights, s_bias, m_weights, m_bias, p_weights = get_model_and_config(Path(__file__).parent.parents[2] / "extra" / "datasets" / "wiki" / "bert_config.json")
  optimizer = optim.LAMB(get_parameters(model), 1 / WARMUP_STEPS, eps=1e-6, wd=0.01, adam=True) # TODO: Keep in FP32?, Exclude LayerNorm, and bias from weight decay
  lr_scheduler = OneCycleLR(optimizer, MAX_LR, MAX_LR * WARMUP_STEPS, MAX_LR * 1e12, STEPS, WARMUP_STEPS / STEPS)

  @TinyJit
  def eval_step_jitted(model, embedding_table, input_ids, input_mask, segment_ids, masked_lm_ids, masked_lm_positions):
    output = model(input_ids=input_ids, attention_mask=input_mask, token_type_ids=segment_ids)
    acc = get_masked_lm_accuracy(output, embedding_table, m_weights, m_bias, masked_lm_positions, masked_lm_ids)
    return acc.realize()

  @TinyJit
  def train_step_jitted(model, embedding_table, optimizer, lr_scheduler, input_ids, input_mask, segment_ids, masked_lm_ids, masked_lm_positions, next_sentence_labels):
    output = model(input_ids=input_ids, attention_mask=input_mask, token_type_ids=segment_ids)
    pooled_output = pool_output(output, p_weights)

    masked_lm_loss = get_masked_lm_output(output, embedding_table, m_weights, m_bias, masked_lm_positions, masked_lm_ids)
    next_sentence_loss = get_next_sentence_output(pooled_output, next_sentence_labels, s_weights, s_bias)
    loss = masked_lm_loss + next_sentence_loss

    if not getenv('DISABLE_BACKWARD', 0):
      optimizer.zero_grad()
      loss.backward()

      optimizer.step()
      lr_scheduler.step()
    return loss.realize()
  
  def get_data(X):
    input_ids = Tensor(X["input_ids"])
    input_mask = Tensor(X["input_mask"])
    segment_ids = Tensor(X["segment_ids"])
    masked_lm_ids = Tensor(X["masked_lm_ids"], dtype=dtypes.int32)
    masked_lm_positions = Tensor(X["masked_lm_positions"], dtype=dtypes.int32)
    next_sentence_labels = Tensor(X["next_sentence_labels"], dtype=dtypes.int32)
    return input_ids.realize(), input_mask.realize(), segment_ids.realize(), masked_lm_ids.realize(), masked_lm_positions.realize(), next_sentence_labels.realize()
  
  train_batcher = iterate(bs=BS, val=False)
  eval_batcher = iterate(bs=EVAL_BS, val=True)
  accuracy_achieved = False
  wallclock_start = time.monotonic()
  for _ in range(EPOCH):
    i = 0
    while i <= STEPS:
      if i % EVAL_FREQ == 0 and i > 0:
        e = 0
        while e <= MAX_EVAL_STEPS:
          st = time.monotonic()
          X, _ = next(eval_batcher)
          input_ids, input_mask, segment_ids, masked_lm_ids, masked_lm_positions, next_sentence_labels = get_data(X)
          acc = eval_step_jitted(model, embedding_table, input_ids, input_mask, segment_ids, masked_lm_ids, masked_lm_positions)
          et = time.monotonic()
          acc = acc.numpy()
          cl = time.monotonic()
          
          acc = (sum(acc) / len(acc))*100.0 if getenv('DIST') else acc
          print(f"MLM accuarcy: {acc:.2f}%, val_loss STEP={i} (in {(time.monotonic()-st)*1e3:.2f} ms)")
          if acc > 72.0:
            wallclock_end = time.monotonic()
            hours, remainder = divmod(wallclock_end - wallclock_start, 3600)
            minutes, seconds = divmod(remainder, 60)
            print(f"MLM accuracy achieved in {int(hours)} hours, {int(minutes)} minutes, and {int(seconds)} seconds.")
            accuracy_achieved = True
            print("Saving weights...")
            safe_save(get_state_dict(model), "bert.safetensors")
            safe_save(get_state_dict(p_weights), "/tmp/p_weights.safetensor")
            safe_save(get_state_dict(s_weights), "/tmp/s_weights.safetensor")
            safe_save(get_state_dict(s_bias), "/tmp/s_bias.safetensor")
            safe_save(get_state_dict(m_weights), "/tmp/m_weights.safetensor")
            safe_save(get_state_dict(m_bias), "/tmp/m_bias.safetensor")
            break
          e += 1
          st = cl
      if accuracy_achieved or STEPS == 0 or i == STEPS: break

      if accuracy_achieved: break

      st = time.monotonic()
      X, _ = next(train_batcher)
      input_ids, input_mask, segment_ids, masked_lm_ids, masked_lm_positions, next_sentence_labels = get_data(X)
      GlobalCounters.reset()
      loss = train_step_jitted(model, embedding_table, optimizer, lr_scheduler, input_ids, input_mask, segment_ids, masked_lm_ids, masked_lm_positions, next_sentence_labels)

      et = time.monotonic()
      loss_cpu = loss.numpy()
      cl = time.monotonic()

      print(f"{i:3d} {(cl-st)*1000.0:7.2f} ms run, {(et-st)*1000.0:7.2f} ms python, {(cl-et)*1000.0:7.2f} ms CL, {loss_cpu:7.2f} loss, {optimizer.lr.numpy()[0]:.6f} LR, {GlobalCounters.mem_used/1e9:.2f} GB used, {GlobalCounters.global_ops*1e-9/(cl-st):9.2f} GFLOPS")
      st = cl
      i += 1

if __name__ == "__main__":
  with Tensor.train(): pretrain()