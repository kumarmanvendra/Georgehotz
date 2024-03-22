import ctypes, time, collections
from typing import Any, Optional, Tuple, Dict, List, cast
import tinygrad.runtime.autogen.cuda as cuda
from tinygrad.helpers import init_c_var, all_same, GraphException, dedup
from tinygrad.device import CompiledASTRunner, update_stats, Buffer, MultiDeviceJITGraph, BufferXfer
from tinygrad.runtime.ops_cuda import CUDADevice, check, encode_args, cu_time_execution
from tinygrad.shape.symbolic import Variable
from tinygrad.features.jit import JitItem, get_input_replace, get_jit_stats, \
                                  get_jc_idxs_with_updatable_launch_dims, get_jc_idxs_with_updatable_var_vals

class CUDAGraph(MultiDeviceJITGraph):
  def __init__(self, jit_cache: List[JitItem], input_rawbuffers: List[Buffer], var_vals: Dict[Variable, int]):
    self.jit_cache = jit_cache
    self.input_replace = get_input_replace(jit_cache, input_rawbuffers)
    self.op_estimate, self.mem_estimate = get_jit_stats(jit_cache)
    self.jc_idxs_with_updatable_launch_dims = get_jc_idxs_with_updatable_launch_dims(jit_cache)
    self.jc_idxs_with_updatable_var_vals = get_jc_idxs_with_updatable_var_vals(jit_cache)
    self.jc_idxs_with_updatable_rawbufs = list(set([x[0] for x in self.input_replace.keys()]))
    self.updatable_nodes: Dict[int, Tuple[Compiled, Any, Any, Any]] = {} # Dict[jc index] = tuple(dev, graph node, node params, input kernel params)

    # Check all jit items are compatible.
    compiled_devices = set()
    for ji in self.jit_cache:
      if isinstance(ji.prg, CompiledASTRunner): compiled_devices.add(ji.prg.device)
      elif isinstance(ji.prg, BufferXfer):
        for x in ji.rawbufs[0:2]: compiled_devices.add(cast(Buffer, x).d)
      else: raise GraphException
    if any(not isinstance(d, CUDADevice) for d in compiled_devices): raise GraphException

    self.devices: List[CUDADevice] = list(compiled_devices) #type:ignore
    self.graph = init_c_var(cuda.CUgraph(), lambda x: check(cuda.cuGraphCreate(ctypes.byref(x), 0)))
    self.w_dependency_map: Dict[Any, cuda.CUgraphNode] = {}
    self.r_dependency_map: Dict[Any, List[cuda.CUgraphNode]] = collections.defaultdict(list)

    for j,ji in enumerate(self.jit_cache):
      if isinstance(ji.prg, CompiledASTRunner):
        global_size, local_size = ji.prg.launch_dims(var_vals)

        new_node = cuda.CUgraphNode()
        deps = self.access_resources(ji.rawbufs[(outs:=ji.prg.outcount):], ji.rawbufs[:outs], new_dependency=new_node)
        c_deps = (cuda.CUgraphNode*len(deps))(*deps) if deps else None

        c_args, vargs = encode_args([cast(Buffer, x)._buf for x in ji.rawbufs], [var_vals[x] for x in ji.prg.vars])
        kern_params = cuda.CUDA_KERNEL_NODE_PARAMS(ji.prg.clprg.prg, *global_size, *local_size, 0, None, vargs)
        check(cuda.cuGraphAddKernelNode(ctypes.byref(new_node), self.graph, c_deps, len(deps), ctypes.byref(kern_params)))

        if j in self.jc_idxs_with_updatable_launch_dims or j in self.jc_idxs_with_updatable_var_vals or j in self.jc_idxs_with_updatable_rawbufs:
          self.updatable_nodes[j] = (new_node, kern_params, c_args)
      elif isinstance(ji.prg, BufferXfer):
        dest, src = [cast(Buffer, x) for x in ji.rawbufs[0:2]]
        dest_dev, src_dev = cast(CUDADevice, dest.d), cast(CUDADevice, src.d)

        new_node = cuda.CUgraphNode()
        deps = self.access_resources(read=[src], write=[dest], new_dependency=new_node)
        c_deps = (cuda.CUgraphNode*len(deps))(*deps) if deps else None

        cp_params = cuda.CUDA_MEMCPY3D_v2(srcMemoryType=cuda.CU_MEMORYTYPE_DEVICE, srcDevice=src._buf, srcPitch=src.nbytes, srcHeight=1,
                                          dstMemoryType=cuda.CU_MEMORYTYPE_DEVICE, dstDevice=dest._buf, dstPitch=dest.nbytes, dstHeight=1,
                                          WidthInBytes=dest.nbytes, Height=1, Depth=1)
        check(cuda.cuGraphAddMemcpyNode(ctypes.byref(new_node), self.graph, c_deps, len(deps), ctypes.byref(cp_params), src_dev.context))

        if j in self.jc_idxs_with_updatable_rawbufs:
          self.updatable_nodes[j] = (new_node, cp_params, None)

    self.instance = init_c_var(cuda.CUgraphExec(), lambda x: check(cuda.cuGraphInstantiate_v2(ctypes.byref(x), self.graph, None, None, 0)))

  def __call__(self, input_rawbuffers: List[Buffer], var_vals: Dict[Variable, int], wait=False, jit=False) -> Optional[float]:
    # Update rawbuffers in the c_args struct.
    for (j,i),input_idx in self.input_replace.items():
      setattr(self.updatable_nodes[j][2], f'f{i}', input_rawbuffers[input_idx]._buf)

    # Update var_vals in the c_args struct.
    for j in self.jc_idxs_with_updatable_var_vals:
      for i,v in enumerate(cast(CompiledASTRunner, self.jit_cache[j].prg).vars):
        setattr(self.updatable_nodes[j][2], f'f{len(self.jit_cache[j].rawbufs) + i}', var_vals[v])

    # Update launch dims in the kern_params struct.
    for j in self.jc_idxs_with_updatable_launch_dims:
      self.set_kernel_node_launch_dims(self.updatable_nodes[j][1], *cast(CompiledASTRunner, self.jit_cache[j].prg).launch_dims(var_vals))

    # Update graph nodes with the updated structs.
    for node, c_node_params, _ in self.updatable_nodes.values():
      check(cuda.cuGraphExecKernelNodeSetParams(self.instance, node, ctypes.byref(c_node_params)))

    et = cu_time_execution(lambda: check(cuda.cuGraphLaunch(self.instance, None)), enable=wait)
    update_stats(f"<batched {len(self.jit_cache)}>", self.op_estimate, self.mem_estimate, var_vals, et, buf_count=len(input_rawbuffers),
                 jit=jit, num_kernels=len(self.jit_cache), device=f"CUDA")
    return et

  def __del__(self):
    if hasattr(self, 'graph'): check(cuda.cuGraphDestroy(self.graph))
    if hasattr(self, 'instance'): check(cuda.cuGraphExecDestroy(self.instance))

  def set_kernel_node_launch_dims(self, node, global_size: Tuple[int, int, int], local_size: Tuple[int, int, int]):
    node.blockDimX, node.blockDimY, node.blockDimZ, node.gridDimX, node.gridDimY, node.gridDimZ = *local_size, *global_size

  def access_resources(self, read, write, new_dependency):
    wait_nodes: List[cu.CUgraphNode] = []

    for rawbuf in read:
      if rawbuf._buf.value in self.w_dependency_map: wait_nodes.append(self.w_dependency_map[rawbuf._buf.value])
    for rawbuf in write:
      if rawbuf._buf.value in self.w_dependency_map: wait_nodes.append(self.w_dependency_map[rawbuf._buf.value])
      if rawbuf._buf.value in self.r_dependency_map: wait_nodes.extend(self.r_dependency_map.pop(rawbuf._buf.value))

    if new_dependency is not None:
      for rawbuf in read: self.r_dependency_map[rawbuf._buf.value].append(new_dependency)
      for rawbuf in write: self.w_dependency_map[rawbuf._buf.value] = new_dependency
    return {id(x):x for x in wait_nodes}.values()
