from __future__ import annotations
from typing import Tuple, List
import ctypes, fcntl, os, mmap, tempfile
from tinygrad.device import BufferOptions, LRUAllocator, Compiled, Allocator
from tinygrad.helpers import from_mv, getenv, DEBUG
from tinygrad.runtime.ops_clang import ClangCompiler
from tinygrad.renderer.cstyle import CStyleLanguage
from tinygrad.dtype import DType
from tinygrad.ops import UOp

from tinygrad.runtime.autogen import libc, ion, msm_ion, adsprpc, qcom_dsp
# ION_IOC_ALLOC = 0
# ION_IOC_FREE = 1
# ION_IOC_SHARE = 4
# ION_IOC_SYNC = 7
# ION_IOC_CLEAN_INV_CACHES = 2

if getenv("IOCTL"): import extra.dsp.run # noqa: F401 # pylint: disable=unused-import

adsp = ctypes.CDLL(ctypes.util.find_library("adsprpc"))

class DSPProgram:
  def __init__(self, name:str, lib:bytes):
    with tempfile.NamedTemporaryFile(delete=True) as output_file:
      output_file.write(lib)
      output_file.flush()
      if DEBUG >= 6: os.system(f"llvm-objdump -mhvx -d {output_file.name}")
      self.handle = ctypes.c_int64(-1)
      fp = f"file:///{output_file.name}?entry&_modver=1.0&_dom=cdsp"
      adsp.remote_handle64_open(ctypes.create_string_buffer(fp.encode()), ctypes.byref(self.handle))
      # print("OPEN", self.handle.value)
      assert self.handle.value != -1, "load failed"

  def __del__(self):
    if self.handle.value != -1:
      x = adsp.remote_handle64_close(self.handle)
      assert x == 0, "CLOSE failed"

  def __call__(self, *bufs, vals:Tuple[int, ...]=(), wait=False):
    assert len(vals) == 0
    assert len(bufs) < 16
    pra = (adsprpc.union_remote_arg64 * (2+len(bufs)))()
    test = (ctypes.c_int32 * len(bufs))()
    time_est = ctypes.c_uint64(0)
    pra[0].buf.pv = ctypes.addressof(test)
    pra[0].buf.len = 4 * len(bufs)
    pra[1].buf.pv = ctypes.addressof(time_est)
    pra[1].buf.len = 8
    for i,b in enumerate(bufs):
      test[i] = b.size
      pra[i+2].dma.fd = b.share_info.fd
      pra[i+2].dma.len = b.size
    ret = adsp.remote_handle64_invoke(self.handle, (1<<24) | (1<<16) | (1<<8) | len(bufs), pra)
    # print("invoke", ret)
    assert ret == 0, f"!!! invoke returned {ret}"
    # if ret != 0:
    #   print("errr,wow", ret)
    #   time.sleep(10)
    #return time_est.value / 19_200_000
    return time_est.value / 1e6

# def ion_iowr(fd, nr, args, magc=ion.ION_IOC_MAGIC):
#   ret = fcntl.ioctl(fd, (3 << 30) | (ctypes.sizeof(args) & 0x1FFF) << 16 | (ord(magc) & 0xFF) << 8 | (nr & 0xFF), args)
#   if ret != 0: raise RuntimeError(f"ioctl returned {ret}")

class DSPBuffer:
  def __init__(self, va_addr:int, size:int, share_info:Any): self.va_addr, self.size, self.share_info = va_addr, size, share_info

class DSPAllocator(Allocator):
  def __init__(self, device):
    self.device = device
    # self.ion_fd = os.open("/dev/ion", os.O_RDWR | os.O_CLOEXEC)
    super().__init__()

  def _alloc(self, size:int, options:BufferOptions):
    # arg3 = ion.struct_ion_allocation_data(len=size, align=0x200, heap_id_mask=1<<msm_ion.ION_SYSTEM_HEAP_ID, flags=ion.ION_FLAG_CACHED)
    # ion_iowr(self.ion_fd, ION_IOC_ALLOC, arg3)
    # ion_iowr(self.ion_fd, ION_IOC_SHARE, arg2:=ion.struct_ion_fd_data(handle=arg3.handle))
    # res = libc.mmap(0, size, mmap.PROT_READ|mmap.PROT_WRITE, mmap.MAP_SHARED, arg2.fd, 0)
    # ion_iowr(self.ion_fd, ION_IOC_CLEAN_INV_CACHES, msm_ion.struct_ion_flush_data(handle=arg3.handle, fd=arg2.fd, vaddr=res, len=size), magc='M')

    alloc = qcom_dsp.ION_IOC_ALLOC(self.device.ion_fd, len=size, align=0x200, heap_id_mask=1<<msm_ion.ION_SYSTEM_HEAP_ID, flags=ion.ION_FLAG_CACHED)
    share_info = qcom_dsp.ION_IOC_SHARE(self.device.ion_fd, handle=alloc.handle)
    va_addr = libc.mmap(0, size, mmap.PROT_READ|mmap.PROT_WRITE, mmap.MAP_SHARED, share_info.fd, 0)
    return DSPBuffer(va_addr, size, share_info)

  def _free(self, opaque, options:BufferOptions):
    libc.munmap(opaque.va_addr, opaque.size)
    os.close(opaque.share_info.fd)
    qcom_dsp.ION_IOC_FREE(self.device.ion_fd, handle=opaque.share_info.handle)

  def copyin(self, dest, src:memoryview): ctypes.memmove(dest.va_addr, from_mv(src), src.nbytes)
  def copyout(self, dest:memoryview, src): ctypes.memmove(from_mv(dest), src.va_addr, dest.nbytes)

