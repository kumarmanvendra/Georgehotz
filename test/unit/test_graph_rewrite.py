import unittest, math
from tinygrad import dtypes
from tinygrad.ops import UOp, UOps, BinaryOps, exec_alu
from tinygrad.codegen.uopgraph import full_graph_rewrite

# Helper function to apply the graph rewrite
def apply_rewrite(expr):
  return full_graph_rewrite(expr.sink()).src[0]

def evaluate_uop(uop, variables):
  if uop.op == UOps.CONST:
    return uop.arg
  elif uop.op == UOps.DEFINE_VAR:
    var_name = uop.arg[0]
    return variables[var_name]
  elif uop.op == UOps.ALU:
    src_values = [evaluate_uop(src, variables) for src in uop.src]
    return exec_alu(uop.arg, uop.dtype, src_values)
  else:
    raise NotImplementedError(f"Unsupported UOp {uop.op}")

class TestArithmeticSimplifications(unittest.TestCase):
  def test_full_graph_rewrite_division_by_zero(self):
    optimized_div_uop = apply_rewrite(UOp.const(dtypes.float32, 10.0) / UOp.const(dtypes.float32, 0.0))
    self.assertEqual(optimized_div_uop.op, UOps.CONST)
    self.assertTrue(math.isinf(optimized_div_uop.arg) or math.isnan(optimized_div_uop.arg))

  def test_full_graph_rewrite_redundant_operations(self):
    optimized_uop = apply_rewrite((UOp.const(dtypes.float32, 10.0) + UOp.const(dtypes.float32, 0.0)) * UOp.const(dtypes.float32, 1.0))
    self.assertEqual(optimized_uop.op, UOps.CONST)
    self.assertEqual(optimized_uop.arg, 10.0)

  def test_full_graph_rewrite_large_graph(self):
    prev_uop = UOp.const(dtypes.int32, 0)
    for i in range(1, 101):
      prev_uop += UOp.const(dtypes.int32, i)
    optimized_uop = apply_rewrite(prev_uop)
    self.assertEqual(optimized_uop.op, UOps.CONST)
    self.assertEqual(optimized_uop.arg, sum(range(1, 101)))

  def test_full_graph_rewrite_division_by_one(self):
    optimized_uop = apply_rewrite(UOp.const(dtypes.float32, 42.0) / UOp.const(dtypes.float32, 1.0))
    self.assertEqual(optimized_uop.op, UOps.CONST)
    self.assertEqual(optimized_uop.arg, 42.0)

  def test_full_graph_rewrite_modulo_by_one(self):
    optimized_uop = apply_rewrite(UOp.const(dtypes.int32, 42) % UOp.const(dtypes.int32, 1))
    self.assertEqual(optimized_uop.op, UOps.CONST)
    self.assertEqual(optimized_uop.arg, 0)


class TestFoldingAndReduction(unittest.TestCase):
  def test_full_graph_rewrite_constant_reduction_folding(self):
    const1 = UOp.const(dtypes.int32, 5)
    const2 = UOp.const(dtypes.int32, 10)
    const3 = UOp.const(dtypes.int32, 20)
    optimized_sink = apply_rewrite((const1 + const2 + const3).reduce(BinaryOps.ADD))
    expected_sum = 5 + 10 + 20
    self.assertEqual(optimized_sink.arg, expected_sum)

  def test_full_graph_rewrite_reduction_with_unused_range(self):
    const1 = UOp.const(dtypes.int32, 15)
    const2 = UOp.const(dtypes.int32, 25)
    rng = UOp.range(dtypes.int32, 0, 10, idx=0)
    optimized_sink = apply_rewrite((const1 + const2).reduce(BinaryOps.ADD, rng))
    expected_sum = 10 * (15 + 25)
    self.assertEqual(optimized_sink.arg, expected_sum)

  @unittest.skip("currently failing")
  def test_full_graph_rewrite_range_reduction(self):
    simple_range = UOp.range(dtypes.int32, 0, 5, idx=0)
    optimized_sink = apply_rewrite(simple_range.reduce(BinaryOps.ADD, simple_range))
    expected_sum = sum(range(5))
    self.assertEqual(optimized_sink.arg, expected_sum)

  @unittest.skip("currently failing")
  def test_full_graph_rewrite_simple_reduction_folding(self):
    simple_range = UOp.range(dtypes.int32, 0, 4, idx=0)
    add_uop = simple_range + UOp.const(dtypes.int32, 1)
    optimized_sink = apply_rewrite(add_uop.reduce(BinaryOps.ADD, simple_range))
    expected_sum = sum(i + 1 for i in range(4))
    self.assertEqual(optimized_sink.arg, expected_sum)

  @unittest.skip("currently failing")
  def test_full_graph_rewrite_nested_loop_collapse(self):
    outer_range = UOp.range(dtypes.int32, 0, 8, 0)
    inner_range = UOp.range(dtypes.int32, 0, 4, 1)
    expr = (outer_range * 10) + inner_range
    optimized_reduce_uop = apply_rewrite(expr.reduce(BinaryOps.ADD, outer_range, inner_range))
    self.assertEqual(optimized_reduce_uop.op, UOps.CONST)
    self.assertEqual(optimized_reduce_uop.arg, sum((i * 10) + j for i in range(8) for j in range(4)))


