from __future__ import annotations
from typing import Callable, List, Tuple, Dict, cast, Union, Optional, TypeVar, Generic
import functools, itertools, operator
from tinygrad.helpers import DEBUG, DType, merge_dicts, getenv, all_int
from tinygrad.device import Device, JITRunner, CompiledASTRunner, Buffer
from tinygrad.tensor import Tensor
from tinygrad.shape.shapetracker import ShapeTracker
from tinygrad.shape.symbolic import Variable, NumNode, Node
from weakref import ref, WeakKeyDictionary
from dataclasses import dataclass

@dataclass(frozen=True)
class JitItem:
  prg: JITRunner  # or a graph executor like MetalGraph
  rawbufs: List[Optional[Buffer]]

def get_jit_stats(jit_cache: List[JitItem]) -> Tuple[Node, Node]:
  return functools.reduce(operator.__add__, [ji.prg.op_estimate for ji in jit_cache], NumNode(0)), functools.reduce(operator.__add__, [ji.prg.mem_estimate for ji in jit_cache], NumNode(0))
def get_input_replace(jit_cache: List[JitItem], input_rawbuffers:List[Buffer]) -> Dict[Tuple[int, int], int]:
  input_replace: Dict[Tuple[int, int], int] = {}
  for j,ji in enumerate(jit_cache):
    for i,a in enumerate(ji.rawbufs):
      if a in input_rawbuffers:
        input_replace[(j,i)] = input_rawbuffers.index(a)
  return input_replace
def get_jc_idxs_with_updatable_launch_dims(jit_cache: List[JitItem]) -> List[int]:
  return [j for j,ji in enumerate(jit_cache) if isinstance(ji.prg, CompiledASTRunner) and ((ji.prg.global_size and not all_int(tuple(ji.prg.global_size))) or (ji.prg.local_size and not all_int(tuple(ji.prg.local_size))))]
def get_jc_idxs_with_updatable_var_vals(jit_cache: List[JitItem]) -> List[int]:
  return [j for j,ji in enumerate(jit_cache) if isinstance(ji.prg, CompiledASTRunner) and ji.prg.vars]

class GraphException(Exception): pass

