# a python uops emulator
# works to test the tensor cores, and all the uops in general
# this is the (living) definition of uops
from typing import Tuple, List
import pickle, base64, itertools
from tinygrad.device import Compiled, Allocator, Compiler
from tinygrad.codegen.kernel import LinearizerOptions
from tinygrad.codegen.uops import UOp, UOps
from tinygrad.ops import UnaryOps, BinaryOps, TernaryOps

class PythonProgram:
  def __init__(self, name:str, lib:bytes):
    self.uops: List[UOp] = pickle.loads(lib)
  def __call__(self, *bufs, global_size:Tuple[int,int,int]=(1,1,1), local_size:Tuple[int,int,int]=(1,1,1), vals:Tuple[int, ...]=(), wait=False):
    # TODO: abstract this out so it can be used for constant folding
    for idxs in itertools.product(*[range(x) for x in global_size[::-1]+local_size[::-1]]):
      ul = {}
      pbufs: List[memoryview] = list(bufs)
      for i,u in enumerate(self.uops):
        inp = [ul[self.uops.index(v)] for v in u.vin]
        if u.uop is UOps.DEFINE_GLOBAL: ul[i] = pbufs.pop(0).cast(u.dtype.fmt)
        elif u.uop is UOps.SPECIAL:
          if u.arg[1][0] == 'g': ul[i] = idxs[2-u.arg[0]]
          elif u.arg[1][0] == 'l': ul[i] = idxs[5-u.arg[0]]
        elif u.uop is UOps.CONST: ul[i] = u.arg
        elif u.uop is UOps.CAST:
          assert u.dtype.sz > 1
          ul[i] = inp
        elif u.uop is UOps.STORE:
          if isinstance(inp[2], list):
            for j,val in enumerate(inp[2]):
              inp[0][inp[1] + j] = val
          else:
            inp[0][inp[1]] = inp[2]
        elif u.uop is UOps.LOAD:
          if u.dtype.sz > 1:
            ul[i] = [inp[0][inp[1]+j] for j in range(u.dtype.sz)]
          else:
            ul[i] = inp[0][inp[1]]
        elif u.uop is UOps.GEP:
          ul[i] = inp[0][u.arg]
        elif u.uop is UOps.ALU:
          if u.arg == BinaryOps.MUL:
            ul[i] = inp[0] * inp[1]
          elif u.arg == BinaryOps.ADD:
            ul[i] = inp[0] + inp[1]

class PythonCompiler(Compiler):
  linearizer_opts = LinearizerOptions()
  def render(self, name:str, uops) -> str: return base64.b64encode(pickle.dumps(uops))
  def compile(self, src:str) -> bytes: return base64.b64decode(src)

class PythonAllocator(Allocator):
  def _alloc(self, size): return memoryview(bytearray(size))
  def copyin(self, dest, src:memoryview): dest[:] = src
  def copyout(self, dest:memoryview, src): dest[:] = src

class PythonDevice(Compiled):
  def __init__(self, device:str):
    super().__init__(device, PythonAllocator(), PythonCompiler(), PythonProgram)