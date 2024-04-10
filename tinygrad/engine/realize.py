from typing import List, Dict, Optional
from tinygrad.helpers import getenv
from tinygrad.ops import ScheduleItem, BufferOps, LoadOps
from tinygrad.device import JITRunner, Device, BufferCopy, BufferXfer
from tinygrad.buffer import Buffer
from tinygrad.shape.symbolic import Variable

class CustomOp(JITRunner):
  def __init__(self, fxn):
    self.fxn = fxn
    super().__init__()
  def __call__(self, rawbufs:List[Buffer], var_vals:Dict[Variable, int], wait=False, jit=False): self.fxn(*rawbufs)

def lower_schedule_item(si:ScheduleItem) -> JITRunner:
  assert len(set(x.device for x in si.outputs+si.inputs)) == 1 or si.ast[0].op is LoadOps.COPY
  if si.ast[0].op is BufferOps.STORE: return Device[si.outputs[0].device].get_runner(*si.ast)
  assert len(si.ast) == 1 and len(si.outputs) == 1, "only ASTRunner supports multioutput"
  out, ast = si.outputs[0], si.ast[0]
  if ast.op is LoadOps.COPY:
    if hasattr(Device[out.device].allocator, 'transfer') and out.device.split(":")[0] == si.inputs[0].device.split(":")[0]: return BufferXfer()
    return BufferCopy()
  if ast.op is LoadOps.CUSTOM: return CustomOp(ast.arg)
  raise Exception(f"can't lower {ast.op}")

logops = open(getenv("LOGOPS", ""), "a") if getenv("LOGOPS", "") else None
def run_schedule(schedule:List[ScheduleItem], var_vals:Optional[Dict[Variable, int]] = None):
  while len(schedule):
    si = schedule.pop(0)
    if logops and si.ast[0].op not in LoadOps and not any(i.device.startswith("DISK:") for i in si.inputs): logops.write(str(si.ast)+"\n")

    # get the program
    prg = lower_schedule_item(si)

    for out in si.outputs:
      # we don't have an output buffer, we have to create it, and create to max size if it has symbolic shape
      if out.size > 0 and not (out.device.startswith("DISK") and si.ast[0].op is BufferOps.STORE) and not hasattr(out, "_buf"): out.allocate()

    # run the function (put it in JIT)
    prg.exec(list(si.outputs+si.inputs), var_vals if var_vals is not None else {})
