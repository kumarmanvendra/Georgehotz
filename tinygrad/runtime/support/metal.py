from ctypes import CDLL, c_void_p, Structure, c_ulong
from typing import Any, List, Tuple

class objc_id(c_void_p): # Wrapping it so ctypes doesn't convert it to plain int
  def __hash__(self): return self.value
  def __eq__(self, other): return self.value == other.value
  # def __del__(self): msg(self, "release")

def load_library(path: str): return CDLL(path)

libobjc = load_library("/usr/lib/libobjc.dylib")
libmetal = load_library("/Library/Frameworks/Metal.framework/Metal")
# Must be loaded for defalt Metal Device: https://developer.apple.com/documentation/metal/1433401-mtlcreatesystemdefaultdevice?language=objc
load_library("/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
libdispatch = load_library("/usr/lib/libSystem.dylib")
libobjc.objc_msgSend.restype = objc_id
libobjc.objc_getClass.restype = objc_id
libobjc.sel_registerName.restype = objc_id
libmetal.MTLCreateSystemDefaultDevice.restype = objc_id
libdispatch.dispatch_data_create.restype = objc_id

def msg(ptr: objc_id, selector: str, /, *args: Any, restype: type = objc_id) -> Any:
  sender = libobjc["objc_msgSend"] # Using attribute access returns a new reference so setting restype is safe
  sender.restype = restype
  return sender(ptr, libobjc.sel_registerName(selector.encode()), *args)

def to_ns_str(s: str) -> objc_id: return msg(libobjc.objc_getClass(b"NSString"), "stringWithUTF8String:", s.encode())

def to_ns_array(items: List[Any]): return (objc_id * len(items))(*items)

def int_tuple_to_struct(t: Tuple[int, ...], _type: type = c_ulong):
  class Struct(Structure): pass
  Struct._fields_ = [(f"field{i}", _type) for i in range(len(t))]
  return Struct(*t)

MTLIndirectCommandTypeConcurrentDispatch = 0 # (1 << 5)
MTLResourceCPUCacheModeDefaultCache = 0
MTLResourceUsageRead_MTLResourceUsageWrite = 3 # MTLResourceUsageRead (01) | MTLResourceUsageWrite (10)
