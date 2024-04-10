from collections import defaultdict
from typing import DefaultDict, Dict, List
from tinygrad.buffer import Buffer
from tinygrad.engine.realize import run_schedule
from tinygrad.features.toposort import find_all_sorts
from tinygrad.helpers import DEBUG, colored
from tinygrad.lazy import LazyBuffer
from tinygrad.engine.schedule import graph_schedule
from tinygrad.ops import ScheduleItem

def fuzz_schedule(outs: List[LazyBuffer]):
  graph, in_degree, prescheduled = graph_schedule(outs, seen:=set())
  sorts = find_all_sorts(graph, in_degree)
  if DEBUG >= 2: print(colored(f"fuzzing {len(sorts)} toposorts", "yellow"))
  schedules: List[List[ScheduleItem]] = [[] for _ in sorts]
  fuzz_items: DefaultDict[LazyBuffer, List[ScheduleItem]] = defaultdict(list)
  for i, s in enumerate(sorts):
    rawbufs_map: Dict[LazyBuffer, Buffer] = {}
    for key in s:
      for buf in (ps:=prescheduled[key]).outputs: seen.add(buf)
      for x in ps.outputs: rawbufs_map[x] = Buffer(x.device, x.size, x.dtype) if i > 0 else x.buffer
      inputs = tuple(x.buffer if hasattr(x.buffer, "_buf") else rawbufs_map[x] for x in ps.inputs if x.size != 0)
      schedules[i].append(si:=ScheduleItem(ps.ast, tuple(rawbufs_map[x] for x in ps.outputs if x.size != 0), inputs))
      fuzz_items[key].append(si)

  for i, schedule in enumerate(schedules):
    if DEBUG >= 2: print(f"toposort permutation {i}")
    run_schedule(schedule)

  for items in fuzz_items.values():
    raw_outs = [[out.as_buffer().tobytes() for out in si.outputs] for si in items]
    assert all(o == raw_outs[0] for o in raw_outs)
  if DEBUG >= 2: print(colored("all toposorts passed", "green"))