class TestModuloAndDivisionFolding(unittest.TestCase):
  def test_full_graph_rewrite_modulo_folding_with_define_var(self):
    x_var_uop = UOp.define_var('x', dtypes.int32, 0, 100)
    optimized_mod_uop = apply_rewrite(((x_var_uop * 4) + 2) % 4)
    self.assertEqual(optimized_mod_uop.op, UOps.CONST)
    self.assertEqual(optimized_mod_uop.arg, 2)

  def test_full_graph_rewrite_division_folding_with_define_var(self):
    n_var_uop = UOp.define_var('n', dtypes.int32, 1, 1000)
    optimized_div_uop = apply_rewrite((n_var_uop * 6) // 3)
    self.assertEqual(optimized_div_uop.op, UOps.ALU)
    self.assertEqual(optimized_div_uop.arg, BinaryOps.MUL)
    self.assertEqual(optimized_div_uop.src[1].arg, 2)

  def test_full_graph_rewrite_complex_mod_div_folding(self):
    k_var_uop = UOp.define_var('k', dtypes.int32, 0, 50)
    optimized_div_uop = apply_rewrite(((k_var_uop * 12 + 8) % 6) // 2)
    self.assertEqual(optimized_div_uop.op, UOps.CONST)
    self.assertEqual(optimized_div_uop.arg, 1)

  def test_full_graph_rewrite_modulo_large_divisor(self):
    x_var_uop = UOp.define_var('x', dtypes.int32, 1, 5)
    self.assertIs(apply_rewrite(x_var_uop % 10), x_var_uop)

  def test_full_graph_rewrite_division_with_remainder(self):
    x_var_uop = UOp.define_var('x', dtypes.int32, 7, 9)
    optimized_sink = apply_rewrite(x_var_uop // 2)
    for x_value in range(7, 10):
      self.assertEqual(x_value // 2, evaluate_uop(optimized_sink, {'x': x_value}))

  def test_full_graph_rewrite_complex_mod_div_expression(self):
    x_var_uop = UOp.define_var('x', dtypes.int32, 1, 10)
    optimized_sink = apply_rewrite(((x_var_uop * 5) % 3) // 2)
    for x_value in range(1, 11):
      original_result = ((x_value * 5) % 3) // 2
      optimized_result = evaluate_uop(optimized_sink, {'x': x_value})
      self.assertEqual(original_result, optimized_result)


class TestEdgeCasesAndSpecialOperations(unittest.TestCase):
  def test_full_graph_rewrite_transcendental_edge_cases(self):
    optimized_sink = full_graph_rewrite(UOp.const(dtypes.float32, -1.0).log2().sink(UOp.const(dtypes.float32, 0.0).recip()))
    optimized_log2_neg, optimized_recip_zero = optimized_sink.src
    self.assertTrue(math.isnan(optimized_log2_neg.arg), f"Expected NaN for log2(-1.0), got {optimized_log2_neg.arg}")
    self.assertTrue(math.isinf(optimized_recip_zero.arg) and optimized_recip_zero.arg > 0,
                    f"Expected +inf for recip(0.0), got {optimized_recip_zero.arg}")

  @unittest.skip("broken")
  def test_full_graph_rewrite_modulo_negative_dividend(self):
    x_var_uop = UOp.define_var('x', dtypes.int32, -5, -1)
    optimized_sink = full_graph_rewrite((x_var_uop % 3).sink())
    for x_value in range(-5, 0):
      self.assertEqual(x_value % 3, evaluate_uop(optimized_sink.src[0], {'x': x_value}))

  @unittest.skip("broken")
  def test_full_graph_rewrite_division_negative_divisor(self):
    x_var_uop = UOp.define_var('x', dtypes.int32, 1, 5)
    optimized_sink = full_graph_rewrite((x_var_uop // -2).sink())
    for x_value in range(1, 6):
      self.assertEqual(x_value // -2, evaluate_uop(optimized_sink.src[0], {'x': x_value}))


if __name__ == '__main__':
  unittest.main()
