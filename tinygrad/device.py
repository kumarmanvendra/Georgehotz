from __future__ import annotations
import multiprocessing
from dataclasses import dataclass, replace
from collections import defaultdict
from typing import TYPE_CHECKING, List, Optional, Dict, Tuple, ClassVar, Callable, Any
import importlib, inspect, functools, pathlib, os, ctypes
from tinygrad.helpers import prod, getenv, all_int, to_function_name, diskcache_get, diskcache_put, DEBUG,BEAM,NOOPT, GlobalCounters, flat_mv, from_mv
from tinygrad.shape.symbolic import Variable, sym_infer, sint
from tinygrad.dtype import DType, ImageDType
from tinygrad.ops import LazyOp, get_lazyop_info
from tinygrad.codegen.uops import UOpGraph

if TYPE_CHECKING:
  from tinygrad.codegen.linearizer import Linearizer

# **************** Device ****************

class _Device:
  def __init__(self) -> None: self._devices: List[str] = [x.stem[len("ops_"):].upper() for x in (pathlib.Path(__file__).parent/"runtime").iterdir() if x.stem.startswith("ops_")]  # noqa: E501
  @functools.lru_cache(maxsize=None)  # this class is a singleton, pylint: disable=method-cache-max-size-none
  def _canonicalize(self, device:str) -> str: return (device.split(":", 1)[0].upper() + ((":"+device.split(":", 1)[1]) if ':' in device else '')).replace(":0", "")   # noqa: E501
  # NOTE: you can't cache canonicalize in case Device.DEFAULT changes
  def canonicalize(self, device:Optional[str]) -> str: return self._canonicalize(device) if device is not None else Device.DEFAULT
  def __getitem__(self, ix:str) -> Compiled: return self.__get_canonicalized_item(self.canonicalize(ix))
  @functools.lru_cache(maxsize=None)  # this class is a singleton, pylint: disable=method-cache-max-size-none
  def __get_canonicalized_item(self, ix:str) -> Compiled:
    if DEBUG >= 1: print(f"opening device {ix} from pid:{os.getpid()}")
    assert multiprocessing.current_process().name == "MainProcess" or ix.split(":")[0] in ["DISK", "NPY"], f"can only open device {ix} from parent"
    x = ix.split(":")[0].upper()
    return [cls for cname, cls in inspect.getmembers(importlib.import_module(f'tinygrad.runtime.ops_{x.lower()}')) if (cname.lower() == x.lower() + "device") and x in self._devices][0](ix)  # noqa: E501
  @functools.cached_property
  def DEFAULT(self) -> str:
    device_from_env: Optional[str] = functools.reduce(lambda val, ele: ele if getenv(ele) == 1 else val, self._devices, None)   # type: ignore
    if device_from_env: return device_from_env
    for device in ["METAL", "HSA", "CUDA", "GPU", "CLANG", "LLVM"]:
      try:
        if self[device]:
          os.environ[device] = "1"   # we set this in environment for spawned children
          return device
      except Exception: pass
    raise RuntimeError("no usable devices")
Device = _Device()

# **************** Buffer + Allocators ****************

@dataclass(frozen=True, eq=True)
class BufferOptions:
  image: Optional[ImageDType] = None
  uncached: bool = False
  cpu_access: bool = False
  host: bool = False
  nolru: bool = False

