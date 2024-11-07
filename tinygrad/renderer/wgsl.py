from typing import List, Tuple
from tinygrad.dtype import DType, PtrDType, dtypes
from tinygrad.ops import UOp, Ops, UnaryOps, TernaryOps, PatternMatcher, UPat, GroupOp
from tinygrad.renderer.cstyle import CStyleLanguage, base_rewrite
from tinygrad.helpers import strip_parens
import math

def fixup_binops(c,a,b):
  if c.op == Ops.CMPLT and a.dtype == dtypes.bool: return UOp(c.op, c.dtype, (a.cast(dtypes.int), b.cast(dtypes.int)))
  if c.op in (Ops.XOR, Ops.MAX) and c.dtype == dtypes.bool:
    return UOp(c.op, dtypes.int, (a.cast(dtypes.int), b.cast(dtypes.int))).cast(dtypes.bool)

wgsl_matcher = PatternMatcher([
  (UPat((Ops.DEFINE_GLOBAL, Ops.DEFINE_LOCAL), dtype=dtypes.bool.ptr(), name="a"), lambda a: UOp(a.op, dtypes.int32.ptr(), a.src, a.arg)),
  (UPat(Ops.LOAD, name="root", dtype=dtypes.bool, src=(UPat.var("x"),UPat.var("y"),UPat.var("g", dtype=dtypes.bool))),
    lambda root,x,y,g: UOp(root.op, dtypes.int, (x,y.cast(dtypes.int), g), root.arg).cast(dtypes.bool)),
  (UPat(Ops.LOAD, name="root", dtype=dtypes.bool, src=(UPat())), lambda root: UOp(root.op, dtypes.int, root.src, root.arg).cast(dtypes.bool)),
  (UPat(GroupOp.ALU, src=(UPat(name="a"), UPat(name="b")), name="c"), fixup_binops),
  *[(UPat(a, src=(UPat(name="b", dtype=(dtypes.uint, dtypes.int, dtypes.bool))), name="a"),
     lambda a,b: UOp(a, dtypes.float, (b.cast(dtypes.float),)).cast(b.dtype))
    for a in (UnaryOps.EXP2, UnaryOps.SIN, UnaryOps.LOG2, UnaryOps.SQRT)],
  (UPat.store(UPat.var("bidx"), UPat.var("var", dtype=dtypes.bool), UPat.var("gate")),
   lambda bidx,val,gate: UOp.store(bidx, val.cast(dtypes.int), gate)),
  (UPat.store(UPat.var("bidx"), UPat.var("var", dtype=dtypes.bool)), lambda bidx,var: UOp.store(bidx, var.cast(dtypes.int))),
  (UPat(Ops.MAX, name="m"), lambda m: (m.src[0] < m.src[1]).where(m.src[1], m.src[0])),
  # prevent ulong mul: 'a * select(0, 1 << 32, cond) -> select(0, a << 32, cond)'
  (UPat(Ops.MUL, name="m", src=(UPat(name="a"), UPat(TernaryOps.WHERE, src=(UPat.var("g"), \
    UPat(op=Ops.CONST, name="c1"), UPat(op=Ops.CONST, name="c2")))), dtype=dtypes.ulong), \
    lambda m,a,g,c1,c2: UOp(TernaryOps.WHERE, dtype=m.dtype, src=(g, a << 32, UOp.const(dtype=m.dtype, b=0))) \
    if c1.arg == (1 << 32) and c2.arg == 0 else None),
  # fix nan propagation: 'a * select(1, nan, cond) -> select(a, nan, cond)'
  (UPat(Ops.MUL, name="m", src=(UPat(name="a"), UPat(TernaryOps.WHERE, src=(UPat.var("g"), \
    UPat(op=Ops.CONST, name="c1"), UPat(op=Ops.CONST, name="c2"))))), \
    lambda m,a,g,c1,c2: UOp(TernaryOps.WHERE, dtype=m.dtype, src=(g, UOp.const(dtype=dtypes.float, b=float('nan')), a)) \
    if math.isnan(c1.arg) and c2.arg == 1.0 else None),
  ])

