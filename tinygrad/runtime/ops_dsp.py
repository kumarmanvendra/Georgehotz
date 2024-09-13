from __future__ import annotations
from typing import Tuple, Any
import ctypes, os, mmap, tempfile, pathlib, array, functools, threading
from tinygrad.device import BufferOptions, Compiled, Allocator
from tinygrad.helpers import from_mv, getenv, DEBUG, round_up, mv_address, to_mv
from tinygrad.runtime.ops_clang import ClangCompiler
from tinygrad.renderer.cstyle import DSPRenderer
from tinygrad.runtime.autogen import libc, qcom_dsp
if getenv("IOCTL"): import extra.dsp.run # noqa: F401 # pylint: disable=unused-import

def rpc_sc(method=0, ins=0, outs=0, fd=0): return (method << 24) | (ins << 16) | (outs << 8)
def rpc_prep_args(ins=None, outs=None, in_fds=None):
  ins, outs, in_fds = ins or list(), outs or list(), in_fds or list()

  pra = (qcom_dsp.union_remote_arg * (len(ins) + len(outs) + len(in_fds)))()
  fds = (ctypes.c_int32 * (len(ins) + len(outs) + len(in_fds)))(*([-1] * (len(ins) + len(outs))), *in_fds)
  attrs = (ctypes.c_uint32 * (len(ins) + len(outs) + len(in_fds)))(*([0] * (len(ins) + len(outs))), *([1] * (len(in_fds))))

  for i, mv in enumerate(ins + outs): pra[i].buf.pv, pra[i].buf.len = mv_address(mv) if mv.nbytes > 0 else 0, mv.nbytes
  return pra, fds, attrs, (ins, outs)

class DSPProgram:
  def __init__(self, device:DSPDevice, name:str, lib:bytes):
    self.device, self.lib = device, lib

    # TODO: Remove lib flush to FS.
    with tempfile.NamedTemporaryFile(delete=False) as output_file:
      output_file.write(lib)
      output_file.flush()
      if DEBUG >= 6: os.system(f"llvm-readelf -S {output_file.name}")
      self.filepath = output_file.name

  def __del__(self): os.remove(self.filepath)

  def __call__(self, *bufs, vals:Tuple[int, ...]=(), wait=False):
    pra, fds, attrs, _ = rpc_prep_args(ins=[var_vals_mv:=memoryview(bytearray((len(bufs) + len(vals)) * 4))],
                                       outs=[timer:=memoryview(bytearray(8)).cast('Q')], in_fds=[b.share_info.fd for b in bufs])
    var_vals_mv.cast('i')[:] = array.array('i', tuple(b.size for b in bufs) + vals)
    self.device.exec_lib(self.filepath, (2<<24) | (1<<16) | (1<<8) | len(bufs), pra, fds, attrs)
    return timer[0] / 1e6

class DSPBuffer:
  def __init__(self, va_addr:int, size:int, share_info:Any): self.va_addr, self.size, self.share_info = va_addr, size, share_info

class DSPAllocator(Allocator):
  def __init__(self, device:DSPDevice):
    self.device = device
    super().__init__()

  def _alloc(self, size:int, options:BufferOptions):
    b = qcom_dsp.ION_IOC_ALLOC(self.device.ion_fd, len=size, align=0x200, heap_id_mask=1<<qcom_dsp.ION_SYSTEM_HEAP_ID, flags=qcom_dsp.ION_FLAG_CACHED)
    share_info = qcom_dsp.ION_IOC_SHARE(self.device.ion_fd, handle=b.handle)
    va_addr = libc.mmap(0, size, mmap.PROT_READ|mmap.PROT_WRITE, mmap.MAP_SHARED, share_info.fd, 0)
    return DSPBuffer(va_addr, size, share_info)

  def _free(self, opaque:DSPBuffer, options:BufferOptions):
    libc.munmap(opaque.va_addr, opaque.size)
    os.close(opaque.share_info.fd)
    qcom_dsp.ION_IOC_FREE(self.device.ion_fd, handle=opaque.share_info.handle)

  def as_buffer(self, src:DSPBuffer) -> memoryview: return to_mv(src.va_addr, src.size)
  def copyin(self, dest:DSPBuffer, src:memoryview): ctypes.memmove(dest.va_addr, from_mv(src), src.nbytes)
  def copyout(self, dest:memoryview, src:DSPBuffer): ctypes.memmove(from_mv(dest), src.va_addr, dest.nbytes)

