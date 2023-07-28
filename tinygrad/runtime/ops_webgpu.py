import numpy as np
from wgpu.utils._device import get_default_device  # type: ignore
from tinygrad.runtime.lib import RawBufferCopyIn, LRUAllocator
from tinygrad.helpers import dtypes, DType, DeviceInfo
from tinygrad.ops import Compiled
from tinygrad.codegen.cstyle import CStyleCodegen
from tinygrad.codegen.wgsl import WGSLLanguage
import wgpu  # type: ignore

class _WGPU:
  def __init__(self):
    self.device = get_default_device()
    self.dev_info = DeviceInfo(memory_size=self.device.limits['max_buffer_size'])
WGPU = _WGPU()

class WebGPUProgram:
  def __init__(self, name: str, prg: str): self.name,self.prg = name,WGPU.device.create_shader_module(code=prg)
  def __call__(self, global_size, local_size, *bufs, wait=False):
    binding_layouts = [{"binding": i, "visibility": wgpu.ShaderStage.COMPUTE, "buffer": {"type": wgpu.BufferBindingType.storage}} for i in range(len(bufs))]
    bindings = [{"binding": i, "resource": {"buffer": x._buf, "offset": 0, "size": x._buf.size}} for i, x in enumerate(bufs)]
    bind_group_layout = WGPU.device.create_bind_group_layout(entries=binding_layouts)
    pipeline_layout = WGPU.device.create_pipeline_layout(bind_group_layouts=[bind_group_layout])
    bind_group = WGPU.device.create_bind_group(layout=bind_group_layout, entries=bindings)
    compute_pipeline = WGPU.device.create_compute_pipeline(layout=pipeline_layout,compute={"module": self.prg, "entry_point": self.name},)
    command_encoder = WGPU.device.create_command_encoder()
    compute_pass = command_encoder.begin_compute_pass()
    compute_pass.set_pipeline(compute_pipeline)
    compute_pass.set_bind_group(0, bind_group, [], 0, 999999) # last 2 not used
    compute_pass.dispatch_workgroups(*global_size)  # x y z
    compute_pass.end()
    WGPU.device.queue.submit([command_encoder.finish()])

class WGSLCodegen(CStyleCodegen):
  lang = WGSLLanguage()
  supports_float4: bool = False

class RawWebGPUBuffer(RawBufferCopyIn):
  def __init__(self, size:int, dtype:DType):
    assert dtype not in [dtypes.int8,dtypes.uint8,dtypes.int64,dtypes.uint64], f"dtype {dtype} not supported on WEBGPU"
    super().__init__(size, dtype, WGPU.device.create_buffer(size=size*dtype.itemsize, usage=wgpu.BufferUsage.STORAGE | wgpu.BufferUsage.COPY_DST | wgpu.BufferUsage.COPY_SRC))
  def _copyin(self, x:np.ndarray): WGPU.device.queue.write_buffer(self._buf, 0, np.ascontiguousarray(x))
  def toCPU(self) -> np.ndarray: return np.frombuffer(WGPU.device.queue.read_buffer(self._buf, 0), dtype=np.dtype(self.dtype.np, metadata={"backing": self})) # type: ignore

WebGpuAlloc = LRUAllocator(RawWebGPUBuffer)
WebGpuBuffer = Compiled(WebGpuAlloc, WGSLCodegen, WebGPUProgram)
