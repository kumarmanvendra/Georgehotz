from typing import List, Dict, Optional, cast, Generator, DefaultDict, Tuple, Iterable
from collections import defaultdict
from dataclasses import dataclass
from tinygrad.dtype import DType
from tinygrad.helpers import colored, getenv, dedup, DEBUG
from tinygrad.ops import ScheduleItem, BufferOps, LoadOps, copy_ast
from tinygrad.device import Runner, Device, BufferCopy, BufferXfer, update_stats
from tinygrad.buffer import Buffer
from tinygrad.shape.symbolic import Variable

@dataclass(frozen=True)
class ExecItem:
  prg: Runner
  rawbufs: List[Optional[Buffer]]
  def run(self, var_vals:Optional[Dict[Variable, int]]=None, wait=False, jit=False):
    self.prg([cast(Buffer, x).ensure_allocated() for x in self.rawbufs], var_vals if var_vals is not None else {}, wait=wait, jit=jit)

class CustomOp(Runner):
  def __init__(self, fxn):
    self.fxn = fxn
    super().__init__()
  def __call__(self, rawbufs:List[Buffer], var_vals:Dict[Variable, int], wait=False, jit=False): self.fxn(*rawbufs)

class EmptyOp(Runner):
  def __call__(self, rawbufs:List[Buffer], var_vals:Dict[Variable, int], wait=False, jit=False):
    update_stats(colored(f"empty {rawbufs[0].size:10d} {rawbufs[0].dtype}", "yellow"), 0, 0, {}, jit, 1, device=rawbufs[0].device)

def lower_schedule_item(si:ScheduleItem) -> Runner:
  assert len(set(x.device for x in si.outputs+si.inputs)) == 1 or si.ast[0].op is LoadOps.COPY
  if si.ast[0].op is BufferOps.STORE: return Device[si.outputs[0].device].get_runner(*si.ast)
  assert len(si.ast) == 1 and len(si.outputs) == 1, "only ASTRunner supports multioutput"
  out, ast = si.outputs[0], si.ast[0]
  if ast.op is LoadOps.COPY:
    if hasattr(Device[out.device].allocator, 'transfer') and out.device.split(":")[0] == si.inputs[0].device.split(":")[0]:
      return Device[si.outputs[0].device].get_runner(copy_ast(ast.arg)) if getenv("USE_COPY_KERNEL") else BufferXfer()
    return BufferCopy()
  if ast.op is LoadOps.CUSTOM: return CustomOp(ast.arg)
  if ast.op is LoadOps.EMPTY: return EmptyOp()
  raise RuntimeError(f"don't know how to lower {ast}")

def lower_schedule(schedule:List[ScheduleItem]) -> Generator[ExecItem, None, None]:
  while len(schedule): yield ExecItem(lower_schedule_item(si:=schedule.pop(0)), list(si.outputs+si.inputs))

capturing: List = []  # put classes with an add method in here

def _internal_memory_planner(buffers:List[Iterable[Buffer]], debug_prefix="") -> Dict[Buffer, Buffer]:
  first_appearance, last_appearance = {}, {}
  for i,u in enumerate(buffers):
    for buf in u: 
      if buf not in first_appearance: first_appearance[buf] = i
      last_appearance[buf] = i

  # Sort buffer by len, process requests starting from the biggest buffers, since it's 100% to allocate.
  # Choose any buffer from already allocated buffers which does not intersect with the new usage segment or allocate a new buffer.
  # TODO: Time complexity should be better
  assigned: Dict[Buffer, Buffer] = {}
  if getenv("NEW_MEMPLANNER", 1):
    buffer_requests: List[Tuple[int, int, int]] = []
    for buf in first_appearance.keys():
      if buf.is_allocated() or buf.lb_refcount > 0: continue
      buffer_requests.append((buf.nbytes, (first_appearance[buf], last_appearance[buf]), buf))

    buffer_pool = []
    buffer_requests = sorted(buffer_requests, key=lambda x: x[0], reverse=True)
    for _, seg, buf in buffer_requests:
      found_buf = None
      for i,(reuse_buf, used_segments) in enumerate(buffer_pool):
        if reuse_buf.device != buf.device or reuse_buf.dtype != buf.dtype: continue
        if not any(seg[0] <= useg[1] and seg[1] >= useg[0] for useg in used_segments):
          found_buf = i
          break
      if found_buf is None:
        buffer_pool.append((Buffer(buf.device, buf.size, buf.dtype), []))
        found_buf = -1
      assigned[buf] = buffer_pool[found_buf][0]
      buffer_pool[found_buf][1].append(seg)
  else:
    # LRU algorithm
    local_cache: DefaultDict[Tuple[str, int, DType], List[Buffer]] = defaultdict(list)
    for i,u in enumerate(buffers):
      for buf in u:
        # all unallocated unparented buffers are fair game to replace
        if buf.is_allocated() or buf.lb_refcount > 0: continue
        key = (buf.device, buf.size, buf.dtype)
        if buf not in assigned:
          if len(ll:=local_cache[key]): assigned[buf] = ll.pop()
          else: assigned[buf] = Buffer(*key)
        if i == last_appearance[buf]:
          local_cache[key].append(assigned[buf])

  if DEBUG >= 0 and len(ak:=dedup(assigned.keys())) != len(av:=dedup(assigned.values())):
    print(debug_prefix+f"memory reduced from {sum([x.nbytes for x in ak])/1e6:.2f} MB to {sum([x.nbytes for x in av])/1e6:.2f} MB")
  return assigned

def memory_planner(schedule:List[ScheduleItem]) -> List[ScheduleItem]:
  assigned = _internal_memory_planner([si.outputs+si.inputs for si in schedule])
  return [ScheduleItem(si.ast, tuple(assigned.get(x, x) for x in si.outputs),
                               tuple(assigned.get(x, x) for x in si.inputs)) for si in schedule]

def run_schedule(schedule:List[ScheduleItem], var_vals:Optional[Dict[Variable, int]]=None):
  for ei in lower_schedule(schedule):
    if len(capturing): capturing[0].add(ei)
    ei.run(var_vals)
