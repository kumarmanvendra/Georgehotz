from __future__ import annotations
from typing import Tuple, Optional, Union
import ctypes, functools
import gpuctypes.opencl as cl
from tinygrad.helpers import to_char_p_p, from_mv, diskcache, OSX
from tinygrad.codegen.kernel import LinearizerOptions
from tinygrad.renderer.opencl import OpenCLRenderer
from tinygrad.device import Compiled

OSX_TIMING_RATIO = (125/3) if OSX else 1.0   # see test/external/external_osx_profiling.py to determine this ratio. it's in like GPU clocks or something

def check(status):
  if status != 0: raise RuntimeError(f"OpenCL Error {status}")
def checked(ret, status):
  check(status.value)
  return ret

@diskcache    # TODO: this should be global
def compile_cl(prg:str) -> bytes:
  assert CLDevice.compiler_context is not None, 'OpenCL requires a "compiler_context" to compile, init a device before you call this'
  prg = prg.encode()
  program = checked(cl.clCreateProgramWithSource(CLDevice.compiler_context.context, 1, to_char_p_p([prg]), (ctypes.c_size_t * 1)(len(prg)), ctypes.byref(status := ctypes.c_int32())), status)
  status = cl.clBuildProgram(program, 1, ctypes.byref(CLDevice.compiler_context.device_id), None, ctypes.cast(None, cl.clBuildProgram.argtypes[4]), None)
  if status != 0:
    cl.clGetProgramBuildInfo(program, CLDevice.compiler_context.device_id, cl.CL_PROGRAM_BUILD_LOG, 0, None, ctypes.byref(log_size := ctypes.c_size_t()))
    cl.clGetProgramBuildInfo(program, CLDevice.compiler_context.device_id, cl.CL_PROGRAM_BUILD_LOG, log_size.value, mstr := ctypes.create_string_buffer(log_size.value), None)
    raise RuntimeError(f"OpenCL Compile Error\n\n{ctypes.string_at(mstr, size=log_size.value).decode()}")
  binary_sizes = (ctypes.c_size_t * 1)()
  check(cl.clGetProgramInfo(program, cl.CL_PROGRAM_BINARY_SIZES, ctypes.sizeof(binary_sizes), ctypes.byref(binary_sizes), None))
  binary = (ctypes.c_char * binary_sizes[0])()
  binary_pointers = (ctypes.c_char_p * 1)(ctypes.addressof(binary))
  check(cl.clGetProgramInfo(program, cl.CL_PROGRAM_BINARIES, ctypes.sizeof(binary_pointers), ctypes.byref(binary_pointers), None))
  check(cl.clReleaseProgram(program))
  return bytes(binary)

class CLProgram:
  def __init__(self, device:CLDevice, name:str, prg:bytes):
    self.device = device
    self.program = checked(cl.clCreateProgramWithBinary(device.context, 1, ctypes.byref(device.device_id), (ctypes.c_size_t * 1)(len(prg)),
                                                        to_char_p_p([prg], ctypes.c_ubyte),
                                                        ctypes.byref(binary_status := ctypes.c_int32()), ctypes.byref(errcode_ret := ctypes.c_int32())), errcode_ret)
    check(binary_status.value)
    check(cl.clBuildProgram(self.program, 1, ctypes.byref(device.device_id), None, ctypes.cast(None, cl.clBuildProgram.argtypes[4]), None)) # NOTE: OSX requires this
    self.kernel = checked(cl.clCreateKernel(self.program, name.encode(), ctypes.byref(status := ctypes.c_int32())), status)

  def __del__(self):
    check(cl.clReleaseKernel(self.kernel))
    check(cl.clReleaseProgram(self.program))

  def __call__(self, *bufs:Union[cl.cl_mem, int], global_size:Tuple[int,...], local_size:Optional[Tuple[int,...]]=None, wait=False) -> Optional[float]:
    for i,b in enumerate(bufs): cl.clSetKernelArg(self.kernel, i, ctypes.sizeof(b), ctypes.byref(b))
    if local_size is not None: global_size = tuple(int(g*l) for g,l in zip(global_size, local_size))
    event = cl.cl_event() if wait else None
    check(cl.clEnqueueNDRangeKernel(self.device.queue, self.kernel, len(global_size), None, (ctypes.c_size_t * len(global_size))(*global_size), (ctypes.c_size_t * len(local_size))(*local_size) if local_size else None, 0, None, event))
    if wait:
      start, end = ctypes.c_ulong(), ctypes.c_ulong()
      check(cl.clWaitForEvents(1, ctypes.byref(event)))
      check(cl.clGetEventProfilingInfo(event, cl.CL_PROFILING_COMMAND_START, ctypes.sizeof(start), ctypes.byref(start), None))
      check(cl.clGetEventProfilingInfo(event, cl.CL_PROFILING_COMMAND_END, ctypes.sizeof(end), ctypes.byref(end), None))
      return float(end.value-start.value) * OSX_TIMING_RATIO * 1e-9

