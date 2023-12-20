import random
from typing import List
from tqdm import trange
from tinygrad.helpers import getenv, DEBUG, colored
from tinygrad.shape.shapetracker import ShapeTracker
from test.external.fuzz_shapetracker import shapetracker_ops
from test.external.fuzz_shapetracker import do_permute, do_reshape_split_one, do_reshape_combine_two, do_flip, do_pad
from test.unit.test_shapetracker_math import st_equal

class MultiShapeTracker:
  def __init__(self, sts:List[ShapeTracker]): self.sts = sts
  @property
  def shape(self): return self.sts[0].shape
  def reshape(self, arg): self.sts = [x.reshape(arg) for x in self.sts]
  def permute(self, arg): self.sts = [x.permute(arg) for x in self.sts]
  def expand(self, arg): self.sts = [x.expand(arg) for x in self.sts]
  def shrink(self, arg): self.sts = [x.shrink(arg) for x in self.sts]
  def stride(self, arg): self.sts = [x.stride(arg) for x in self.sts]
  def pad(self, arg): self.sts = [x.pad(arg) for x in self.sts]

def fuzz_plus():
  m = MultiShapeTracker([ShapeTracker.from_shape((random.randint(1, 10), random.randint(1, 10), random.randint(1, 10)))])
  for _ in range(4): random.choice(shapetracker_ops)(m)
  backup = m.sts[0]
  m.sts.append(ShapeTracker.from_shape(m.sts[0].shape))
  for _ in range(4): random.choice(shapetracker_ops)(m)
  st_sum = backup + m.sts[1]
  return m.sts[0], st_sum

# shrink and expand aren't invertible, and stride is only invertible in the flip case
# do_pad and do_flip were removed because simplify isn't good
invertible_shapetracker_ops = [do_permute, do_reshape_split_one, do_reshape_combine_two, do_flip, do_pad]

def fuzz_invert():
  start = ShapeTracker.from_shape((random.randint(1, 10), random.randint(1, 10), random.randint(1, 10)))
  m = MultiShapeTracker([start])
  for _ in range(8): random.choice(invertible_shapetracker_ops)(m)
  inv = m.sts[0].invert(start.shape)
  st_sum = (m.sts[0] + inv) if inv else None
  return start, st_sum

if __name__ == "__main__":
  # random.seed(42)
  total = getenv("CNT", 1000)
  for fuzz in [globals()[f'fuzz_{x}'] for x in getenv("FUZZ", "invert,plus").split(",")]:
    good = 0
    for _ in trange(total, desc=f"{fuzz}"):
      st1, st2 = fuzz()
      eq = st_equal(st1, st2)
      if DEBUG >= 1:
        print(f"EXP: {st1}")
        print(f"GOT: {st2}")
        print(colored("****", "green" if eq else "red"))
      if not eq: exit(0)
    print(f"hit {good}/{total}")
    assert good == total
