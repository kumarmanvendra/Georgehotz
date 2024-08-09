# thanks to https://github.com/openai/whisper for a good chunk of MIT licensed code

import sys, base64, multiprocessing, itertools, collections
from typing import Optional, Union, Literal, List, Iterator, Tuple

from tinygrad import Tensor, TinyJit, Variable, nn
from tinygrad.nn.state import torch_load, load_state_dict
from tinygrad.helpers import getenv, DEBUG, fetch, colored

import numpy as np
import librosa, functools

class MultiHeadAttention:
  def __init__(self, n_state, n_head, kv_caching: Literal['cross', 'self']=None, max_self_attn_cache_len=None):
    self.n_head = n_head
    self.query = nn.Linear(n_state, n_state)
    self.key = nn.Linear(n_state, n_state, bias=False)
    self.value = nn.Linear(n_state, n_state)
    self.out = nn.Linear(n_state, n_state)

    self.kv_caching = kv_caching
    self.max_self_attn_cache_len = max_self_attn_cache_len

  def __call__(self, x:Tensor, xa:Optional[Tensor]=None, mask:Optional[Tensor]=None, len: Union[Variable,int]=None):
    if self.kv_caching == 'cross':
      if xa is not None:
        k, v = self.key(xa), self.value(xa)
        if not hasattr(self, 'cache_k'):
          self.cache_k, self.cache_v = k, v
        else:
          self.cache_k.assign(k).realize()
          self.cache_v.assign(v).realize()
      else:
        k, v = self.cache_k, self.cache_v
    else:
      k, v = self.key(x), self.value(x)
      if self.kv_caching == 'self':
        if not hasattr(self, 'cache_k'):
          self.cache_k = Tensor.zeros(x.shape[0], self.max_self_attn_cache_len, x.shape[2])
          self.cache_v = Tensor.zeros(x.shape[0], self.max_self_attn_cache_len, x.shape[2])
        k = self.cache_k.shrink((None, (0, len), None)).cat(k, dim=1)
        v = self.cache_v.shrink((None, (0, len), None)).cat(v, dim=1)
        padding = self.max_self_attn_cache_len-len-x.shape[1]
        self.cache_k.assign(k.pad((None, (0, padding), None)).contiguous()).realize()
        self.cache_v.assign(v.pad((None, (0, padding), None)).contiguous()).realize()

    q = self.query(x)
    n_ctx = q.shape[1]
    assert(q.shape[-1] == k.shape[-1] == v.shape[-1])
    head_dim = q.shape[-1] // self.n_head
    q = q.reshape(*q.shape[:2], self.n_head, head_dim).permute(0, 2, 1, 3)
    k = k.reshape(*k.shape[:2], self.n_head, head_dim).permute(0, 2, 1, 3)
    v = v.reshape(*v.shape[:2], self.n_head, head_dim).permute(0, 2, 1, 3)
    attn = Tensor.scaled_dot_product_attention(q, k, v, mask[:n_ctx,:n_ctx] if mask is not None else None)
    wv = attn.permute(0, 2, 1, 3).flatten(start_dim=2)
    return self.out(wv)

class ResidualAttentionBlock:
  def __init__(self, n_state, n_head, is_decoder_block=False, max_self_attn_cache_len=None):
    self.attn = MultiHeadAttention(n_state, n_head, kv_caching='self' if is_decoder_block else None, max_self_attn_cache_len=max_self_attn_cache_len)
    self.attn_ln = nn.LayerNorm(n_state)

    self.cross_attn = MultiHeadAttention(n_state, n_head, kv_caching='cross') if is_decoder_block else None
    self.cross_attn_ln = nn.LayerNorm(n_state) if is_decoder_block else None

    self.mlp = [nn.Linear(n_state, n_state*4), Tensor.gelu, nn.Linear(n_state*4, n_state)]
    self.mlp_ln = nn.LayerNorm(n_state)

  def __call__(self, x, xa=None, mask=None, len: Union[Variable, int]=None):
    x = x + self.attn(self.attn_ln(x), mask=mask, len=len)
    if self.cross_attn: x = x + self.cross_attn(self.cross_attn_ln(x), xa)
    x = x + self.mlp_ln(x).sequential(self.mlp)
    return x.realize()

