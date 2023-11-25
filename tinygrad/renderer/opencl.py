import functools
from tinygrad.helpers import OSX, ImageDType, dtypes
from tinygrad.ops import BinaryOps, TernaryOps
from tinygrad.renderer.cstyle import uops_to_cstyle, CStyleLanguage

type_map = { dtypes.uint8: "uchar", dtypes.uint32: "uint", dtypes.uint64: "ulong" }
class OpenCLLanguage(CStyleLanguage):
  kernel_prefix = "__kernel "
  buffer_prefix = "__global "
  smem_align = "__attribute__ ((aligned (16))) "
  smem_prefix = "__local "
  arg_int_prefix = "const int"
  half_prekernel = "#pragma OPENCL EXTENSION cl_khr_fp16 : enable"
  barrier = "barrier(CLK_LOCAL_MEM_FENCE);"
  float4 = "(float4)"
  gid = [f'get_group_id({i})' for i in range(3)]
  lid = [f'get_local_id({i})' for i in range(3)]
  xid = [f'get_global_id({i})' for i in range(3)]
  uses_vload = True
  # NOTE: mad is used so the loads aren't reordered into the math on 845
  code_for_op = {**CStyleLanguage().code_for_op, TernaryOps.MULACC: lambda a,b,c: f"mad({a},{b},{c})", BinaryOps.MAX: CStyleLanguage().code_for_op[BinaryOps.MAX] if not OSX else lambda a,b: f"max((float){a},(float){b})"} # HACK: OpenCL to METAL doesn't support non-fp32 max!

  def render_store(self, buf_name, buf_dtype, var_name, var_dtype, idx, local):
    if var_dtype.sz > 1 and not isinstance(buf_dtype, ImageDType):
      return f"*(({self.smem_prefix if local and self.smem_prefix_for_cast else self.buffer_prefix}{buf_dtype.name}{var_dtype.sz}*)({buf_name}+{idx})) = {self.render_cast([f'{var_name}.s{i}' for i in range(var_dtype.sz)], buf_dtype.vec(var_dtype.sz))};"
    return super().render_store(buf_name, buf_dtype, var_name, var_dtype, idx, local)

OpenCLRenderer = functools.partial(uops_to_cstyle, OpenCLLanguage())
