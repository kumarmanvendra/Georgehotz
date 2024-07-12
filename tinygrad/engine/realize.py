from typing import List, Dict, Optional, cast, Generator, Tuple
import time, pprint
from dataclasses import dataclass, replace
from tinygrad.helpers import colored, getenv, DEBUG, GlobalCounters, ansilen, BEAM, NOOPT, all_int, CAPTURING, Metadata
from tinygrad.ops import BufferOps, MetaOps, LazyOp
from tinygrad.device import Device, Buffer
from tinygrad.shape.symbolic import Variable, sym_infer, sint
from tinygrad.renderer import Renderer, Program
from tinygrad.codegen.linearizer import Linearizer
from tinygrad.engine.schedule import ScheduleItem

# **************** Program Creation ****************

logkerns, logkerns_level = open(getenv("LOGKERNS", ""), "a") if getenv("LOGKERNS", "") else None, getenv("LOGKERNS_LEVEL", 1)
def get_linearizer(renderer:Renderer, ast:Tuple[LazyOp, ...]) -> Linearizer:
  if DEBUG >= 5:
    from tinygrad.engine.graph import print_tree
    for op in ast: print_tree(op)
  k = Linearizer(*ast, opts=renderer)
  k.required_optimizations()
  if not NOOPT:
    if not (used_tensor_cores:=k.apply_tensor_cores(getenv("TC", 1))): k.hand_coded_optimizations()
    if BEAM >= 1:
      from tinygrad.engine.search import beam_search, time_linearizer, bufs_from_lin
      kb, k_opt = Linearizer(*ast, opts=renderer), k
      kb.required_optimizations()
      rawbufs = bufs_from_lin(kb, allocate=False)
      k = beam_search(kb, rawbufs, BEAM.value, bool(getenv("BEAM_ESTIMATE", 1)))
      if getenv("BEAM_COMPARE", 1):
        # TODO: move the HC/TC/BEAM compare to beam_search so it can be optionally cached which choice is better
        lins: List[Tuple[str, Linearizer]] = [(f"beam{BEAM.value}", k), (("tc" if used_tensor_cores else "hc"), k_opt)]
        if used_tensor_cores:
          lins.append(("hc", Linearizer(*ast, opts=renderer)))
          lins[-1][1].hand_coded_optimizations()
        timed = sorted([(nm, tk, time_linearizer(tk, rawbufs, allow_test_size=False, clear_l2=True)) for nm, tk in lins], key=lambda x: x[2])
        if DEBUG >= 1: print("  <  ".join(f"{nm:6s} : {lin.colored_shape(30, dense=True)} : {tm*1e6:8.2f} us" for nm, lin, tm in timed))
        k = timed[0][1]
        if logkerns is not None and logkerns_level > 1: logkerns.writelines([f"{(lin.ast, lin.applied_opts)}\n" for (_,lin,_) in timed[1:]])
  # TODO: check the correctness inline once compare_linearizer is in core
  if logkerns is not None: logkerns.writelines([f"{(k.ast, k.applied_opts)}\n"])
  if DEBUG >= 5: print((k.ast, k.applied_opts)) # print here to show final applied_opts for all kernels instead of just in beam_search
  return k

# **************** Runners ****************

class Runner:
  def __init__(self, display_name:str, dname:str, op_estimate:sint=0, mem_estimate:sint=0):
    self.first_run, self.display_name, self.dname, self.op_estimate, self.mem_estimate = True, display_name, dname, op_estimate, mem_estimate
  @property
  def device(self): return Device[self.dname]
  def exec(self, rawbufs:List[Buffer], var_vals:Optional[Dict[Variable, int]]=None) -> Optional[float]:
    return self(rawbufs, {} if var_vals is None else var_vals)
  def __call__(self, rawbufs:List[Buffer], var_vals:Dict[Variable, int], wait=False) -> Optional[float]:
    raise NotImplementedError("override this")

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
      from tinygrad.engine.search import optimize_local_size
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

class CustomOp(Runner):
  def __init__(self, fxn):
    self.fxn = fxn
    super().__init__(self.fxn.__name__, "CUSTOM", 0, 0)
  def __call__(self, rawbufs:List[Buffer], var_vals:Dict[Variable, int], wait=False): self.fxn(*rawbufs)