class AudioEncoder:
  def __init__(self, n_mels, n_audio_ctx, n_audio_state, n_audio_head, n_audio_layer, **_):
    self.conv1 = nn.Conv1d(n_mels, n_audio_state, kernel_size=3, padding=1)
    self.conv2 = nn.Conv1d(n_audio_state, n_audio_state, kernel_size=3, stride=2, padding=1)
    self.blocks = [ResidualAttentionBlock(n_audio_state, n_audio_head) for _ in range(n_audio_layer)]
    self.ln_post = nn.LayerNorm(n_audio_state)
    self.positional_embedding = Tensor.empty(n_audio_ctx, n_audio_state)
    self.encode = TinyJit(self.__call__)

  def __call__(self, x):
    x = self.conv1(x).gelu()
    x = self.conv2(x).gelu()
    x = x.permute(0, 2, 1)
    x = x + self.positional_embedding[:x.shape[1]]
    x = x.sequential(self.blocks)
    x = self.ln_post(x)
    return x.realize()

class TextDecoder:
  def __init__(self, n_vocab, n_text_ctx, n_text_state, n_text_head, n_text_layer, **_):
    self.max_tokens_to_sample = n_text_ctx // 2
    self.max_self_attn_cache_len = self.max_tokens_to_sample * 2 + 5  # roughly prompt + start toks + max_tokens_to_sample

    self.token_embedding = nn.Embedding(n_vocab, n_text_state)
    self.positional_embedding = Tensor.empty(n_text_ctx, n_text_state)
    self.blocks = [ResidualAttentionBlock(n_text_state, n_text_head, is_decoder_block=True, max_self_attn_cache_len=self.max_self_attn_cache_len) for _ in range(n_text_layer)]
    self.ln = nn.LayerNorm(n_text_state)
    self.mask = Tensor.full((n_text_ctx, n_text_ctx), -np.inf).triu(1).realize()
    self.getjitted = collections.defaultdict((lambda: ([TinyJit(block.__call__) for block in self.blocks], TinyJit(self.output_tok))))

  def __call__(self, x: Tensor, pos: int, encoded_audio: Tensor):

    seqlen = x.shape[-1]
    x = self.token_embedding(x) + self.positional_embedding[pos:pos+seqlen]

    jitblocks, jitoutput = self.getjitted[seqlen]
    for block in jitblocks:
      if pos == 0: x = block(x, xa=encoded_audio, mask=self.mask, len=0)  # pass xa for cross attn kv caching
      else: x = block(x, mask=self.mask, len=Variable("self_attn_cache_len", 1, self.max_self_attn_cache_len).bind(pos))
    return jitoutput(x)

  def output_tok(self, x):
    return (self.ln(x) @ self.token_embedding.weight.T).realize()

class Whisper:
  def __init__(self, dims, batch_size=1):
    self.encoder = AudioEncoder(**dims)
    self.decoder = TextDecoder(**dims)
    self.is_multilingual = dims["n_vocab"] >= 51865
    self.num_languages = dims["n_vocab"] - 51765 - int(self.is_multilingual)
    print(f'{dims["n_vocab"]},{self.num_languages=}')
    self.batch_size = batch_size
    self.n_mels = dims["n_mels"]


RATE = 16000
SEGMENT_SECONDS=30
SAMPLES_PER_SEGMENT = RATE * SEGMENT_SECONDS # 480000
N_FFT = 400
HOP_LENGTH = 160
FRAMES_PER_SECOND = RATE // HOP_LENGTH # 100
FRAMES_PER_SEGMENT = SAMPLES_PER_SEGMENT // HOP_LENGTH # 3000

def prep_audio(waveforms: List[np.ndarray], batch_size: int, n_mels: int = 80, truncate=False) -> Tensor:
  """
  :param waveforms: A list of possibly variable length 16000Hz audio samples
  :param batch_size: The batch_size associated with the Whisper model being used to transcribe the audio.
                     Used to prevent JIT mismatch errors since the encoder does not accept symbolic shapes
  :param truncate: If true, truncates (or pads) audio to exactly 30s for a single encoder pass
  :return: mel spectrogram of the given waveforms
  """
  print(waveforms[0].shape)
  def pad_or_trim(arr, target_len):
    curr_len = len(arr)
    if curr_len == target_len:
      return arr
    elif curr_len < target_len:
      return np.pad(arr, (0, target_len - curr_len), 'constant')
    else:
      return arr[:target_len]

  max_len = SAMPLES_PER_SEGMENT if truncate else max(len(wav) for wav in waveforms)
  if (r := max_len % SAMPLES_PER_SEGMENT) > 0: max_len += SAMPLES_PER_SEGMENT - r
  waveforms = np.array(list(map(lambda w: pad_or_trim(w, max_len), waveforms)))
  assert waveforms.shape[0] <= batch_size
  if waveforms.shape[0] < batch_size:
    # we could have a symbolic batch_size dim instead of manually padding here if conv/layernorm supported symbolic shapes
    waveforms = np.pad(waveforms, pad_width=((0, batch_size - waveforms.shape[0]), (0, 0)))

  stft = librosa.stft(waveforms, n_fft=N_FFT, hop_length=HOP_LENGTH, window='hann', dtype=np.csingle)
  magnitudes = np.absolute(stft[..., :-1]) ** 2
  mel_spec = librosa.filters.mel(sr=RATE, n_fft=N_FFT, n_mels=n_mels) @ magnitudes

  log_spec = np.log10(np.clip(mel_spec, 1e-10, None))
  log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
  log_spec = (log_spec + 4.0) / 4.0

  print(log_spec.shape)
  return Tensor(log_spec)