class Buffer:
  def __init__(self, device:str, size:int, dtype:DType, opaque:Any=None, options:Optional[BufferOptions]=None,
               initial_value:Optional[bytes]=None, lb_refcount=0, base:Optional[Buffer]=None, offset:int=0, preallocate=False):
    assert isinstance(dtype, DType)
    if isinstance(dtype, ImageDType): options = BufferOptions(image=dtype) # TODO: image hack shouldn't be here. where should it be?
    self.device, self.size, self.dtype, self.options, self.offset = device, size, dtype, options, offset
    if base is None:
      assert offset == 0, "base buffers can't have offset"
      self._base = None
      self._lb_refcount = lb_refcount
      if opaque is not None: self.allocate(opaque)
      if initial_value is not None:
        self.allocate()
        self.copyin(memoryview(initial_value))
    else:
      assert base._base is None, "base can't have a base"
      assert device == base.device, "base must have the same device"
      self._base = base
    if preallocate: self.allocate()
  @property
  def base(self) -> Buffer: return self._base if self._base is not None else self
  @property
  def lb_refcount(self): return self.base._lb_refcount
  def ref(self, cnt): self.base._lb_refcount += cnt
  def is_allocated(self) -> bool: return hasattr(self, '_buf')
  def ensure_allocated(self) -> Buffer: return self.allocate() if not hasattr(self, '_buf') else self
  def allocate(self, opaque=None) -> Buffer:
    assert not hasattr(self, '_buf'), "can't allocate already allocated buffer"
    self.allocator = Device[self.device].allocator
    if self._base is not None:
      self._base.ensure_allocated()
      assert hasattr(self.allocator, "offset"), "offset function required for view"
      self._buf: Any = self.allocator.offset(self.base._buf, self.nbytes, self.offset)
    else:
      self._buf = opaque if opaque is not None else self.allocator.alloc(self.nbytes, self.options)
      if not self.device.startswith("DISK"): GlobalCounters.mem_used += self.nbytes
    return self
  def __reduce__(self):
    buf = None
    if self._base is not None:
      return self.__class__, (self.device, self.size, self.dtype, None, None, None, 0, self.base, self.offset, hasattr(self, '_buf'))
    if self.device == "NPY": return self.__class__, (self.device, self.size, self.dtype, self._buf, self.options, None, self.lb_refcount)
    if self.is_allocated():
      buf = bytearray(self.nbytes)
      self.copyout(memoryview(buf))
    return self.__class__, (self.device, self.size, self.dtype, None, self.options, buf, self.lb_refcount)
  @property
  def nbytes(self): return self.size*self.dtype.itemsize
  def __del__(self):
    if not hasattr(self, '_buf'): return
    if self._base is None:
      if not self.device.startswith("DISK"): GlobalCounters.mem_used -= self.nbytes
      self.allocator.free(self._buf, self.nbytes, self.options)
  def __repr__(self):
    return f"<buf real:{hasattr(self, '_buf')} device:{self.device} size:{self.size} dtype:{self.dtype}" + \
           (f" offset:{self.offset}" if hasattr(self, "base") else "") + \
           (">" if self.options is None else f" {self.options=}>")
  def as_buffer(self, allow_zero_copy=False, force_zero_copy=False) -> memoryview:
    # zero copy with as_buffer (disabled by default due to use after free)
    if (force_zero_copy or allow_zero_copy) and hasattr(self.allocator, 'as_buffer'): return self.allocator.as_buffer(self._buf)
    assert not force_zero_copy, "force zero copy was passed, but copy is required"
    return self.copyout(memoryview(bytearray(self.nbytes)))
  def copyin(self, mv:memoryview):
    mv = flat_mv(mv)
    assert len(mv) == self.nbytes, f"size mismatch, {len(mv)=} != {self.dtype=} {self.size=}"
    assert self.is_allocated(), "can't copyin to unallocated buffer"
    self.allocator.copyin(self._buf, mv)
    return self
  def copyout(self, mv:memoryview) -> memoryview:
    mv = flat_mv(mv)
    assert len(mv) == self.nbytes, f"size mismatch, {len(mv)=} != {self.dtype=} {self.size=}"
    assert self.is_allocated(), "can't copyout unallocated buffer"
    self.allocator.copyout(mv, self._buf)
    return mv
  def view(self, size:int, dtype:DType, offset:int) -> Buffer:
    assert offset < self.nbytes, "offset must be less than nbytes"
    if self._base is not None: return Buffer(self.device, size, dtype, base=self._base, offset=self.offset+offset)
    return Buffer(self.device, size, dtype, base=self, offset=offset)

# TODO: size, dest, src are the same type. can we enforce this?
class Allocator:
  def alloc(self, size:int, options:Optional[BufferOptions]=None):
    assert not isinstance(size, int) or size > 0, f"alloc size must be positve, getting {size}"
    return self._alloc(size, options if options is not None else BufferOptions())
  def _alloc(self, size:int, options:BufferOptions): raise NotImplementedError("need alloc")
  def free(self, opaque, size:int, options:Optional[BufferOptions]=None):
    self._free(opaque, options if options is not None else BufferOptions())
  def _free(self, opaque, options:BufferOptions): pass  # if opaque is a Python object, you don't need a free
  def copyin(self, dest, src:memoryview): raise NotImplementedError("need copyin")
  def copyout(self, dest:memoryview, src): raise NotImplementedError("need copyout")

class LRUAllocator(Allocator):  # pylint: disable=abstract-method
  def __init__(self): self.cache: Dict[Tuple[int, Optional[BufferOptions]], Any] = defaultdict(list)
  def alloc(self, size:int, options:Optional[BufferOptions]=None):
    if len(c := self.cache[(size, options)]): return c.pop()
    try: return super().alloc(size, options)
    except (RuntimeError, MemoryError):
      self.free_cache()
      return super().alloc(size, options)
  def free_cache(self):
    for (sz,options),opaques in self.cache.items():
      for opaque in opaques: super().free(opaque, sz, options)
      opaques.clear()
  def free(self, opaque:Any, size:int, options:Optional[BufferOptions]=None):
    if getenv("LRU", 1) and (options is None or not options.nolru): self.cache[(size, options)].append(opaque)
    else: super().free(opaque, size, options)

