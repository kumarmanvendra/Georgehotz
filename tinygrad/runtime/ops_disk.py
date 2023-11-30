import os, mmap
try: import _posixshmem
except Exception: pass
from typing import Optional
from typing import Callable, Dict, Tuple
from tinygrad.helpers import prod, DType, OSX
from tinygrad.runtime.lib import RawBufferMapped
from tinygrad.device import Interpreted
from tinygrad.ops import Op, MovementOps, UnaryOps, BufferOps
from tinygrad.shape.view import strides_for_shape

MAP_LOCKED, MAP_POPULATE = 0x2000, 0x008000

class UnderlyingDiskBuffer:
  def __init__(self, fd, mem): self.fd, self.mem = fd, mem
  def __del__(self):
    if self.fd: self.fd.close()

class RawDiskBuffer(RawBufferMapped):
  def __init__(self, size, dtype:DType, buf=None, device:Optional[str]=None, offset:int=0):  # pylint: disable=super-init-not-called
    assert device is not None or buf is not None, "disk tensor needs a path or a buf"
    self.fn: str = str(None)
    if device is not None:
      if str(device).startswith("shm:"):
        if OSX:
          with open(f"/tmp/shm_{device[4:]}", "w+b") as f:
            self.fn = f.name
            f.truncate(size * dtype.itemsize)
            shm = mmap.mmap(f.fileno(), size * dtype.itemsize, flags=mmap.MAP_SHARED)
        else:
          fd = _posixshmem.shm_open(device[4:], os.O_RDWR, 0o600)
          # TODO: these flags are somewhat platform specific, but python doesn't expose the ones we need
          shm = mmap.mmap(fd, size * dtype.itemsize, flags=mmap.MAP_SHARED | MAP_LOCKED | MAP_POPULATE)
          shm.madvise(mmap.MADV_HUGEPAGE)     # type: ignore   # not on OSX
          os.close(fd)
        buf = UnderlyingDiskBuffer(None, shm)
      else:
        self.fn = device
        f = open(device, "a+b")
        if os.path.getsize(device) < size * dtype.itemsize: os.ftruncate(f.fileno(), size * dtype.itemsize)
        buf = None 
        #UnderlyingDiskBuffer(f, mmap.mmap(f.fileno(), size * dtype.itemsize))
    # NOTE: we don't call super since disk tensors don't use RAM
    self.__buf: Optional[UnderlyingDiskBuffer] = buf
    self.size, self.dtype, self.offset = size, dtype, offset
  @property
  def _buf(self) -> UnderlyingDiskBuffer:
    if self.__buf is None:
      f = open(self.fn, "a+b")
      if os.path.getsize(self.fn) < self.size * self.dtype.itemsize: os.ftruncate(f.fileno(), self.size * self.dtype.itemsize)
      self.__buf = UnderlyingDiskBuffer(f, mmap.mmap(f.fileno(), self.size * self.dtype.itemsize))
    return self.__buf
  @_buf.setter
  def _buf(self, val: UnderlyingDiskBuffer): self.__buf = val
  def cast(self, arg:Tuple[DType, bool]):
    return RawDiskBuffer(self.size, arg[0], self._buf, offset=self.offset)
  def as_strided(self, arg):
    assert strides_for_shape(arg[0]) == arg[1], "disk tensors don't support strides"
    return RawDiskBuffer(prod(arg[0]), self.dtype, self._buf, offset=self.offset+arg[2]*self.dtype.itemsize)
  def _buffer(self): return memoryview(self._buf.mem)[self.offset:self.offset+self.size*self.dtype.itemsize]
  def readinto(self, buf:memoryview):
    if self._buf.fd is not None:
      self._buf.fd.seek(self.offset)
      self._buf.fd.readinto(buf)
    else:
      buf.cast('B')[:] = self._buffer()

disk_fxn_for_op: Dict[Op, Callable] = { BufferOps.LOAD: lambda x: x, BufferOps.STORE: lambda x: x, UnaryOps.NOOP: lambda x: x, UnaryOps.CAST: RawDiskBuffer.cast, MovementOps.AS_STRIDED: RawDiskBuffer.as_strided }
DiskDevice = Interpreted(RawDiskBuffer, disk_fxn_for_op)