type_map = { dtypes.float: "f32", dtypes.uchar: "u32", dtypes.ushort: "u32", dtypes.short: "i32",
            dtypes.char: "i32", dtypes.int32: "i32", dtypes.uint32: "u32", dtypes.bool: "bool", dtypes.ulong: "vec2<u32>" }

# convert from pointer style indexing to array style
def render_load_store(r, bidx, sext = False, sext_am = 8):
  sbidx = strip_parens(r[bidx])
  buf, idx = sbidx.split("+")[0], '+'.join(sbidx.split("+")[1:])
  # sign-extend when loading char/short
  return f"bitcast<i32>(select(0u, 0xffffffffu << {sext_am}, (({buf}[{idx}] >> {sext_am-1}) > 0)) | bitcast<u32>({buf}[{idx}]))" \
    if sext else f"{buf}[{idx}]"

# emulate ulong shift
def render_ushift(r, v, am, left):
  v, am = r[v], f"{r[am]}.x" if am.dtype == dtypes.ulong else f"{r[am]}"
  return f"select(vec2<u32>({v}.x << {am}, ({v}.y << {am}) | ({v}.x >> (32u-{am}))), vec2<u32>(0u, {v}.x << ({am}-32u)), {am} >= 32u)" \
  if left else f"select(vec2<u32>(({v}.x >> {am}) | ({v}.y << (32u - {am})), {v}.y >> {am}), vec2<u32>({v}.y >> ({am} - 32u), 0u), {am} >= 32u)"

