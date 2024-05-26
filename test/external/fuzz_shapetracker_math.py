import random
from typing import Tuple
from tqdm import trange
from tinygrad.helpers import getenv, DEBUG, colored
from tinygrad.shape.shapetracker import ShapeTracker
from test.external.mergeable import simplify2
from test.external.fuzz_shapetracker import shapetracker_ops
from test.external.fuzz_shapetracker import do_permute, do_reshape_split_one, do_reshape_combine_two, do_flip, do_pad
from test.unit.test_shapetracker_math import st_equal, MultiShapeTracker

def fuzz_plus() -> Tuple[ShapeTracker, ShapeTracker]:
  m = MultiShapeTracker([ShapeTracker.from_shape((random.randint(1, 10), random.randint(1, 10), random.randint(1, 10)))])
  for _ in range(4): random.choice(shapetracker_ops)(m)
  backup = m.sts[0]
  m.sts.append(ShapeTracker.from_shape(m.sts[0].shape))
  for _ in range(4): random.choice(shapetracker_ops)(m)
  st_sum = backup + m.sts[1]
  return m.sts[0], st_sum

# shrink and expand aren't invertible, and stride is only invertible in the flip case
invertible_shapetracker_ops = [do_permute, do_reshape_split_one, do_reshape_combine_two, do_flip, do_pad]

def fuzz_invert() -> Tuple[ShapeTracker, ShapeTracker]:
  start = ShapeTracker.from_shape((random.randint(1, 10), random.randint(1, 10), random.randint(1, 10)))
  m = MultiShapeTracker([start])
  for _ in range(8): random.choice(invertible_shapetracker_ops)(m)
  inv = m.sts[0].invert(start.shape)
  st_sum = (m.sts[0] + inv) if inv else None
  return start, st_sum

if __name__ == "__main__":
  if seed:=getenv("SEED"): random.seed(seed)
  total = getenv("CNT", 1000)
  for fuzz in [globals()[f'fuzz_{x}'] for x in getenv("FUZZ", "invert,plus").split(",")]:
    same_but_neq = 0
    ref_win = 0
    ref_loss = 0
    multiview = 0
    for _ in trange(total, desc=f"{fuzz}"):
      st1, st2 = fuzz()
      eq = st_equal(st1, st2)
      if getenv("CHECK_NEQ") and eq and st1.simplify() != st2.simplify():
        print(colored("same but unequal", "yellow"))
        print(st1.simplify())
        print(st2.simplify())
        same_but_neq += 1
      if getenv("CHECK_MV"):
        prod = st2.simplify()
        ref = simplify2(st2)
        assert(st_equal(prod, ref))
        if len(prod.views) > len(ref.views):
          print(colored("prod simplify worse than ref", "red"))
          print(f"prod = {prod}")
          print(f"ref =  {ref}")
          ref_win += 1
        if len(prod.views) < len(ref.views):
          print(colored("prod simplify better than ref ", "green"))
          print(f"prod =  {prod}")
          print(f"ref = {ref}")
          ref_loss += 1
        if len(prod.views) > 1:
          multiview += 1
      if DEBUG >= 1:
        print(f"EXP: {st1}")
        print(f"GOT: {st2}")
        print(colored("****", "green" if eq else "red"))
      if not eq: exit(0)
    if getenv("CHECK_NEQ"): print(f"same but unequal {(same_but_neq/total)*100:.2f}%")
    if getenv("CHECK_MV"):
      print(f"prod worse than ref {(ref_win/multiview)*100:.2f}%")
      print(f"prod better than ref {(ref_loss/multiview)*100:.2f}%")
