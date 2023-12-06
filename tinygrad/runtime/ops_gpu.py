from __future__ import annotations
from typing import Tuple, Optional, List
import ctypes, functools
import gpuctypes.opencl as cl
from tinygrad.helpers import init_c_var, to_char_p_p, from_mv, OSX, ImageDType, DEBUG
from tinygrad.codegen.kernel import LinearizerOptions
from tinygrad.renderer.cstyle import OpenCLRenderer
from tinygrad.device import Compiled, LRUAllocator, CachedCompiler

OSX_TIMING_RATIO = (125/3) if OSX else 1.0   # see test/external/external_osx_profiling.py to determine this ratio. it's in like GPU clocks or something

def check(status):
  if status != 0: raise RuntimeError(f"OpenCL Error {status}")
def checked(ret, status): return (check(status.value), ret)[1]

class CLProgram:
  def __init__(self, device:CLDevice, name:str, lib:bytes):
    self.device, self.name, self.lib = device, name, lib
    self.program = checked(cl.clCreateProgramWithBinary(device.context, 1, ctypes.byref(device.device_id), (ctypes.c_size_t * 1)(len(lib)), to_char_p_p([lib], ctypes.c_ubyte),
                                                        ctypes.byref(binary_status := ctypes.c_int32()), ctypes.byref(errcode_ret := ctypes.c_int32())), errcode_ret)
    check(binary_status.value)
    check(cl.clBuildProgram(self.program, 1, ctypes.byref(device.device_id), None, cl.clBuildProgram.argtypes[4](), None)) # NOTE: OSX requires this
    self.kernel = checked(cl.clCreateKernel(self.program, name.encode(), ctypes.byref(status := ctypes.c_int32())), status)

  def __del__(self):
    check(cl.clReleaseKernel(self.kernel))
    check(cl.clReleaseProgram(self.program))

  def __call__(self, *bufs:cl.cl_mem, global_size:Tuple[int,...], local_size:Optional[Tuple[int,...]]=None, vals:Tuple[int, ...]=(), wait=False) -> Optional[float]:
    for i,b in enumerate(bufs): cl.clSetKernelArg(self.kernel, i, ctypes.sizeof(b), ctypes.byref(b))
    for i,b in enumerate(vals,start=len(bufs)): cl.clSetKernelArg(self.kernel, i, 4, ctypes.byref(ctypes.c_int32(b)))
    if local_size is not None: global_size = tuple(int(g*l) for g,l in zip(global_size, local_size))
    event = cl.cl_event() if wait else None
    check(cl.clEnqueueNDRangeKernel(self.device.queue, self.kernel, len(global_size), None, (ctypes.c_size_t * len(global_size))(*global_size), (ctypes.c_size_t * len(local_size))(*local_size) if local_size else None, 0, None, event))
    if wait:
      check(cl.clWaitForEvents(1, ctypes.byref(event)))
      start = init_c_var(ctypes.c_ulong(), lambda x: check(cl.clGetEventProfilingInfo(event, cl.CL_PROFILING_COMMAND_START, ctypes.sizeof(x), ctypes.byref(x), None)))
      end = init_c_var(ctypes.c_ulong(), lambda x: check(cl.clGetEventProfilingInfo(event, cl.CL_PROFILING_COMMAND_END, ctypes.sizeof(x), ctypes.byref(x), None)))
      return float(end.value-start.value) * OSX_TIMING_RATIO * 1e-9
    return None

class CLAllocator(LRUAllocator):
  def __init__(self, device:CLDevice):
    self.device = device
    super().__init__()
  def _alloc(self, size:int) -> cl.cl_mem:
    return checked(cl.clCreateBuffer(self.device.context, cl.CL_MEM_READ_WRITE, size, None, ctypes.byref(status := ctypes.c_int32())), status)
  def _free(self, buf:cl.cl_mem): check(cl.clReleaseMemObject(buf))
  def _cast_image(self, buf:cl.cl_mem, dtype:ImageDType, row_pitch:int) -> cl.cl_mem:
    desc = cl.cl_image_desc(image_type=cl.CL_MEM_OBJECT_IMAGE2D, image_width=dtype.shape[1], image_height=dtype.shape[0], image_row_pitch=row_pitch)
    desc._0.mem_object = buf
    return checked(cl.clCreateImage(self.device.context, cl.CL_MEM_READ_WRITE,
                                    cl.cl_image_format(cl.CL_RGBA, {2: cl.CL_HALF_FLOAT, 4: cl.CL_FLOAT}[dtype.itemsize]),
                                    desc, None, ctypes.byref(status := ctypes.c_int32())), status)
  def copyin(self, dest:cl.cl_mem, src:memoryview):
    check(cl.clEnqueueWriteBuffer(self.device.queue, dest, False, 0, len(src)*src.itemsize, from_mv(src), 0, None, None))
    self.device.pending_copyin.append(src)    # NOTE: these can't be freed until the GPU actually executes this command
  def copyout(self, dest:memoryview, src:cl.cl_mem):
    check(cl.clEnqueueReadBuffer(self.device.queue, src, False, 0, len(dest)*dest.itemsize, from_mv(dest), 0, None, None))
    self.device.synchronize()

