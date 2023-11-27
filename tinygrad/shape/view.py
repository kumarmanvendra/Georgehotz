from __future__ import annotations
import functools, operator
from dataclasses import dataclass
from typing import Tuple, List, Optional, Dict, cast
from tinygrad.helpers import prod, all_int
from tinygrad.shape.symbolic import Node, NumNode, Variable, VariableOrNum, Set, sint

@functools.lru_cache(maxsize=None)
def filter_strides(shape:Tuple[int, ...], strides:Tuple[int, ...]) -> Tuple[int, ...]:
  return tuple(stride if shp != 1 else 0 for stride, shp in zip(strides, shape))

@functools.lru_cache(maxsize=None)
def strides_for_shape(shape:Tuple[int, ...]) -> Tuple[int, ...]:
  strides = [1] if shape else []
  for d in reversed(shape[1:]): strides.append(d*strides[-1])
  return filter_strides(shape, tuple(reversed(strides)))

@functools.lru_cache(maxsize=None)
def to_shape_strides(shape:Tuple[int, ...], strides:Tuple[int, ...], mask:Optional[Tuple[Tuple[int, int], ...]] = None) -> Tuple[Tuple[int, int, int], ...]:
  assert len(shape) == len(strides) 
  # state (0, 1, 2) -> (not started, in progress, done). This is wrt merging of dim with zero stride & range one mask.
  state = 1 if mask and strides[0] == 0 and mask[0][1] - mask[0][0] == 1 and shape[0] != 1 else 0
  ret = [(shape[0], strides[0], shape[0] if strides[0] else 0)] if shape else [] # third dim of ret[i] represents accumulated dim w/o zero stride.
  for i in range(1, len(shape)):
    if shape[i] == 1: continue
    if state == 1 or ret[-1][1] == shape[i] * strides[i]: 
      ret[-1] = (ret[-1][0] * shape[i], strides[i], (shape[i] if state == 1 else ret[-1][2] * shape[i]) if strides[i] else 0)
    else: ret.append((shape[i], strides[i], shape[i] if strides[i] else 0))
    state = 1 if mask and strides[i] == 0 and mask[i][1] - mask[i][0] == 1 else (2 if state != 0 else 0)      
  return tuple(ret)

