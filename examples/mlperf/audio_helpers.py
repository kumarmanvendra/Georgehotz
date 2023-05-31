import math
from tinygrad.tensor import Tensor, dtypes
from scipy.signal import get_window
import numpy as np
import math
from typing import List

def create_fourier_kernels(
    n_fft,
    win_length=None,
    freq_bins=None,
    fmin=50,
    fmax=6000,
    sr=16000,
    freq_scale="linear",
    window="hann",
):
  if freq_bins == None:
    freq_bins = n_fft // 2 + 1
  if win_length == None:
    win_length = n_fft

  s = np.arange(0, n_fft, 1.0)
  wsin = np.empty((freq_bins, 1, n_fft))
  wcos = np.empty((freq_bins, 1, n_fft))
  start_freq = fmin
  end_freq = fmax
  bins2freq = []
  binslist = []

  # Choosing window shape
  window_mask = get_window(window, int(win_length), fftbins=True)
  n = window_mask.shape[-1]
  lpad = int((n_fft - n) // 2)
  lengths = [(0, 0)] * window_mask.ndim
  lengths[-1] = (lpad, int(n_fft - n - lpad))
  assert lpad <= 0
  window_mask =  np.pad(window_mask, lengths)

  if freq_scale == "linear":
    start_bin = start_freq * n_fft / sr
    scaling_ind = (end_freq - start_freq) * (n_fft / sr) / freq_bins

    for k in range(freq_bins):  # Only half of the bins contain useful info
      bins2freq.append((k * scaling_ind + start_bin) * sr / n_fft)
      binslist.append((k * scaling_ind + start_bin))
      wsin[k, 0, :] = np.sin(2 * np.pi * (k * scaling_ind + start_bin) * s /
                             n_fft)
      wcos[k, 0, :] = np.cos(2 * np.pi * (k * scaling_ind + start_bin) * s /
                             n_fft)

  elif freq_scale == "log":
    start_bin = start_freq * n_fft / sr
    scaling_ind = np.log(end_freq / start_freq) / freq_bins

    for k in range(freq_bins):  # Only half of the bins contain useful info
      # print("log freq = {}".format(np.exp(k*scaling_ind)*start_bin*sr/n_fft))
      bins2freq.append(np.exp(k * scaling_ind) * start_bin * sr / n_fft)
      binslist.append((np.exp(k * scaling_ind) * start_bin))
      wsin[k, 0, :] = np.sin(2 * np.pi *
                             (np.exp(k * scaling_ind) * start_bin) * s / n_fft)
      wcos[k, 0, :] = np.cos(2 * np.pi *
                             (np.exp(k * scaling_ind) * start_bin) * s / n_fft)

  elif freq_scale == "log2":
    start_bin = start_freq * n_fft / sr
    scaling_ind = np.log2(end_freq / start_freq) / freq_bins

    for k in range(freq_bins):  # Only half of the bins contain useful info
      # print("log freq = {}".format(np.exp(k*scaling_ind)*start_bin*sr/n_fft))
      bins2freq.append(2**(k * scaling_ind) * start_bin * sr / n_fft)
      binslist.append((2**(k * scaling_ind) * start_bin))
      wsin[k, 0, :] = np.sin(2 * np.pi * (2**(k * scaling_ind) * start_bin) *
                             s / n_fft)
      wcos[k, 0, :] = np.cos(2 * np.pi * (2**(k * scaling_ind) * start_bin) *
                             s / n_fft)

  elif freq_scale == "no":
    for k in range(freq_bins):  # Only half of the bins contain useful info
      bins2freq.append(k * sr / n_fft)
      binslist.append(k)
      wsin[k, 0, :] = np.sin(2 * np.pi * k * s / n_fft)
      wcos[k, 0, :] = np.cos(2 * np.pi * k * s / n_fft)
  else:
    raise NotImplementedError(
        'Please select the correct frequency scale: "linear", "log", "log2", "no"'
    )
  return (
      wsin.astype(np.float32),
      wcos.astype(np.float32),
      bins2freq,
      binslist,
      window_mask.astype(np.float32),
  )


def mel_frequencies(n_mels=128, fmin=0.0, fmax=11025.0, htk=False):
  # 'Center freqs' of mel bands - uniformly spaced between limits
  min_mel = hz_to_mel(fmin, htk=htk)
  max_mel = hz_to_mel(fmax, htk=htk)

  mels = np.linspace(min_mel, max_mel, n_mels)

  return mel_to_hz(mels, htk=htk)


def hz_to_mel(frequencies, htk=False):

  frequencies = np.asanyarray(frequencies)

  if htk:
    return 2595.0 * np.log10(1.0 + frequencies / 700.0)

  # Fill in the linear part
  f_min = 0.0
  f_sp = 200.0 / 3

  mels = (frequencies - f_min) / f_sp

  # Fill in the log-scale part

  min_log_hz = 1000.0  # beginning of log region (Hz)
  min_log_mel = (min_log_hz - f_min) / f_sp  # same (Mels)
  logstep = np.log(6.4) / 27.0  # step size for log region

  if frequencies.ndim:
    # If we have array data, vectorize
    log_t = frequencies >= min_log_hz
    mels[log_t] = min_log_mel + np.log(
        frequencies[log_t] / min_log_hz) / logstep
  elif frequencies >= min_log_hz:
    # If we have scalar data, heck directly
    mels = min_log_mel + np.log(frequencies / min_log_hz) / logstep

  return mels


def mel_to_hz(mels, htk=False):
  mels = np.asanyarray(mels)

  if htk:
    return 700.0 * (10.0**(mels / 2595.0) - 1.0)

  # Fill in the linear scale
  f_min = 0.0
  f_sp = 200.0 / 3
  freqs = f_min + f_sp * mels

  # And now the nonlinear scale
  min_log_hz = 1000.0  # beginning of log region (Hz)
  min_log_mel = (min_log_hz - f_min) / f_sp  # same (Mels)
  logstep = np.log(6.4) / 27.0  # step size for log region

  if mels.ndim:
    # If we have vector data, vectorize
    log_t = mels >= min_log_mel
    freqs[log_t] = min_log_hz * np.exp(logstep * (mels[log_t] - min_log_mel))
  elif mels >= min_log_mel:
    # If we have scalar data, check directly
    freqs = min_log_hz * np.exp(logstep * (mels - min_log_mel))

  return freqs


def get_mel(sr,
            n_fft,
            n_mels=128,
            fmin=0.0,
            fmax=None,
            htk=False,
            norm=1,
            dtype=np.float32):

  if fmax is None:
    fmax = float(sr) / 2

  assert norm is not None and norm != 1 and norm != np.inf, (
    "Unsupported norm: {}".format(repr(norm))
    )

  # Initialize the weights
  n_mels = int(n_mels)
  weights = np.zeros((n_mels, int(1 + n_fft // 2)), dtype=dtype)

  # Center freqs of each FFT bin
  # fftfreqs = fft_frequencies(sr=sr, n_fft=n_fft)
  fftfreqs = np.linspace(0, float(sr) / 2, int(1 + n_fft // 2), endpoint=True)

  # 'Center freqs' of mel bands - uniformly spaced between limits
  mel_f = mel_frequencies(n_mels + 2, fmin=fmin, fmax=fmax, htk=htk)

  fdiff = np.diff(mel_f)
  ramps = np.subtract.outer(mel_f, fftfreqs)

  for i in range(n_mels):
    # lower and upper slopes for all bins
    lower = -ramps[i] / fdiff[i]
    upper = ramps[i + 2] / fdiff[i + 1]

    # .. then intersect them with each other and zero
    weights[i] = np.maximum(0, np.minimum(lower, upper))

  if norm == 1:
    # Slaney-style mel is scaled to be approx constant energy per channel
    enorm = 2.0 / (mel_f[2:n_mels + 2] - mel_f[:n_mels])
    weights *= enorm[:, np.newaxis]

  # Only check weights if f_mel[0] is positive
  if not np.all((mel_f[:-2] == 0) | (weights.max(axis=1) > 0)):
    # This means we have an empty channel somewhere
    print("Empty filters detected in mel frequency basis. "
          "Some channels will produce empty responses. "
          "Try increasing your sampling rate (and fmax) or "
          "reducing n_mels.")

  return weights


def col2im(input: Tensor,
           output_size: List[int],
           kernel_size: List[int],
           dilation: List[int],
           padding: List[int],
           stride: List[int],
           dtype=dtypes.float32) -> Tensor:
  assert len(kernel_size) == 2, "only 2D kernel supported"
  assert len(dilation) == 2, "only 2D dilation supported"
  assert len(padding) == 2, "only 2D padding supported"
  assert len(stride) == 2, "only 2D stride supported"

  assert all(e >= 0 for e in kernel_size), "kernel_size must be positive"
  assert all(e >= 0 for e in dilation), "dilation must be positive"
  assert all(e >= 0 for e in padding), "padding must be positive"
  assert all(e >= 0 for e in stride), "stride must be positive"
  assert all(e >= 0 for e in output_size), "output_size must be positive"

  shape = input.shape
  ndim = len(shape)
  assert ndim in (2, 3) and all(d != 0 for d in shape[-2:]), (
      f"Expected 2D or 3D (batch mode) tensor for input with possible 0 batch size "
      f"and non-zero dimensions, but got: {tuple(shape)}", )
  prod_kernel_size = kernel_size[0] * kernel_size[1]
  assert shape[-2] % prod_kernel_size == 0, (
      f"Expected size of input's first non-batch dimension to be divisible by the "
      f"product of kernel_size, but got input.shape[-2] = {shape[-2]} and "
      f"kernel_size={kernel_size}", )
  col = [
      1 + (out + 2 * pad - dil * (ker - 1) - 1) // st for out, pad, dil, ker,
      st in zip(output_size, padding, dilation, kernel_size, stride)
  ]
  L = col[0] * col[1]
  assert shape[-1] == L, (
      f"Given output_size={output_size}, kernel_size={kernel_size}, "
      f"dilation={dilation}, padding={padding}, stride={stride}, "
      f"expected input.size(-1) to be {L} but got {shape[-1]}.", )
  assert L > 0, (
      f"Given output_size={output_size}, kernel_size={kernel_size}, "
      f"dilation={dilation}, padding={padding}, stride={stride}, "
      f"expected input.size(-1) to be {L} but got {shape[-1]}.", )
  batched_input = ndim == 3
  if not batched_input:
    input = input.unsqueeze(0)

  shape = input.shape

  out_h, out_w = output_size
  stride_h, stride_w = stride
  padding_h, padding_w = padding
  dilation_h, dilation_w = dilation
  kernel_h, kernel_w = kernel_size

  input = input.reshape([shape[0], shape[1] // prod_kernel_size] +
                        list(kernel_size) + col)
  input = input.permute(0, 1, 2, 4, 3, 5)

  def indices_along_dim(input_d, kernel_d, dilation_d, padding_d, stride_d):
    blocks_d = input_d + padding_d * 2 - dilation_d * (kernel_d - 1)
    blocks_d_indices = np.arange(0, blocks_d, stride_d)[None, ...]
    kernel_grid = np.arange(0, kernel_d * dilation_d, dilation_d)[..., None]
    return blocks_d_indices + kernel_grid

  indices_row = indices_along_dim(out_h, kernel_h, dilation_h, padding_h,
                                  stride_h)
  for _ in range(4 - len(indices_row.shape)):
    indices_row = indices_row[..., None]
  indices_col = indices_along_dim(out_w, kernel_w, dilation_w, padding_w,
                                  stride_w)

  output_padded_size = [o + 2 * p for o, p in zip(output_size, padding)]
  output = np.zeros([shape[0], shape[1] // math.prod(kernel_size)] +
                    output_padded_size).astype(dtype.np)
  output = Tensor(output)
  output_shape = output.shape
  input = input.reshape(input.shape[0], -1)
  output = output.reshape(output.shape[0], -1)
  indices_col = indices_col.flatten()
  # TODO: only 1D here, make it 2D
  # TODO: for loop are slow, try to vectorize by batching then mul then sum
  for i, idx_col in enumerate(indices_col):
    out = Tensor.zeros_like(output)
    idxs = (Tensor.arange(math.prod(out.shape[1:]))).reshape(
        out.shape[1:])[None, :].expand(out.shape[0], out.shape[1])
    mask = idxs.eq(idx_col)
    masked = input[:, i:i + 1] * mask
    output = output + masked
  output = output.reshape(output_shape)
  output = output[:, :, padding_w:(-padding_w if padding_w != 0 else None),
                  padding_h:(-padding_h if padding_h != 0 else None)]

  if not batched_input:
    output = output.squeeze(0)

  return output


class STFT:
  def __init__(self,
               n_fft=128,
               win_length=128,
               freq_bins=None,
               hop_length=64,
               window="hann",
               freq_scale="no",
               center=True,
               fmin=50,
               fmax=6000,
               sr=16000,
               trainable=False,
               eps=1e-10):

    super().__init__()

    # Trying to make the default setting same as librosa
    if win_length == None:
      win_length = n_fft
    if hop_length == None:
      hop_length = int(win_length // 4)

    self.trainable = trainable
    self.stride = hop_length
    self.center = center
    self.n_fft = n_fft
    self.freq_bins = freq_bins
    self.trainable = trainable
    self.pad_amount = self.n_fft // 2
    self.window = window
    self.win_length = win_length
    self.eps = eps

    # Create filter windows for stft
    (
        kernel_sin,
        kernel_cos,
        self.bins2freq,
        self.bin_list,
        window_mask,
    ) = create_fourier_kernels(
        n_fft,
        win_length=win_length,
        freq_bins=freq_bins,
        window=window,
        freq_scale=freq_scale,
        fmin=fmin,
        fmax=fmax,
        sr=sr,
    )

    kernel_sin = Tensor(kernel_sin, dtype=dtypes.float32)
    kernel_cos = Tensor(kernel_cos, dtype=dtypes.float32)

    self.kernel_sin_inv = kernel_sin.cat(-kernel_sin[1:-1].flip(0),
                                         dim=0).unsqueeze(-1)
    self.kernel_cos_inv = kernel_cos.cat(kernel_cos[1:-1].flip(0),
                                         dim=0).unsqueeze(-1)

    # Applying window functions to the Fourier kernels
    window_mask = Tensor(window_mask)
    self.wsin = kernel_sin * window_mask
    self.wcos = kernel_cos * window_mask
    self.window_mask = window_mask.unsqueeze(0).unsqueeze(-1)
    self.wsin.requires_grad = self.trainable
    self.wcos.requires_grad = self.trainable

  def __call__(self, x, inverse=False, *args, **kwargs):
    return self.forward(x, *args, **kwargs) if not inverse else self.inverse(
        x, *args, **kwargs)

  def forward(self, x, return_spec=False):
    self.num_samples = x.shape[-1]

    assert len(x.shape) == 2, "Input shape must be (batch, len) "
    if self.center:
      x = x.pad(((0, 0), (self.pad_amount, self.pad_amount)), )
    x = x[:, None, :]

    spec_imag = x.conv2d(self.wsin, stride=self.stride)[:, :self.freq_bins, :]
    spec_real = x.conv2d(self.wcos, stride=self.stride)[:, :self.freq_bins, :]
    if return_spec:
      spec = (spec_real.pow(2) + spec_imag.pow(2)).sqrt()
      spec = (spec + self.eps) if self.trainable else spec
      return spec
    else:
      return Tensor.stack((spec_real, -spec_imag), -1)

  def inverse(self, X, onesided=True, length=None):
    assert len(X.shape) == 4, (
        "Tensor must be in the shape of (batch, freq_bins, timesteps, 2)."
        "Where last dim is real and imaginary number dim")
    # If the input spectrogram contains only half of the n_fft
    # Use extend_fbins function to get back another half
    if onesided:
      # Extending the number of frequency bins from `n_fft//2+1` back to `n_fft` by
      # reversing all bins except DC and Nyquist and append it on top of existing spectrogram"""
      X_ = X[:, 1:-1].flip(1)
      X_upper1 = X_[:, :, :, 0]
      X_upper2 = -X_[:, :, :, 1]
      X_upper = Tensor.stack([X_upper1, X_upper2], dim=3)
      X = X.cat(X_upper, dim=1)
    X_real, X_imag = X[:, :, :, 0][:, None], X[:, :, :, 1][:, None]
    a1 = X_real.conv2d(self.kernel_cos_inv, stride=(1, 1))
    b2 = X_imag.conv2d(self.kernel_sin_inv, stride=(1, 1))
    real = a1 - b2
    real = real[:, :, 0, :] * self.window_mask
    real = real / self.n_fft

    # Overlap and Add algorithm to connect all the frames
    n_fft = real.shape[1]
    output_len = n_fft + self.stride * (real.shape[2] - 1)
    real = col2im(real, (1, output_len),
                  kernel_size=(1, n_fft),
                  stride=(self.stride, self.stride),
                  dilation=(1, 1),
                  padding=(0, 0)).flatten(1)
    win = self.window_mask.flatten()
    n_frames = X.shape[2]
    win_stacks = win[:, None].repeat((1, n_frames))[None, :]
    output_len = win_stacks.shape[1] + self.stride * (win_stacks.shape[2] - 1)
    w_sum = col2im(win_stacks**2, (1, output_len),
                   kernel_size=(1, n_fft),
                   stride=(self.stride, self.stride),
                   dilation=(1, 1),
                   padding=(0, 0)).flatten(1)
    real = real / w_sum

    if length is None:
      if self.center:
        real = real[:, self.pad_amount:-self.pad_amount]
    else:
      if self.center:
        real = real[:, self.pad_amount:self.pad_amount + length]
      else:
        real = real[:, :length]
    return real


class MelSpectrogram:
  def __init__(self,
               sr=16000,
               n_fft=2048,
               win_length=None,
               n_mels=128,
               hop_length=512,
               window="hann",
               center=True,
               power=2.0,
               htk=False,
               fmin=0.0,
               fmax=None,
               norm=1,
               trainable_mel=False,
               trainable_STFT=False,
               **kwargs):
    self.stride = hop_length
    self.center = center
    self.n_fft = n_fft
    self.power = power
    self.trainable_mel = trainable_mel
    self.trainable_STFT = trainable_STFT

    self.stft = STFT(n_fft=n_fft,
                     win_length=win_length,
                     freq_bins=None,
                     hop_length=hop_length,
                     window=window,
                     freq_scale="no",
                     center=center,
                     sr=sr,
                     trainable=trainable_STFT,
                     **kwargs)

    mel_basis = get_mel(sr, n_fft, n_mels, fmin, fmax, htk=htk, norm=norm)
    self.mel_basis = Tensor(mel_basis, requires_grad=trainable_mel)

  def __call__(self, x, return_log=False):
    spec = self.stft(x, return_spec=True)**self.power
    mel_spec = self.mel_basis @ spec
    return mel_spec if not return_log else (mel_spec+self.stft.eps).log()


class MFCC:
  def __init__(self,
               sr=1600,
               n_mfcc=20,
               norm="ortho",
               ref=1.0,
               amin=1e-10,
               top_db=80.0,
               **kwargs):
    super().__init__()
    self.melspec_layer = MelSpectrogram(sr=sr, **kwargs)
    self.m_mfcc = n_mfcc
    self.norm = norm

    # attributes that will be used for _power_to_db
    assert amin <= 0, "amin must be strictly positive"
    if top_db is not None:
        assert top_db < 0, "top_db must be strictly positive"
    self.amin = Tensor([amin])
    self.ref = Tensor([ref]).abs()
    self.top_db = top_db
    self.n_mfcc = n_mfcc
    self.log10denom = Tensor([10.]).log()
    self.log10aminref = self.amin.maximum(self.ref).log()/self.log10denom

  def _power_to_db(self, S):
    log_spec = 10.0 * S.maximum(self.amin).log()/self.log10denom
    log_spec -= 10.0 * self.log10aminref
    if self.top_db is not None:
      batch_wise_max = log_spec.flatten(1).max(1)[0].unsqueeze(1).unsqueeze(1)
      log_spec = log_spec.maximum(batch_wise_max - self.top_db)
    return log_spec

  def _dct(self, x, norm=None):
    """
        Refer to https://github.com/zh217/torch-dct for the original implmentation.
        """
    x = x.permute(
        0, 2,
        1)  # make freq the last axis, since dct applies to the frequency axis
    x_shape = x.shape
    N = x_shape[-1]

    v = x[:, :, ::2].cat(x[:, :, 1::2].flip([2]), dim=2)
    _, wcos, *_ = create_fourier_kernels(
    n_fft=v.shape[2],
    win_length=v.shape[2],
    freq_bins=None,
    sr=16000,
    freq_scale="linear",
    window="hann",)
    Vc = wcos.dot(v)[:,0]

    k = -Tensor.arange(N, dtype=x.dtype, device=x.device)[None, :] * np.pi / (
        2 * N)
    W_r = k.cos()
    W_i = k.sin()

    V = Vc[:, :, :, 0] * W_r - Vc[:, :, :, 1] * W_i

    if norm == "ortho":
      V = Tensor.stack([
      V[:, :, 0] / math.sqrt(N) * 2,
      V[:, :, 1] / math.sqrt(N / 2) * 2], dim=2)
    V = 2 * V

    return V.permute(0, 2, 1)  # swapping back the time axis and freq axis

  def __call__(self, x):
    x = self.melspec_layer(x)
    x = self._power_to_db(x)
    x = self._dct(x, norm="ortho")[:, :self.m_mfcc, :]
    return x