class DSPDevice(Compiled):
  def __init__(self, device:str=""):
    self.ion_fd = os.open('/dev/ion', os.O_RDONLY)

    # Generate link script to pass into clang. Aligning all used sections to 4k fixes invoke problem.
    sections = ['hash', 'text', 'rela.plt', 'got', 'got.plt', 'dynamic', 'dynsym', 'dynstr', 'plt', 'data', 'bss']
    sections_link = '\n'.join([f'.{n} : ALIGN(4096) {{ *(.{n}) }}' for n in sections])
    with tempfile.NamedTemporaryFile(delete=False) as self.link_ld:
      self.link_ld.write(f"SECTIONS {{ . = 0x0; {sections_link}\n /DISCARD/ : {{ *(.note .note.* .gnu.hash .comment) }} }}".encode())
      self.link_ld.flush()

    fastrpc_shell = memoryview(bytearray(pathlib.Path('/dsp/cdsp/fastrpc_shell_3').read_bytes()))
    self.shell_mem = qcom_dsp.ION_IOC_ALLOC(self.ion_fd, len=round_up(fastrpc_shell.nbytes, 0x1000), align=0x1000, heap_id_mask=0x2000000, flags=0x1)
    self.shell_mapped = qcom_dsp.ION_IOC_MAP(self.ion_fd, handle=self.shell_mem.handle)
    self.fastrpc_shell_addr = libc.mmap(0, self.shell_mem.len, mmap.PROT_READ|mmap.PROT_WRITE, mmap.MAP_SHARED, self.shell_mapped.fd, 0)
    ctypes.memmove(self.fastrpc_shell_addr, mv_address(fastrpc_shell), fastrpc_shell.nbytes)
    self.init_dsp()

    RPCListner(self).start()

    compiler_args = ["--target=hexagon", "-mcpu=hexagonv65", "-fuse-ld=lld", "-nostdlib", "-mhvx=v65", "-mhvx-length=128b", f"-T{self.link_ld.name}"]
    super().__init__(device, DSPAllocator(self), DSPRenderer(), ClangCompiler("compile_dsp", args=compiler_args), functools.partial(DSPProgram, self))

  def open_lib(self, filepath):
    fp = f"file:///{filepath}?entry&_modver=1.0&_dom=cdsp\0"
    pra, _, _, _ = rpc_prep_args(ins=[memoryview(array.array('I', [len(fp), 0xff])), memoryview(bytearray(f"{fp}".encode()))],
                                 outs=[o1:=memoryview(bytearray(0x8)), o2:=memoryview(bytearray(0xff))])
    qcom_dsp.FASTRPC_IOCTL_INVOKE(self.rpc_fd, handle=0, sc=rpc_sc(method=0, ins=2, outs=2), pra=pra)
    if o1.cast('I')[1] >= 0xf0000000: raise RuntimeError(f"Cannot open lib: {o2.tobytes().decode()}")
    return o1.cast('I')[0]

  def close_lib(self, handle):
    pra, _, _, _ = rpc_prep_args(ins=[memoryview(array.array('I', [handle, 0xff]))], outs=[memoryview(bytearray(0x8)), memoryview(bytearray(0xff))])
    qcom_dsp.FASTRPC_IOCTL_INVOKE(self.rpc_fd, handle=0, sc=rpc_sc(method=1, ins=1, outs=2), pra=pra)

  def exec_lib(self, filepath, sc, args, fds, attrs):
    def _exec_lib():
      handle = self.open_lib(filepath)
      qcom_dsp.FASTRPC_IOCTL_INVOKE_ATTRS(self.rpc_fd, fds=fds, attrs=attrs, inv=qcom_dsp.struct_fastrpc_ioctl_invoke(handle=handle, sc=sc, pra=args))
      self.close_lib(handle)
    try: _exec_lib()
    except OSError:
      # DSP might ask for a connection reset or just fail with operation not permitted, try to reset connection.
      self.init_dsp()
      _exec_lib()

  def init_dsp(self):
    if hasattr(self, 'rpc_fd'): os.close(self.rpc_fd) # pylint: disable=access-member-before-definition

    self.rpc_fd = os.open('/dev/adsprpc-smd', os.O_RDONLY | os.O_NONBLOCK)
    qcom_dsp.FASTRPC_IOCTL_GETINFO(self.rpc_fd, 3)
    qcom_dsp.FASTRPC_IOCTL_CONTROL(self.rpc_fd, req=0x3)
    qcom_dsp.FASTRPC_IOCTL_INIT(self.rpc_fd, flags=0x1, file=self.fastrpc_shell_addr, filelen=self.shell_mem.len, filefd=self.shell_mapped.fd)
    qcom_dsp.FASTRPC_IOCTL_INVOKE(self.rpc_fd, handle=3, sc=rpc_sc(method=3, ins=0, outs=0))

