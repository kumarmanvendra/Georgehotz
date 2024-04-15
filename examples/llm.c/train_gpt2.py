#!/usr/bin/env python3
import os, math, time
import numpy as np
from tinygrad import Tensor, nn, fetch, Device
from dataclasses import dataclass

@dataclass
class GPTConfig:
  block_size: int = 1024
  vocab_size: int = 50257
  n_layer: int = 12
  n_head: int = 12
  n_embd: int = 768

class CausalSelfAttention:
  def __init__(self, config:GPTConfig):
    assert config.n_embd % config.n_head == 0
    # key, query, value projections for all heads, but in a batch
    self.c_attn = nn.Linear(config.n_embd, 3 * config.n_embd)
    # output projection
    self.c_proj = nn.Linear(config.n_embd, config.n_embd)
    # regularization
    self.n_head = config.n_head
    self.n_embd = config.n_embd
    # not really a 'bias', more of a mask, but following the OpenAI/HF naming though
    self.bias = Tensor.ones(1, 1, config.block_size, config.block_size).tril()
    self.bias.requires_grad = False

  def __call__(self, x:Tensor):
    B, T, C = x.shape
    qkv = self.c_attn(x)
    q, k, v = qkv.split(self.n_embd, dim=2)
    k = k.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
    q = q.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
    v = v.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)

    # manual implementation of attention
    att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
    att = att.masked_fill(self.bias[:,:,:T,:T] == 0, float('-inf'))
    att = att.softmax()
    y = att @ v # (B, nh, T, T) x (B, nh, T, hs) -> (B, nh, T, hs)
    y = y.transpose(1, 2).view(B, T, C) # re-assemble all head outputs side by side
    # output projection
    y = self.c_proj(y)
    return y

class MLP:
  def __init__(self, config:GPTConfig):
    self.c_fc = nn.Linear(config.n_embd, 4 * config.n_embd)
    self.c_proj = nn.Linear(4 * config.n_embd, config.n_embd)

  def __call__(self, x:Tensor) -> Tensor:
    return self.c_proj(self.c_fc(x).gelu())

class Block:
  def __init__(self, config:GPTConfig):
    self.ln_1 = nn.LayerNorm(config.n_embd)
    self.attn = CausalSelfAttention(config)
    self.ln_2 = nn.LayerNorm(config.n_embd)
    self.mlp = MLP(config)

  def __call__(self, x:Tensor):
    x = x + self.attn(self.ln_1(x))
    x = x + self.mlp(self.ln_2(x))
    return x

class GPT:
  def __init__(self, config:GPTConfig):
    self.config = config

    self.wte = nn.Embedding(config.vocab_size, config.n_embd)
    self.wpe = nn.Embedding(config.block_size, config.n_embd)
    self.h = [Block(config) for _ in range(config.n_layer)]
    self.ln_f = nn.LayerNorm(config.n_embd)
    self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)
    self.wte.weight = self.lm_head.weight # https://paperswithcode.com/method/weight-tying

  def load_pretrained(self):
    weights = nn.state.torch_load(fetch(f'https://huggingface.co/gpt2/resolve/main/pytorch_model.bin'))
    transposed = ('attn.c_attn.weight', 'attn.c_proj.weight', 'mlp.c_fc.weight', 'mlp.c_proj.weight')
    for k in weights:
      if k.endswith(transposed):
        weights[k] = weights[k].to(Device.DEFAULT).T.contiguous()
    # lm head and wte are tied
    weights['lm_head.weight'] = weights['wte.weight']
    nn.state.load_state_dict(self, weights)

  def generate(self, idx, max_new_tokens, temperature=1.0, top_k=None):
    for _ in range(max_new_tokens):
      idx_cond = idx if idx.shape[1] <= self.config.block_size else idx[:, -self.config.block_size:]
      logits, _ = self(idx_cond)
      logits = logits[:, -1, :] / temperature
      idx_next = logits.softmax().multinomial()
      idx = Tensor.cat(idx, idx_next, dim=1)
    return idx

  def __call__(self, idx:Tensor, targets=None):
    b, t = idx.shape
    pos = Tensor.arange(0, t)

    tok_emb = self.wte(idx) # token embeddings of shape (b, t, n_embd)
    pos_emb = self.wpe(pos) # position embeddings of shape (t, n_embd)
    x = tok_emb + pos_emb

    x = self.ln_f(x.sequential(self.h))

    if targets is not None:
      logits = self.lm_head(x)
      loss = logits.sparse_categorical_crossentropy(targets)
    else:
      logits = self.lm_head(x[:, [-1], :])
      loss = None

    return logits, loss

if __name__ == "__main__":
  import tiktoken, argparse

  parser = argparse.ArgumentParser()
  parser.add_argument("--num_iterations", type=int, default=10, help="number of iterations to run")
  parser.add_argument("--batch_size", type=int, default=4, help="batch size")
  parser.add_argument("--sequence_length", type=int, default=64, help="sequence length")
  args = parser.parse_args()
  B, T = args.batch_size, args.sequence_length
  assert 1 <= T <= 1024

  model = GPT(GPTConfig(n_layer=12, n_head=12, n_embd=768))
  model.load_pretrained()

  # init the tokenizer
  enc = tiktoken.get_encoding("gpt2")
  encode = lambda s: enc.encode(s, allowed_special={"<|endoftext|>"})
  decode = lambda l: enc.decode(l)

  # load the tokens
  # prefer to use tiny_shakespeare if it's available, otherwise use tiny_stories
  # we're using val instead of train split just because it is smaller/faster
  shake_tokens_bin = "data/tiny_shakespeare_val.bin"
  story_tokens_bin = "data/TinyStories_val.bin"
  assert os.path.isfile(shake_tokens_bin) or os.path.isfile(story_tokens_bin), "you must run prepro on some dataset"
  tokens_bin = shake_tokens_bin if os.path.isfile(shake_tokens_bin) else story_tokens_bin
  assert os.path.isfile(tokens_bin)
  print(f"loading cached tokens in {tokens_bin}")
  with open(tokens_bin, "rb") as f:
    tokens = np.frombuffer(f.read(), dtype=np.int32)
  tokens = Tensor(tokens)

  # lightweight dataloader
  def get_batch():
    assert B*T+1 <= len(tokens), "not enough tokens"
    # for 338,025 tokens. E.g. with B=8 T=1024, this will yield 41 batches before looping
    i = 0
    while True:
      x = tokens[i:i+B*T].view(B, T)
      y = tokens[i+1:i+B*T+1].view(B, T)
      yield x, y
      i += B*T
      if i + B*T + 1 >= len(tokens):
        i = 0 # in prod we'd want to randomize the start point a bit

  # forward backward for a few iterations
  data_iter = iter(get_batch())
  x, y = next(data_iter) # we'll overfit this batch below
  optimizer = nn.optim.Adam(nn.state.get_parameters(model), lr=1e-4)
  for i in range(args.num_iterations):
    t0 = time.time()
    logits, loss = model(x, y)
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()
    Device[Device.DEFAULT].synchronize()
    t1 = time.time()
    print(f"iteration {i}, loss: {loss.item()}, time: {(t1-t0)*1000:.3f}ms")

  start = "<|endoftext|>"
  start_ids = encode(start)
  x = (Tensor(start_ids)[None, ...])
  max_new_tokens = 16
  temperature = 1.0
  top_k = 40
  y = model.generate(x, max_new_tokens, temperature=temperature, top_k=top_k)
  print(decode(y[0].tolist()))

