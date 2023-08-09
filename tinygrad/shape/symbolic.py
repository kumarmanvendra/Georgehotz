from __future__ import annotations
from abc import abstractmethod
import functools
from math import gcd
from tinygrad.helpers import partition, GlobalCounters
from typing import List, Dict, Callable, Tuple, Type, Union, Optional, Any

# NOTE: Python has different behavior for negative mod and floor div than c
# symbolic matches the Python behavior, but the code output is agnostic, and will never have negative numbers in div or mod

def is_sym_int(x: Any) -> bool: return isinstance(x, (int, Node))
def sym_vars(x: Union[Node, int]) -> List[Variable]: return [] if isinstance(x, int) else x.vars()

def sym_infer(expr) -> int:
  if isinstance(expr, int): return expr
  local_vars = {k.expr: v for k,v in GlobalCounters.var_vals.items()}
  exec("INFERRED="+render_python[type(expr)](expr, ops=None, ctx=None), None, local_vars)  # pylint: disable=exec-used
  return local_vars["INFERRED"]

class Node:
  b: Union[Node, int]
  min: int
  max: int
  def render(self, ops=None, ctx=None, strip_parens=False) -> str:
    if ops is None: ops = render_python
    assert self.__class__ in (Variable, NumNode) or self.min != self.max
    ret = ops[type(self)](self, ops, ctx)
    if strip_parens and ret[0] == '(' and ret[-1] == ')': ret = ret[1:-1]
    return ret
  def vars(self): return []
  @functools.cached_property
  def key(self) -> str: return self.render(ctx="DEBUG")
  @functools.cached_property
  def hash(self) -> int: return hash(self.key)
  def __repr__(self): return "<"+self.key+">"
  def __hash__(self): return self.hash
  def __bool__(self): return not (self.max == self.min == 0)
  def __eq__(self, other:object) -> bool:
    if not isinstance(other, Node): return NotImplemented
    return self.key == other.key
  def __neg__(self): return self*-1
  def __add__(self, b:Union[Node,int]): return Variable.sum([self, b if isinstance(b, Node) else Variable.num(b)])
  def __radd__(self, b:int): return self+b
  def __sub__(self, b:Union[Node,int]): return self+-b
  def __le__(self, b:Union[Node,int]): return self < (b+1)
  def __gt__(self, b:Union[Node,int]): return (-self) < (-b)
  def __ge__(self, b:Union[Node,int]): return (-self) < (-b+1)
  def __lt__(self, b:Union[Node,int]):
    if self == b: return NumNode(0)
    lhs = self
    if isinstance(lhs, SumNode):
      muls, others = partition(lhs.nodes, lambda x: isinstance(x, MulNode) and x.b > 0 and x.max >= b)
      if len(muls):
        # NOTE: gcd in python 3.8 takes exactly 2 args
        mul_gcd = muls[0].b
        for x in muls[1:]: mul_gcd = gcd(mul_gcd, x.b)
        if b%mul_gcd == 0:
          all_others = Variable.sum(others)
          #print(mul_gcd, muls, all_others)
          if all_others.min >= 0 and all_others.max < mul_gcd:
            # TODO: should we divide both by mul_gcd here?
            lhs = Variable.sum(muls)
    return create_node(LtNode(lhs, b))
  def __mul__(self, b:Union[Node, int]):
    if b == 0: return NumNode(0)
    if b == 1: return self
    return create_node(MulNode(self, b))
  def __rmul__(self, b:int): return self*b

  # *** complex ops ***

  def __rfloordiv__(self, b:int): raise RuntimeError(f"not supported: {b} // {self}")
  def __floordiv__(self, b:Union[Node,int], factoring_allowed=True):
    if isinstance(b, Node):
      if b - self.max > 0: return NumNode(0)
      raise RuntimeError(f"not supported: {self} // {b}")
    assert b != 0
    if b < 0: return (self//-b)*-1
    if b == 1: return self

    # the numerator of div is not allowed to be negative
    if self.min < 0:
      offset = self.min//b
      # factor out an "offset" to make the numerator positive. don't allowing factoring again
      return (self + -offset*b).__floordiv__(b, factoring_allowed=False) + offset
    return create_node(DivNode(self, b))

  def __rmod__(self, b:int): raise RuntimeError(f"not supported: {b} % {self}")
  def __mod__(self, b:Union[Node,int]):
    if isinstance(b, Node):
      if b - self.max > 0: return self
      if self - b == 0: return NumNode(0)
      raise RuntimeError(f"not supported: {self} % {b}")
    assert b > 0
    if b == 1: return NumNode(0)
    if self.min >= 0 and self.max < b: return self
    if self.min < 0: return (self - ((self.min//b)*b)) % b
    return create_node(ModNode(self, b))

  @staticmethod
  def num(num:int) -> NumNode: return NumNode(num)

  @staticmethod
  def factorize(nodes:List[Node]) -> List[Node]:
    mul_groups: Dict[Node, int] = {}
    for x in nodes:
      a,b = (x.a,x.b) if isinstance(x, MulNode) else (x,1)
      mul_groups[a] = mul_groups.get(a, 0) + b
    return [MulNode(a, b_sum) if b_sum != 1 else a for a, b_sum in mul_groups.items() if b_sum != 0]

  @staticmethod
  def sum(nodes:List[Node]) -> Node:
    nodes = [x for x in nodes if x.max or x.min]
    if not nodes: return NumNode(0)
    if len(nodes) == 1: return nodes[0]

    new_nodes: List[Node] = []
    num_node_sum = 0
    for node in SumNode(nodes).flat_components:
      if node.__class__ is NumNode: num_node_sum += node.b
      else: new_nodes.append(node)

    if len(new_nodes) > 1 and len(set([x.a if isinstance(x, MulNode) else x for x in new_nodes])) < len(new_nodes):
      new_nodes = Node.factorize(new_nodes)
    if num_node_sum: new_nodes.append(NumNode(num_node_sum))
    return create_rednode(SumNode, new_nodes) if len(new_nodes) > 1 else new_nodes[0] if len(new_nodes) == 1 else NumNode(0)

  @staticmethod
  def ands(nodes:List[Node]) -> Node:
    if not nodes: return NumNode(1)
    if len(nodes) == 1: return nodes[0]
    if any(not x for x in nodes): return NumNode(0)

    # filter 1s
    nodes = [x for x in nodes if x.min != x.max]
    return create_rednode(AndNode, nodes) if len(nodes) > 1 else (nodes[0] if len(nodes) == 1 else NumNode(1))

# 4 basic node types

class Variable(Node):
  def __new__(cls, expr:Optional[str], nmin:int, nmax:int):
    assert nmin >= 0 and nmin <= nmax
    if nmin == nmax: return NumNode(nmin)
    return super().__new__(cls)

  def __init__(self, expr:Optional[str], nmin:int, nmax:int):
    self.expr, self.min, self.max, self.set = expr, nmin, nmax, False
  def vars(self): return [self]

class NumNode(Node):
  def __init__(self, num:int):
    self.b, self.min, self.max = num, num, num
  def __int__(self): return self.b
  def __eq__(self, other): return self.b == other
  def __hash__(self): return self.hash  # needed with __eq__ override

def create_node(ret:Node):
  assert ret.min <= ret.max, f"min greater than max! {ret.min} {ret.max} when creating {type(ret)} {ret}"
  if ret.min == ret.max: return NumNode(ret.min) if isinstance(ret.min, int) else ret.min
  return ret

class OpNode(Node):
  def __init__(self, a:Node, b:Union[Node, int]):
    self.a, self.b = a, b
    self.min, self.max = self.get_bounds()
  def vars(self): return self.a.vars() + (self.b.vars() if isinstance(self.b, Node) else [])
  @abstractmethod
  def get_bounds(self) -> Tuple[int, int]: pass

class LtNode(OpNode):
  def __mul__(self, b: Union[Node, int]): return (self.a*b) < (self.b*b)
  def __floordiv__(self, b: Union[Node, int], _=False):
    assert isinstance(b, int), f"not supported: {self} // {b}"
    return (self.a//b) < (self.b//b)
  def get_bounds(self) -> Tuple[int, int]:
    if isinstance(self.b, int): return int(self.a.max < self.b), int(self.a.min < self.b)
    return (1, 1) if self.a.max < self.b.min else (0, 0) if self.a.min > self.b.max else (0, 1)

class MulNode(OpNode):
  def __mul__(self, b: Union[Node, int]): return self.a*(self.b*b) # two muls in one mul
  def __floordiv__(self, b: Union[Node, int], factoring_allowed=False): # NOTE: mod negative isn't handled right
    if isinstance(b, Node):
      assert isinstance(self.b, Node), f"not supported: {self} // {b}"
      if b - self.b == 0: return self.a
      if b - self.a == 0: return self.b
      if isinstance(self.b, MulNode):
        if b - self.b.b == 0: return create_node(MulNode(self.a, self.b.a))
        if b - self.b.a == 0: return create_node(MulNode(self.a, self.b.b))
      elif isinstance(self.b, SumNode):
        if self.b % b == 0: return create_node(MulNode(self.a, self.b // b))
      raise RuntimeError(f"not supported: {self} // {b}")
    if self.b % b == 0: return self.a*(self.b//b)
    if b % self.b == 0 and self.b > 0: return self.a//(b//self.b)
    return Node.__floordiv__(self, b, factoring_allowed)
  def __mod__(self, b: Union[Node, int]):
    if isinstance(b, Node):
      if b - self.b == 0 or b - self.a == 0 or b - self.a*self.b == 0: return NumNode(0)
      if isinstance(self.b, MulNode):
        if b - self.b.b == 0 or b - self.b.a == 0: return NumNode(0)
      elif isinstance(self.b, SumNode):
        if self.b % b == 0: return NumNode(0)
      if b - self.max > 0: return self
      raise RuntimeError(f"not supported: {self} % {b}")
    a = (self.a * (self.b%b))
    return Node.__mod__(a, b)
  def get_bounds(self) -> Tuple[int, int]:
    return (self.a.min*self.b, self.a.max*self.b) if self.b >= 0 else (self.a.max*self.b, self.a.min*self.b)

class DivNode(OpNode):
  def __floordiv__(self, b: Union[Node, int], _=False):
    assert isinstance(b, int), f"not supported: {self} // {b}"
    return self.a//(self.b*b) # two divs is one div
  def get_bounds(self) -> Tuple[int, int]:
    assert self.a.min >= 0 and isinstance(self.b, int)
    return self.a.min//self.b, self.a.max//self.b

class ModNode(OpNode):
  def __floordiv__(self, b: Union[Node, int], factoring_allowed=True):
    assert isinstance(b, int), f"not supported: {self} // {b}"
    if (self.b % b == 0): return (self.a//b) % (self.b//b) # put the div inside mod
    return Node.__floordiv__(self, b, factoring_allowed)
  def get_bounds(self) -> Tuple[int, int]:
    assert self.a.min >= 0 and isinstance(self.b, int)
    return (0, self.b-1) if self.a.max - self.a.min >= self.b or (self.a.min != self.a.max and self.a.min%self.b >= self.a.max%self.b) else (self.a.min%self.b, self.a.max%self.b)

class RedNode(Node):
  def __init__(self, nodes:List[Node]): self.nodes = nodes
  def vars(self): return functools.reduce(lambda l,x: l+x.vars(), self.nodes, [])

class SumNode(RedNode):
  def __mul__(self, b: Union[Node, int]): return Node.sum([x*b for x in self.nodes]) # distribute mul into sum
  def __floordiv__(self, b: Union[Node, int], factoring_allowed=True):
    fully_divided: List[Node] = []
    rest: List[Node] = []
    if isinstance(b, SumNode):
      numerator_num = sum([node.b for node in self.flat_components if node.__class__ is NumNode])
      denominator_num = sum([node.b for node in b.flat_components if node.__class__ is NumNode])
      if numerator_num and denominator_num and numerator_num % denominator_num == 0:
        attempt = numerator_num // denominator_num
        if b * attempt - self == 0: return NumNode(attempt)
    elif isinstance(b, MulNode) and isinstance(b.b, int):  # try to divide b.b if it's int
      fully_divided, rest = [], []
      for x in self.flat_components:
        if x % b.b == 0: fully_divided.append(x // b.b)
        else: rest.append(x)
      rest_sum = create_rednode(SumNode, rest)
      if b - rest_sum.max > 0: return create_rednode(SumNode, fully_divided) // b.a
    if isinstance(b, Node):  # try to divide the whole b
      fully_divided, rest = [], []
      for x in self.flat_components:
        try:
          if x % b == 0: fully_divided.append(x // b)
          else: rest.append(x)
        except RuntimeError:
          rest.append(x)
      rest_sum = create_rednode(SumNode, rest)
      if b - rest_sum.max > 0: return create_rednode(SumNode, fully_divided)
      raise RuntimeError(f"not supported: {self} // {b}")
    if b == 1: return self
    if not factoring_allowed: return Node.__floordiv__(self, b, factoring_allowed)
    fully_divided, rest = [], []
    _gcd = b
    divisor = 1
    for x in self.flat_components:
      if x.__class__ in (NumNode, MulNode):
        if x.b%b == 0: fully_divided.append(x//b)
        else:
          rest.append(x)
          _gcd = gcd(_gcd, x.b)
          if x.__class__ == MulNode and divisor == 1 and b%x.b == 0: divisor = x.b
      else:
        rest.append(x)
        _gcd = 1
    if _gcd > 1: return Node.sum(fully_divided) + Node.sum(rest).__floordiv__(_gcd) // (b//_gcd)
    if divisor > 1: return Node.sum(fully_divided) + Node.sum(rest).__floordiv__(divisor) // (b//divisor)
    return Node.sum(fully_divided) + Node.__floordiv__(Node.sum(rest), b)

  def __mod__(self, b: Union[Node, int]):
    if isinstance(b, SumNode):
      numerator_num = sum([node.b for node in self.flat_components if node.__class__ is NumNode])
      denominator_num = sum([node.b for node in b.flat_components if node.__class__ is NumNode])
      if numerator_num and denominator_num and numerator_num % denominator_num == 0:
        attempt = numerator_num // denominator_num
        if b * attempt - self == 0: return NumNode(0)
    if isinstance(b, Node):
      indivisible = []
      for node in self.nodes:
        try:
          if node % b != 0: indivisible.append(node)
        except RuntimeError:
          indivisible.append(node)
      sum_indivisible = create_rednode(SumNode, indivisible)
      if b - sum_indivisible.max > 0: return sum_indivisible
      raise RuntimeError(f"not supported: {self} % {b}")
    new_nodes: List[Node] = []
    for x in self.nodes:
      if x.__class__ is NumNode: new_nodes.append(Variable.num(x.b%b))
      elif isinstance(x, MulNode): new_nodes.append(x.a * (x.b%b))
      else: new_nodes.append(x)
    return Node.__mod__(Node.sum(new_nodes), b)

  @property
  def flat_components(self): # recursively expand sumnode components
    new_nodes = []
    for x in self.nodes: new_nodes += (x.flat_components if isinstance(x, SumNode) else [x])
    return new_nodes

class AndNode(RedNode):
  def __mul__(self, b: Union[Node, int]): Variable.ands([x*b for x in self.nodes])
  def __floordiv__(self, b: Union[Node, int], _=True): return Variable.ands([x//b for x in self.nodes])

def create_rednode(typ:Type[RedNode], nodes:List[Node]):
  ret = typ(nodes)
  if typ == SumNode: ret.min, ret.max = (sum([x.min for x in nodes]), sum([x.max for x in nodes]))
  elif typ == AndNode: ret.min, ret.max = (min([x.min for x in nodes]), max([x.max for x in nodes]))
  return create_node(ret)

@functools.lru_cache(maxsize=None)
def sym_rename(s) -> str: return f"s{sym_rename.cache_info().currsize}"
def sym_render(a: Union[Node, int], ops=None, ctx=None) -> str: return str(a) if isinstance(a, int) else a.render(ops, ctx)

render_python: Dict[Type, Callable] = {
  Variable: lambda self,ops,ctx: f"{self.expr}[{self.min}-{self.max}]" if ctx == "DEBUG" else f"{self.expr}",
  NumNode: lambda self,ops,ctx: f"{self.b}",
  MulNode: lambda self,ops,ctx: f"({self.a.render(ops,ctx)}*{sym_render(self.b)})",
  DivNode: lambda self,ops,ctx: f"({self.a.render(ops,ctx)}//{self.b})",
  ModNode: lambda self,ops,ctx: f"({self.a.render(ops,ctx)}%{self.b})",
  LtNode: lambda self,ops,ctx: f"({self.a.render(ops,ctx)}<{sym_render(self.b)})",
  SumNode: lambda self,ops,ctx: f"({'+'.join(sorted([x.render(ops,ctx) for x in self.nodes]))})",
  AndNode: lambda self,ops,ctx: f"({' and '.join(sorted([x.render(ops,ctx) for x in self.nodes]))})"
}