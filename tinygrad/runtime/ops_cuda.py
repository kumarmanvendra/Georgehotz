import subprocess, time, re, hashlib, tempfile, ctypes, ctypes.util
from pathlib import Path
from typing import Tuple, cast, Callable, Any, List, Dict, Optional
import numpy as np
import gpuctypes.cuda as cuda
from tinygrad.helpers import DEBUG, getenv, colored, diskcache, to_char_p_p, get_bytes, create_c_struct, ARCWrapper
from tinygrad.device import Compiled, CompiledASTRunner, update_stats
from tinygrad.runtime.lib import RawBuffer, RawBufferCopyInOut, RawMallocBuffer, LRUAllocator, cpu_time_execution
from tinygrad.codegen.kernel import LinearizerOptions
from tinygrad.renderer.cuda import CUDARenderer
from tinygrad.shape.symbolic import Variable
from tinygrad.jit import JitItem, get_input_replace, get_jit_stats, get_jc_idxs_with_updatable_launch_dims, get_jc_idxs_with_updatable_var_vals, GraphException

def check(status):
  if status != 0: raise RuntimeError(f"CUDA Error {status}, {ctypes.string_at((cuda.cuGetErrorString(status, ctypes.byref(c_ptr := ctypes.c_char_p())), c_ptr)[1]).decode()}")

def compile_cuda_style(prg, compile_options, prog_t, create_prog, compile_prog, get_code, get_code_size, get_log, get_log_size, check) -> bytes:
  check(create_prog(ctypes.byref(prog := prog_t()), prg.encode(), "<null>".encode(), 0, None, None))
  status = compile_prog(prog, len(compile_options), to_char_p_p([ctypes.create_string_buffer(o.encode("utf-8")) for o in compile_options]))

  if status != 0: raise RuntimeError(f"compile failed: {get_bytes(prog, get_log_size, get_log, check)}")
  return get_bytes(prog, get_code_size, get_code, check)

def encode_args_cuda_style(args, marks=(1,2,0)) -> Tuple[ctypes.Array, ctypes.Structure]:
  c_args = create_c_struct(tuple([(f'f{i}', ctypes.c_void_p if isinstance(x, RawBuffer) else ctypes.c_int) for i,x in enumerate(args)]))(*[x._buf if isinstance(x, RawBuffer) else x for x in args])
  return (ctypes.c_void_p * 5)(ctypes.c_void_p(marks[0]), ctypes.cast(ctypes.pointer(c_args), ctypes.c_void_p), ctypes.c_void_p(marks[1]),
                               ctypes.cast(ctypes.pointer(ctypes.c_size_t(ctypes.sizeof(c_args))), ctypes.c_void_p), ctypes.c_void_p(marks[2])), c_args

def time_execution_cuda_style(cb, ev_t, evcreate, evrecord, evsync, evdestroy, evtime, enable=False) -> Optional[float]:
  if not enable: return cb()
  evs = [ARCWrapper((evcreate(ctypes.byref(ev := ev_t()), 0), ev)[1], evdestroy) for _ in range(2)]
  evrecord(evs[0].obj, None)
  cb()
  evrecord(evs[1].obj, None)
  evsync(evs[1].obj)
  return (ret := ctypes.c_float(), evtime(ctypes.byref(ret), evs[0].obj, evs[1].obj))[1]
def cu_time_execution(cb, enable=False) -> Optional[float]: return time_execution_cuda_style(cb, cuda.CUevent, cuda.cuEventCreate, cuda.cuEventRecord, cuda.cuEventSynchronize, cuda.cuEventDestroy_v2, cuda.cuEventElapsedTime, enable=enable)

