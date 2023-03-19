from typing import Final, Dict, Callable
import math
from tinygrad.codegen.linearizer import Linearizer, UOps
from tinygrad.ops import ASTRunner, Op, UnaryOps, BinaryOps
from tinygrad.helpers import prod, getenv, DEBUG
from tinygrad.runtime.lib import RawConst
from tinygrad.shape.symbolic import DivNode, AndNode, render_python, NumNode

# div is different in cl than python
render_cl = render_python.copy()
render_cl[DivNode] = lambda self,ops,ctx: f"({self.a.render(ops, ctx)}/{self.b})"
render_cl[AndNode] = lambda self,ops,ctx: f"({'&&'.join(sorted([x.render(ops,ctx) for x in self.nodes]))})"

NATIVE_EXPLOG = getenv("NATIVE_EXPLOG", 0)  # this is needed as a switch for the tests to pass

class CStyleCodegen(Linearizer):
  code_for_op: Final[Dict[Op, Callable]] = {
    UnaryOps.EXP: lambda x: f"native_exp({x})" if NATIVE_EXPLOG else f"exp({x})",
    UnaryOps.LOG: lambda x: f"native_log({x})" if NATIVE_EXPLOG else f"log({x})",
    BinaryOps.ADD: lambda a,b: f"({a}+{b})", BinaryOps.SUB: lambda a,b: f"({a}-{b})",
    BinaryOps.MUL: lambda a,b: f"({a}*{b})", BinaryOps.DIV: lambda a,b: f"({a}/{b})",
    BinaryOps.POW: lambda a,b: f"pow({a},{b})", BinaryOps.MAX: lambda a,b: f"max({a},{b})",
    BinaryOps.CMPEQ: lambda a,b: f"({a}=={b})",
  }

  def codegen(self):
    self.process()
    self.hand_coded_optimizations()
    self.linearize()

    kernel = []
    global_size = []

    for uop,newvar,args in self.uops:
      if uop == UOps.LOOP:
        for i,var in enumerate(args[0]):
          if isinstance(var, NumNode):
            # one number, not an index
            kernel.append("{")
          else:
            if args[1] == "global" and self.lang.gid:
              kernel.append(f"int {var.expr} = {self.lang.gid[len(args[0])-1-i]}; {{")
              global_size.append(var.max+1)
            else:
              kernel.append(f"for (int {var.expr} = {var.min}; {var.expr} <= {var.max}; ++{var.expr}) {{")
      if uop == UOps.ENDLOOP:
        kernel.append("}"*len(args[0]))
      if uop == UOps.CONST:
        if args[0] == -math.inf:
          kernel.append(f"float {newvar} = -INFINITY;")
        else:
          kernel.append(f"float {newvar} = {args[0]}f;")
      if uop == UOps.ALU:
        if newvar is None:
          kernel.append(f"{args[2]} = {self.code_for_op[args[0]](*args[1])};")
        else:
          kernel.append(f"float {newvar} = {self.code_for_op[args[0]](*args[1])};")
      if uop == UOps.LOAD:
        # NOTE: if min and max are both 0, it should be a CONST in the Linearizer
        if args[2].min == 1:
          kernel.append(f"float {newvar} = {args[0]}[{args[1].render(render_cl)}];")
        else:
          kernel.append(f"float {newvar} = ({args[2].render(render_cl)}) ? ({args[0]}[{args[1].render(render_cl)}]) : 0.0f;")
      if uop == UOps.STORE:
        kernel.append(f"{args[0]}[{args[1].render(render_cl)}] = {args[3]};")
      if uop == UOps.DEFINE_LOCAL:
        kernel.append(f"float {args[0]}[{args[1]}];")

    buftypes = [(i,f"{'read_only' if i > 0 else 'write_only'} image2d_t" if x.dtype.name.startswith('image') else self.lang.buffer_prefix+"float*"+self.lang.buffer_suffix) for i,x in enumerate(self.bufs) if x is not None and not isinstance(x.realized, RawConst)]
    prg = ''.join([f"{self.lang.kernel_prefix} void KERNEL_NAME_PLACEHOLDER(",] +
      [', '.join([f'{t} data{i}' for i,t in buftypes] + self.lang.extra_args)] +
      [") {\n"] + ['\n'.join(kernel), "}"])

    if DEBUG >= 3:
      print(prg)

    function_name = "exec"
    return ASTRunner(function_name, prg.replace("KERNEL_NAME_PLACEHOLDER", function_name),
      global_size[::-1], None,
      op_estimate=self.info.flops,
      mem_estimate=sum(x.dtype.itemsize*(x.realized.size if x.realized is not None else prod(x.shape)) for x in self.bufs if x is not None))