class EmptyOp(Runner):
  def __init__(self, buf:Buffer): super().__init__(colored(f"empty {buf.size:10d} {buf.dtype}", "yellow"), buf.device)
  def __call__(self, rawbufs:List[Buffer], var_vals:Dict[Variable, int], wait=False): pass

class ViewOp(Runner):
  def __init__(self, buf:Buffer): super().__init__(colored(f"view {buf.nbytes:8d} @ {buf.offset:<10d}", "yellow"), buf.device)
  def __call__(self, rawbufs:List[Buffer], var_vals:Dict[Variable, int], wait=False):
    assert rawbufs[0]._base is not None and rawbufs[0]._base == rawbufs[1].base, f"must be base {rawbufs}"

class BufferCopy(Runner):
  def __init__(self, total_sz, dest_device, src_device):
    if total_sz >= 1e6: name = f"{type(self).__name__[6:].lower()} {total_sz/1e6:7.2f}M, {dest_device[:7]:>7s} <- {src_device[:7]:7s}"
    else: name = f"{type(self).__name__[6:].lower()} {total_sz:8d}, {dest_device[:7]:>7s} <- {src_device[:7]:7s}"
    super().__init__(colored(name, "yellow"), dest_device, 0, total_sz)
  def copy(self, dest, src):
    disk_supports_fast_copyout = src.device.startswith("DISK") and hasattr(src.allocator.device, 'io_uring') and hasattr(src.allocator.device, 'fd')
    if src.device.startswith("DISK") and hasattr(dest.allocator, 'copy_from_disk') and disk_supports_fast_copyout and src.nbytes >= 4096:
      dest.allocator.copy_from_disk(dest._buf, src._buf, src.nbytes)
    elif src.device.startswith("DISK") and hasattr(dest.allocator, 'as_buffer'):
      # fast(ish) path, uses readinto in diskbuffers
      src.allocator.copyout(dest.allocator.as_buffer(dest._buf), src._buf)
    else:
      dest.copyin(src.as_buffer(allow_zero_copy=True))  # may allocate a CPU buffer depending on allow_zero_copy
  def __call__(self, rawbufs:List[Buffer], var_vals:Dict[Variable, int], wait=False):
    dest, src = rawbufs[0:2]
    assert dest.size == src.size and dest.dtype == src.dtype, f"buffer copy mismatch, {dest.size} != {src.size}, {dest.dtype} != {src.dtype}"
    st = time.perf_counter()
    self.copy(dest, src)
    if wait:
      Device[dest.device].synchronize()
      return time.perf_counter() - st

class BufferXfer(BufferCopy):
  def copy(self, dest, src):
    if hasattr(dest.allocator.device, "track_cross_buffer") and hasattr(src.allocator, "track_cross_device"):
      dest.allocator.device.track_cross_buffer.append(src)
      src.allocator.track_cross_device.add(dest.allocator.device)
    dest.allocator.transfer(dest._buf, src._buf, dest.nbytes, src_dev=src.allocator.device, dest_dev=dest.allocator.device)

# **************** method cache ****************

method_cache: Dict[Tuple[str, Tuple[LazyOp, ...], int, bool], CompiledRunner] = {}
def get_runner(dname:str, ast:Tuple[LazyOp, ...]) -> CompiledRunner:
  ckey = (dname, ast, BEAM.value, False)
  if cret:=method_cache.get(ckey): return cret
  bkey = (dname.split(":")[0], ast, BEAM.value, True)
  if bret:=method_cache.get(bkey):
    method_cache[ckey] = ret = CompiledRunner(replace(bret.p, dname=dname), bret.lib)
  else:
    prg: Program = get_linearizer(Device[dname].renderer, ast).to_program()
    if hasattr(prg.uops, "_fuzz_paths"):
      from test.external.fuzz_uops import UOpsFuzzerRunner
      return UOpsFuzzerRunner(replace(prg, dname=dname))
    method_cache[ckey] = method_cache[bkey] = ret = CompiledRunner(replace(prg, dname=dname))
  return ret

