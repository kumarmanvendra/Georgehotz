import sys, os, logging, time, io, math, argparse, operator, numpy as np
from functools import partial, reduce
from pathlib import Path
from typing import Tuple, Optional
from tinygrad import nn
from tinygrad.tensor import Tensor
from tinygrad.helpers import dtypes, getenv
from tinygrad.state import torch_load
from examples.vits import ResidualCouplingBlock, PosteriorEncoder, Encoder, ResBlock1, ResBlock2, LRELU_SLOPE, sequence_mask, split, download_if_not_present, get_hparams_from_file, load_checkpoint, weight_norm, HParams
from examples.sovits_helpers import preprocess
import soundfile

# original code: https://github.com/svc-develop-team/so-vits-svc

DEBUG = getenv("DEBUG")

F0_BIN = 256
F0_MAX = 1100.0
F0_MIN = 50.0
F0_MEL_MIN = 1127 * np.log(1 + F0_MIN / 700)
F0_MEL_MAX = 1127 * np.log(1 + F0_MAX / 700)

# original code for contentvec: https://github.com/auspicious3000/contentvec/
class ContentVec:
  # TODO: self.label_embs_concat and self.final_proj dims are hardcoded
  # and depend on fairseq.data.dictionary Dictionary in the checkpoint.
  # This param can't yet be loaded since there is no pickle for it. See with DEBUG=2.
  # This means that the model only works with the given weights (they are the same for every svc model)
  def __init__(self, cfg: HParams):
    self.feature_grad_mult, self.untie_final_proj = cfg.feature_grad_mult, cfg.untie_final_proj
    feature_enc_layers = eval(cfg.conv_feature_layers)
    self.embed = feature_enc_layers[-1][0]
    final_dim = cfg.final_dim if cfg.final_dim > 0 else cfg.encoder_embed_dim
    self.feature_extractor = ConvFeatureExtractionModel(conv_layers=feature_enc_layers, dropout=0.0, mode=cfg.extractor_mode, conv_bias=cfg.conv_bias)
    self.post_extract_proj = nn.Linear(self.embed, cfg.encoder_embed_dim) if self.embed != cfg.encoder_embed_dim else None
    self.encoder = TransformerEncoder(cfg)
    self.layer_norm = nn.LayerNorm(self.embed)
    self.final_proj = nn.Linear(cfg.encoder_embed_dim, final_dim * 1) if self.untie_final_proj else nn.Linear(cfg.encoder_embed_dim, final_dim)
    self.mask_emb = Tensor.uniform(cfg.encoder_embed_dim, dtype=dtypes.float32)
    self.label_embs_concat = Tensor.uniform(504, final_dim, dtype=dtypes.float32)

  def forward_features(self, source, padding_mask):
    if self.feature_grad_mult > 0:
      features = self.feature_extractor(source, padding_mask)
      if self.feature_grad_mult != 1.0:
        features = features  # GradMultiply.forward(features, self.feature_grad_mult): this isn't required for inference
    else:
      features = self.feature_extractor(source, padding_mask)
    return features

  def forward_padding_mask(self, features, padding_mask):
    # replaces original forward_padding_mask for batch inference
    lengths_org = tilde(padding_mask.cast(dtypes.bool)).cast(dtypes.int64).sum(1)  # ensure its bool for tilde
    lengths = (lengths_org - 400).float().div(320).floor().cast(dtypes.int64) + 1  # intermediate float to divide
    padding_mask = lengths_to_padding_mask(lengths)
    return padding_mask

  def extract_features(self, source: Tensor, spk_emb:Tensor=None, padding_mask=None, ret_conv=False, output_layer=None, tap=False):
    features = self.forward_features(source, padding_mask)
    if padding_mask is not None:
      padding_mask = self.forward_padding_mask(features, padding_mask)
    features = features.transpose(1, 2)
    features = self.layer_norm(features)
    if self.post_extract_proj is not None:
      features = self.post_extract_proj(features)
    x, layer_results = self.encoder(features, spk_emb, padding_mask=padding_mask, layer=(None if output_layer is None else output_layer - 1), tap=tap)
    res = features if ret_conv else x
    return res, padding_mask

  @classmethod
  def load_model(cls, checkpoint_path:str, checkpoint_url:str):
    download_if_not_present(checkpoint_path, checkpoint_url)
    cfg = load_fairseq_cfg(checkpoint_path)
    enc = cls(cfg.model)
    _ = load_checkpoint_enc(checkpoint_path, enc, None)
    logging.debug(f"{cls.__name__}: Loaded model with cfg={cfg}")
    return enc

class SpeechEncoder:
  def __init__(self, hidden_dim, model:ContentVec):
    self.hidden_dim = hidden_dim
    self.model = model

  def encode(self, ): raise NotImplementedError("implement me")

  @classmethod
  def load_model(cls, checkpoint_path:str, checkpoint_url:str):
    contentvec = ContentVec.load_model(checkpoint_path, checkpoint_url)
    return cls(contentvec)

class ContentVec256L9(SpeechEncoder):
  def __init__(self, model:ContentVec):
    super().__init__(hidden_dim=256, model=model)

  def encode(self, wav: Tensor):
    feats = wav
    if len(feats.shape) == 2:  # double channels
      feats = feats.mean(-1)
    assert len(feats.shape) == 1, feats.dim()
    feats = feats.reshape(1, -1)
    padding_mask = Tensor.zeros_like(feats).cast(dtypes.bool)
    logits = self.model.extract_features(feats.to(wav.device), padding_mask=padding_mask.to(wav.device), output_layer=9)
    feats = self.model.final_proj(logits[0])
    return feats.transpose(1,2)

class ContentVec768L12(SpeechEncoder):
  def __init__(self, model:ContentVec):
    super().__init__(hidden_dim=768, model=model)

  def encode(self, wav: Tensor):
    feats = wav
    if len(feats.shape) == 2:  # double channels
      feats = feats.mean(-1)
    assert len(feats.shape) == 1, feats.dim()
    feats = feats.reshape(1, -1)
    padding_mask = Tensor.zeros_like(feats).cast(dtypes.bool)
    logits = self.model.extract_features(feats.to(wav.device), padding_mask=padding_mask.to(wav.device), output_layer=12)
    return logits[0].transpose(1,2)

