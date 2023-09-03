from typing import Any, Optional, Tuple, cast
from extra import dist
from multiprocessing import shared_memory
from tinygrad.helpers import DEBUG, colored
from tinygrad.lazy import LazyBuffer
from tinygrad.runtime.lib import RawBuffer, RawBufferCopyIn, RawBufferCopyInOut, RawBufferTransfer
from tinygrad.runtime.ops_hip import RawHIPBuffer
from tinygrad.runtime.ops_shm import RawShmBuffer
from tinygrad.jit import CacheCollector
from tinygrad.tensor import Tensor, Function
import extra.hip_wrapper as hip
import numpy as np

# fake the function signature of ASTRunner so we can put it in the cache
def __send_rb(args, variables=None, jit=False, force_wait=False):
  x, target_rank, y, extra = args
  if y is not None:
    if isinstance(x, RawBufferCopyInOut): x._copyout(np.frombuffer(y._buffer(), dtype=x.dtype.np))
    else: y.fromCPU(x.toCPU())
  dist.OOB.send(extra, target_rank)
  if DEBUG >= 2: print(f"{colored('****', 'magenta' if jit else None)}   sent {x} to rank {target_rank}")

def __recv_rb(args, variables=None, jit=False, force_wait=False):
  x, target_rank, y = args
  dist.OOB.recv(target_rank)
  if isinstance(x, RawHIPBuffer):
    x._transfer(cast(RawHIPBuffer, y))
  elif isinstance(x, RawBufferCopyIn): x._copyin(y.toCPU())
  else: x.fromCPU(y.toCPU())
  if DEBUG >= 2: print(f"{colored('****', 'magenta' if jit else None)}   recv {x} from rank {target_rank}")

# send a rawbuffer from out rank to the target rank
def _send_rb(x:RawBuffer, target_rank:int, cache_id:Optional[str]=None):
  if isinstance(x, RawHIPBuffer):
    # send ipc handle
    handle = hip.hipIpcGetMemHandle(x._buf)
    dist.OOB.send(handle, target_rank)

    CacheCollector.add(__send_rb, [x, target_rank, None, None], {})
  else:
    # cache the shared memory so we don't have to create it every time
    if cache_id is not None and cache_id in _send_rb.shared_memory_cache:
      shm_name = _send_rb.shared_memory_cache[cache_id]
    else:
      shm_name = (s := shared_memory.SharedMemory(create=True, size=x.size * x.dtype.itemsize)).name
      s.close()
      if cache_id is not None: _send_rb.shared_memory_cache[cache_id] = shm_name
    # copy the buffer into shared memory
    device = f"{shm_name},{cache_id}" if cache_id is not None else shm_name
    y = RawShmBuffer(x.size, x.dtype, device=device)

    # fast path when we can directly copyout
    if isinstance(x, RawBufferCopyInOut): x._copyout(np.frombuffer(y._buffer(), dtype=x.dtype.np))
    else: y.fromCPU(x.toCPU())
    dist.OOB.send((shm_name, cache_id), target_rank)

    # jit support
    CacheCollector.add(__send_rb, [x, target_rank, y, None], {})
setattr(_send_rb, "shared_memory_cache", {})

# receive a rawbuffer from the target rank
def _recv_rb(x:RawBuffer, target_rank:int):
  if isinstance(x, RawHIPBuffer):
    # open ipc handle
    handle = dist.OOB.recv(target_rank)
    ptr = hip.hipIpcOpenMemHandle(handle, 0)
    # build a new buffer
    y = RawBuffer(x.size, x.dtype, buf=ptr)
    x._transfer(cast(RawHIPBuffer, y))

    if DEBUG >= 2: print(f"****   got {x} from rank {target_rank}")

    CacheCollector.add(__recv_rb, [x, target_rank, y], {})
  else:
    extra = dist.OOB.recv(target_rank)
    device = f"{extra[0]},{extra[1]}" if extra[1] is not None else f"{extra[0]}"
    y = RawShmBuffer(x.size, x.dtype, device=device)

    # fast path when we can directly copyin
    if isinstance(x, RawBufferCopyIn): x._copyin(y.toCPU())
    else: x.fromCPU(y.toCPU())
    if DEBUG >= 2: print(f"****   got {x} from rank {target_rank}")

    if extra[1] is None:
      (s := shared_memory.SharedMemory(name=extra[0])).close()
      s.unlink()

    # jit support
    CacheCollector.add(__recv_rb, [x, target_rank, y], {})

# sends a lazybuffer from our rank to the target rank
def _send_lb(x:LazyBuffer, target_rank:int, cache_id:Optional[str]=None) -> None: _send_rb(x.contiguous().realize().realized, target_rank, cache_id=cache_id)

# receive a lazybuffer from the target rank
def _recv_lb(x:LazyBuffer, target_rank:int) -> LazyBuffer:
  _recv_rb(x.contiguous().realize().realized, target_rank)
  return x

class Send(Function):
  def forward(self, x:LazyBuffer, target_rank:int, cache_id:Optional[str]=None) -> LazyBuffer:
    self.target_rank, self.shape, self.dtype = target_rank, x.shape, x.dtype
    _send_lb(x, target_rank, cache_id=cache_id)
    return x

class Recv(Function):
  def forward(self, x:LazyBuffer, target_rank:int, cache_id:Optional[str]=None) -> LazyBuffer:
    self.target_rank, self.cache_id = target_rank, cache_id
    return _recv_lb(x, target_rank)

def send(x:Tensor, target_rank:int, cache_id:Optional[str]=None) -> Tensor: return Send.apply(x, target_rank=target_rank, cache_id=cache_id)
def recv(x:Tensor, target_rank:int, cache_id:Optional[str]=None) -> Tensor: return Recv.apply(x, target_rank=target_rank, cache_id=cache_id)