class CLDevice(Compiled):
  linearizer_opts, renderer, compiler = LinearizerOptions(), staticmethod(OpenCLRenderer), staticmethod(compile_cl)    # these are the same for all instantiations of the device

  device_ids = None                 # this is global and only initted once
  compiler_context = None           # this is the first created context. we make an assumption they are all the same for the compiler
  def __init__(self, device:str=""):
    if CLDevice.device_ids is None:
      check(cl.clGetPlatformIDs(0, None, ctypes.byref(num_platforms := ctypes.c_uint32())))
      check(cl.clGetPlatformIDs(num_platforms.value, platform_ids := (cl.cl_platform_id * num_platforms.value)(), None))
      check(cl.clGetDeviceIDs(platform_ids[0], cl.CL_DEVICE_TYPE_DEFAULT, 0, None, ctypes.byref(num_devices := ctypes.c_uint32())))
      CLDevice.device_ids = (cl.cl_device_id * num_devices.value)()
      check(cl.clGetDeviceIDs(platform_ids[0], cl.CL_DEVICE_TYPE_DEFAULT, num_devices, CLDevice.device_ids, None))
    self.device_id = CLDevice.device_ids[0 if ":" not in device else int(device.split(":")[1])]
    self.context = checked(cl.clCreateContext(None, 1, ctypes.byref(self.device_id), ctypes.cast(None, cl.clCreateContext.argtypes[3]), None, ctypes.byref(status := ctypes.c_int32())), status)
    if CLDevice.compiler_context is None: CLDevice.compiler_context = self
    self.queue = checked(cl.clCreateCommandQueue(self.context, self.device_id, cl.CL_QUEUE_PROFILING_ENABLE, ctypes.byref(status)), status)
    self.pending_copyin = []
    super().__init__(None, LinearizerOptions(), OpenCLRenderer, compile_cl, CLProgram, self.synchronize)
  def synchronize(self): check(cl.clFinish(self.queue))

  # low level buffer api (not checked!)
  def alloc(self, size:int) -> cl.cl_mem: return checked(cl.clCreateBuffer(self.context, cl.CL_MEM_READ_WRITE, size, None, ctypes.byref(status := ctypes.c_int32())), status)
  def free(self, buf:cl.cl_mem): check(cl.clReleaseMemObject(buf))
  def copyin(self, dest:cl.cl_mem, src:memoryview):
    check(cl.clEnqueueWriteBuffer(self.queue, dest, False, 0, len(src)*src.itemsize, from_mv(src), 0, None, None))
    self.pending_copyin.append(src)    # NOTE: these can't be freed until the GPU actually executes this command
  def copyout(self, dest:memoryview, src:cl.cl_mem, block=True):
    check(cl.clEnqueueReadBuffer(self.queue, src, block, 0, len(dest)*dest.itemsize, from_mv(dest), 0, None, None))
    if block: self.pending_copyin = []
