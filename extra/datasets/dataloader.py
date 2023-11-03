import multiprocessing
import cloudpickle
from typing import Any
import glob, random
import json
import numpy as np
from itertools import repeat
from PIL import Image
import functools, pathlib
import cloudpickle
from simplejpeg import decode_jpeg
from multiprocessing import Pool
from functools import partial

BASEDIR = pathlib.Path(__file__).parent / "imagenet"
ci = json.load(open(BASEDIR / "imagenet_class_index.json"))
cir = {v[0]: int(k) for k,v in ci.items()}

@functools.lru_cache(None)
def get_train_files():
  train_files = glob.glob(str(BASEDIR/"train/*/*"))
  return train_files

@functools.lru_cache(None)
def get_val_files():
  val_files = glob.glob(str(BASEDIR / "val/*/*"))
  return val_files

import time
import torch
import torchvision.transforms.functional as F
from torchvision.transforms import RandomResizedCrop

mean = [0.485, 0.456, 0.406]
std = [0.229, 0.224, 0.225]

def decode(fn):
  with open(fn, 'rb') as f:
    return decode_jpeg(f.read())

rrc = RandomResizedCrop(224)
def image_load(fn, val):
  s = time.perf_counter()
  #img = Image.open(fn).convert('RGB')
  try:
    img = Image.fromarray(decode(fn))
  except:
    print(fn)
    return np.random.rand(224,224,3),0
  r = time.perf_counter()
  img = F.resize(img, 256, Image.BILINEAR,antialias=True)
  e = time.perf_counter()
  load_t = e-s
  if val:
    img = F.center_crop(img,224)
  else:
    s1 = time.perf_counter()
    img = rrc.forward(img)
    e1 = time.perf_counter()
    if random.random() < 0.5:
      #rhf=RandomHorizontalFlip(p=0.5)
      #img=rhf.forward(img)
      img = F.hflip(img)
    #print(f'load timn {load_t*1000:7.2f}ms norm {(e-r)*1000:7.2f}ms resize {(e1-s1)*1000:7.2f}ms randresize')
  # TODO: normalize
  #print('bef')
  #print(img.shape)
  n = time.perf_counter()
  img = torch.from_numpy(np.float32(img).transpose([2, 0, 1])) / 255.0
  img = F.normalize(img, mean, std)
  e = time.perf_counter()
  return img, e-s, e-n

def iterate(bs=16, val=True, shuffle=True, num_workers=16):
  files = get_val_files() if val else get_train_files()
  order = list(range(0, len(files)))
  if shuffle: random.shuffle(order)
  p = Pool(num_workers)
  for i in range(0, len(files), bs)[:-1]:
    s = time.perf_counter()
    X = p.map(partial(image_load,val=val),[files[i] for i in order[i:i+bs]],chunksize=bs//num_workers)
    e = time.perf_counter() 
    X,T = [x[0] for x in X],[x[2] for x in X]
    #print(f'{(e-s)*1000:7.2f}ms all imgs tm {((e-s)-max(T))*1000:7.2f} mult process tm')
    Y = [cir[files[i].split("/")[-2]] for i in order[i:i+bs]]
    yield torch.stack(X).numpy(), np.array(Y), (e-s), (sum(T)/len(T))
  
def proc(itermaker, q) -> None:
  try:
    for x in itermaker(): q.put(x)
  except Exception as e:
    q.put(e)
  finally:
    q.put(None)
    q.close()

class _CloudpickleFunctionWrapper:
  def __init__(self, fn): self.fn = fn
  def __getstate__(self): return cloudpickle.dumps(self.fn)
  def __setstate__(self, pfn): self.fn = cloudpickle.loads(pfn)
  def __call__(self, *args, **kwargs) -> Any:  return self.fn(*args, **kwargs)

def cross_process(itermaker, maxsize=8):
  q: multiprocessing.Queue = multiprocessing.Queue(maxsize)
  p = multiprocessing.Process(target=proc, args=(_CloudpickleFunctionWrapper(itermaker), q))
  p.start()
  while True:
    ret = q.get()
    if isinstance(ret, Exception): raise ret
    elif ret is None: break
    else: yield ret

if __name__ == '__main__':
  import time
  r = []
  for i,f in enumerate(get_train_files()[:100]):
    s = time.monotonic()
    image_load(f)
    s1 = time.monotonic()  
    r.append(s1-s)
    if i != 0 and i%100==0:break
  print(f'{(sum(r)/len(r))*1000:7.2f}ms')