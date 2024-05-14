import itertools
from tinygrad import Device
from tinygrad.engine.realize import CompiledRunner
from tinygrad.helpers import getenv, colored
from extra.optimization.helpers import load_worlds, ast_str_to_lin
from tinygrad.features.search import bufs_from_lin
from tinygrad.runtime.ops_cuda import PTXCompiler, PTXRenderer, CUDACompiler

# move to helpers?
def colorize_float(x):
  ret = f"{x:7.2f}x"
  if x < 0.75: return colored(ret, 'green')
  elif x > 1.15: return colored(ret, 'red')
  else: return colored(ret, 'yellow')

if __name__ == "__main__":
  ast_strs = load_worlds(filter_reduce=False, filter_novariable=True)
  # no bfloat16 for ptx at the moment
  ast_strs = [x for x in ast_strs if "dtypes.bfloat16" not in x]
  dev = Device["CUDA"]
  ptx = PTXRenderer(dev.arch)

  # NUM=112 python3 test/external/speed_compare_cuda_ptx.py

  single = getenv("NUM", -1)
  if single != -1: ast_strs = ast_strs[single:single+1]

  average_tm_cuda, average_tm_ptx = 0, 0
  for num,ast in enumerate(ast_strs):
    # cuda compile
    dev.compiler = CUDACompiler(dev.arch)
    lin = ast_str_to_lin(ast, opts=dev.renderer)
    lin.hand_coded_optimizations()
    cuda_prg = CompiledRunner(lin.to_program())

    bufs = bufs_from_lin(lin)

    # ptx compile
    dev.compiler = PTXCompiler(dev.arch)
    lin = ast_str_to_lin(ast, opts=ptx)
    lin.hand_coded_optimizations()
    lin.linearize()
    ptx_prg = CompiledRunner(lin.to_program())

    # warmup
    try:
      cuda_prg(bufs, {}, wait=True)
    except RuntimeError:
      print("cuda failed ast:", num)
      continue
    ptx_prg(bufs, {}, wait=True)

    tm_cuda, tm_ptx = [], []
    for i in range(5):
      tm_cuda.append(cuda_prg(bufs, {}, wait=True))
      tm_ptx.append(ptx_prg(bufs, {}, wait=True))
    average_tm_cuda += min(tm_cuda)
    average_tm_ptx += min(tm_ptx)
    ratio = min(tm_ptx)/min(tm_cuda)
    print(f"{average_tm_ptx/average_tm_cuda:5.2f}x -- {num:4d} {colorize_float(ratio)}  {min(tm_ptx)*1e6:7.2f} us", lin.name)
    if ratio > 1.5:
      def fix(x): return x.replace('\t', ' ').strip()
      ll1, ll2 = cuda_prg.lib.decode().split('\n'), ptx_prg.lib.decode().split('\n')
      if single != -1:
        for ln, (l1, l2) in enumerate(itertools.zip_longest(ll1, ll2, fillvalue='')):
          print(f"{ln:5d} | {fix(l1):80s} | {fix(l2):80s}")
      print(len(ll1), len(ll2), "RATIO", ratio, "us", min(tm_ptx)*1e6)