def pretty_ptx(s):
  # all expressions match `<valid_before><expr><valid_after>` and replace it with `<valid_before>color(<expr>)<valid_after>`
  s = re.sub(r'([!@<\[\s,\+\-;\n])((?:[_%$][\w%\$_]+(?:\.[xyz])?\:?)|(?:buf\d+))([<>\]\s,\+\-;\n\)])', lambda m:m[1]+colored(m[2], "blue")+m[3], s, flags=re.M) # identifiers
  s = re.sub(r'(.)((?:b|s|u|f)(?:8|16|32|64)|pred)([\.\s])', lambda m:m[1]+colored(m[2], "green")+m[3], s, flags=re.M) # types
  s = re.sub(r'^(\s*)([\w]+)(.*?;$)', lambda m:m[1]+colored(m[2], "yellow")+m[3], s, flags=re.M) # instructions
  s = re.sub(r'([<>\[\]\s,\+\-;])((?:0[fF][0-9a-fA-F]{8})|(?:[0-9]+)|(?:0[xX][0-9a-fA-F]+))([<>\[\]\s,\+\-;])', lambda m:m[1]+colored(m[2], "yellow")+m[3], s, flags=re.M) # numbers
  s = re.sub(r'(\.)(param|reg|global)', lambda m:m[1]+colored(m[2], "magenta"), s, flags=re.M) # space
  s = re.sub(r'(\.)(version|target|address_size|visible|entry)', lambda m:m[1]+colored(m[2], "magenta"), s, flags=re.M) # derivatives
  return s

if getenv("CUDACPU", 0) == 1:
  lib = ctypes.CDLL(ctypes.util.find_library("gpuocelot"))
  lib.ptx_run.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]

  RawCUDABuffer = RawMallocBuffer
  cu_time_execution = cpu_time_execution
  major, minor = ctypes.c_int(3), ctypes.c_int(5)
  cuda.cuLaunchKernel = lambda src, gx, gy, gz, lx, ly, lz, shared, stream, args: lib.ptx_run(src, len(args), (ctypes.c_void_p * len(args))(*[ctypes.cast(x, ctypes.c_void_p) for x in args]), lx, ly, lz, gx, gy, gz, shared)  # noqa: E731
  cuda.cuCtxSynchronize = lambda: None
else:
  check(cuda.cuInit(0))
  check(cuda.cuDeviceGet(ctypes.byref(device := cuda.CUdevice()), 0))
  check(cuda.cuCtxCreate_v2(ctypes.byref(context := cuda.CUcontext()), 0, device))
  check(cuda.cuDeviceComputeCapability(ctypes.byref(major := ctypes.c_int()), ctypes.byref(minor := ctypes.c_int()), 0))

  class CUDAAllocator(LRUAllocator):
    def __init__(self): super().__init__(self._get_cur_free_space(None))
    def _do_alloc(self, size, dtype, device, **kwargs): return (check(cuda.cuMemAlloc_v2(ctypes.byref(buf := cuda.CUdeviceptr()), size * dtype.itemsize)), buf.value)[1]
    def _do_free(self, buf): cuda.cuMemFree(buf)
    def _cached_bufkey(self, size, dtype, device): return (device, size*dtype.itemsize) # Buffers of the same length could be reused, no matter what dtype.
    def _get_cur_free_space(self, device): return (check(cuda.cuMemGetInfo_v2(ctypes.byref(free := ctypes.c_size_t()), ctypes.byref(total := ctypes.c_size_t()))), free.value)[1]
  CUDAAlloc = CUDAAllocator()

  class RawCUDABuffer(RawBufferCopyInOut):
    def __init__(self, size, dtype): super().__init__(size, dtype, allocator=CUDAAlloc)
    def _copyin(self, x:np.ndarray): cuda.cuMemcpyHtoDAsync_v2(self._buf, np.require(x, requirements='C').ctypes.data_as(ctypes.c_void_p), self.size * self.dtype.itemsize, None)
    def _copyout(self, x:np.ndarray): cuda.cuMemcpyDtoH_v2(np.require(x, requirements='C').ctypes.data_as(ctypes.c_void_p), self._buf, self.size * self.dtype.itemsize)