@functools.lru_cache(maxsize=None)
def _reshape_mask(view: View, new_shape:Tuple[sint, ...]) -> Tuple[Optional[Tuple[Tuple[sint, sint], ...]], Optional[Tuple[sint, ...]], bool]:
  if view.mask is None: return view.mask, tuple(), False
  new_mask: List[Tuple[int, int]] = []
  r_masks, r_shape, r_new_shape = reversed(view.mask), reversed(view.shape), reversed(new_shape)
  # stride represents the curr stride while accumulating. offset is needed while combining masks of range one & zero stride
  # View(shape=(10, 10), strides=(0, 1), offset=0, mask=((3, 4), (1, 5)), contiguous=False).reshape((100,))
  stride, off, offsets, old_dim, new_dim, mask = 1, 0, [], next(r_shape, 1), next(r_new_shape, 1), next(r_masks, (0,1))
  while len(new_mask) < len(new_shape):
    if mask[1] - mask[0] < 1: return ((0, 0),) * len(new_shape), tuple(), False
    if old_dim == new_dim * stride: 
      new_mask.append((mask[0] // stride, (mask[1] - 1) // stride + 1))
      offsets.append(off)
      stride, off, old_dim, new_dim, mask = 1, 0, next(r_shape, 1), next(r_new_shape, 1), next(r_masks, (0,1))
    elif old_dim > new_dim: # split the mask if reshape doesn't cut across mask.
      next_stride = new_dim * stride
      if (mask[0] % next_stride != 0 or mask[1] % next_stride != 0) and mask[0] // next_stride != (mask[1] - 1) // next_stride: return view.mask, tuple(), True
      new_mask.append((mask[0] % next_stride // stride, (mask[1] - 1) % next_stride // stride + 1))
      offsets.append(off)
      stride, new_dim = next_stride, next(r_new_shape, 1) # maintain stride for splitting the remaining mask
    elif old_dim < new_dim * stride: # combine if the mask can unfold continuously
      next_mask = next(r_masks, (0, 1)) 
      if (mask[0] != 0 or mask[1] != old_dim) and next_mask[1] - next_mask[0] != 1: return view.mask, tuple(), True
      if (next_mask[1] - next_mask[0] == 1 and next_mask[0]) and not (mask[1] - mask[0] == 1 and not mask[0]): off += next_mask[0] * old_dim
      mask, old_dim = (next_mask[0] * old_dim + mask[0], (next_mask[1] - 1) * old_dim + mask[1]), old_dim * next(r_shape, 1) 
  for mask in r_masks: 
    if mask != (0, 1): return ((0, 0),) * len(new_shape), tuple(), False
  return tuple(reversed(new_mask)), tuple(offsets), False

@dataclass(frozen=True)
class View:
  shape:Tuple[sint, ...]
  strides:Tuple[sint, ...]
  offset:sint
  mask:Optional[Tuple[Tuple[sint, sint], ...]]
  contiguous:bool

  @staticmethod
  @functools.lru_cache(maxsize=None)
  def create(shape:Tuple[sint, ...], strides:Optional[Tuple[sint, ...]]=None, offset:sint=0, mask:Optional[Tuple[Tuple[sint, sint], ...]]=None):
    strides = filter_strides(shape, strides) if strides else strides_for_shape(shape)
    contiguous = offset == 0 and mask is None and strides == strides_for_shape(shape)
    return View(shape, strides, offset, mask, contiguous)

  def vars(self) -> Set[Variable]:
    flatten_mask = tuple(x for m in self.mask for x in m) if self.mask is not None else tuple()
    return functools.reduce(operator.or_, [x.vars() for x in self.shape+self.strides+(self.offset,)+flatten_mask if isinstance(x, Node)], set())

  def unbind(self) -> View:
    unbound_vars:Dict[VariableOrNum,Node] = {v: v.unbind()[0] for v in self.vars() if v.val is not None}
    new_shape = tuple([s if isinstance(s, int) else s.substitute(unbound_vars) for s in self.shape])
    new_strides = tuple([s if isinstance(s, int) else s.substitute(unbound_vars) for s in self.strides])
    new_offset = self.offset if isinstance(self.offset, int) else self.offset.substitute(unbound_vars)
    new_mask = tuple((a if isinstance(a, int) else a.substitute(unbound_vars), b if isinstance(b, int) else b.substitute(unbound_vars)) for (a, b) in self.mask) if self.mask is not None else None
    return View.create(new_shape, new_strides, new_offset, new_mask)

  # MovementOps live here now

  def __unsafe_resize(self, arg: Tuple[Tuple[sint, sint], ...], mask=None) -> View:
    offset = sum([s * x[0] for s, x in zip(self.strides,arg)])
    if self.mask:
      # move the old mask
      nmask = tuple([(max(mx-ax, 0), min(my-ax, ay-ax)) for (mx,my),(ax,ay) in zip(self.mask, arg)])
      # merge the masks if we have two
      mask = tuple([(max(mx1, mx2), min(my1, my2)) for (mx1, my1), (mx2, my2) in zip(nmask, mask)]) if mask is not None else nmask
    shape = [y-x for x,y in arg]
    return View.create(tuple(s.b if isinstance(s, NumNode) else s for s in shape), self.strides, self.offset+offset, mask)

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def pad(self, arg: Tuple[Tuple[int, int], ...]) -> View:
    assert all((b>=0 and e>=0) for b,e in arg) and len(arg) == len(self.shape)
    if any(b or e for b, e in arg):
      zvarg = tuple([(-b,s+e) for s,(b,e) in zip(self.shape, arg)])
      mask = tuple([(b,s+b) for s,(b,_) in zip(self.shape, arg)])
      return self.__unsafe_resize(zvarg, mask=mask)
    return self

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def shrink(self, arg: Tuple[Tuple[sint, sint], ...]) -> View:
    assert all((b>=0 and e<=s) for s,(b,e) in zip(self.shape,arg)) and len(arg) == len(self.shape)
    return self.__unsafe_resize(arg)

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def expand(self, new_shape: Tuple[sint, ...]) -> View:
    if len(new_shape) != len(self.shape): raise ValueError(f"expand arg {new_shape=} must have same number of dimensions as shape {self.shape=}")
    if 0 in self.shape:
      assert all((s == x == 0) or (s > 0 and (x % s) == 0) for s,x in zip(self.shape, new_shape)), f"can't expand {self.shape} into {new_shape}"
      return View.create(new_shape)
    assert all((s == x or (s == 1 and st == 0)) for s,x,st in zip(self.shape, new_shape, self.strides)), f"can't expand {self.shape} into {new_shape}"
    # NOTE: can the mask ever be (0,0)?
    mask = tuple([(((0,0) if m != (0,1) else (0,ns)) if s != ns else m) for m,s,ns in zip(self.mask, self.shape, new_shape)]) if self.mask else None
    return View.create(new_shape, self.strides, self.offset, mask)

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def permute(self, axis: Tuple[int, ...]) -> View:
    assert all(isinstance(x, int) and x >= 0 and x < len(self.shape) for x in axis), f"invalid permute {axis} for {self.shape}"
    assert len(set(axis)) == len(axis) and len(axis) == len(self.shape), f"can't permute {self.shape} with {axis}"
    return View.create(tuple([self.shape[a] for a in axis]), tuple([self.strides[a] for a in axis]), self.offset, tuple([self.mask[a] for a in axis]) if self.mask is not None else None)

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def stride(self, mul: Tuple[int, ...]) -> View:
    # except for the negative case, you can build this from the others. invertible in the negative case
    assert all(isinstance(x, int) and x != 0 for x in mul), f"invalid stride {mul} for {self.shape}"
    strides = tuple([z*m for z,m in zip(self.strides, mul)])
    new_shape = tuple([(s+(abs(m)-1))//abs(m) for s,m in zip(self.shape, mul)])
    offset = sum([(s-1)*z for s,z,m in zip(self.shape, self.strides, mul) if m < 0])
    mask = tuple([(((mx if m > 0 else s-my)+(abs(m)-1))//abs(m), ((my if m > 0 else s-mx)+(abs(m)-1))//abs(m)) for (mx,my),s,m in zip(self.mask, self.shape, mul)]) if self.mask is not None else None
    return View.create(new_shape, strides, self.offset + offset, mask)

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def reshape(self, new_shape: Tuple[sint, ...]) -> Optional[View]:
    if self.shape == new_shape: return self

    assert all(x >= 0 for x in new_shape), f"shape can't contain negative numbers {new_shape}"
    if 0 in self.shape:
      assert 0 in new_shape, f"cannot reshape 0 size to {new_shape}"
      return View.create(new_shape)
    # check for the same size
    if all_int(self.shape):
      assert all(isinstance(s, (int, Variable)) for s in new_shape), f"{self.shape=} -> {new_shape=} contains non (int, Variable) dim"
      if prod(self.shape) != prod([s if isinstance(s, int) else cast(Variable,s).val for s in new_shape]): raise ValueError(f"size mismatched, can't reshape {self.shape=} -> {new_shape=}")

    # after the asserts, it's okay to check contiguous
    if self.contiguous: return View.create(new_shape)

    strides, reverse_shape = [], reversed(new_shape)
    for d, s, real_dim in reversed(to_shape_strides(self.shape, self.strides, self.mask)):
      acc, new_stride, equal = 1, s, False
      while acc <= d and not equal:
        try: new_dim = next(reverse_shape)
        except StopIteration: break
        strides.append(new_stride) if new_dim != 1 else strides.append(0)
        if new_dim == 1: continue
        acc *= new_dim
        new_stride = new_stride * new_dim if acc < real_dim else 0
        if acc == d: equal = True
      if not equal and d != 1: break
    else:
      strides += [0,] * (len(new_shape) - len(strides))
      mask, off_mask, extra = _reshape_mask(self, new_shape)
      total_offset = sum([off * s for off, s in zip(off_mask, strides)]) if off_mask else 0
      if not extra: return View.create(new_shape, tuple(reversed(strides)), self.offset - total_offset, mask)

    return None
