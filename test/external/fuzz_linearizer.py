import random
import numpy as np
from collections import Counter, defaultdict
from extra.optimization.helpers import load_worlds, ast_str_to_lin
from tinygrad.codegen.linearizer import Linearizer
from tinygrad.features.search import get_linearizer_actions
from tinygrad.graph import print_tree
from tinygrad.helpers import prod
from tinygrad.ops import Device, Compiled

random.seed(42)
np.random.seed(42)
device = Device[Device.DEFAULT]

class LB:
  # placeholder LazyBuffer
  def __init__(self, rawbuf, dtype):
    self.realized = rawbuf
    self.output_buffer = rawbuf
    self.dtype = dtype


def fuzz_linearizer(lin: Linearizer):
  print_tree(lin.ast)
  print(lin.colored_shape())

  outputbuffer = device.buffer(size=prod(lin.membufs[0].st.shape), dtype=lin.membufs[0].dtype)
  rawbufs = [outputbuffer]
  rawbuf_size = defaultdict(int)
  for buf in lin.membufs[1:]:
    idx, valid = buf.st.expr_idxs()
    # TODO: image type and variable shape
    size = idx.max+1
    rawbuf_size[buf.idx] = max(rawbuf_size[buf.idx], size)

  for i, size in sorted(rawbuf_size.items()):
    assert len(rawbufs) == i
    # TODO: different range for int type v.s. float type
    rawbuf = device.buffer.fromCPU(np.random.uniform(low=-5.0, high=5.0, size=size).astype(buf.dtype.np))
    rawbufs.append(rawbuf)

  # NOTE: copied from beam_search
  def tuplize_uops(uops): return tuple([(x.uop, x.dtype, tuple(x.num for x in x.vin), x.arg) for x in uops])
  seen_uops = {}

  output = None
  while 1:
    if len(seen_uops) >= 20:
      # enough for this kernel
      break
    # TODO: if this is too slow, we can reject sample until first valid action, instead of getting all actions first
    actions = get_linearizer_actions(lin.copy(), include_0=False)
    if not actions: break
    lin = random.choice(list(actions.values()))
    if lin.applied_opts: print(f"last action: {lin.applied_opts[-1]}")

    # TODO: why is there a noop action? local a local can permute and have a loop
    tuops = tuplize_uops(lin.copy().linearize().uops)
    if tuops in seen_uops:
      break
    seen_uops[tuops] = tuple(lin.applied_opts)

    print(lin.colored_shape())
    # get a new output buffer
    rawbufs[0] = device.buffer(size=prod(lin.membufs[0].st.shape), dtype=lin.membufs[0].dtype)

    if isinstance(device, Compiled):
      try:
        prg = device.to_program(lin.copy())
      except:
        import traceback
        traceback.print_exc()
        print("COMPILE FAILED!!")
        return "COMPILE_ERROR"
      try:
        prg.exec(rawbufs, force_wait=True)
      except:
        print("EXEC FAILED!!")
        return "EXEC_ERROR"
    else:
      # TODO: Interpreted does not work with symbolic shape
      try:
        device.exec_ast(lin.ast, output=LB(rawbufs[0], rawbufs[0].dtype), inputs=[LB(buf, buf.dtype) for buf in rawbufs[1:]])
      except Exception as e:
        import traceback
        traceback.print_exc()
        return str(type(e))

    result = rawbufs[0].toCPU()

    if output is None:
      output = result
    else:
      try:
        # TODO: assert based on L2 distance not elementwise
        np.testing.assert_allclose(result, output, rtol=1e-4, atol=1e-4)
      except AssertionError:
        return "NOT_ALLCLOSE"
      except Exception as e:
        import traceback
        traceback.print_exc()
        return str(type(e))
  return "PASS"

if __name__ == "__main__":
  ast_strs = load_worlds()
  print(f"{len(ast_strs)=}")
  tested = 0
  c = Counter()
  failed = []
  # TODO: ast_strs[0] output contains nan?
  for i, ast in enumerate(ast_strs):
    print(f"testing ast {i}")
    tested += 1
    lin = ast_str_to_lin(ast)
    fuzz = fuzz_linearizer(lin)
    c[fuzz] += 1
    if fuzz != "PASS":
      failed.append(i)
  print(f"{tested=}")
  print(c.most_common())
  print(f"{failed=}")