compile_opts = ["-I/usr/local/cuda/include", f'--gpu-architecture=sm_{major.value}{minor.value}']

@diskcache
def compile_cuda(prg) -> bytes: return compile_cuda_style(prg, compile_opts, cuda.nvrtcProgram, cuda.nvrtcCreateProgram, cuda.nvrtcCompileProgram, cuda.nvrtcGetPTX, cuda.nvrtcGetPTXSize, cuda.nvrtcGetProgramLog, cuda.nvrtcGetProgramLogSize, check)

class CUDAProgram:
  def __init__(self, name:str, _prg:bytes):
    prg = _prg.decode('utf-8')
    if DEBUG >= 5: print(pretty_ptx(prg))
    if DEBUG >= 6:
      try:
        fn = (Path(tempfile.gettempdir()) / f"tinycuda_{hashlib.md5(prg.encode('utf-8')).hexdigest()}").as_posix()
        with open(fn + ".ptx", "wb") as f: f.write(prg.encode('utf-8'))
        subprocess.run(["ptxas", f"-arch=sm_{major.value}{minor.value}", "-o", fn, fn+".ptx"], check=True)
        print(subprocess.check_output(['nvdisasm', fn]).decode('utf-8'))
      except Exception as e: print("failed to generate SASS", str(e))

    if getenv("CUDACPU", 0) == 0:
      self.module = ARCWrapper((check(cuda.cuModuleLoadData(ctypes.byref(module := cuda.CUmodule()), prg.encode('utf-8'))), module)[1], cuda.cuModuleUnload)
      check(cuda.cuModuleGetFunction(ctypes.byref(prg := cuda.CUfunction()), self.module.obj, name.encode("utf-8")))
    self.prg = prg

  def __call__(self, *args, global_size:Tuple[int,int,int], local_size:Tuple[int,int,int], wait=False):
    if getenv("CUDACPU", 0) == 1: c_params = [x._buf if not isinstance(x, int) else x for x in args]
    else: c_params, _ = encode_args_cuda_style(args)

    return cu_time_execution(lambda: check(cuda.cuLaunchKernel(self.prg, *global_size, *local_size, 0, None, None, c_params)), enable=wait)

