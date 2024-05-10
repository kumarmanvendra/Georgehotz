from tinygrad import Device, dtypes
from tinygrad.helpers import getenv, colored
from extra.optimization.helpers import load_worlds, ast_str_to_lin
from test.external.fuzz_linearizer import get_fuzz_rawbufs
from tinygrad.features.search import bufs_from_lin
import numpy as np

# move to helpers?
def colorize_float(x):
  ret = f"{x:7.2f}x"
  if x < 0.75: return colored(ret, 'green')
  elif x > 1.15: return colored(ret, 'red')
  else: return colored(ret, 'yellow')

if __name__ == "__main__":
  ast_strs = load_worlds(filter_reduce=False, filter_novariable=True)
  cudev = Device["CUDA"]
  nvdev = Device["NV"]

  # NUM=112 python3 test/external/speed_compare_cuda_nv.py

  single = getenv("NUM", -1)
  if single != -1: ast_strs = ast_strs[single:single+1]

  average_tm_cuda, average_tm_nv = 0, 0
  for num,ast in enumerate(ast_strs):
    # cuda compile
    culin = ast_str_to_lin(ast, opts=cudev.renderer)
    culin.hand_coded_optimizations()
    has_bf16 = any(b.dtype == dtypes.bfloat16 for b in culin.membufs)

    cuda_prg = cudev.to_runner(culin)
    cubufs = bufs_from_lin(culin)
    test_cubufs = get_fuzz_rawbufs(culin) if not has_bf16 else cubufs

    nvlin = ast_str_to_lin(ast, opts=nvdev.renderer)
    nvlin.hand_coded_optimizations()
    nv_prg = nvdev.to_runner(nvlin)
    nvbufs = bufs_from_lin(nvlin)
    test_nvbufs = get_fuzz_rawbufs(nvlin) if not has_bf16 else nvbufs
    if not has_bf16:
      for i,rawbuf in enumerate(test_nvbufs): rawbuf.copyin(test_cubufs[i].as_buffer())

    # warmup
    tm_cuda, tm_nv, failed = [], [], False
    try:
      cuda_prg(test_cubufs, {}, wait=True)
      for i in range(5): tm_cuda.append(cuda_prg(cubufs, {}, wait=True))
    except RuntimeError:
      print("CUDA FAILED")
      tm_cuda = [1e9]
      failed = True

    try:
      nv_prg(test_nvbufs, {}, wait=True)
      for i in range(5): tm_nv.append(nv_prg(nvbufs, {}, wait=True))
    except RuntimeError:
      print("NV FAILED")
      tm_nv = [1e9]
      failed = True

    if not failed and not has_bf16:
      curesult = np.frombuffer(test_cubufs[0].as_buffer(), test_cubufs[0].dtype.np)
      nvresult = np.frombuffer(test_nvbufs[0].as_buffer(), test_nvbufs[0].dtype.np)
      np.testing.assert_allclose(curesult, nvresult, rtol=1e-2, atol=1e-2)

    average_tm_cuda += min(tm_cuda)
    average_tm_nv += min(tm_nv)
    ratio = min(tm_nv)/min(tm_cuda)
    print(f"{average_tm_nv/average_tm_cuda:5.2f}x -- {num:4d} {colorize_float(ratio)}  {min(tm_nv)*1e6:7.2f} us", nvlin.name)
    if ratio > 1.04: print(f"NV slower {ratio}", nvlin.ast, nvlin.applied_opts)
