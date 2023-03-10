import pickle
import numpy as np
from tqdm import tqdm
import tempfile
import concurrent.futures
from collections import defaultdict
from tinygrad.helpers import prod, getenv
from tinygrad.ops import GlobalCounters
from tinygrad.lazy import Device

def fetch(url):
  if url.startswith("/"):
    with open(url, "rb") as f:
      return f.read()
  import os, hashlib, tempfile
  fp = os.path.join(tempfile.gettempdir(), hashlib.md5(url.encode('utf-8')).hexdigest())
  download_file(url, fp, skip_if_exists=not getenv("NOCACHE"))
  with open(fp, "rb") as f:
    return f.read()

def download_file(url, fp, skip_if_exists=False):
  import requests, os
  if skip_if_exists and os.path.isfile(fp) and os.stat(fp).st_size > 0:
    return
  r = requests.get(url, stream=True)
  assert r.status_code == 200
  progress_bar = tqdm(total=int(r.headers.get('content-length', 0)), unit='B', unit_scale=True, desc=url)
  with tempfile.NamedTemporaryFile(delete=False) as f:
    for chunk in r.iter_content(chunk_size=16384):
      progress_bar.update(f.write(chunk))
    f.close()
    os.rename(f.name, fp)

def my_unpickle(fb0):
  key_prelookup = defaultdict(list)
  class HackTensor:
    def __new__(cls, *args):
      ident, storage_type, obj_key, location, obj_size = args[0][0:5]
      assert ident == 'storage'

      # allocate an empty buffer on the DEFAULT device
      buffer = Device._buffers[Device.DEFAULT]
      ret = buffer.empty(args[2], dtype=storage_type)

      key_prelookup[obj_key].append((storage_type, obj_size, ret, args[2], args[3]))
      return ret

  class HackParameter:
    def __new__(cls, *args):
      #print(args)
      pass

  class Dummy:
    pass

  class MyPickle(pickle.Unpickler):
    def find_class(self, module, name):
      #print(module, name)
      if name == 'FloatStorage':
        return np.float32
      if name == 'LongStorage':
        return np.int64
      if name == 'HalfStorage':
        return np.float16
      if module == "torch._utils":
        if name == "_rebuild_tensor_v2":
          return HackTensor
        elif name == "_rebuild_parameter":
          return HackParameter
      else:
        try:
          return pickle.Unpickler.find_class(self, module, name)
        except Exception:
          return Dummy

    def persistent_load(self, pid):
      return pid

  return MyPickle(fb0).load(), key_prelookup

def fake_torch_load_zipped(fb0, load_weights=True, base_name="archive", multithreaded=True):
  import zipfile
  with zipfile.ZipFile(fb0, 'r') as myzip:
    #print(myzip.filelist)
    with myzip.open(f'{base_name}/data.pkl') as myfile:
      ret = my_unpickle(myfile)
    if load_weights:
      def load_weight(k, vv):
        with myzip.open(f'{base_name}/data/{k}') as myfile:
          for v in vv:
            if Device.DEFAULT in ["METAL", "CLANG", "LLVM"]:
              # Metal/clang/llvm allow direct reading
              myfile.readinto(v[2].raw()._buffer())
            elif Device.DEFAULT == "CPU":
              # numpy direct reading
              myfile.readinto(v[2]._buf.data)
            elif Device.DEFAULT == "TORCH":
              # torch "direct" reading
              myfile.readinto(v[2]._buf.numpy().data)
            elif Device.DEFAULT == "GPU":
              # GPU doesn't support direct reading
              dat = myfile.read(prod(v[2].shape) * np.dtype(v[2].dtype).itemsize)
              v[2].raw().copyin(dat)
            else:
              raise NotImplementedError(f"no read in for {Device.DEFAULT}")

          assert myfile.tell() == np.dtype(v[0]).itemsize * v[1], "didn't read the whole file"

      if multithreaded:
        # 2 seems fastest
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
          futures = {executor.submit(load_weight, k, v):k for k,v in ret[1].items()}
          for future in (t:=tqdm(concurrent.futures.as_completed(futures), total=len(futures))):
            if future.exception() is not None: raise future.exception()
            k = futures[future]
            t.set_description(f"loading {k} ram used: {GlobalCounters.mem_used/1e9:5.2f} GB")
      else:
        for k,v in tqdm(ret[1].items()): load_weight(k,v)

      """
      for k,v in (t:=tqdm(ret[1].items())):
        t.set_description(f"loading {k} ram used: {GlobalCounters.mem_used/1e9:5.2f} GB shape:{v[3]}")
        with myzip.open(f'{base_name}/data/{k}') as myfile:
          if getenv("METAL"):
            # TODO: make this lazy
            myfile.readinto(v[2].raw()._buffer())
          else:
            if v[2].dtype == "object":
              continue
            np.copyto(v[2], np.frombuffer(myfile.read(), v[2].dtype).reshape(v[3]))
      """
  return ret[0]

def fake_torch_load(b0):
  import io
  import struct

  # convert it to a file
  fb0 = io.BytesIO(b0)

  if b0[0:2] == b"\x50\x4b":
    return fake_torch_load_zipped(fb0)

  # skip three junk pickles
  pickle.load(fb0)
  pickle.load(fb0)
  pickle.load(fb0)

  ret, key_prelookup = my_unpickle(fb0)

  # create key_lookup
  key_lookup = pickle.load(fb0)
  key_real = [None] * len(key_lookup)
  for k,v in key_prelookup.items():
    key_real[key_lookup.index(k)] = v

  # read in the actual data
  for storage_type, obj_size, np_array, np_shape, np_strides in key_real:
    ll = struct.unpack("Q", fb0.read(8))[0]
    assert ll == obj_size
    bytes_size = {np.float32: 4, np.int64: 8}[storage_type]
    mydat = fb0.read(ll * bytes_size)
    np.copyto(np_array, np.frombuffer(mydat, storage_type).reshape(np_shape))

    # numpy stores its strides in bytes
    real_strides = tuple([x*bytes_size for x in np_strides])
    np_array.strides = real_strides

  return ret

def get_child(parent, key):
  obj = parent
  for k in key.split('.'):
    if k.isnumeric():
      obj = obj[int(k)]
    elif isinstance(obj, dict):
      obj = obj[k]
    else:
      obj = getattr(obj, k)
  return obj