class CUDAGraph:
  def __init__(self, jit_cache: List[JitItem], input_rawbuffers: List[RawBuffer], var_vals: Dict[Variable, int]):
    if not all(isinstance(ji.prg, CompiledASTRunner) for ji in jit_cache): raise GraphException

    self.jit_cache = jit_cache
    self.input_replace = get_input_replace(jit_cache, input_rawbuffers)
    self.op_estimate, self.mem_estimate = get_jit_stats(jit_cache)
    self.jc_idxs_with_updatable_launch_dims = get_jc_idxs_with_updatable_launch_dims(jit_cache)
    self.jc_idxs_with_updatable_var_vals = get_jc_idxs_with_updatable_var_vals(jit_cache)
    self.jc_idxs_with_updatable_rawbufs = list(set([x[0] for x in self.input_replace.keys()]))
    self.updatable_nodes: Dict[int, Tuple[Any, Any, Any]] = {} # Dict[jc index] = tuple(graph_node, node_params)

    self.graph, graph_node = self.graph_create(), None

    for (j,i),input_name in self.input_replace.items(): self.jit_cache[j].rawbufs[i] = input_rawbuffers[input_name]
    for j,ji in enumerate(self.jit_cache):
      prg: CompiledASTRunner = cast(CompiledASTRunner, ji.prg)

      c_deps = (type(graph_node)*1)(*(graph_node,)) if graph_node else None
      c_config, c_struct = encode_args_cuda_style(ji.rawbufs + [var_vals[x] for x in prg.vars], marks=self.launch_params_indicators())
      c_params = self.build_kernel_node_params(ji, prg, *prg.launch_dims(var_vals), c_config)
      graph_node = self.graph_add_kernel_node(self.graph.obj, c_deps, c_params)

      if j in self.jc_idxs_with_updatable_launch_dims or j in self.jc_idxs_with_updatable_var_vals or j in self.jc_idxs_with_updatable_rawbufs:
        self.updatable_nodes[j] = (graph_node, c_params, c_struct)

    self.instance = self.graph_instantiate(self.graph.obj)

  def __call__(self, input_rawbuffers: List[RawBuffer], var_vals: Dict[Variable, int], wait=False, jit=False) -> Optional[float]:
    # Update rawbuffers in the c_params struct.
    for (j,i),input_idx in self.input_replace.items():
      setattr(self.updatable_nodes[j][2], f'f{i}', input_rawbuffers[input_idx]._buf)

    # Update var_vals in the c_params struct.
    for j in self.jc_idxs_with_updatable_var_vals:
      for i,v in enumerate(cast(CompiledASTRunner, self.jit_cache[j].prg).vars):
        setattr(self.updatable_nodes[j][2], f'f{len(self.jit_cache[j].rawbufs) + i}', var_vals[v])
    
    # Update launch dims in the c_config struct.
    for j in self.jc_idxs_with_updatable_launch_dims:
      self.set_kernel_node_launch_dims(self.updatable_nodes[j][1], *cast(CompiledASTRunner, self.jit_cache[j].prg).launch_dims(var_vals))

    # Update graph nodes with the updated structs.
    for node, c_params, _ in self.updatable_nodes.values():
      self.graph_exec_kernel_node_set_params(self.instance.obj, node, ctypes.byref(c_params))

    et = self.graph_launch(self.instance.obj, None, wait=wait)
    update_stats(f"<batched {len(self.jit_cache)}>", self.op_estimate, self.mem_estimate, var_vals, et, buf_count=len(input_rawbuffers), jit=jit, num_kernels=len(self.jit_cache))
    return et

  def launch_params_indicators(self): return (1,2,0)
  def graph_create(self) -> ARCWrapper: return ARCWrapper((check(cuda.cuGraphCreate(ctypes.byref(graph := cuda.CUgraph()), 0)), graph)[1], cuda.cuGraphDestroy)
  def graph_instantiate(self, graph) -> ARCWrapper: return ARCWrapper((check(cuda.cuGraphInstantiate_v2(ctypes.byref(instance := cuda.CUgraphExec()), graph, None, None, 0)), instance)[1], cuda.cuGraphExecDestroy)
  def graph_add_kernel_node(self, graph, c_deps, c_params): return (check(cuda.cuGraphAddKernelNode(ctypes.byref(graph_node := cuda.CUgraphNode()), graph, c_deps, ctypes.sizeof(c_deps)//8 if c_deps else 0, ctypes.byref(c_params))), graph_node)[1]
  def graph_launch(self, *args, wait=False): return cu_time_execution(lambda: check(cuda.cuGraphLaunch(*args)), enable=wait)
  def graph_exec_kernel_node_set_params(self, *args): return check(cuda.cuGraphExecKernelNodeSetParams(*args))

  def build_kernel_node_params(self, ji, prg, global_size, local_size, c_config): return cuda.CUDA_KERNEL_NODE_PARAMS(prg.clprg.prg, *global_size, *local_size, 0, None, c_config)
  def set_kernel_node_launch_dims(self, node, global_size, local_size): node.blockDimX, node.blockDimY, node.blockDimZ, node.gridDimX, node.gridDimY, node.gridDimZ = *local_size, *global_size

CUDADevice = Compiled(RawCUDABuffer, LinearizerOptions(supports_float4=False if getenv("PTX") else True, supports_float4_alu=False, global_max = [65535, 65535, 2147483647], local_max = [64, 1024, 1024]),
                      CUDARenderer, compile_cuda, CUDAProgram, cuda.cuCtxSynchronize, graph=CUDAGraph)