# **************** lowering functions ****************

@dataclass(frozen=True)
class ExecItem:
  prg: Runner
  bufs: List[Optional[Buffer]]
  metadata: Optional[List[Metadata]] = None
  def run(self, var_vals:Optional[Dict[Variable, int]]=None, wait=False, jit=False, do_update_stats=True) -> Optional[float]:
    bufs = [cast(Buffer, x) for x in self.bufs] if jit else [cast(Buffer, x).ensure_allocated() for x in self.bufs]
    et = self.prg(bufs, var_vals if var_vals is not None else {}, wait=wait or DEBUG >= 2)
    if do_update_stats:
      GlobalCounters.kernel_count += 1
      GlobalCounters.global_ops += (op_estimate:=sym_infer(self.prg.op_estimate, var_vals))
      GlobalCounters.global_mem += (mem_estimate:=sym_infer(self.prg.mem_estimate, var_vals))
      if et is not None: GlobalCounters.time_sum_s += et
      if DEBUG >= 2:
        ptm = (colored(f"{et*1e3:9.2f}ms", "yellow") if et > 0.01 else f"{et*1e6:9.2f}us") if et is not None else ""
        print(f"{colored(f'*** {self.prg.dname[:7]:7s} {GlobalCounters.kernel_count:4d}', 'magenta' if jit else ('green' if self.prg.first_run else None))} {self.prg.display_name+' '*(38-ansilen(self.prg.display_name))} arg {len(self.bufs):3d} mem {GlobalCounters.mem_used/1e9:5.2f} GB " +  # noqa: E501
              (str() if et is None else f"tm {ptm}/{GlobalCounters.time_sum_s*1e3:9.2f}ms ({op_estimate/((et or 1e-20)*1e9):8.2f} GFLOPS, {mem_estimate/((et or 1e-20)*1e9):7.2f} GB/s)" +  # noqa: E501
               f" {[repr(m) if DEBUG >= 3 else str(m) for m in self.metadata] if self.metadata else ''}"))
      self.prg.first_run = False
    return et

def lower_schedule_item(si:ScheduleItem) -> ExecItem:
  assert len(set(x.device for x in si.bufs)) == 1 or si.ast[0].op is MetaOps.COPY or getenv("USE_COPY_KERNEL")
  if si.ast[0].op is BufferOps.STORE:
    runner = get_runner(si.outputs[0].device, si.ast)
    return ExecItem(runner, [si.bufs[x[0]] for x in runner.p.globals], si.metadata)
  out, ast = si.outputs[0], si.ast[0]
  if ast.op is MetaOps.COPY:
    kernel_type = BufferCopy
    if hasattr(Device[out.device].allocator, 'transfer') and out.device.split(":")[0] == si.inputs[0].device.split(":")[0]:
      kernel_type = BufferXfer
    return ExecItem(kernel_type(ast.arg, out.device, si.inputs[0].device), list(si.bufs))
  if ast.op is MetaOps.CUSTOM: return ExecItem(CustomOp(ast.arg), list(si.bufs))
  if ast.op is MetaOps.EMPTY: return ExecItem(EmptyOp(out), list(si.bufs))
  if ast.op is MetaOps.VIEW: return ExecItem(ViewOp(out), list(si.bufs))
  raise RuntimeError(f"don't know how to lower {ast}")

def lower_schedule(schedule:List[ScheduleItem]) -> Generator[ExecItem, None, None]:
  while len(schedule):
    si = schedule.pop(0)
    try: yield lower_schedule_item(si)
    except Exception as e:
      if DEBUG >= 2:
        print(f"error lowering {si.ast[0].op}")
        print("tensor operations:")
        pprint.pprint(si.metadata, indent=2)
      raise e

# **************** main run function ****************

capturing: List = []  # put classes with an add method in here

def run_schedule(schedule:List[ScheduleItem], var_vals:Optional[Dict[Variable, int]]=None, do_update_stats=True):
  for ei in lower_schedule(schedule):
    if len(capturing) and CAPTURING: capturing[0].add(ei)
    ei.run(var_vals, do_update_stats=do_update_stats)