LANGUAGES=['en', 'zh', 'de', 'es', 'ru', 'ko', 'fr', 'ja', 'pt', 'tr', 'pl', 'ca', 'nl', 'ar', 'sv', 'it', 'id', 'hi', 'fi', 'vi', 'he', 'uk', 'el', 'ms', 'cs', 'ro', 'da', 'hu', 'ta', 'no',
           'th', 'ur', 'hr', 'bg', 'lt', 'la', 'mi', 'ml', 'cy', 'sk', 'te', 'fa', 'lv', 'bn', 'sr', 'az', 'sl', 'kn', 'et', 'mk', 'br', 'eu', 'is', 'hy', 'ne', 'mn', 'bs', 'kk', 'sq', 'sw',
           'gl', 'mr', 'pa', 'si', 'km', 'sn', 'yo', 'so', 'af', 'oc', 'ka', 'be', 'tg', 'sd', 'gu', 'am', 'yi', 'lo', 'uz', 'fo', 'ht', 'ps', 'tk', 'nn', 'mt', 'sa', 'lb', 'my', 'bo', 'tl',
           'mg', 'as', 'tt', 'haw', 'ln', 'ha', 'ba', 'jw', 'su', 'yue']

def get_encoding(encoding_name, num_languages):
  with fetch(f"https://raw.githubusercontent.com/openai/whisper/main/whisper/assets/{encoding_name}.tiktoken").open() as f:
    ranks = {base64.b64decode(token): int(rank) for token, rank in (line.split() for line in f if line)}
  n_vocab = len(ranks)
  specials = [
    "<|endoftext|>",
    "<|startoftranscript|>",
    *[f"<|{lang}|>" for lang in LANGUAGES[:num_languages]],
    "<|translate|>",
    "<|transcribe|>",
    "<|startoflm|>",
    "<|startofprev|>",
    "<|nospeech|>",
    "<|notimestamps|>",
    *[f"<|{i * 0.02:.2f}|>" for i in range(1501)],
  ]
  special_tokens = dict(zip(specials, itertools.count(n_vocab)))
  n_vocab += len(specials)
  import tiktoken
  return tiktoken.Encoding(
    name=encoding_name,
    explicit_n_vocab=n_vocab,
    pat_str=r"""'s|'t|'re|'ve|'m|'ll|'d| ?\p{L}+| ?\p{N}+| ?[^\s\p{L}\p{N}]+|\s+(?!\S)|\s+""",
    mergeable_ranks=ranks,
    special_tokens=special_tokens)

MODEL_URLS = {
  "tiny.en": "https://openaipublic.azureedge.net/main/whisper/models/d3dd57d32accea0b295c96e26691aa14d8822fac7d9d27d5dc00b4ca2826dd03/tiny.en.pt",
  "tiny": "https://openaipublic.azureedge.net/main/whisper/models/65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9/tiny.pt",
  "base.en": "https://openaipublic.azureedge.net/main/whisper/models/25a8566e1d0c1e2231d1c762132cd20e0f96a85d16145c3a00adf5d1ac670ead/base.en.pt",
  "base": "https://openaipublic.azureedge.net/main/whisper/models/ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e/base.pt",
  "small.en": "https://openaipublic.azureedge.net/main/whisper/models/f953ad0fd29cacd07d5a9eda5624af0f6bcf2258be67c92b79389873d91e0872/small.en.pt",
  "small": "https://openaipublic.azureedge.net/main/whisper/models/9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794/small.pt",
  "medium.en": "https://openaipublic.azureedge.net/main/whisper/models/d7440d1dc186f76616474e0ff0b3b6b879abc9d1a4926b7adfa41db2d497ab4f/medium.en.pt",
  "medium": "https://openaipublic.azureedge.net/main/whisper/models/345ae4da62f9b3d59415adc60127b97c714f32e89e936602e85993674d08dcb1/medium.pt",
  "large-v1": "https://openaipublic.azureedge.net/main/whisper/models/e4b87e7e0bf463eb8e6956e646f1e277e901512310def2c24bf0e11bd3c28e9a/large-v1.pt",
  "large-v2": "https://openaipublic.azureedge.net/main/whisper/models/81f7c96c852ee8fc832187b0132e569d6c3065a3252ed18e56effd0b6a73e524/large-v2.pt",
  "large-v3": "https://openaipublic.azureedge.net/main/whisper/models/e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb/large-v3.pt",
  "large": "https://openaipublic.azureedge.net/main/whisper/models/81f7c96c852ee8fc832187b0132e569d6c3065a3252ed18e56effd0b6a73e524/large-v2.pt",
}
def init_whisper(model_name="tiny.en", batch_size=1):
  assert MODEL_URLS[model_name] is not None

  filename = fetch(MODEL_URLS[model_name])
  state = torch_load(filename)
  model = Whisper(state['dims'], batch_size)
  load_state_dict(model, state['model_state_dict'], strict=False)
  enc = get_encoding("multilingual" if model.is_multilingual else "gpt2", model.num_languages)
  return model, enc

