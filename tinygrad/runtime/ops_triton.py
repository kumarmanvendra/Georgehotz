from __future__ import annotations
import hashlib
import pycuda.driver as cuda # type: ignore

from triton.compiler import compile as triton_compile

from typing import Any, Tuple, Dict, List, Final, Callable
from tinygrad.ops import UnaryOps, BinaryOps, ReduceOps, Op, Compiled
from tinygrad.helpers import prod, DEBUG, dtypes, ImageDType
from tinygrad.runtime.ops_cuda import RawCUDABuffer
from tinygrad.codegen.linearizer import LinearizerOptions, UOp, UOps
from tinygrad.shape.symbolic import NumNode


class TritonProgram:

  def __init__(self, name:str, prg:str, binary:bool=False):
    signature = ','.join(["*fp32" for _ in range(prg.splitlines()[1].count("data"))])

    prg = "import triton\nimport triton.language as tl\ntl.core.TRITON_MAX_TENSOR_NUMEL = float('inf')\n" + prg
    
    hash = hashlib.md5(prg.encode('utf-8')).hexdigest()
    fn = f"/tmp/{hash}.py"
    with open(fn, "w") as f: f.write(prg)
    codeObject = compile(prg, fn, "exec")
    exec(codeObject, globals())
    self.program = triton_compile(globals()["fxn"], signature=signature, device_type="cuda", debug=True).asm["ptx"]
    if DEBUG>=4: print(self.program)
    self.program = cuda.module_from_buffer(self.program.encode('utf-8')).get_function(self.program.split(".visible .entry ")[1].split("(")[0])

  def __call__(self, global_size, local_size, *args, wait=False) -> Any:
    
    self.program(*[x._buf for x in args], block = tuple(local_size), grid = tuple(global_size))

def uops_to_triton(function_name:str, uops:List[UOp]):
    kernel = []
    global_size: List[int] = []
    local_size: List[int] = []
    depth = 1
    bufs = []
    def kk(s): kernel.append("  "*depth+s)

    full_local_shape: Tuple[Any, ...] = ()
    acc_local_shape = 1

    gid = [f"tl.program_id({i})" for i in range(3)]
    code_for_op: Final[Dict[Op, Callable]] = {
      UnaryOps.EXP2: lambda x: f"tl.math.exp2({x})",
      UnaryOps.LOG2: lambda x: f"tl.math.log2({x})", # TODO: is fast_log2f ok?
      UnaryOps.SIN: lambda x: f"tl.sin({x})",
      UnaryOps.SQRT: lambda x: f"tl.sqrt({x})",
      BinaryOps.ADD: lambda x,y: f"({x}+{y})", BinaryOps.SUB: lambda x,y: f"({x}-{y})",
      BinaryOps.MUL: lambda x,y: f"({x}*{y})", BinaryOps.DIV: lambda x,y: f"({x}/{y})",
      BinaryOps.MAX: lambda x,y: f"tl.maximum({x},{y})", # axis?
      BinaryOps.CMPLT: lambda x,y: f"({x}<{y})",
      ReduceOps.SUM: lambda x: f"tl.expand_dims(tl.sum({x}, axis={len(full_local_shape)-len(self.group_for_reduce)}), axis={len(full_local_shape)-len(self.group_for_reduce)})" if len(self.group_for_reduce) != len(full_local_shape) else f"tl.sum({x}, axis={len(full_local_shape)-len(self.group_for_reduce)})",
    }
   
    for uop,newvar,vin,args in uops:
        if uop == UOps.LOOP:
          for i,var in enumerate(args[0]):
            if isinstance(var, NumNode): continue # python doesnt have block scope
            else:
              if args[1] == "global":
                global_size.append(var.max+1)
                kk(f"{var.expr} = {gid[i]} # {var.max+1}")
              elif args[1] == "local":
                full_local_shape = tuple([var.max+1 for var in args[0]])
                assert var.min == 0, "local loop must start at 0"
                kk(f"{var.expr} = tl.arange({0}, {var.max+1})[{', '.join([':' if i == j else 'None' for j in range(len(args[0]))])}]")
                acc_local_shape *= var.max+1
              else:
                kk(f"for {var.expr} in range({var.min}, {var.max+1}):")
                depth += 1
        elif uop == UOps.ENDLOOP:
          if args[1] not in ["global", "local"] and len(args[0]):
            depth -= 1
            kk(f"# end {args[1]}")
        elif uop == UOps.ALU:
          assert newvar is not None
          kk(f"{newvar.render()} = {code_for_op[args](*[x.render() for x in vin])}")
        elif uop == UOps.LOAD:
          assert newvar is not None
          val = f"{args.name}" # defaults to render_python
          triton_dtype = {dtypes.float32: "tl.float32", dtypes.float16: "tl.float16", dtypes.int8: "tl.int8", dtypes.uint8: "tl.uint8", dtypes.int32: "tl.int32", dtypes.int64: "tl.int64"}[newvar.dtype]
          if args.valid.min == 1: kk(f"{newvar.render()} = tl.load({val}).to({triton_dtype})")
          else: kk(f"{newvar.render()} = tl.where({args.valid.render()}, tl.load({val}, mask={args.valid.render()}), 0.0).to({triton_dtype})")
        elif uop == UOps.STORE:
          assert vin[0].dtype == dtypes.float, "unimplemented: float4 store"
          assert not isinstance(args.memory_dtype, ImageDType), "unimplemented: image store"
          assert args.valid.min == 1, "store must be valid"
          kk(f"tl.store({args.name} + {args.idx.render()}, {vin[0].render()})")
        elif uop == UOps.DEFINE_GLOBAL:
          bufs.append(args)
        elif uop == UOps.CAST: raise NotImplementedError("unimplemented: cast")
        else:
          raise NotImplementedError(f"unimplemented: {uop}")

    prg = "@triton.jit\ndef fxn("+','.join(f"data{i}" for i in range(len(bufs)))+"):\n"
    prg += '\n'.join(kernel)
    return prg, global_size, local_size

TritonBuffer = Compiled(RawCUDABuffer, LinearizerOptions(), uops_to_triton, TritonProgram)