class TransformerEncoder:
  def __init__(self, cfg: HParams):
    def make_conv() -> nn.Conv1d:
      layer = nn.Conv1d(self.embedding_dim, self.embedding_dim, kernel_size=cfg.conv_pos, padding=cfg.conv_pos // 2, groups=cfg.conv_pos_groups)
      std = std = math.sqrt(4 / (cfg.conv_pos * self.embedding_dim))
      layer.weight, layer.bias = (Tensor.normal(*layer.weight.shape, std=std)), (Tensor.zeros(*layer.bias.shape))
      # for training: layer.weights need to be weight_normed
      return layer
    self.dropout, self.embedding_dim, self.layer_norm_first, self.layerdrop, self.num_layers, self.num_layers_1 = cfg.dropout, cfg.encoder_embed_dim, cfg.layer_norm_first, cfg.encoder_layerdrop, cfg.encoder_layers, cfg.encoder_layers_1
    self.pos_conv = [make_conv(), SamePad(cfg.conv_pos), Tensor.gelu]
    self.layers = [
      TransformerSentenceEncoderLayer(embedding_dim=self.embedding_dim,
                                      ffn_embedding_dim=cfg.encoder_ffn_embed_dim,
                                      num_attention_heads=cfg.encoder_attention_heads,
                                      dropout=0.0,  # training: dropout=self.dropout,
                                      attention_dropout=0.0,  # training: attention_dropout=cfg.attention_dropout,
                                      activation_dropout=0.0,  # training: activation_dropout=cfg.activation_dropout,
                                      activation_fn=cfg.activation_fn,
                                      layer_norm_first=self.layer_norm_first,
                                      cond_layer_norm=False)
      for _ in range(cfg.encoder_layers)
      ]
    for _ in range(cfg.encoder_layers_1):  # this one uses CondLayerNorm
      self.layers.append(
        TransformerSentenceEncoderLayer(embedding_dim=self.embedding_dim,
                                        ffn_embedding_dim=cfg.encoder_ffn_embed_dim,
                                        num_attention_heads=cfg.encoder_attention_heads,
                                        dropout=0.0,  # dropout=self.dropout,
                                        attention_dropout=0.0,  # training: attention_dropout=cfg.attention_dropout,
                                        activation_dropout=0.0,  # training: activation_dropout=cfg.activation_dropout,
                                        activation_fn=cfg.activation_fn,
                                        layer_norm_first=self.layer_norm_first,
                                        cond_layer_norm=True)
      )
    self.layer_norm = nn.LayerNorm(self.embedding_dim)
    self.cond_layer_norm = CondLayerNorm(self.embedding_dim) if cfg.encoder_layers_1 > 0 else None
    # training: apply init_bert_params

  def __call__(self, x, spk_emb, padding_mask=None, layer=None, tap=False):
    x, layer_results = self.extract_features(x, spk_emb, padding_mask, layer, tap)
    if self.layer_norm_first and layer is None:
      x = self.cond_layer_norm(x, spk_emb) if (self.num_layers_1 > 0) else self.layer_norm(x)
    return x, layer_results

  def extract_features(self, x: Tensor, spk_emb: Tensor, padding_mask=None, tgt_layer=None, tap=False):
    if tgt_layer is not None:  # and not self.training
      assert tgt_layer >= 0 and tgt_layer < len(self.layers)
    if padding_mask is not None:
      # x[padding_mask] = 0
      assert padding_mask.shape == x.shape[:len(padding_mask.shape)]  # first few dims of x must match padding_mask
      tmp_mask = padding_mask.unsqueeze(-1).repeat((1, 1, x.shape[-1]))
      tmp_mask = tilde(tmp_mask.cast(dtypes.bool))
      x = tmp_mask.where(x, 0)
    x_conv = x.transpose(1,2).sequential(self.pos_conv)
    x_conv = x_conv.transpose(1, 2)
    x = x + x_conv
    x = x.transpose(0, 1)  # B x T x C -> T x B x C
    if not self.layer_norm_first: x = self.layer_norm(x)
    x = x.dropout(p=self.dropout)
    layer_results = []
    r = None
    for i, layer in enumerate(self.layers):
      if i < self.num_layers:  # if (not self.training or (dropout_probability > self.layerdrop)) and (i < self.num_layers):
        assert layer.cond_layer_norm == False
        x = layer(x, self_attn_padding_mask=padding_mask, need_weights=False)
        if tgt_layer is not None or tap:
          layer_results.append(x.transpose(0, 1))
      if i>= self.num_layers:
        assert layer.cond_layer_norm == True
        x = layer(x, emb=spk_emb, self_attn_padding_mask=padding_mask, need_weights=False)
      if i == tgt_layer:
        r = x
        break
    if r is not None:
      x = r
    x = x.transpose(0, 1)  # T x B x C -> B x T x C
    return x, layer_results

class TransformerSentenceEncoderLayer:
  def __init__(self, embedding_dim=768.0, ffn_embedding_dim=3072.0, num_attention_heads=8.0, dropout=0.1, attention_dropout=0.1, activation_dropout=0.1, activation_fn="relu", layer_norm_first=False, cond_layer_norm=False):
    def get_activation_fn(activation):
      if activation == "relu":
        return Tensor.relu
      if activation == "gelu":
        return Tensor.gelu
      else:
        raise RuntimeError(f"activation function={activation} is not forseen")
    self.embedding_dim, self.dropout, self.activation_dropout, self.layer_norm_first, self.num_attention_heads = embedding_dim, dropout, activation_dropout, layer_norm_first, num_attention_heads
    self.activation_fn = get_activation_fn(activation_fn)
    self.cond_layer_norm = cond_layer_norm
    self.self_attn = MultiHeadAttention(self.embedding_dim, self.num_attention_heads)
    self.self_attn_layer_norm = nn.LayerNorm(self.embedding_dim) if not cond_layer_norm else CondLayerNorm(self.embedding_dim)
    self.fc1 = nn.Linear(self.embedding_dim, ffn_embedding_dim)
    self.fc2 = nn.Linear(ffn_embedding_dim, self.embedding_dim)
    self.final_layer_norm = nn.LayerNorm(self.embedding_dim) if not cond_layer_norm else CondLayerNorm(self.embedding_dim)

  def __call__(self, x:Tensor, self_attn_mask:Tensor=None, self_attn_padding_mask:Tensor=None, emb:Tensor=None, need_weights=False):  # TODO: refactor this later
    # TODO: might need to do the following to self_attn_padding_mask:
    #self_attn_padding_mask = self_attn_padding_mask.view(x.shape[0], 1, 1, self_attn_padding_mask.shape[1]).expand(-1, self.num_attention_heads, -1, -1).reshape(x.shape[0] * self.num_attention_heads, 1, self_attn_padding_mask.shape[1]) if self_attn_padding_mask is not None else None
    assert self_attn_mask == None
    assert emb == None
    assert self_attn_padding_mask is not None
    residual = x
    if self.layer_norm_first:
      #if self_attn_mask is None and self_attn_padding_mask is not None: self_attn_mask = self_attn_padding_mask
      #elif self_attn_padding_mask is None and self_attn_mask is not None: self_attn_padding_mask = self_attn_mask
      #mask = self_attn_mask.cast(dtypes.bool) + self_attn_padding_mask.cast(dtypes.bool) if (self_attn_mask is not None) else None  # logical or them here
      x = self.self_attn_layer_norm(x) if not self.cond_layer_norm else self.self_attn_layer_norm(x, emb)
      x = self.self_attn(x=x, mask=self_attn_padding_mask)
      x = x.dropout(self.dropout)
      x = residual + x
      x = self.final_layer_norm(x) if not self.cond_layer_norm else self.final_layer_norm(x, emb)
      x = self.activation_fn(self.fc1(x))
      x = x.dropout(self.activation_dropout)
      x = self.fc2(x)
      x = x.dropout(self.dropout)
      x = residual + x
    else:
      x = self.self_attn(x=x, mask=self_attn_padding_mask)
      x = x.dropout(self.dropout)
      x = residual + x
      x = self.self_attn_layer_norm(x) if not self.cond_layer_norm else self.self_attn_layer_norm(x, emb)
      residual = x
      x = self.activation_fn(self.fc1(x))
      x = x.dropout(self.activation_dropout)
      x = self.fc2(x)
      x = x.dropout(self.dropout)
      x = residual + x
      x = self.final_layer_norm(x) if not self.cond_layer_norm else self.final_layer_norm(x, emb)
    return x

# from examples/whisper.py. Renamed attributes for weight loading and allow bias on k_proj, also transpose
class MultiHeadAttention:
  def __init__(self, n_state, n_head):
    self.n_head = n_head
    self.n_state = n_state
    self.q_proj = nn.Linear(n_state, n_state)
    self.k_proj = nn.Linear(n_state, n_state)
    self.v_proj = nn.Linear(n_state, n_state)
    self.out_proj = nn.Linear(n_state, n_state)

  def __call__(self, x:Tensor, xa:Optional[Tensor]=None, mask:Optional[Tensor]=None):
    x = x.transpose(0,1)  # TxBxC -> BxTxC
    q = self.q_proj(x)
    k = self.k_proj(xa or x)
    v = self.v_proj(xa or x)
    wv, qk = self.qkv_attention(q, k, v, mask)
    # NOTE: we aren't returning qk
    ret =  self.out_proj(wv).transpose(0,1)  # BxTxC -> TxBxC
    return ret

  def qkv_attention(self, q, k, v, mask=None):
    mask=None
    n_batch, n_ctx, n_state = q.shape
    scale = (n_state // self.n_head) ** -0.25
    q = q.reshape(*q.shape[:2], self.n_head, -1).permute(0, 2, 1, 3) * scale
    k = k.reshape(*k.shape[:2], self.n_head, -1).permute(0, 2, 3, 1) * scale
    v = v.reshape(*v.shape[:2], self.n_head, -1).permute(0, 2, 1, 3)
    qk = q @ k
    if mask is not None: qk = qk + mask[:n_ctx, :n_ctx]
    w = qk.softmax(-1)
    return (w @ v).permute(0, 2, 1, 3).flatten(start_dim=2), qk.detach()

class ConvFeatureExtractionModel():
  def __init__(self, conv_layers, dropout=.0, mode="default", conv_bias=False):
    assert mode in {"default", "group_norm_masked", "layer_norm"}
    self.mode = mode
    def block(n_in, n_out, k, stride, is_layer_norm=False, is_group_norm=False, conv_bias=False):
      def make_conv():
        conv = nn.Conv1d(n_in, n_out, k, stride=stride, bias=conv_bias)
        conv.weight = Tensor.kaiming_normal(*conv.weight.shape)
        return conv
      assert (is_layer_norm and is_group_norm) == False, "layer norm and group norm are exclusive"
      if is_layer_norm:
        return [make_conv(), partial(Tensor.dropout, p=dropout),[partial(Tensor.transpose, ax1=-2, ax2=-1), Fp32LayerNorm(dim, elementwise_affine=True), partial(Tensor.transpose, ax1=-2, ax2=-1)], Tensor.gelu]
      elif is_group_norm and mode == "default":
        return [make_conv(), partial(Tensor.dropout, p=dropout), Fp32GroupNorm(dim, dim, affine=True), Tensor.gelu]
      elif is_group_norm and mode == "group_norm_masked":
        return [make_conv(), partial(Tensor.dropout, p=dropout), GroupNormMasked(dim, dim, affine=True), Tensor.gelu]
      else:
        return [make_conv(), partial(Tensor.dropout, p=dropout), Tensor.gelu]
    in_d = 1
    self.conv_layers = []
    for i, cl in enumerate(conv_layers):
      assert len(cl) == 3, "invalid conv definition: " + str(cl)
      (dim, k, stride) = cl
      if i == 0:
        self.cl = cl
      self.conv_layers.append(block(in_d, dim, k, stride, is_layer_norm=(mode == "layer_norm"), is_group_norm=((mode == "default" or mode == "group_norm_masked") and i == 0), conv_bias=conv_bias))
      in_d = dim

  def __call__(self, x:Tensor, padding_mask:Tensor):  # TODO: refactor this later
    x = x.unsqueeze(1)  # BxT -> BxCxT
    if self.mode == "group_norm_masked":
      if padding_mask is not None:
        _, k, stride = self.cl
        lengths_org = tilde(padding_mask.cast(dtypes.bool)).cast(dtypes.int64).sum(1)  # ensure padding_mask is bool for tilde
        lengths = (((lengths_org - k) / stride) + 1).floor().cast(dtypes.int64)
        padding_mask = tilde(lengths_to_padding_mask(lengths)).cast(dtypes.int64)  # lengths_to_padding_mask returns bool tensor
      x = self.conv_layers[0][0](x)  # padding_mask is numeric
      x = self.conv_layers[0][1](x)
      x = self.conv_layers[0][2](x, padding_mask)
      x = self.conv_layers[0][3](x)
    else:
      x = x.sequential(self.conv_layers[0])  # default
    for _, conv in enumerate(self.conv_layers[1:], start=1):
      conv = reduce(lambda a,b: operator.iconcat(a,b if isinstance(b, list) else [b]), conv, [])  # flatten
      x = x.sequential(conv)
    return x

# helpers:
def randn_like(x): return Tensor.randn(*x.shape, dtype=x.dtype).to(device=x.device)

def tilde(x: Tensor) -> Tensor:
  if x.dtype == dtypes.bool: return (1 - x).cast(dtypes.bool)
  return (x + 1) * -1  # this seems to be what the ~ operator does in pytorch for non bool

def lengths_to_padding_mask(lens: Tensor) -> Tensor:
  bsz, max_lens = lens.shape[0], lens.max().numpy().item()
  mask = Tensor.arange(max_lens).to(lens.device).reshape(1, max_lens)
  mask = mask.expand(bsz, -1) >= lens.reshape(bsz, 1).expand(-1, max_lens)
  return mask.cast(dtypes.bool)

class SamePad:
  def __init__(self, kernel_size, causal=False):
    self.remove = (kernel_size - 1) if causal else (1 if kernel_size % 2 == 0 else 0)
  def __call__(self, x):
    if self.remove > 0:
      x = x[:, :, : -self.remove]
    return x

class Fp32LayerNorm(nn.LayerNorm):
  def __init__(self, *args, **kwargs): super().__init__(*args, **kwargs)
  def __call__(self, input: Tensor):
    self.weight = self.weight.float() if self.weight is not None else None
    self.bias = self.bias.float() if self.bias is not None else None
    output = super().__call__(input.float())
    return output.cast(input.dtype)

class Fp32GroupNorm(nn.GroupNorm):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
  def __call__(self, input: Tensor):
    self.weight = self.weight.float() if self.weight is not None else None
    self.bias = self.bias.float() if self.bias is not None else None
    output = super().__call__(input.float())
    return output.cast(input.dtype)

class CondLayerNorm:  # https://github.com/auspicious3000/contentvec/blob/main/contentvec/modules/cond_layer_norm.py#L10
  # this one is a bit weird since it has slightly different constructor args than nn.LayerNorm
  def __init__(self, dim_last, eps=1e-5, dim_spk=256, elementwise_affine=True):
    self.dim_last, self.eps, self.dim_spk, self.elementwise_affine = dim_last, eps, dim_spk, elementwise_affine
    if self.elementwise_affine:
      self.weight_ln = nn.Linear(self.dim_spk, self.dim_last, bias=False)
      self.bias_ln = nn.Linear(self.dim_spk, self.dim_last, bias=False)
      self.weight_ln.weight, self.bias_ln.weight = (Tensor.ones(*self.weight_ln.weight.shape)), (Tensor.zeros(*self.bias_ln.weight.shape))
  def __call__(self, x: Tensor, spk_emb: Tensor):
    axis = tuple(-1-i for i in range(len(x.shape[1:])))
    x = x.layernorm(axis=axis, eps=self.eps)
    if not self.elementwise_affine: return x
    weights, bias = self.weight_ln(spk_emb), self.bias_ln(spk_emb)
    return weights * x + bias

# TODO: not actually used in our case. maybe implement later
class GroupNormMasked:
  def __init__(self, num_groups, num_channels, eps=1e-5, affine=True):
    self.num_groups, self.num_channels, self.eps, self.affine = num_groups, num_channels, eps, affine
    self.weight, self.bias = (Tensor.ones(num_channels)), (Tensor.zeros(num_channels))  if self.affine else (None, None)
    raise NotImplementedError("not needed for for this case")
  def __call__(self, x, mask):
    pass

class Synthesizer:
  def __init__(self, spec_channels, segment_size, inter_channels, hidden_channels, filter_channels, n_heads, n_layers, kernel_size, p_dropout, resblock, resblock_kernel_sizes, resblock_dilation_sizes, upsample_rates, upsample_initial_channel, upsample_kernel_sizes, gin_channels, ssl_dim, n_speakers, sampling_rate=44100, vol_embedding=False, n_flow_layer=4, **kwargs):
    self.spec_channels, self.inter_channels, self.hidden_channels, self.filter_channels, self.n_heads, self.n_layers, self.kernel_size, self.p_dropout, self.resblock, self.resblock_kernel_sizes, self.resblock_dilation_sizes, self.upsample_rates, self.upsample_initial_channel, self.upsample_kernel_sizes, self.segment_size, self.n_speakers, self.gin_channels, self.vol_embedding = spec_channels, inter_channels, hidden_channels, filter_channels, n_heads, n_layers, kernel_size, p_dropout, resblock, resblock_kernel_sizes, resblock_dilation_sizes, upsample_rates, upsample_initial_channel, upsample_kernel_sizes, segment_size, n_speakers, gin_channels, vol_embedding
    self.emb_g = nn.Embedding(n_speakers, gin_channels)
    if vol_embedding:
      self.emb_vol = nn.Linear(1, hidden_channels)
    self.pre = nn.Conv1d(ssl_dim, hidden_channels, kernel_size=5, padding=2)
    self.enc_p = TextEncoder(inter_channels, hidden_channels, kernel_size, n_layers, filter_channels=filter_channels, n_heads=n_heads, p_dropout=p_dropout)
    self.dec = Generator(sampling_rate, inter_channels, resblock, resblock_kernel_sizes, resblock_dilation_sizes, upsample_rates, upsample_initial_channel, upsample_kernel_sizes, gin_channels)
    self.enc_q = PosteriorEncoder(spec_channels, inter_channels, hidden_channels, 5, 1, 16, gin_channels=gin_channels)
    self.flow = ResidualCouplingBlock(inter_channels, hidden_channels, 5, 1, n_flow_layer, gin_channels=gin_channels)
    self.emb_uv = nn.Embedding(vocab_size=2, embed_size=hidden_channels)
    self.character_mix = False
  def EnableCharacterMix(self, n_speakers_map, device):
    self.speaker_map = Tensor.zeros((n_speakers_map, 1, 1, self.gin_channels)).to(device)
    for i in range(n_speakers_map):
      self.speaker_map[i] = self.emb_g(Tensor([[i]], dtype=dtypes.int64).to(device))
    self.speaker_map = self.speaker_map.unsqueeze(0).to(device)
    self.character_mix = True
  def infer(self, c:Tensor, f0:Tensor, uv:Tensor, g:Tensor=None, noise_scale=0.35, seed=52468, vol=None) -> Tuple[Tensor, Tensor]:
    Tensor.manual_seed(getenv('SEED', seed))
    c_lengths = (Tensor.ones([c.shape[0]]) * c.shape[-1]).to(c.device)
    if self.character_mix and g.shape[0] > 1:  # [N, S]  *  [S, B, 1, H]
      g = g.reshape((g.shape[0], g.shape[1], 1, 1, 1))  # [N, S, B, 1, 1]
      g = g * self.speaker_map  # [N, S, B, 1, H]
      g = g.sum(dim=1) # [N, 1, B, 1, H]
      g = g.transpose(0, -1).transpose(0, -2).squeeze(0) # [B, H, N]
    else:
      if len(g.shape) == 1:
        g = g.unsqueeze(0)
      g = self.emb_g(g).transpose(1, 2)
    x_mask = sequence_mask(c_lengths, c.shape[2]).unsqueeze(1).cast(c.dtype)
    vol = self.emb_vol(vol[:,:,None]).transpose(1,2) if vol is not None and self.vol_embedding else 0
    x = self.pre(c) * x_mask + self.emb_uv(uv.cast(dtypes.int64)).transpose(1, 2) + vol
    z_p, _, _, c_mask = self.enc_p.forward(x, x_mask, f0=self._f0_to_coarse(f0), noise_scale=noise_scale)
    z = self.flow.forward(z_p, c_mask, g=g, reverse=True)
    o = self.dec.forward(z * c_mask, g=g, f0=f0)
    return o,f0
  def _f0_to_coarse(self, f0 : Tensor):
    f0_mel = 1127 * (1 + f0 / 700).log()
    a = (F0_BIN - 2) / (F0_MEL_MAX - F0_MEL_MIN)
    b = F0_MEL_MIN * a - 1.
    f0_mel = (f0_mel > 0).where(f0_mel * a - b, f0_mel)
    f0_coarse = f0_mel.ceil().cast(dtype=dtypes.int64)
    f0_coarse = f0_coarse * (f0_coarse > 0)
    f0_coarse = f0_coarse + ((f0_coarse < 1) * 1)
    f0_coarse = f0_coarse * (f0_coarse < F0_BIN)
    f0_coarse = f0_coarse + ((f0_coarse >= F0_BIN) * (F0_BIN - 1))
    return f0_coarse
  @classmethod
  def load_model(cls, config_path:str, config_url:str, weights_path:str, weights_url:str):
    download_if_not_present(config_path, config_url)
    hps = get_hparams_from_file(config_path)
    download_if_not_present(weights_path, weights_url)
    net_g = cls(hps.data.filter_length // 2 + 1, hps.train.segment_size // hps.data.hop_length, **hps.model)
    _ = load_checkpoint(weights_path, net_g, None, skip_list=["f0_decoder"])
    logging.debug(f"{cls.__name__}:Loaded model with hps: {hps}")
    return net_g, hps

class TextEncoder:
  def __init__(self, out_channels, hidden_channels, kernel_size, n_layers, gin_channels=0, filter_channels=None, n_heads=None, p_dropout=None):
    self.out_channels, self.hidden_channels, self.kernel_size, self.n_layers, self.gin_channels = out_channels, hidden_channels, kernel_size, n_layers, gin_channels
    self.proj = nn.Conv1d(hidden_channels, out_channels * 2, 1)
    self.f0_emb = nn.Embedding(256, hidden_channels)  # n_vocab = 256
    self.enc_ = Encoder(hidden_channels, filter_channels, n_heads, n_layers, kernel_size, p_dropout)
  def forward(self, x, x_mask, f0=None, noise_scale=1):
    x = x + self.f0_emb(f0).transpose(1, 2)
    x = self.enc_.forward(x * x_mask, x_mask)
    stats = self.proj(x) * x_mask
    m, logs = split(stats, self.out_channels, dim=1)
    z = (m + randn_like(m) * logs.exp() * noise_scale) * x_mask
    return z, m, logs, x_mask

class Upsample:
  def __init__(self, scale_factor):
    assert scale_factor % 1 == 0, "Only integer scale factor allowed."
    self.scale = int(scale_factor)
  def forward(self, x:Tensor):
    repeats = tuple([1] * len(x.shape) + [self.scale])
    new_shape = (*x.shape[:-1], x.shape[-1] * self.scale)
    return x.unsqueeze(-1).repeat(repeats).reshape(new_shape)

class SineGen():
  def __init__(self, samp_rate, harmonic_num=0, sine_amp=0.1, noise_std=0.003, voice_threshold=0, flag_for_pulse=False):
    self.sine_amp, self.noise_std, self.harmonic_num, self.sampling_rate, self.voiced_threshold, self.flag_for_pulse = sine_amp, noise_std, harmonic_num, samp_rate, voice_threshold, flag_for_pulse
    self.dim = self.harmonic_num + 1
  def _f02uv(self, f0):
    return (f0 > self.voiced_threshold).float()  #generate uv signal
  def _f02sine(self, f0_values):
    def padDiff(x : Tensor): return (x.pad2d((0,0,-1,1)) - x).pad2d((0,0,0,-1))
    def mod(x: Tensor, n: int) -> Tensor: return x - n * x.div(n).floor()  # TODO: this is what the % operator does in pytorch.
    rad_values = mod((f0_values / self.sampling_rate) , 1)  # convert to F0 in rad
    rand_ini = Tensor.rand(f0_values.shape[0], f0_values.shape[2], device=f0_values.device)  # initial phase noise

    #rand_ini[:, 0] = 0
    m = Tensor.ones(f0_values.shape[0]).unsqueeze(1).pad2d((0,f0_values.shape[2]-1,0,0)).cast(dtypes.bool)
    m = tilde(m)
    rand_ini = m.where(rand_ini, 0)

    #rad_values[:, 0, :] = rad_values[:, 0, :] + rand_ini
    tmp = rad_values[:, 0, :] + rand_ini
    m = Tensor.ones(tmp.shape).pad2d((0,0,0,rad_values.shape[1]-1,0)).cast(dtypes.bool)
    m = tilde(m)
    tmp = tmp.unsqueeze(1).pad2d((0,0,0,rad_values.shape[1]-1,0))
    rad_values = m.where(rad_values, tmp)

    tmp_over_one = mod(rad_values.cumsum(1), 1)
    tmp_over_one_idx = padDiff(tmp_over_one) < 0
    cumsum_shift = Tensor.zeros_like(rad_values)

    #cumsum_shift[:, 1:, :] = tmp_over_one_idx * -1.0
    tmp_over_one_idx = (tmp_over_one_idx * -1.0).pad2d((0,0,1,0))
    cumsum_shift = tmp_over_one_idx

    sines = ((rad_values + cumsum_shift).cumsum(1) * 2 * np.pi).sin()
    return sines

  def forward(self, f0, upp=None):
    fn = f0.mul(Tensor([[range(1, self.harmonic_num + 2)]], dtype=dtypes.float32).to(f0.device))
    sine_waves = self._f02sine(fn) * self.sine_amp  #generate sine waveforms
    uv = self._f02uv(f0)  # generate uv signal
    noise_amp = uv * self.noise_std + (1 - uv) * self.sine_amp / 3
    noise = noise_amp * randn_like(sine_waves)
    sine_waves = sine_waves * uv + noise
    return sine_waves, uv, noise

class SourceHnNSF:
  def __init__(self, sampling_rate, harmonic_num=0, sine_amp=0.1, add_noise_std=0.003, voiced_threshold=0):
    self.sine_amp, self.noise_std = sine_amp, add_noise_std
    self.l_sin_gen = SineGen(sampling_rate, harmonic_num, sine_amp, add_noise_std, voiced_threshold)
    self.l_linear = nn.Linear(harmonic_num + 1, 1)
  def forward(self, x, upp=None):
    sine_waves, uv, _ = self.l_sin_gen.forward(x, upp)
    sine_merge = self.l_linear(sine_waves.cast(self.l_linear.weight.dtype)).tanh()
    noise = randn_like(uv) * self.sine_amp / 3
    return sine_merge, noise, uv

# most of the hifigan in standard vits is reused here, but need to upsample and construct harmonic source from f0
class Generator:
  def __init__(self, sampling_rate, inter_channels, resblock, resblock_kernel_sizes, resblock_dilation_sizes, upsample_rates, upsample_initial_channel, upsample_kernel_sizes, gin_channels):
    self.sampling_rate, self.inter_channels, self.resblock, self.resblock_kernel_sizes, self.resblock_dilation_sizes, self.upsample_rates, self.upsample_initial_channel, self.upsample_kernel_sizes, self.gin_channels = sampling_rate, inter_channels, resblock, resblock_kernel_sizes, resblock_dilation_sizes, upsample_rates, upsample_initial_channel, upsample_kernel_sizes, gin_channels
    self.num_kernels, self.num_upsamples = len(resblock_kernel_sizes), len(upsample_rates)
    self.conv_pre = nn.Conv1d(inter_channels, upsample_initial_channel, 7, 1, padding=3)
    self.f0_upsamp = Upsample(scale_factor=np.prod(upsample_rates))
    self.m_source = SourceHnNSF(sampling_rate, harmonic_num=8)
    resblock = ResBlock1 if resblock == '1' else ResBlock2
    self.ups, self.noise_convs = [], []
    for i, (u, k) in enumerate(zip(upsample_rates, upsample_kernel_sizes)):
      c_cur = upsample_initial_channel//(2**(i+1))
      self.ups.append(nn.ConvTranspose1d(upsample_initial_channel//(2**i), c_cur, k, u, padding=(k-u)//2))
      if i + 1 < len(upsample_rates):  # TODO: make oneliner after debugging
        stride_f0 = int(np.prod(upsample_rates[i + 1:]))
        self.noise_convs.append(nn.Conv1d(1, c_cur, kernel_size=stride_f0 * 2, stride=stride_f0, padding=(stride_f0+1) // 2))
      else:
        self.noise_convs.append(nn.Conv1d(1, c_cur, kernel_size=1))
    self.resblocks = []
    for i in range(len(self.ups)):
      ch = upsample_initial_channel // (2 ** (i + 1))
      for _, (k, d) in enumerate(zip(resblock_kernel_sizes, resblock_dilation_sizes)):
        self.resblocks.append(resblock(ch, k, d))
    self.conv_post = nn.Conv1d(ch, 1, 7, 1, padding=3)
    if gin_channels != 0: self.cond = nn.Conv1d(gin_channels, upsample_initial_channel, 1)
    self.upp = np.prod(upsample_rates)
  def forward(self, x, f0, g=None):
    f0 = self.f0_upsamp.forward(f0[:, None]).transpose(1, 2)  # bs,n,t
    har_source, _, _ = self.m_source.forward(f0, self.upp)
    har_source = har_source.transpose(1, 2)
    x = self.conv_pre(x)
    if g is not None:  x = x + self.cond(g)
    for i in range(self.num_upsamples):
      x, xs = self.ups[i](x.leakyrelu(LRELU_SLOPE)), None
      x_source = self.noise_convs[i](har_source)
      x = x + x_source
      for j in range(self.num_kernels):
        if xs is None: xs = self.resblocks[i * self.num_kernels + j].forward(x)
        else: xs += self.resblocks[i * self.num_kernels + j].forward(x)
      x = xs / self.num_kernels
    return self.conv_post(x.leakyrelu()).tanh()

def repeat_expand_2d(content, target_len, mode="left"): # content : [h, t]
  if mode == "left": return repeat_expand_2d_left(content.realize(), target_len)
  elif mode == "nearest": return repeat_expand_2d_nearest(content, target_len)
  raise RuntimeError("Mode: {mode} not supported.")

def repeat_expand_2d_left(content, target_len): # content : [h, t]
  src_len = content.shape[-1]
  temp = np.arange(src_len+1) * target_len / src_len
  current_pos, cols = 0, []
  for i in range(target_len):
    if i >= temp[current_pos+1]:
      current_pos += 1
    cols.append(content[:, current_pos])
  return Tensor.stack(cols).transpose(0, 1)

# TODO no idea how this can be done in a fast way in tinygrad (or without writing custom kernel like pytorch)
# Nearest interpolation with an integral scale factor is already implemented in Upsample (this file)
# Bute here, scale = target_len / content.shape[-1] would be a float...
# How to do this without writing slow loops in python?
# Torch does it with a custom kernel
def repeat_expand_2d_nearest(content, target_len):
  import torch
  content = content[None,:,:]
  return Tensor(torch.nn.functional.interpolate(torch.from_numpy(content.numpy()), target_len, mode="nearest")[0].numpy())

def load_fairseq_cfg(checkpoint_path):
  assert os.path.isfile(checkpoint_path)
  state = torch_load(checkpoint_path)
  cfg = None
  learning_rate = state["cfg"]["lr_scheduler"]["lr"][0]
  epoch = state["extra_state"]["train_iterator"]["epoch"]
  if "cfg" in state and state["cfg"] is not None:
    cfg = state["cfg"]
  elif "args" in state and state["args"] is not None:
    raise NotImplementedError  # TODO: not required for the checkpoint files yet encountered
  else:
    raise RuntimeError(f"Neither args nor cfg exist in state keys = {state.keys()}")
  return HParams(**cfg)

def load_checkpoint_enc(checkpoint_path, model: ContentVec, optimizer=None, skip_list=[]):
  assert os.path.isfile(checkpoint_path)
  start_time = time.time()
  checkpoint_dict = torch_load(checkpoint_path)
  saved_state_dict = checkpoint_dict['model']
  weight_g, weight_v, parent = None, None, None
  for key, v in saved_state_dict.items():
    if any(layer in key for layer in skip_list): continue
    try:
      obj, skip = model, False
      for k in key.split('.'):
        if k.isnumeric(): obj = obj[int(k)]
        elif isinstance(obj, dict): obj = obj[k]
        else:
          if k in ["weight_g", "weight_v"]:
            parent, skip = obj, True
            if k == "weight_g": weight_g = v
            else: weight_v = v
          if not skip: obj = getattr(obj, k)
      if weight_g and weight_v:
        setattr(obj, "weight_g", weight_g.numpy())
        setattr(obj, "weight_v", weight_v.numpy())
        obj, v = getattr(parent, "weight"), weight_norm(weight_v, weight_g, 0)
        weight_g, weight_v, parent, skip = None, None, None, False
      if not skip and obj.shape == v.shape:
        obj.assign(v.to(obj.device))
      elif not skip: logging.error(f"MISMATCH SHAPE IN {key}, {obj.shape} {v.shape}")
    except Exception as e: raise e
  logging.info(f"Loaded checkpoint '{checkpoint_path}' in {time.time() - start_time:.4f}s")
  return model, optimizer

def pad_array(arr, target_length):
  current_length = arr.shape[0]
  if current_length >= target_length:
    return arr
  pad_width = target_length - current_length
  pad_left = pad_width // 2
  pad_right = pad_width - pad_left
  padded_arr = np.pad(arr, (pad_left, pad_right), 'constant', constant_values=(0, 0))
  return padded_arr

def split_list_by_n(list_collection, n, pre=0):
  for i in range(0, len(list_collection), n):
    yield list_collection[i-pre if i-pre>=0 else i: i + n]

def get_sid(spk2id:HParams, speaker:str) -> Tensor:
  speaker_id = spk2id[speaker]
  if not speaker_id and type(speaker) is int:
    if len(spk2id.__dict__) >= speaker: speaker_id = speaker
  if speaker_id is None: raise RuntimeError(f"speaker={speaker} not in the speaker list")
  return Tensor([int(speaker_id)], dtype=dtypes.int64).unsqueeze(0)  # TODO: self.device

def get_encoder(ssl_dim) -> SpeechEncoder:
  if ssl_dim == 256: return ContentVec256L9
  if ssl_dim == 768: return ContentVec768L12

#########################################################################################
# CODE: https://github.com/svc-develop-team/so-vits-svc
#########################################################################################
# CONTENTVEC:
#   CODE: https://github.com/auspicious3000/contentvec
#   PAPER: https://arxiv.org/abs/2204.09224
#########################################################################################
# INSTALLATION: dependencies are for preprocessing and loading/saving audio.
# pip3 install soundfile librosa praat-parselmouth
#########################################################################################
# EXAMPLE USAGE:
# GPU=1 python3 examples/so_vits_svc.py --model tf2spy --file ~/recording.wav
#########################################################################################
SO_VITS_SVC_PATH = Path(__file__).parent.parent / "weights/So-VITS-SVC"
VITS_MODELS = { # config_path, weights_path, config_url, weights_url
  "saul_goodman" : (SO_VITS_SVC_PATH / "config_saul_gman.json", SO_VITS_SVC_PATH / "pretrained_saul_gman.pth", "https://huggingface.co/Amo/so-vits-svc-4.0_GA/resolve/main/ModelsFolder/Saul_Goodman_80000/config.json", "https://huggingface.co/Amo/so-vits-svc-4.0_GA/resolve/main/ModelsFolder/Saul_Goodman_80000/G_80000.pth"),
  "drake" : (SO_VITS_SVC_PATH / "config_drake.json", SO_VITS_SVC_PATH / "pretrained_drake.pth", "https://huggingface.co/jaspa/so-vits-svc/resolve/main/aubrey/config_aubrey.json", "https://huggingface.co/jaspa/so-vits-svc/resolve/main/aubrey/pretrained_aubrey.pth"),
  "cartman" : (SO_VITS_SVC_PATH / "config_cartman.json", SO_VITS_SVC_PATH / "pretrained_cartman.pth", "https://huggingface.co/marcoc2/so-vits-svc-4.0-models/resolve/main/EricCartman/config.json", "https://huggingface.co/marcoc2/so-vits-svc-4.0-models/resolve/main/EricCartman/G_10200.pth"),
  "tf2spy" : (SO_VITS_SVC_PATH / "config_tf2spy.json", SO_VITS_SVC_PATH / "pretrained_tf2spy.pth", "https://huggingface.co/Amo/so-vits-svc-4.0_GA/resolve/main/ModelsFolder/TF2_spy_60k/config.json", "https://huggingface.co/Amo/so-vits-svc-4.0_GA/resolve/main/ModelsFolder/TF2_spy_60k/G_60000.pth"),
  "tf2heavy" : (SO_VITS_SVC_PATH / "config_tf2heavy.json", SO_VITS_SVC_PATH / "pretrained_tf2heavy.pth", "https://huggingface.co/Amo/so-vits-svc-4.0_GA/resolve/main/ModelsFolder/TF2_heavy_100k/config.json", "https://huggingface.co/Amo/so-vits-svc-4.0_GA/resolve/main/ModelsFolder/TF2_heavy_100k/G_100000.pth"),
  "lady_gaga" : (SO_VITS_SVC_PATH / "config_gaga.json", SO_VITS_SVC_PATH / "pretrained_gaga.pth", "https://huggingface.co/marcoc2/so-vits-svc-4.0-models/resolve/main/LadyGaga/config.json", "https://huggingface.co/marcoc2/so-vits-svc-4.0-models/resolve/main/LadyGaga/G_14400.pth")
}
ENCODER_MODELS = { # weights_path, weights_url
  "contentvec": (SO_VITS_SVC_PATH / "contentvec_checkpoint.pt", "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/hubert_base.pt")
}
ENCODER_MODEL = "contentvec"
if __name__=="__main__":
  logging.basicConfig(stream=sys.stdout, level=(logging.INFO if DEBUG < 1 else logging.DEBUG))
  parser = argparse.ArgumentParser()
  parser.add_argument("-m", "--model", default=None, help=f"Specify the model to use. All supported models: {VITS_MODELS.keys()}", required=True)
  parser.add_argument("-f", "--file", default=None, help=f"Specify the path of the input file", required=True)
  parser.add_argument("--out_dir", default=str(Path(__file__).parent.parent / "temp"), help="Specify the output path.")
  parser.add_argument("--out_path", default=None, help="Specify the full output path. Overrides the --out_dir and --name parameter.")
  parser.add_argument("--base_name", default="test", help="Specify the base of the output file name. Default is 'test'.")
  parser.add_argument("--speaker", default=None, help="If not specified, the first available speaker is chosen. Usually there is only one speaker per model.")
  parser.add_argument("--noise_scale", default=0.4)
  parser.add_argument("--tran", default=0.0, help="Pitch shift, supports positive and negative (semitone) values. Default 0.0")
  parser.add_argument("--interpolate_mode", default="left", help="Interpolate mode to scale output of speech encoder to f0 frequency. Default 'left', other option 'nearest' is faster but only supported by torch.")
  parser.add_argument("--pad_seconds", default=0.5)
  parser.add_argument("--lg_num", default=0.0)
  parser.add_argument("--clip_seconds", default=0.0)
  parser.add_argument("--slice_db", default=-40)
  args = parser.parse_args()

  vits_model = args.model
  encoder_location = ENCODER_MODELS[ENCODER_MODEL]
  vits_location = VITS_MODELS[vits_model]

  Tensor.no_grad = True
  Tensor.training = False

  # Synthesizer
  net_g, hps = Synthesizer.load_model(vits_location[0], vits_location[2], vits_location[1], vits_location[3])
  # ContentVec
  Encoder = get_encoder(hps.model.ssl_dim)
  encoder = Encoder.load_model(encoder_location[0], encoder_location[1])

  target_sample = hps.data.sampling_rate
  spk2id = hps.spk
  unit_interpolate_mode = hps.data.unit_interpolate_mode if hasattr(hps.data, "unit_interpolate_mode") and hps.data.unit_interpolate_mode is not None else args.interpolate_mode
  vol_embedding = hps.model.vol_embedding if hasattr(hps.data, "vol_embedding") and hps.model.vol_embedding is not None else False
  feature_retrieval = False
  hop_length = hps.data.hop_length
  target_sample = hps.data.sampling_rate

  # args
  slice_db, clip_seconds, lg_num, pad_seconds, tran, noise_scale, audio_path = args.slice_db, args.clip_seconds, args.lg_num, args.pad_seconds, args.tran, args.noise_scale, args.file
  speaker = args.speaker if args.speaker is not None else list(hps.spk.__dict__.keys())[0]

  ### Loading audio and slicing ###
  assert os.path.isfile(audio_path)
  assert Path(audio_path).suffix == ".wav"
  chunks = preprocess.cut(audio_path, db_thresh=slice_db)
  audio_data, audio_sr = preprocess.chunks2audio(audio_path, chunks)

  per_size = int(clip_seconds * audio_sr)
  lg_size = int(lg_num * audio_sr)

  ### Infer per slice ###
  global_frame = 0
  audio = []
  for (slice_tag, data) in audio_data:
    print(f"\n====segment start, {round(len(data) / audio_sr, 3)}s====")
    length = int(np.ceil(len(data) / audio_sr * target_sample))

    if slice_tag:
      print("empty segment")
      _audio = np.zeros(length)
      audio.extend(list(pad_array(_audio, length)))
      global_frame += length // hop_length
      continue

    datas = [data] if per_size == 0 else split_list_by_n(data, per_size, lg_size)

    for k, dat in enumerate(datas):
      per_length = int(np.ceil(len(dat) / audio_sr * target_sample)) if clip_seconds!=0 else length
      pad_len = int(audio_sr * pad_seconds)
      dat = np.concatenate([np.zeros([pad_len]), dat, np.zeros([pad_len])])
      raw_path = io.BytesIO()
      soundfile.write(raw_path, dat, audio_sr, format="wav")
      raw_path.seek(0)

      ### Infer START ###
      wav, sr = preprocess.load_audiofile(raw_path)
      wav = preprocess.sinc_interp_resample(wav, sr, target_sample)[0]
      wav16k, f0, uv = preprocess.get_unit_f0(wav, tran, hop_length, target_sample, unit_interpolate_mode)
      sid = get_sid(spk2id, speaker)
      n_frames = f0.shape[1]

      # ContentVec infer
      start = time.time()
      c = encoder.encode(wav16k)
      c = repeat_expand_2d(c.squeeze(0), f0.shape[1], unit_interpolate_mode)  # interpolate speech encoding to match f0
      c = c.unsqueeze(0).realize()
      enc_time = time.time() - start

      # VITS infer
      vits_start = time.time()
      out_audio, f0 = net_g.infer(c, f0=f0, uv=uv, g=sid, noise_scale=noise_scale, vol=None)
      out_audio = out_audio[0,0].float().realize()
      vits_time = time.time() - vits_start

      infer_time = time.time() - start
      logging.info("total infer time:{:.2f}s, speech_enc time:{:.2f}s, vits time:{:.2f}s".format(infer_time, enc_time, vits_time))
      ### Infer END ###

      out_sr, out_frame = out_audio.shape[-1], n_frames
      global_frame += out_frame
      _audio = out_audio.numpy()
      pad_len = int(target_sample * pad_seconds)
      _audio = _audio[pad_len:-pad_len]
      _audio = pad_array(_audio, per_length)

      audio.extend(list(_audio))

  audio = np.array(audio)
  out_path = Path(args.out_path or Path(args.out_dir)/f"{args.model}{f'_spk_{speaker}'}_{args.base_name}.wav")
  out_path.parent.mkdir(parents=True, exist_ok=True)
  soundfile.write(out_path, audio, target_sample, format="flac")
  logging.info(f"Saved audio output to {out_path}")