class RPCListner(threading.Thread):
  def __init__(self, device:DSPDevice):
    super().__init__()
    self.device, self.daemon = device, True

  def run(self):
    # Setup initial request arguments.
    context, status = 0, 0xffffffff
    req_args, _, _, _ = rpc_prep_args(ins=[msg_send:=memoryview(bytearray(0x10)).cast('I'), out_buf:=memoryview(bytearray(0x10000)).cast('I')],
                                      outs=[msg_recv:=memoryview(bytearray(0x10)).cast('I'), in_buf:=memoryview(bytearray(0x10000)).cast('I')])
    req_args[1].buf.len = 0

    while True:
      # Update message request and send it.
      msg_send[:] = array.array('I', [context, status, req_args[1].buf.len, in_buf.nbytes])

      try: qcom_dsp.FASTRPC_IOCTL_INVOKE(self.device.rpc_fd, handle=0x3, sc=0x04020200, pra=req_args)
      except OSError: continue # retry

      context, inbufs, outbufs = msg_recv[0], ((sc:=msg_recv[2]) >> 16) & 0xff, (msg_recv[2] >> 8) & 0xff

      in_ptr, out_ptr, objs = mv_address(in_buf), mv_address(out_buf), []
      for i in range(inbufs + outbufs):
        obj_ptr = round_up(in_ptr + 4, 8) if i < inbufs else round_up(out_ptr + 4, 8)
        objs.append(to_mv(obj_ptr, obj_size:=to_mv(in_ptr, 4).cast('I')[0]))
        if i < inbufs: in_ptr = obj_ptr + obj_size
        else:
          to_mv(out_ptr, 4).cast('I')[0] = obj_size
          out_ptr = obj_ptr + obj_size
          in_ptr += 4

      in_args, out_args = objs[:inbufs], objs[inbufs:]
      req_args[1].buf.len = out_ptr - mv_address(out_buf)

      status = 0 # reset status, will set if error
      if sc == 0x20200: pass # greating
      elif sc == 0x13050100: # open
        try: out_args[0].cast('I')[0] = os.open(in_args[3].tobytes()[:-1].decode(), os.O_RDONLY)
        except OSError: status = 1
      elif sc == 0x3010000: os.close(in_args[0].cast('I')[0])
      elif sc == 0x9010000: # seek
        res = os.lseek(in_args[0].cast('I')[0], in_args[0].cast('I')[1], in_args[0].cast('I')[2])
        status = 0 if res >= 0 else res
      elif sc == 0x4010200: # read
        buf = os.read(in_args[0].cast('I')[0], in_args[0].cast('I')[1])
        out_args[1][:len(buf)] = buf
        out_args[0].cast('I')[0:2] = array.array('I', [len(buf), int(len(buf) == 0)])
      elif sc == 0x1f020100: # stat
        stat = os.stat(in_args[1].tobytes()[:-1].decode())
        out_stat = qcom_dsp.struct_apps_std_STAT.from_address(mv_address(out_args[0]))
        for f in out_stat._fields_: out_stat.__setattr__(f[0], int(getattr(stat, f"st_{f[0]}", 0)))
      elif sc == 0x2010100: # mmap
        st = qcom_dsp.FASTRPC_IOCTL_MMAP(self.device.rpc_fd, fd=-1, flags=in_args[0].cast('I')[2], vaddrin=0, size=in_args[0].cast('Q')[3])
        out_args[0].cast('Q')[0:2] = array.array('Q', [0, st.vaddrout])
      else: raise RuntimeError(f"Unknown op: {sc=:X}")
