from typing import List, DefaultDict, Dict, Optional, Final, Callable
import linecache
real_getlines = linecache.getlines
from collections import defaultdict
from tinygrad.helpers import DEBUG
from tinygrad.dtype import dtypes, PtrDType
from tinygrad.renderer import Renderer
from tinygrad.codegen.uopgraph import UOpGraph
from tinygrad.codegen.uops import UOps, UOp
from tinygrad.ops import UnaryOps, BinaryOps, TernaryOps, Op
from triton.compiler import AttrsDescriptor, ASTSource, compile as triton_compile

triton_dtypes = {dtypes.double: "tl.float64", dtypes.float32: "tl.float32", dtypes.float16: "tl.float16",
                 dtypes.bool: "tl.int1", dtypes.int8: "tl.int8", dtypes.uint8: "tl.uint8", dtypes.int32: "tl.int32", dtypes.int64: "tl.int64",
                 dtypes.uint32: "tl.uint32", dtypes.uint64: "tl.uint64", dtypes.int16: "tl.int16", dtypes.uint16: "tl.uint16"}
signature_dtypes = {dtypes.double: "fp64", dtypes.float32: "fp32", dtypes.float16: "fp16", dtypes.bool: "i1",
                    dtypes.int8: "i8", dtypes.uint8: "u8", dtypes.int32: "i32", dtypes.int64: "i64",
                    dtypes.uint32: "u32", dtypes.uint64: "u64", dtypes.int16: "i16", dtypes.uint16: "u16"}
signature_dtypes = {**signature_dtypes, **{PtrDType(k):("*"+v) for k,v in signature_dtypes.items()}}

code_for_op: Final[Dict[Op, Callable]] = {
  UnaryOps.EXP2: lambda x,dtype,: f"tl.math.exp2({x})",
  UnaryOps.LOG2: lambda x,dtype,: f"tl.math.log2({x})",
  UnaryOps.SIN: lambda x,dtype: f"tl.sin({x})",
  UnaryOps.SQRT: lambda x,dtype: f"tl.sqrt({x})",
  UnaryOps.NEG: lambda x,dtype: f"-{x}",
  UnaryOps.RECIP: lambda x,dtype: f"1.0/{x}",
  BinaryOps.ADD: lambda x,y,dtype: f"({x}+{y})",
  BinaryOps.MUL: lambda x,y,dtype: f"({x}*{y})",
  BinaryOps.MAX: lambda x,y,dtype: f"tl.maximum({x},{y})",
  BinaryOps.CMPLT: lambda x,y,dtype: f"({x}<{y})",
  BinaryOps.MOD: lambda x,y,dtype: f"tl.abs({x})%tl.abs({y})*tl.where({x}<0,-1,1)",
  TernaryOps.MULACC: lambda x,y,z,dtype: f"(({x}*{y})+{z})",
  TernaryOps.WHERE: lambda x,y,z,dtype: f"tl.where({x},{y},{z})",
}

