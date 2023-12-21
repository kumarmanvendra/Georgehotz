from tinygrad.device import Compiled, Allocator
from tinygrad.codegen.kernel import LinearizerOptions
from tinygrad.renderer.cstyle import WGSLRenderer
import wgpu

adapter = wgpu.gpu.request_adapter(power_preference="high-performance")
timestamp_supported = wgpu.FeatureName.timestamp_query in adapter.features
device = adapter.request_device(required_features=[wgpu.FeatureName.timestamp_query] if timestamp_supported else [])

def create_uniform(val: int) -> wgpu.GPUBuffer:
  buf = device.create_buffer(size=4, usage=wgpu.BufferUsage.UNIFORM | wgpu.BufferUsage.COPY_DST)
  device.queue.write_buffer(buf, 0, val.to_bytes(4, "little"))
  return buf

class WebGPUProgram:
  def __init__(self, name:str, lib:bytes):
    self.name, self.lib, self.prg = name, lib, device.create_shader_module(code=lib)   # NOTE: this is the compiler
  def __call__(self, *bufs, global_size, local_size, vals=(), wait=False):
    assert len(bufs) <= 8, "WEBGPU only supports 8 buffers"
    binding_layouts = [{"binding": i, "visibility": wgpu.ShaderStage.COMPUTE, "buffer": {"type": wgpu.BufferBindingType.uniform if i >= len(bufs) else wgpu.BufferBindingType.storage }} for i in range(len(bufs)+len(vals))]  # noqa: E501
    bindings = [{"binding": i, "resource": {"buffer": create_uniform(x) if i >= len(bufs) else x, "offset": 0, "size": 4 if i >= len(bufs) else x.size}} for i,x in enumerate(bufs+tuple(vals))]  # noqa: E501
    bind_group_layout = device.create_bind_group_layout(entries=binding_layouts)
    pipeline_layout = device.create_pipeline_layout(bind_group_layouts=[bind_group_layout])
    bind_group = device.create_bind_group(layout=bind_group_layout, entries=bindings)
    compute_pipeline = device.create_compute_pipeline(layout=pipeline_layout,compute={"module": self.prg, "entry_point": self.name},)
    command_encoder = device.create_command_encoder()
    if wait and timestamp_supported:
      query_set = device.create_query_set(type=wgpu.QueryType.timestamp, count=2)
      query_buf = device.create_buffer(size=16, usage=wgpu.BufferUsage.QUERY_RESOLVE | wgpu.BufferUsage.COPY_SRC)
      timestamp_writes = {"query_set": query_set, "beginning_of_pass_write_index": 0, "end_of_pass_write_index": 1}
    compute_pass = command_encoder.begin_compute_pass(timestamp_writes=timestamp_writes if wait and timestamp_supported else None)
    compute_pass.set_pipeline(compute_pipeline)
    compute_pass.set_bind_group(0, bind_group, [], 0, 999999) # last 2 not used
    compute_pass.dispatch_workgroups(*global_size)  # x y z
    compute_pass.end()
    if wait and timestamp_supported:
      command_encoder.resolve_query_set(query_set=query_set, first_query=0, query_count=2, destination=query_buf, destination_offset=0)
    device.queue.submit([command_encoder.finish()])
    return (timestamps := device.queue.read_buffer(query_buf).cast("Q").tolist())[1] - timestamps[0] if wait and timestamp_supported else None

class WebGpuAllocator(Allocator):
  def _alloc(self, size: int):
    return device.create_buffer(size=size, usage=wgpu.BufferUsage.STORAGE | wgpu.BufferUsage.COPY_DST | wgpu.BufferUsage.COPY_SRC)
  def copyin(self, dest, src: memoryview): device.queue.write_buffer(dest, 0, src)
  def copyout(self, dest, src: memoryview): dest[:] = device.queue.read_buffer(src, 0)    # TODO: remove this copy

class WebGpuDevice(Compiled):
  def __init__(self, device:str):
    super().__init__(WebGpuAllocator(), LinearizerOptions(device="WEBGPU", supports_float4=False, local_max=[256, 256, 64],
                                                          global_max=[65535, 65535, 65535]), WGSLRenderer, lambda x: x, WebGPUProgram)
