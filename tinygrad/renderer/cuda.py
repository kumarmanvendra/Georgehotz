import functools
from tinygrad.renderer.cstyle import uops_to_cstyle, CStyleLanguage
from tinygrad.helpers import DType, PtrDType, dtypes

class CUDALanguage(CStyleLanguage):
  kernel_prefix = "__global__ "
  smem_prefix = "__shared__ "
  smem_prefix_for_cast=False
  arg_int_prefix = "const int"
  barrier = "__syncthreads();"
  float4 = "make_float4"
  gid = [f'blockIdx.{chr(120+i)}' for i in range(3)]
  lid = [f'threadIdx.{chr(120+i)}' for i in range(3)]
  xid = [f'(blockIdx.{chr(120+i)}*blockDim.{chr(120+i)}+threadIdx.{chr(120+i)})' for i in range(3)]
  half_prekernel = """
    #include <cuda_fp16.h>
    struct __align__(8) half4 {
      half2 x, y;
      __device__ __forceinline__ explicit half4(const float4& a): x(make_half2(__float2half(a.x), __float2half(a.y))), y(make_half2(__float2half(a.z),__float2half(a.w))) {}
      __device__ __forceinline__ explicit operator float4() const {return make_float4(__half2float(x.x), __half2float(x.y), __half2float(y.x), __half2float(y.y)); }
    };
    """

  def render_cast(self, x, output_dtype, buf_dtype=None) -> str:
    if isinstance(buf_dtype, PtrDType): buf_dtype = DType(buf_dtype.priority, buf_dtype.itemsize, buf_dtype.name, buf_dtype.np, buf_dtype.sz)
    if len(x) == 1 and output_dtype == dtypes._float2 and buf_dtype == dtypes.half: return f"make_float2(__half2float({x[0][1:]}->x), __half2float({x[0][1:]}->y));"
    return super().render_cast(x, output_dtype, buf_dtype)

CUDARenderer = functools.partial(uops_to_cstyle, CUDALanguage())
