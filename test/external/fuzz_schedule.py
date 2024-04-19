import itertools
import numpy as np
from typing import DefaultDict, Dict, List, Set, Tuple, TypeVar
from tinygrad.buffer import Buffer
from tinygrad.engine.realize import CustomOp, ExecItem, capturing, lower_schedule_item
from tinygrad.helpers import DEBUG, MULTIOUTPUT, colored, getenv
from tinygrad.lazy import LazyBuffer
from tinygrad.engine.schedule import _graph_schedule, _LBScheduleItem
from tinygrad.ops import LoadOps, ScheduleItem
from tinygrad.tensor import Tensor

ctx_vars = { MULTIOUTPUT: (0, 1) }

def fuzz_schedule(outs: List[LazyBuffer]):
  toposorts: Dict[Tuple[LazyBuffer, ...], Tuple[Dict, Dict[LazyBuffer, _LBScheduleItem]]] = {}
  for combination in itertools.product(*ctx_vars.values()):
    for var, val in zip(ctx_vars, combination): var.value = val
    graph, in_degree, prescheduled = _graph_schedule(outs, set())
    for ts in find_all_toposorts(graph, in_degree):
      if ts not in toposorts: toposorts[ts] = (dict(zip([v.key for v in ctx_vars], combination)), prescheduled)
  if DEBUG >= 1: print(colored(f"fuzzing {len(toposorts)} schedule permutations", "yellow"))

  ground_truth: Dict[LazyBuffer, memoryview] = {}
  # IMPORTANT: freeze prerealized bufs before ScheduleItem exec
  prerealized: Dict[LazyBuffer, memoryview] = {}
  seed = Tensor._seed

  for i, (ts, (ctx, prescheduled)) in enumerate(toposorts.items()):
    if DEBUG >= 1: print(colored(f"testing permutation {i} {ctx}", "yellow"))
    rawbufs: Dict[LazyBuffer, Buffer] = {}
    for key in ts:
      # setup ground truth
      if i == 0:
        for out in (ps:=prescheduled[key]).outputs:
          # freeze assign state before exec
          if out.op is LoadOps.ASSIGN: prerealized[out] = out.buffer.as_buffer()
        for x in ps.inputs:
          if x not in ground_truth and x.device != "NPY": prerealized[x] = x.buffer.as_buffer()
        si = ScheduleItem(ps.ast, tuple(x.buffer for x in ps.outputs if x.size != 0), tuple(x.buffer for x in ps.inputs if x.size != 0))
        _exec_si(si, seed)
        for out in ps.outputs:
          ground_truth[out] = out.buffer.as_buffer()
          del out.srcs # only schedule the LazyBuffer in this fuzz run
        continue

      # exec and validate the permutation with new Buffers
      for out in (ps:=prescheduled[key]).outputs:
        rawbufs[out] = Buffer(out.buffer.device, out.buffer.size, out.buffer.dtype)
        if out.op is LoadOps.ASSIGN: rawbufs[out].ensure_allocated().copyin(prerealized[out])
      for x in ps.inputs:
        if x not in rawbufs:
          if x.device == "NPY": rawbufs[x] = x.buffer
          # copy the pre realized input
          else: rawbufs[x] = Buffer(x.buffer.device, x.buffer.size, x.buffer.dtype, initial_value=prerealized[x])
      si = ScheduleItem(ps.ast, tuple(rawbufs[x] for x in ps.outputs if x.size != 0), tuple(rawbufs[x] for x in ps.inputs if x.size != 0))
      _exec_si(si, seed)
      for out in ps.outputs:
        outbuf = np.frombuffer(rawbufs[out].as_buffer(), out.dtype.np)
        try: np.testing.assert_allclose(outbuf, np.frombuffer(ground_truth[out], out.dtype.np), atol=1e-2, rtol=1e-2)
        except Exception as e:
          print(f"FAILED FOR {out}")
          raise e

def _exec_si(si: ScheduleItem, seed:int):
  ei = ExecItem(lower_schedule_item(si), list(si.outputs+si.inputs))
  if len(capturing): capturing[0].add(ei)
  if isinstance(ei.prg, CustomOp): Tensor._seed = seed
  ei.run()

T = TypeVar("T")
def find_all_toposorts(graph:DefaultDict[T, List[T]], in_degree:DefaultDict[T, int]) -> List[Tuple[T, ...]]:
  visited: Set[T] = set()
  ret: List[Tuple[T, ...]] = []
  path: List[T] = []

  def recurse_paths(path:List[T]):
    for v, d in in_degree.items():
      if d != 0 or v in visited: continue
      for u in graph[v]: in_degree[u] -= 1
      path.append(v)
      visited.add(v)
      recurse_paths(path)
      if len(ret) >= getenv("FUZZ_SCHEDULE_MAX_PATHS", 10): return
      # backtrack
      for u in graph[v]: in_degree[u] += 1
      path.pop()
      visited.remove(v)
    if len(path) == len(in_degree): ret.append(tuple([*path]))
  recurse_paths(path)

  if len(ret) == 0: raise RuntimeError("detected cycle in the graph")
  # verify all paths are unique
  assert len(ret) == len(set(ret))
  return ret