class _MallocAllocator(LRUAllocator):
  def _alloc(self, size:int, options:BufferOptions): return (ctypes.c_uint8 * size)()
  def as_buffer(self, src) -> memoryview: return flat_mv(memoryview(src))
  def copyin(self, dest, src:memoryview): ctypes.memmove(dest, from_mv(src), len(src))
  def copyout(self, dest:memoryview, src): ctypes.memmove(from_mv(dest), src, len(dest))
  def offset(self, buf, size:int, offset:int): return from_mv(self.as_buffer(buf)[offset:offset+size])

MallocAllocator = _MallocAllocator()

# **************** base Runner + helpers ****************

class Runner:
  def __init__(self, display_name:str, dname:str, op_estimate:sint=0, mem_estimate:sint=0):
    self.first_run, self.display_name, self.dname, self.op_estimate, self.mem_estimate = True, display_name, dname, op_estimate, mem_estimate
  @property
  def device(self): return Device[self.dname]
  def exec(self, rawbufs:List[Buffer], var_vals:Optional[Dict[Variable, int]]=None) -> Optional[float]:
    return self(rawbufs, {} if var_vals is None else var_vals)
  def __call__(self, rawbufs:List[Buffer], var_vals:Dict[Variable, int], wait=False) -> Optional[float]:
    raise NotImplementedError("override this")

# **************** for Compiled Devices ****************

def fake_renderer(name, uops): raise NotImplementedError("needs a renderer")

@dataclass(frozen=True)
class CompilerOptions:
  device: str = ""
  suffix: str = ""
  # TODO: make this generic with a list of supported types
  supports_float4: bool = True
  has_local: bool = True
  has_shared: bool = True
  has_tensor_cores: bool = False
  # NOTE: these two should be in z,y,x(reversed) order for cstyle backends, they are flipped when kernel is rendered
  global_max: Optional[List[int]] = None
  local_max: Optional[List[int]] = None
  shared_max: int = 32768
  renderer: Callable = fake_renderer

class Compiler:
  compiler_opts: ClassVar[CompilerOptions]
  def __init__(self, cachekey:Optional[str]=None): self.cachekey = None if getenv("DISABLE_COMPILER_CACHE") else cachekey
  def compile(self, src:str) -> bytes: raise NotImplementedError("need a compile function")
  def compile_cached(self, src:str) -> bytes:
    if self.cachekey is None or (lib := diskcache_get(self.cachekey, src)) is None:
      lib = self.compile(src)
      if self.cachekey is not None: diskcache_put(self.cachekey, src, lib)
    return lib

  def to_program(self, k:Linearizer, override_device:Optional[str]=None) -> Program:
    k.linearize()
    info = get_lazyop_info(k.ast[0])
    ops, mem = k.uops.flops_mem()
    run_count = prod((k.global_size if k.global_size else []) + (k.local_size if k.local_size else []))
    # NOTE: we use min here to ignore the indexing FLOPS
    return Program(k.name, self.compiler_opts.renderer(to_function_name(k.name), k.uops),
                   override_device if override_device else self.compiler_opts.device,
                   k.global_size, k.local_size, k.uops, min(info.flops, ops * run_count), min(info.mem_estimate, mem * run_count))

@dataclass(frozen=True)
class Program:
  name:str
  src:str
  dname:str
  global_size:Optional[List[int]]=None
  local_size:Optional[List[int]]=None
  uops:Optional[UOpGraph]=None
  op_estimate:sint=0
  mem_estimate:sint=0

  @functools.cached_property
  def vars(self) -> List[Variable]: return [] if self.uops is None else self.uops.vars()

  @functools.cached_property
  def globals(self) -> List[Tuple[int, bool]]: return [] if self.uops is None else self.uops.globals()

  @functools.cached_property
  def outcount(self) -> int: return sum(x[1] for x in self.globals)

  @functools.cached_property
  def function_name(self) -> str: return to_function_name(self.name)

  def launch_dims(self, var_vals:Dict[Variable, int]):
    global_size = [sym_infer(sz, var_vals) for sz in self.global_size] if self.global_size is not None else None
    local_size = [sym_infer(sz, var_vals) for sz in self.local_size] if self.local_size is not None else None
    return global_size, local_size