def load_file_waveform(filename):
  for i in range(0, int(librosa.get_duration(path=filename)+1), SEGMENT_SECONDS):
    yield librosa.load(filename, sr=RATE, offset=max(0,i-1), duration=SEGMENT_SECONDS+2)[0]

def transcribe_file(model, enc, filename):
  return ''.join(chunk[0] for chunk in transcribe_waveform(model, enc, [load_file_waveform(filename)]))

def transcribe_waveform(model:Whisper, enc, waveforms:List[Iterator], truncate=False, use_timestamps=False):
  """
  Expects an array of shape (N,S) where N is the number waveforms to transcribe in parallel and S is number of 16000Hz samples
  Returns Iterator[List[str]] if not use_timestampse else Iterator[List[Tuple[str, float, float]]]
  """

  def parse_timestamps(tokens:np.ndarray, enc, timeoffset:float) -> List[Tuple[str, float, float]]:
    print(f'parsing {enc.decode(tokens)}')
    tokens = tokens[np.where(tokens == start_tokens[-1])[0][0]+1:]
    tokens = tokens[:np.where(tokens == eot)[0][0]]
    if not use_timestamps: return enc.decode(tokens)
    res = []
    content = None
    timestart = timeoffset
    for tok in tokens:
      if tok < enc._special_tokens['<|notimestamps|>']: content = (content or '') + enc.decode([tok])
      else:
        time = int(tok - enc._special_tokens['<|notimestamps|>']) * 0.02 + timeoffset
        if content is not None:
          res.append((content, timestart, time))
          content = None
        else: content = ''
        timestart = time
    return res

  def inferloop(curr_segment_tokens):
    pos = 0
    if DEBUG >= 1: print(colored(f"Prompt: {enc.decode(curr_segment_tokens[0])}", "yellow"))
    for curr_tok in range(model.decoder.max_tokens_to_sample):
      intoks = Tensor(curr_segment_tokens if curr_tok == 0 else curr_segment_tokens[:, -1:])
      probs = model.decoder(intoks, pos, encoded_audio)[:, -1]
      next_tokens = probs.argmax(axis=-1).numpy().astype(np.int32)
      next_tokens[curr_segment_tokens[:, -1] == eot] = eot
      curr_segment_tokens = np.concatenate((curr_segment_tokens, next_tokens.reshape(-1, 1)), axis=1)
      pos = curr_segment_tokens.shape[-1] - 1

      if DEBUG >= 1: print(curr_tok, list(map(lambda tokens: enc.decode(tokens), curr_segment_tokens)))
      if (curr_segment_tokens[:, -1] == eot).all(): break

    return curr_segment_tokens

  # TODO detect language
  start_tokens = ["<|startoftranscript|>", *(["<|en|>", "<|transcribe|>"] if model.is_multilingual else []), *(["<|notimestamps|>"] if not use_timestamps else [])]
  start_tokens = [enc._special_tokens[x] for x in start_tokens]
  transcription_start_index = len(start_tokens)
  eot = enc._special_tokens["<|endoftext|>"]
  transcription_tokens = [np.array([], dtype=np.int32)] * model.batch_size
  N_audio = len(waveforms)
  for curr_frame, chunks in enumerate(zip(*waveforms, strict=True)):
    if curr_frame > 0 and N_audio > 1: raise Exception("Multi segment streaming not supported for batch")
    log_spec = prep_audio(chunks, model.batch_size, model.n_mels, truncate)

    curr_segment_tokens = np.tile(start_tokens, (model.batch_size, 1))
    if curr_frame > 0:
      # remove 1s pre padding
      log_spec = log_spec[:,:,FRAMES_PER_SECOND:]
      # pass the previously inferred tokens as 'prompt' - https://github.com/openai/whisper/discussions/117#discussioncomment-3727051
      prompt = np.concatenate((
        [enc._special_tokens["<|startofprev|>"]],
        transcription_tokens[0][-model.decoder.max_tokens_to_sample+1:],
        start_tokens))
      curr_segment_tokens = np.tile(prompt, (model.batch_size, 1))
      transcription_start_index = len(curr_segment_tokens[0])

    print(f"{log_spec[:,:,:FRAMES_PER_SEGMENT].shape=}")
    encoded_audio = model.encoder.encode(log_spec[:,:,:FRAMES_PER_SEGMENT].contiguous())

    curr_segment_tokens = inferloop(curr_segment_tokens)

    if not (curr_segment_tokens[:, -1] == eot).all():
      print(colored("REFRAME", "red"))
      transcription_tokens[0] = np.concatenate((transcription_tokens[0], curr_segment_tokens[0][transcription_start_index:]))
      prompt = np.concatenate((start_tokens, transcription_tokens[0][-model.decoder.max_tokens_to_sample+1:]))
      curr_segment_tokens = np.tile(prompt, (model.batch_size, 1))
      transcription_start_index = len(curr_segment_tokens[0])

      curr_segment_tokens = inferloop(curr_segment_tokens)

    assert (curr_segment_tokens[:, -1] == eot).all(), f"Token overflow. More than {model.decoder.max_tokens_to_sample*2} tokens for 30s segment"
    yield [parse_timestamps(toks, enc, curr_frame*SEGMENT_SECONDS) for toks in curr_segment_tokens]

    for curr_tok, t in enumerate(curr_segment_tokens):
      eot_index = np.where(t == eot)[0]
      eot_index = None if len(eot_index) == 0 else eot_index[0]
      transcription_tokens[curr_tok] = np.concatenate((transcription_tokens[curr_tok], t[transcription_start_index:eot_index]))