class CLCompiler(CachedCompiler):
  def __init__(self, device, **kwargs):
    super().__init__(**kwargs)
    self.device = device
  def _compile(self, prg:str, **kwargs): # ignore kwargs, take all data from device
    program = checked(cl.clCreateProgramWithSource(self.device.context, 1, to_char_p_p([prg_bytes := prg.encode()]), ctypes.byref(ctypes.c_size_t(len(prg_bytes))), ctypes.byref(status := ctypes.c_int32())), status)
    status = cl.clBuildProgram(program, 1, ctypes.byref(self.device.device_id), None, cl.clBuildProgram.argtypes[4](), None)
    if status != 0:
      cl.clGetProgramBuildInfo(program, self.device.device_id, cl.CL_PROGRAM_BUILD_LOG, 0, None, ctypes.byref(log_size := ctypes.c_size_t()))
      cl.clGetProgramBuildInfo(program, self.device.device_id, cl.CL_PROGRAM_BUILD_LOG, log_size.value, mstr := ctypes.create_string_buffer(log_size.value), None)
      raise RuntimeError(f"OpenCL Compile Error\n\n{ctypes.string_at(mstr, size=log_size.value).decode()}")
    binary_sizes = init_c_var((ctypes.c_size_t * 1)(), lambda x: check(cl.clGetProgramInfo(program, cl.CL_PROGRAM_BINARY_SIZES, ctypes.sizeof(x), ctypes.byref(x), None)))
    binary = init_c_var(ctypes.create_string_buffer(binary_sizes[0]), lambda x: check(cl.clGetProgramInfo(program, cl.CL_PROGRAM_BINARIES, ctypes.sizeof(ctypes.c_void_p), ctypes.byref((ctypes.c_void_p * 1)(ctypes.addressof(x))), None)))
    check(cl.clReleaseProgram(program))
    return bytes(binary)

class CLDevice(Compiled):
  device_ids = None                 # this is global and only initted once
  def __init__(self, device:str=""):
    if CLDevice.device_ids is None:
      num_platforms = init_c_var(ctypes.c_uint32(), lambda x: check(cl.clGetPlatformIDs(0, None, ctypes.byref(x))))
      platform_ids = init_c_var((cl.cl_platform_id * num_platforms.value)(), lambda x: check(cl.clGetPlatformIDs(num_platforms.value, x, None)))
      for device_type in [cl.CL_DEVICE_TYPE_GPU, cl.CL_DEVICE_TYPE_DEFAULT]:
        num_devices = ctypes.c_uint32()
        err = cl.clGetDeviceIDs(platform_ids[0], device_type, 0, None, ctypes.byref(num_devices))
        if err == 0 and num_devices.value != 0: break
      if DEBUG >= 1: print(f"CLDevice: got {num_platforms.value} platforms and {num_devices.value} devices")
      CLDevice.device_ids = init_c_var((cl.cl_device_id * num_devices.value)(), lambda x: check(cl.clGetDeviceIDs(platform_ids[0], device_type, num_devices, x, None)))

    self.device_id = CLDevice.device_ids[0 if ":" not in device else int(device.split(":")[1])]
    self.device_name = (cl.clGetDeviceInfo(self.device_id, cl.CL_DEVICE_NAME, 256, ctypes.byref(buf := ctypes.create_string_buffer(256)), ctypes.byref(total := ctypes.c_size_t())), ctypes.string_at(buf, size=total.value).decode())[1]
    self.driver_version = (cl.clGetDeviceInfo(self.device_id, cl.CL_DRIVER_VERSION, 64, ctypes.byref(buf := ctypes.create_string_buffer(64)), ctypes.byref(total := ctypes.c_size_t())), ctypes.string_at(buf, size=total.value).decode())[1]
    self.context = checked(cl.clCreateContext(None, 1, ctypes.byref(self.device_id), cl.clCreateContext.argtypes[3](), None, ctypes.byref(status := ctypes.c_int32())), status)
    self.queue = checked(cl.clCreateCommandQueue(self.context, self.device_id, cl.CL_QUEUE_PROFILING_ENABLE, ctypes.byref(status)), status)
    self.pending_copyin: List[memoryview] = []
    super().__init__(CLAllocator(self), LinearizerOptions(), OpenCLRenderer, CLCompiler(self, device_name=self.device_name, driver_version=self.driver_version), functools.partial(CLProgram, self))
  def synchronize(self):
    check(cl.clFinish(self.queue))
    self.pending_copyin.clear()

GPUDevice = CLDevice # for legacy reasons
