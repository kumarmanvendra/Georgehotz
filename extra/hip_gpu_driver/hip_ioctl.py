import ctypes, ctypes.util, struct, platform, pathlib, re

# *** ioctl lib ***
libc = ctypes.CDLL(ctypes.util.find_library("c"))
processor = platform.processor()
IOCTL_SYSCALL = {"aarch64": 0x1d, "x86_64":16}[processor]

def get_struct(argp, stype):
  return ctypes.cast(ctypes.c_void_p(argp), ctypes.POINTER(stype)).contents

def format_struct(s):
  sdats = []
  for field_name, field_type in s._fields_:
    dat = getattr(s, field_name)
    if isinstance(dat, int): sdats.append(f"{field_name}:0x{dat:X}")
    else: sdats.append(f"{field_name}:{dat}")
  return sdats

def install_hook(c_function, python_function):
  python_function_addr = ctypes.cast(ctypes.byref(python_function), ctypes.POINTER(ctypes.c_ulong)).contents.value
  # AARCH64 trampoline to ioctl
  if processor == "aarch64":
    # 0x0000000000000000:  70 00 00 10    adr x16, #0xc
    # 0x0000000000000004:  10 02 40 F9    ldr x16, [x16]
    # 0x0000000000000008:  00 02 1F D6    br  x16
    tramp = b"\x70\x00\x00\x10\x10\x02\x40\xf9\x00\x02\x1f\xd6"
    tramp += struct.pack("Q", python_function_addr)
  elif processor == "x86_64":
    # 0x0000000000000000:  49 B8 aa aa aa aa aa aa aa aa    movabs r8, <address>
    # 0x000000000000000a:  41 FF E0                         jmp    r8
    tramp = b"\x49\xB8" + struct.pack("Q", python_function_addr) + b"\x41\xFF\xE0"
  else:
    raise Exception(f"processor {processor} not supported")

  # get real ioctl address
  ioctl_address = ctypes.cast(ctypes.byref(c_function), ctypes.POINTER(ctypes.c_ulong))

  # hook ioctl
  ret = libc.mprotect(ctypes.c_ulong((ioctl_address.contents.value//0x1000)*0x1000), 0x2000, 7)
  assert ret == 0
  libc.memcpy(ioctl_address.contents, ctypes.create_string_buffer(tramp), len(tramp))

# *** ioctl lib end ***

# clang2py kfd_ioctl.h -o kfd_ioctl.py
from extra.hip_gpu_driver import kfd_ioctl
def ioctls_from_header():
  hdr = (pathlib.Path(__file__).parent.parent.parent / "extra/hip_gpu_driver/kfd_ioctl.h").read_text().replace("\\\n", "")
  pattern = r'#define\s+(AMDKFD_IOC_[A-Z0-9_]+)\s+AMDKFD_IOWR?\((0x[0-9a-fA-F]+),\s+struct\s([A-Za-z0-9_]+)\)'
  matches = re.findall(pattern, hdr, re.MULTILINE)
  return {int(nr, 0x10):(name, getattr(kfd_ioctl, "struct_"+sname)) for name, nr, sname in matches}
nrs = ioctls_from_header()

@ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p)
def ioctl(fd, request, argp):
  ret = libc.syscall(IOCTL_SYSCALL, ctypes.c_int(fd), ctypes.c_ulong(request), ctypes.c_void_p(argp))
  idir, size, itype, nr = (request>>30), (request>>16)&0x3FFF, (request>>8)&0xFF, request&0xFF
  if nr in nrs and itype == 75:
    name, stype = nrs[nr]
    s = get_struct(argp, stype)
    print(f"{ret:2d} = {name:40s}", ' '.join(format_struct(s)))
  #print("ioctl", idir, size, itype, nr)
  return ret

install_hook(libc.ioctl, ioctl)

print("***** import tinygrad")
from tinygrad import Tensor, Device, TinyJit
print("***** access HIP")
dev = Device["HIP"]