class WGSLRenderer(CStyleLanguage):
  device = "WEBGPU"
  global_max = (65535, 65535, 65535)
  local_max = (256, 256, 64)
  code_for_workitem = {"g": lambda x: f"i32(gindex.{'xyz'[int(x)]})", "l": lambda x: f"i32(lindex.{'xyz'[int(x)]})"}
  extra_matcher = wgsl_matcher
  external_local_bufs = True
  supports_float4 = False
  barrier = "workgroupBarrier();"
  code_for_op = {**CStyleLanguage.code_for_op, TernaryOps.WHERE: lambda a,b,c,dtype: f"select({c},{b},{a})"}
  nan = "nan()"
  type_map = type_map

  string_rewrite = PatternMatcher([
    (UPat(Ops.CONST, dtype=dtypes.bool, name="x"), lambda ctx,x: "true" if x.arg else "false"),
    (UPat(Ops.CONST, dtype=(dtypes.char, dtypes.short), name="x"), lambda ctx,x: f"i32({x.arg})"),
    (UPat(Ops.CONST, dtype=(dtypes.uchar, dtypes.ushort, dtypes.uint32), name="x"), lambda ctx,x: f"bitcast<u32>({x.arg}i)" \
     if x.arg < 0 else f"{x.arg&0xFFFFFFFF}u"),
    (UPat(Ops.CONST, dtype=dtypes.ulong, name="x"), lambda ctx,x: f"vec2<u32>({x.arg}, 0u)" if x.arg <= 0xFFFFFFFF \
     else  f"vec2<u32>({x.arg&0xFFFFFFFF}, {x.arg>>32})"),
    (UPat(Ops.CONST, arg=math.inf, name="x"), lambda ctx, x: f"{type_map[x.dtype]}({ctx.infinity})"),
    (UPat(Ops.CONST, arg=-math.inf, name="x"), lambda ctx, x: f"{type_map[x.dtype]}(-{ctx.infinity})"),
    (UPat(Ops.CONST, dtype=dtypes.floats, name="x"), lambda ctx,x: f"({type_map[x.dtype]}({ctx.nan}))" if math.isnan(x.arg) else None),
    (UPat(Ops.DEFINE_LOCAL, name="x"), lambda ctx,x: f"var<workgroup> {ctx[x]}: array<{type_map[x.dtype.base]}, {x.arg[1]}>;"),
    (UPat(Ops.CAST, name="x"), lambda ctx,x: f"vec2<u32>(({ctx[x.src[0]]}), 0u)" if x.dtype == dtypes.uint64 \
      else f"{type_map[x.dtype]}({ctx[x.src[0]]}.x)" if x.src[0].dtype == dtypes.uint64 else f"{type_map[x.dtype]}({ctx[x.src[0]]})"),
    (UPat(Ops.BITCAST, dtype=(dtypes.char, dtypes.uchar), name="x"), lambda ctx,x: f"bitcast<{type_map[x.dtype]}>({ctx[x.src[0]]}&0xFF)"),
    (UPat(Ops.BITCAST, dtype=(dtypes.short, dtypes.ushort), name="x"), lambda ctx,x: f"bitcast<{type_map[x.dtype]}>({ctx[x.src[0]]}&0xFFFF)"),
    (UPat(Ops.BITCAST, name="x"), lambda ctx,x: f"bitcast<{type_map[x.dtype]}>({ctx[x.src[0]]})"),
    # sign extended loads for char, short
    (UPat(Ops.LOAD, name="l", src=(UPat.var("bidx"), UPat.var('var'), UPat.var("gate"))),
      lambda ctx,l,bidx,var,gate: f"select({ctx[var]}, {render_load_store(ctx, bidx, l.dtype in [dtypes.char, dtypes.short], \
        8*l.dtype.itemsize)}, {ctx[gate]})"),
    (UPat(Ops.LOAD, name="l", src=(UPat.var('bidx'),), allow_any_len=True), lambda ctx,l, bidx:
     f"{render_load_store(ctx, bidx, l.dtype in [dtypes.char, dtypes.short], 8*l.dtype.itemsize)}"),
    (UPat(Ops.STORE, src=(UPat.var('bidx'), UPat.var("var")), allow_any_len=True),
     lambda ctx,bidx,var: f"{render_load_store(ctx,bidx)} = {ctx[var]};"),
    # fix nan check: 'a != a -> is_nan()'
    (UPat(Ops.CMPNE, src=(UPat.var("a"), UPat.var("b"))), lambda ctx,a,b: f"is_nan({ctx[a]})" if a == b else None),
    (UPat(Ops.SHL, name="x", dtype=dtypes.ulong), lambda ctx,x: render_ushift(ctx, x.src[0], x.src[1], left=True)),
    (UPat(Ops.SHR, name="x", dtype=dtypes.ulong), lambda ctx,x: render_ushift(ctx, x.src[0], x.src[1], left=False)),
  ]) + base_rewrite

  def render_dtype(self, dt:DType, mutable=True) -> str: return "var"
  def render_kernel(self, function_name:str, kernel:List[str], bufs:List[Tuple[str,Tuple[DType,bool]]], uops:List[UOp], prefix=None) -> str:
    local_size = [num for _, num in sorted([u.arg for u in uops if u.op is Ops.SPECIAL and u.arg[0][0] == 'l'], key=lambda x: x[0])]
    if not local_size: local_size = [1]
    bind_it = iter(range(len(bufs)))
    prg = "fn nan() -> f32 { let bits = 0xffffffffu; return bitcast<f32>(bits); }\n"
    # trick to obfuscate compiler so that nan is detected properly
    prg += "fn is_nan(v:f32) -> bool { return min(v, 1.0) == 1.0 && max(v, -1.0) == -1.0; }\n"
    prg += "@group(0) @binding(0)\nvar<uniform> INFINITY : f32;\n"
    prg += "\n".join((prefix or [])+[f"@group(0) @binding({next(bind_it)+1}) {'var<storage,read_write>' if isinstance(dtype, PtrDType) else 'var<uniform>'} {name}: {f'array<{self.type_map[dtype.base]}>' if isinstance(dtype, PtrDType) else 'i32'};" for name,(dtype,rw) in bufs])  # noqa: E501
    prg += f"\n@compute @workgroup_size({','.join([str(x) for x in local_size])}) fn {function_name}(@builtin(workgroup_id) gindex: vec3<u32>, @builtin(local_invocation_id) lindex: vec3<u32>) {{\n" + "\n".join(kernel) + "\n}"  # noqa: E501
    return prg