ReturnType = TypeVar('ReturnType')
class TinyJit(Generic[ReturnType]):
  def __init__(self, fxn:Callable[..., ReturnType]):
    self.fxn = fxn
    self.reset()

  def reset(self):
    self.jit_cache: List[JitItem] = []
    self.input_replace: Dict[Tuple[int, int], int] = {}
    self.cnt: int = 0
    self.ret: Optional[ReturnType] = None
    self.expected_vals: Optional[Tuple[Variable, ...]] = None
    self.expected_name_sts_dtype: Optional[Tuple[Tuple[Union[int, str], ShapeTracker, DType], ...]] = None

  # add support for instance methods
  def __get__(self, obj, objtype): return functools.partial(self.__call__, obj)

  def __call__(self, *args, **kwargs) -> ReturnType:
    # all inputs are realized
    input_tensors: Dict[Union[int, str], Tensor] = {cast(Union[int, str], k):v.realize() for k,v in itertools.chain(enumerate(args), kwargs.items()) if v.__class__ is Tensor}
    expected_name_sts_dtype = tuple([(k, v.lazydata.st.unbind(), v.dtype) for k,v in input_tensors.items()])

    # get rawbuffers
    input_rawbuffers: List[Buffer] = [cast(Buffer, v.lazydata.realized) for v in input_tensors.values()]
    assert len(set(input_rawbuffers)) == len(input_rawbuffers), "duplicate inputs to JIT"

    # get variables: they can either be in Tensors or passed in as arguments, and all must be bound. these are all global
    var_vals: Dict[Variable, int] = merge_dicts([arg.lazydata.st.var_vals for arg in input_tensors.values()] + [dict(x.unbind() for x in itertools.chain(args, kwargs.values()) if isinstance(x, Variable))])
    expected_vals = tuple(var_vals.keys())

    if self.cnt >= 2:
      # jit exec
      assert self.expected_vals == expected_vals, "mismatch of var_vals"
      assert self.expected_name_sts_dtype == expected_name_sts_dtype, f"mismatch of sts, expected {self.expected_name_sts_dtype} got {expected_name_sts_dtype}"
      for (j,i),input_idx in self.input_replace.items(): self.jit_cache[j].rawbufs[i] = input_rawbuffers[input_idx]
      for ji in self.jit_cache: ji.prg(cast(List[Buffer], ji.rawbufs), var_vals, wait=DEBUG>=2, jit=True)
    elif self.cnt == 1:
      # jit capture
      self.expected_vals, self.expected_name_sts_dtype = expected_vals, expected_name_sts_dtype
      CacheCollector.start(var_vals)
      self.ret = self.fxn(*args, **kwargs)
      self.jit_cache = CacheCollector.finish()
      assert len(self.jit_cache) != 0, "didn't JIT anything!"
      if DEBUG >= 1: print(f"JIT captured {len(self.jit_cache)} kernels with {len(input_rawbuffers)} inputs")

      # Split JIT cache into batches for execution. This allows the accelerator to run some batches while subsequent graphs are still being updated.
      jit_batches: List[List[JitItem]] = [list()]
      for ji in self.jit_cache:
        if len(jit_batches[-1]) >= getenv("JIT_MAX_BATCH_SIZE", 32) or not isinstance(ji.prg, CompiledASTRunner): jit_batches.append(list())
        jit_batches[-1].append(ji)

      self.jit_cache = []
      for i,jb in enumerate(jit_batches):
        # if your Device supports it, condense the items into a graph executor.
        if (make_graph := Device[Device.DEFAULT].graph) and getenv("JIT") != 2:
          try:
            jb = [JitItem(make_graph(jb, input_rawbuffers, var_vals), cast(List[Optional[Buffer]], input_rawbuffers))]
            if DEBUG >= 2: print(f"JIT GRAPHing batch {i} with {len(jb)} kernels")
          except GraphException as e:
            if DEBUG >= 2: print(f"JIT GRAPHing failed batch {i} with {len(jb)} kernels: {e}")
        elif DEBUG >= 2: print(f"JIT regular batch {i} with {len(jb)} kernels")
        self.jit_cache.extend(jb)

      self.input_replace = get_input_replace(self.jit_cache, input_rawbuffers)
      assert len(set(self.input_replace.values())) == len(input_rawbuffers), "some input tensors not found"
    elif self.cnt == 0:
      # jit ignore
      self.ret = self.fxn(*args, **kwargs)

    # clear jit inputs
    for (j,i) in self.input_replace.keys(): self.jit_cache[j].rawbufs[i] = None

    self.cnt += 1
    return cast(ReturnType, self.ret)

class PlaceHolder:
  def __init__(self, buf:Buffer): self.size, self.dtype, self.device, self.ref, self.bufid = buf.size, buf.dtype, buf.device, ref(buf), id(buf._buf)
  def to_tuple(self): return (self.size, self.dtype, self.device, self.bufid)
  def __hash__(self): return hash(self.to_tuple())
  def __eq__(self, x): return isinstance(x, PlaceHolder) and self.to_tuple() == x.to_tuple()
  def alloc_if_needed(self, buffer_cache: Dict[PlaceHolder, Buffer]) -> Buffer:
    ret = self.ref()
    if ret: return ret
    if self not in buffer_cache: buffer_cache[self] = Buffer(self.device, self.size, self.dtype)
    return buffer_cache[self]

class _CacheCollector:
  def __init__(self):
    self.cache: Optional[List[Tuple[JITRunner, List[Union[Buffer, PlaceHolder]]]]] = None

  def start(self, var_vals:Optional[Dict[Variable, int]]=None):
    self.cache = []
    self.placeholders: WeakKeyDictionary[Buffer, PlaceHolder] = WeakKeyDictionary()
    self.var_vals = var_vals if var_vals is not None else {}

  def add(self, prg, rawbufs, var_vals):
    if self.cache is None: return
    for k,v in var_vals.items(): assert k in self.var_vals and self.var_vals[k] == v, f"var_vals {k} mismatch {v} != {self.var_vals.get(k)}"
    self.placeholders[rawbufs[0]] = PlaceHolder(rawbufs[0])    # NOTE: this is making an assumption that 0 is special
    self.cache.append((prg, [self.placeholders.get(x, x) if isinstance(x, Buffer) else x for x in rawbufs]))

  def finish(self) -> List[JitItem]:
    if self.cache is None: return []
    buffer_cache: Dict[PlaceHolder, Buffer] = {}
    saved_cache, self.cache = self.cache, None
    return [JitItem(prg, [x.alloc_if_needed(buffer_cache) if isinstance(x, PlaceHolder) else x for x in pl]) for prg, pl in saved_cache]
CacheCollector = _CacheCollector()