class TritonRenderer(Renderer):
  device = "CUDA"
  suffix = "TRITON"

  def render(self, name:str, uops:UOpGraph) -> str:
    kernel:List[str] = []
    depth = 1
    def kk(s): kernel.append("  "*depth+s)

    c: DefaultDict[str, int] = defaultdict(int)
    r: Dict[UOp, str] = {}

    def ssa(prefix:str, u:Optional[UOp]=None):
      nonlocal c, r
      ret = f"{prefix}{c[prefix]}"
      if u is not None: r[u] = ret
      c[prefix] += 1
      return ret

    bufs, signature = [], []
    uops.print()

    locals = []
    expand_axis = {}
    #for u in uops:
    #  if u.op is UOps.SPECIAL:
    #    if u.arg[0][0] == "l":
    #      locals.append(u.arg[1])
    reduce_expands = []
    for u in uops:
      if u.op is UOps.REDUCE:
        reduce_expands += [x for x in u.src if x.op is UOps.EXPAND]
      if u.op is UOps.EXPAND:
        assert len(u.arg) == 1
        expand_axis[u.arg[0][0]] = len(locals)
        locals.append(u.arg[0][1])
    for u in uops:
      if u.op is UOps.DEFINE_GLOBAL:
        r[u] = f"buf{u.arg[0]}"
        bufs.append(r[u])
        signature.append(signature_dtypes[u.dtype])
      elif u.op is UOps.CONST:
        r[u] = str(u.arg)
      elif u.op is UOps.EXPAND:
        # TODO: assert it's all consts in range
        idxx = ["None"]*len(locals)
        idxx[expand_axis[u.arg[0][0]]] = ":"
        kk(f"{ssa('ex', u)} = tl.arange(0, {u.arg[0][1]})[{', '.join(idxx)}]")
      elif u.op is UOps.REDUCE:
        rngs = [x for x in u.src[1:] if x.op is UOps.RANGE]
        assert len(rngs) == 0
        ex_axis = tuple([expand_axis[x.arg[0][0]] for x in u.src[1:] if x.op is UOps.EXPAND])
        assert len(ex_axis) == 1
        kk(f"{ssa('ex', u)} = tl.sum({r[u.src[0]]}, {ex_axis[0]}, keep_dims=True)")
      elif u.op is UOps.RANGE:
        kk(f"for {ssa('rng', u)} in range({r[u.src[0]]}, {r[u.src[1]]}):")
        depth += 1
      elif u.op is UOps.ENDRANGE: depth -= 1
      elif u.op is UOps.SPECIAL:
        if u.arg[0][0] == "g":
          kk(f"{u.arg[0]} = tl.program_id(axis={u.arg[0][4]})")
        else:
          idxx = ["None"]*len(locals)
          idxx[int(u.arg[0][4])] = ":"
          kk(f"{u.arg[0]} = tl.arange(0, {u.arg[1]})[{', '.join(idxx)}]")
        r[u] = u.arg[0]
      elif u.op is UOps.ALU:
        r[u] = code_for_op[u.arg](*[r[x] for x in u.src], u.dtype)
        #val = code_for_op[u.arg](*[r[x] for x in u.src], u.dtype)
        #kk(f"{ssa('alu', u)} = ({val})")
      elif u.op is UOps.DEFINE_ACC:
        real_locals = locals[:]
        for re in reduce_expands: real_locals[expand_axis[re.arg[0][0]]] = 1
        real_locals = [x for x in real_locals if x != 1]
        kk(f"{ssa('acc', u)} = tl.zeros({str(tuple(real_locals))}, dtype={triton_dtypes[u.dtype]})")
        #kk(f"{ssa('acc', u)} = {r[u.src[0]]}")
      elif u.op is UOps.LOAD:
        kk(f"{ssa('val', u)} = tl.load({r[u.src[0]]} + {r[u.src[1]]}.reshape(16,16))")
      elif u.op is UOps.STORE:
        kk(f"tl.store({r[u.src[0]]} + {r[u.src[1]]}.reshape(16,16), {r[u.src[2]]})")
      elif u.op is UOps.CAST:
        kk(f"{ssa('cast', u)} = tl.cast({r[u.src[0]]}, {triton_dtypes[u.dtype]})")
      elif u.op is UOps.PHI:
        r[u] = r[u.src[0]]
        kk(f"{r[u]} = {r[u.src[1]]};")
      else:
        raise RuntimeError(f"unimplemented {u.op}")

    src = f"import triton\nimport triton.language as tl\n@triton.jit\ndef {name}({', '.join(bufs)}):\n" + '\n'.join(kernel)
    #src = src.replace("ex3 = tl.sum((acc0+(val0*val1)), 2, keep_dims=True)", "ex3 = tl.dot(val0.reshape(16,16), val1.reshape(16,16), acc0.reshape(16,16), out_dtype=tl.float16).reshape(16,16,1)")
    src = src.replace("ex3 = tl.sum((acc0+(val0*val1)), 2, keep_dims=True)", "ex3 = tl.dot(val0, val1, acc0, out_dtype=tl.float16)")
    if DEBUG >= 3: print(src)

    linecache.getlines = lambda filename, module_globals=None: src.splitlines(keepends=True) \
      if "<triton>" == filename else real_getlines(filename, module_globals)
    exec(compile(src, "<triton>", "exec"), globals()) # pylint: disable=W0122
    compiled = triton_compile(ASTSource(globals()[name], ','.join(signature),
                                        attrs=AttrsDescriptor(divisible_by_16=tuple(range(len(bufs))), equal_to_1=())))
    ptx = compiled.asm["ptx"]
    # specify the shared memory here so we don't need to do it dynamically
    ptx = ptx.replace(".extern .shared .align 16 .b8 global_smem[];", f".shared .align 16 .b8 global_smem[{compiled.metadata.shared}];")
    # useless comment spam
    ptx = ptx.replace("\t// begin inline asm\n", "")
    ptx = ptx.replace("\t// end inline asm\n", "")
    # remove debug sections
    ptx = ptx.split("\t.file")[0]
    return {"src":ptx, "local_size":[32*compiled.metadata.num_warps, 1, 1]}