class CompiledRunner(Runner):
  def __init__(self, p:Program, precompiled:Optional[bytes]=None):
    if DEBUG >= 4: print(p.src)
    self.p:Program = p
    self.lib:bytes = precompiled if precompiled is not None else Device[p.dname].compiler.compile_cached(p.src)
    self.clprg = Device[p.dname].runtime(p.function_name, self.lib)
    super().__init__(p.name, p.dname, p.op_estimate, p.mem_estimate)

  def __reduce__(self): return self.__class__, (self.p, self.lib)

  def __call__(self, rawbufs:List[Buffer], var_vals:Dict[Variable, int], wait=False) -> Optional[float]:
    global_size, local_size = self.p.launch_dims(var_vals)
    if global_size is not None and local_size is None and all_int(self.p.global_size): # type: ignore[arg-type]
      # TODO: this is copied from get_program
      from tinygrad.features.search import optimize_local_size
      local_size = optimize_local_size(self.clprg, global_size, rawbufs)
      global_size = [g//l if g%l == 0 else g/l for g,l in zip(global_size, local_size)]
      self.p = replace(self.p, global_size=global_size, local_size=local_size)
    lra = {}
    if global_size:
      lra['global_size'] = global_size
      assert len(global_size) == 3, "global size must have len 3"
    if local_size:
      lra['local_size'] = local_size
      assert len(local_size) == 3, "local size must have len 3"
    return self.clprg(*[x._buf for x in rawbufs], **lra, vals=tuple(var_vals[k] for k in self.p.vars), wait=wait)

method_cache: Dict[Tuple[str, Tuple[LazyOp, ...], int, bool], CompiledRunner] = {}
logkerns, logkerns_level = open(getenv("LOGKERNS", ""), "a") if getenv("LOGKERNS", "") else None, getenv("LOGKERNS_LEVEL", 1)
class Compiled:
  def __init__(self, device:str, allocator:Allocator, compiler:Optional[Compiler], runtime, graph=None):
    self.dname, self.allocator, self.compiler, self.runtime, self.graph = device, allocator, compiler if compiler else Compiler(), runtime, graph
  def synchronize(self): pass  # override this in your device

  def to_runner(self, k:Linearizer) -> CompiledRunner: return CompiledRunner(self.compiler.to_program(k, override_device=self.dname))

  def get_linearizer(self, *ast:LazyOp) -> Linearizer:
    if DEBUG >= 3:
      from tinygrad.features.graph import print_tree
      for op in ast: print_tree(op)
    from tinygrad.codegen.linearizer import Linearizer
    k = Linearizer(*ast, opts=self.compiler.compiler_opts)
    k.required_optimizations()
    if not NOOPT:
      if not (used_tensor_cores:=k.apply_tensor_cores(getenv("TC", 1))): k.hand_coded_optimizations()
      if BEAM >= 1:
        from tinygrad.features.search import beam_search, time_linearizer, bufs_from_lin
        kb, k_opt = Linearizer(*ast, opts=self.compiler.compiler_opts), k
        kb.required_optimizations()
        rawbufs = bufs_from_lin(kb, allocate=False)
        k = beam_search(kb, rawbufs, BEAM.value, bool(getenv("BEAM_ESTIMATE", 1)))
        if getenv("BEAM_COMPARE", 1):
          # TODO: move the HC/TC/BEAM compare to beam_search so it can be optionally cached which choice is better
          lins: List[Tuple[str, Linearizer]] = [(f"beam{BEAM.value}", k), (("tc" if used_tensor_cores else "hc"), k_opt)]
          if used_tensor_cores:
            lins.append(("hc", Linearizer(*ast, opts=self.compiler.compiler_opts)))
            lins[-1][1].hand_coded_optimizations()
          timed = sorted([(nm, tk, time_linearizer(tk, rawbufs, allow_test_size=False, clear_l2=True)) for nm, tk in lins], key=lambda x: x[2])
          if DEBUG >= 1: print("  <  ".join(f"{nm:6s} : {lin.colored_shape(30, dense=True)} : {tm*1e6:8.2f} us" for nm, lin, tm in timed))
          k = timed[0][1]
          if logkerns is not None and logkerns_level > 1: logkerns.writelines([f"{(lin.ast, lin.applied_opts)}\n" for (_,lin,_) in timed[1:]])
    # TODO: check the correctness inline once compare_linearizer is in core
    if logkerns is not None: logkerns.writelines([f"{(k.ast, k.applied_opts)}\n"])
    if DEBUG >= 4: print((k.ast, k.applied_opts)) # print here to show final applied_opts for all kernels instead of just in beam_search
    return k

  def get_runner(self, *ast:LazyOp) -> CompiledRunner:
    ckey = (self.dname, ast, BEAM.value, False)
    if cret:=method_cache.get(ckey): return cret
    bkey = (self.dname.split(":")[0], ast, BEAM.value, True)
    if bret:=method_cache.get(bkey):
      method_cache[ckey] = ret = CompiledRunner(replace(bret.p, dname=self.dname), bret.lib)
    else:
      method_cache[ckey] = method_cache[bkey] = ret = self.to_runner(self.get_linearizer(*ast))
    return ret