CHUNK = 1600
RECORD_SECONDS = 10

def listener(q):
  import pyaudio
  p = pyaudio.PyAudio()
  stream = p.open(format=pyaudio.paInt16, channels=1, rate=RATE, input=True, frames_per_buffer=CHUNK)
  print("listening")
  for _ in range(0, int(RATE / CHUNK * RECORD_SECONDS)):
    data = stream.read(CHUNK)
    waveform = ((np.frombuffer(data, np.int16)/32768).astype(np.float32)*3)
    q.put(waveform)
  print("done listening")

if __name__ == "__main__":
  model, enc = init_whisper(getenv("MODEL", "tiny.en"), batch_size=1)

  if len(sys.argv) > 1:

    for lines in transcribe_waveform(model, enc, [load_file_waveform(sys.argv[1])], use_timestamps=False, truncate=True):
      print("RESUL:")
      if type(lines[0]) == str: print(lines[0])
      else:
        for chunk in lines[0]:
          start, end, text = chunk
          print(f'[{start:.2f} - {end:.2f}] {text}')

  else:
    # online
    q = multiprocessing.Queue()
    p = multiprocessing.Process(target=listener, args=(q,))
    p.daemon = True
    p.start()

    lst = [enc._special_tokens["<|startoftranscript|>"], enc._special_tokens["<|notimestamps|>"]]
    total = None
    did_read = False
    for i in range(0, int(RATE / CHUNK * RECORD_SECONDS)):
      while not q.empty() or total is None:
        waveform = q.get()
        if total is None: total = waveform
        else: total = np.concatenate([total, waveform])
        did_read = True
      if did_read:
        log_spec = prep_audio(total.reshape(1, -1), model.batch_size, model.n_mels, truncate=True)
        encoded_audio = model.encoder.encode(Tensor(log_spec))
      # pass the previously inferred tokens as 'prefix' - https://github.com/openai/whisper/discussions/117#discussioncomment-3727051
      out = model.decoder(Tensor([lst]), 0, encoded_audio, streaming=True).realize()
      idx = int(out[0,-1].argmax().numpy().item())
      lst.append(idx)
      dec = enc.decode(lst)
      print(dec) # DO NOT REMOVE PRINT. IT'S VERY IMPORTANT
      if dec.endswith("<|endoftext|>"):
        lst.pop()