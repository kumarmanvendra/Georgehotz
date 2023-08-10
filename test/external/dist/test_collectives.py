from extra import dist
from tinygrad.jit import TinyJit
if __name__ == "__main__":
  dist.preinit()

from extra.dist import collectives
from tinygrad.helpers import CI, getenv
from tinygrad.tensor import Tensor
import numpy as np

@TinyJit
def allreduce_jit(t:Tensor, cache_id=None) -> Tensor:
  return collectives.allreduce(t, cache_id=cache_id).realize()

SIZE = 2048 if not CI else 2
SIZE_2 = 255 if not CI else 3

def run():
  # set a deterministic seed so that both ranks generate the same random tensor
  Tensor.manual_seed(42)

  rank = getenv("RANK")

  # loop 3 times to make sure it works with the jit
  for _ in range(3):
    # create a tensor to send
    t = Tensor.randn(SIZE, SIZE)
    t2 = allreduce_jit(t, cache_id="test")
    assert np.allclose(t.numpy() * 2, t2.numpy())

  # reset jit
  allreduce_jit.cnt = 0

  # test uneven chunk sizes
  for _ in range(3):
    # create a tensor to send
    t = Tensor.randn(SIZE_2, SIZE_2, SIZE_2)
    t2 = allreduce_jit(t, cache_id="test2")
    assert np.allclose(t.numpy() * 2, t2.numpy())

  print(f"rank {rank} passed")

if __name__ == "__main__":
  devices = ["gpu:0", "gpu:1" if not CI else "gpu:0"]
  world_size = len(devices)

  dist.init_oob(world_size)

  processes = []
  for rank, device in enumerate(devices):
    processes.append(dist.spawn(rank, device, fn=run, args=()))
  for p in processes: p.join()