class DSPRenderer(CStyleLanguage):
  device = "DSP"
  supports_float4 = False
  has_local = False
  buffer_suffix = " restrict __attribute__((align_value(128)))"
  kernel_prefix = "__attribute__((noinline)) "

  def render_kernel(self, function_name:str, kernel:List[str], bufs:List[Tuple[str,Tuple[DType,bool]]], uops:List[UOp], prefix=None) -> str:
    ret = super().render_kernel(function_name, kernel, bufs, uops, prefix)
    prefix = ['#define max(a,b) ({ __typeof__ (a) _a = (a); __typeof__ (b) _b = (b); _a > _b ? _a : _b; })', 'typedef int bool;']
    msrc = ['typedef struct { int fd; unsigned int offset; } remote_dma_handle;',
            'typedef struct { void *pv; unsigned int len; } remote_buf;',
            '#include "HAP_power.h"',
            'unsigned long long HAP_perf_get_time_us(void);',
            'typedef union { remote_buf buf; remote_dma_handle dma; } remote_arg;',
            'void* HAP_mmap(void *addr, int len, int prot, int flags, int fd, long offset);',
            'int HAP_munmap(void *addr, int len);',
            'int entry(unsigned long long handle, unsigned int sc, remote_arg* pra) {']
    msrc += ["""
    // Set client class
    {
      HAP_power_request_t request = {0};
      request.type = HAP_power_set_apptype;
      request.apptype = HAP_POWER_COMPUTE_CLIENT_CLASS;
      int retval = HAP_power_set((void*)handle, &request);
      if (retval) return 42;
    }
    // Set to turbo and disable DCVS
    {
      HAP_power_request_t request = {0};
      request.type = HAP_power_set_DCVS_v2;
      request.dcvs_v2.dcvs_enable = FALSE;
      request.dcvs_v2.set_dcvs_params = TRUE;
      request.dcvs_v2.dcvs_params.min_corner = HAP_DCVS_VCORNER_DISABLE;
      request.dcvs_v2.dcvs_params.max_corner = HAP_DCVS_VCORNER_DISABLE;
      request.dcvs_v2.dcvs_params.target_corner = HAP_DCVS_VCORNER_TURBO;
      request.dcvs_v2.set_latency = TRUE;
      request.dcvs_v2.latency = 100;
      int retval = HAP_power_set((void*)handle, &request);
      if (retval) return 42;
    }
    // Vote for HVX power
    {
      HAP_power_request_t request = {0};
      request.type = HAP_power_set_HVX;
      request.hvx.power_up = TRUE;
      int retval = HAP_power_set((void*)handle, &request);
      if (retval) return 42;
    }
    """]
    msrc.append('if (sc>>24 == 1) {')
    msrc += [f'  void *buf_{i} = HAP_mmap(0, ((int*)pra[0].buf.pv)[{i}], 3, 0, pra[{i+2}].dma.fd, 0);' for i in range(len(bufs))]
    #msrc.append("  unsigned long long start = HAP_perf_get_time_us();")
    msrc.append("  unsigned long long start, end;")
    msrc.append("  start = HAP_perf_get_time_us();")
    #msrc.append('  asm volatile ("%0 = C15:14" : "=r"(start));')
    msrc.append(f"  {function_name}({', '.join([f'buf_{i}' for i in range(len(bufs))])});")
    msrc.append("  end = HAP_perf_get_time_us();")
    #msrc.append('  asm volatile ("%0 = C15:14" : "=r"(end));')
    msrc.append("  *(unsigned long long *)(pra[1].buf.pv) = end - start;")
    msrc += [f'  HAP_munmap(buf_{i}, ((int*)pra[0].buf.pv)[{i}]);' for i in range(len(bufs))]
    # msrc.append("return ((int*)buf_0)[0];")
    msrc.append("}")
    msrc.append("return 0;")
    msrc.append("}")
    return '\n'.join(prefix) + '\n' + ret + '\n' + '\n'.join(msrc)

class DSPDevice(Compiled):
  def __init__(self, device:str=""):
    self.ion_fd = os.open('/dev/ion', os.O_RDONLY)
    self.rpc_fd = os.open('/dev/adsprpc-smd', os.O_RDONLY | os.O_NONBLOCK)

    compiler_args = ["--target=hexagon", "-mcpu=hexagonv65", "-fuse-ld=lld", "-nostdlib", "-mhvx=v65", "-mhvx-length=128b",
                     "-I/data/home/nimlgen/tinygrad/extra/dsp/include"]
    super().__init__(device, DSPAllocator(self), DSPRenderer(), ClangCompiler("compile_dsp", args=compiler_args), DSPProgram)
