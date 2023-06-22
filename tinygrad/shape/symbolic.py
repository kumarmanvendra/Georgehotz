from __future__ import annotations
from abc import abstractmethod
import functools
from math import gcd
from typing import List, Dict, Callable, Tuple, Type, Union

# NOTE: Python has different behavior for negative mod and floor div than c
# symbolic matches the Python behavior, but the code output is agnostic, and will never have negative numbers in div or mod

class Node:
  b: int
  min: int
  max: int
  def render(self, ops=None, ctx=None) -> str:
    if ops is None: ops = render_python
    assert self.__class__ in (Variable, NumNode) or self.min != self.max
    return ops[type(self)](self, ops, ctx)
  @functools.cached_property
  def key(self) -> str: return self.render(ctx="DEBUG")
  def __repr__(self): return "<"+self.key+">"
  def __hash__(self): return hash(self.__repr__())
  def __eq__(self, other:object) -> bool:
    if not isinstance(other, Node): return NotImplemented
    return self.key == other.key
  def __neg__(self): return self*-1
  def __add__(self, b:Union[Node, int]): return Variable.sum([self, b if isinstance(b, Node) else Variable.num(b)])
  def __sub__(self, b:Union[Node, int]): return self+-b
  def __ge__(self, b:int): return create_node(LtNode(-self, -b+1))
  def __lt__(self, b:int): return create_node(LtNode(self, b))
  def __mul__(self, b:int):
    if b == 0: return NumNode(0)
    elif b == 1: return self
    return create_node(MulNode(self, b))

  # *** complex ops ***

  def __floordiv__(self, b:int, factoring_allowed=True):

    if b < 0: return (self//-b)*-1
    if b == 1: return self

    # the numerator of div is not allowed to be negative
    if self.min < 0:
      offset = self.min//b
      # factor out an "offset" to make the numerator positive. don't allowing factoring again
      return (self + -offset*b).__floordiv__(b, factoring_allowed=False) + offset
    return create_node(DivNode(self, b))

  def __mod__(self, b:int):
    assert b > 0
    if b == 1: return NumNode(0)
    if self.min >= 0 and self.max < b: return self
    if self.min < 0: return (self - ((self.min//b)*b)) % b
    return create_node(ModNode(self, b))

  @staticmethod
  def num(num:int) -> Node: return NumNode(num)


  @staticmethod
  def factorize(nodes: List[Node]):
    mul_groups: Dict[Node, int] = {}
    for x in nodes:
      a,b = (x.a,x.b) if isinstance(x, MulNode) else (x,1)
      mul_groups[a] = mul_groups.get(a, 0) + b
    return [a * b_sum if b_sum != 1 else a for a, b_sum in mul_groups.items() if b_sum != 0]

  @staticmethod
  def sum(nodes:List[Node]) -> Node:
    if not nodes: return NumNode(0)
    if len(nodes) == 1: return nodes[0]

    new_nodes: List[Node] = []
    num_node_sum = 0

    # flatten all sumnodes and gather numnodes
    for node in nodes:
      if isinstance(node, SumNode):
        flat = node.flat_components_grouped_num  # last component is always numnode
        new_nodes += flat[:-1]
        num_node_sum += flat[-1].b
      elif node.__class__ is NumNode: 
        num_node_sum += node.b
      else: 
        new_nodes.append(node)

    if not new_nodes: return NumNode(num_node_sum)
    # factorize nodes, but only if duplicate a-nodes exist. Check before adding the NumNode
    if len(new_nodes) > 1 and len(set([x.a if isinstance(x, MulNode) else x for x in new_nodes])) < len(new_nodes): 

      if num_node_sum: new_nodes.append(NumNode(num_node_sum))
      new_nodes = Node.factorize(new_nodes)
    elif num_node_sum: new_nodes.append(NumNode(num_node_sum))

    return create_rednode(SumNode, new_nodes) if len(new_nodes) > 1 else new_nodes[0] if len(new_nodes) == 1 else NumNode(0)

  @staticmethod
  def ands(nodes:List[Node]) -> Node:
    if not nodes: return NumNode(1)
    if len(nodes) == 1: return nodes[0]
    if any([x.min == x.max == 0 for x in nodes]): return NumNode(0)

    # filter 1s
    nodes = [x for x in nodes if x.min != x.max]
    return create_rednode(AndNode, nodes) if len(nodes) > 1 else (nodes[0] if len(nodes) == 1 else NumNode(1))

# 4 basic node types

class Variable(Node):
  def __new__(cls, expr:str, nmin:int, nmax:int):
    assert nmin >= 0 and nmin <= nmax
    if nmin == nmax: return NumNode(nmin)
    return super().__new__(cls)

  def __init__(self, expr:str, nmin:int, nmax:int):
    self.expr, self.min, self.max = expr, nmin, nmax

class NumNode(Node):
  def __init__(self, num:int):
    self.b, self.min, self.max = num, num, num

def create_node(ret:Node):
  assert ret.min <= ret.max, f"min greater than max! {ret.min} {ret.max} when creating {type(ret)} {ret}"
  if ret.min == ret.max: return NumNode(ret.min)
  return ret

class OpNode(Node):
  def __init__(self, a:Node, b:int):
    self.a, self.b = a, b
    self.min, self.max = self.get_bounds()
  @abstractmethod
  def get_bounds(self) -> Tuple[int, int]: pass

class LtNode(OpNode):
  def __mul__(self, b: int): return (self.a*b) < (self.b*b)
  def __floordiv__(self, b: int, _=False): return (self.a//b) < (self.b//b)
  def get_bounds(self) -> Tuple[int, int]: return int(self.a.max < self.b), int(self.a.min < self.b)
class MulNode(OpNode):
  def __mul__(self, b: int): return self.a*(self.b*b) # two muls in one mul
  def __floordiv__(self, b: int, factoring_allowed=False): # NOTE: mod negative isn't handled right
    if self.b % b == 0: return self.a*(self.b//b)
    if b % self.b == 0 and self.b > 0: return self.a//(b//self.b)
    return Node.__floordiv__(self, b, factoring_allowed)
  def __mod__(self, b: int):
    a = (self.a * (self.b%b))
    return Node.__mod__(a, b)
  def get_bounds(self) -> Tuple[int, int]:
    return (self.a.min*self.b, self.a.max*self.b) if self.b >= 0 else (self.a.max*self.b, self.a.min*self.b)
class DivNode(OpNode):
  def __floordiv__(self, b: int, _=False): return self.a//(self.b*b) # two divs is one div
  def get_bounds(self) -> Tuple[int, int]:
    assert self.a.min >= 0
    return self.a.min//self.b, self.a.max//self.b
class ModNode(OpNode):
  def __floordiv__(self, b: int, factoring_allowed=True):
    if (self.b % b == 0): return (self.a//b) % (self.b//b) # put the div inside mod
    return Node.__floordiv__(self, b, factoring_allowed)
  def get_bounds(self) -> Tuple[int, int]:
    assert self.a.min >= 0
    return (0, self.b-1) if self.a.max - self.a.min >= self.b or (self.a.min != self.a.max and self.a.min%self.b >= self.a.max%self.b) else (self.a.min%self.b, self.a.max%self.b)

class RedNode(Node):
  def __init__(self, nodes:List[Node]): self.nodes = nodes

class SumNode(RedNode):
  def __mul__(self, b: int): return Node.sum([x*b for x in self.nodes]) # distribute mul into sum
  def __floordiv__(self, b: int, factoring_allowed=True): 
    if b == 1: return self
    if not factoring_allowed: return Node.__floordiv__(self, b, factoring_allowed)
    factors: List[Node] = []
    tmp_nofactor: List[Node] = []
    nofactor: List[Node] = []
    for x in self.flat_components: factors.append(x) if x.__class__ in (MulNode, NumNode) and x.b%b == 0 else nofactor.append(x)
    if len(factors) > 0:
      factor_term = []
      for x in factors:
        if x.__class__ is MulNode: factor_term.append(x.a if x.b//b == 1 else MulNode(x.a, x.b//b))
        elif x.__class__ is NumNode: factor_term.append(NumNode(x.b//b))

      mul, other = [],[]
      print("\n", factors, nofactor,b, "\n")
      for x in nofactor: mul.append(x) if x.__class__ is MulNode else other.append(x)
      if mul:
        gcds = [gcd(x.b, b) if  x.__class__ is MulNode else None for x in mul]
        t = min(gcds) if gcds and None not in gcds else 1
        # these don't have to be the same, just having a common factor
        if t > 1 and all([x%t == 0 for x in gcds]):
          nofactor_term = Node.sum([Node.sum([(x.a * (x.b//t)) for x in mul])//(b//t)] + Node.sum(other)//b if other else [])
        else:
          nofactor_term = Node.sum(nofactor)//b
      else:
        nofactor_term = Node.sum(nofactor)//b
      return Node.sum(factor_term + [nofactor_term])
    else:
      muls = [x.b for x in nofactor if x.__class__ is MulNode]
      for m in muls:
        if m > 1 and b%m == 0:
          return (self//m)//(b//m)
      return Node.__floordiv__(self, b, factoring_allowed)
  def __mod__(self, b: int):
    new_nodes = []
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
    
  @property
  def flat_components_grouped_num(self):
    nodes = []
    num_node_sum = 0
    for node in self.flat_components:
      if node.__class__ is NumNode: num_node_sum += node.b
      else: nodes.append(node)
    nodes.append(NumNode(num_node_sum))
    return nodes

class AndNode(RedNode):
  def __mul__(self, b: int): Variable.ands([x*b for x in self.nodes])
  def __floordiv__(self, b: int, _=True): return Variable.ands([x//b for x in self.nodes])

def create_rednode(typ:Type[RedNode], nodes:List[Node]):
  ret = typ(nodes)
  if typ == SumNode: ret.min, ret.max = (sum([x.min for x in nodes]), sum([x.max for x in nodes]))
  elif typ == AndNode: ret.min, ret.max = (min([x.min for x in nodes]), max([x.max for x in nodes]))
  return create_node(ret)

render_python: Dict[Type, Callable] = {
  Variable: lambda self,ops,ctx: f"{self.expr}[{self.min}-{self.max}]" if ctx == "DEBUG" else f"{self.expr}",
  NumNode: lambda self,ops,ctx: f"{self.b}",
  MulNode: lambda self,ops,ctx: f"({self.a.render(ops,ctx)}*{self.b})",
  DivNode: lambda self,ops,ctx: f"({self.a.render(ops,ctx)}//{self.b})",
  ModNode: lambda self,ops,ctx: f"({self.a.render(ops,ctx)}%{self.b})",
  LtNode: lambda self,ops,ctx: f"({self.a.render(ops,ctx)}<{self.b})",
  SumNode: lambda self,ops,ctx: f"({'+'.join(sorted([x.render(ops,ctx) for x in self.nodes]))})",
  AndNode: lambda self,ops,ctx: f"({' and '.join(sorted([x.render(ops,ctx) for x in self.nodes]))})"
}