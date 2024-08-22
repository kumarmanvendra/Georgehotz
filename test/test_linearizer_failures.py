# ruff: noqa: E501
import unittest, random
import numpy as np
from tinygrad.codegen.kernel import KernelOptError
from tinygrad.codegen.kernel import Kernel
from tinygrad.ops import UOp, UOps
from tinygrad.engine.search import Opt, OptOps
from tinygrad import Device, dtypes, Tensor
from tinygrad.dtype import PtrDType
from tinygrad.helpers import CI
from test.external.fuzz_linearizer import compare_linearizer
from test.helpers import is_dtype_supported

from extra.ops import LazyOp, BinaryOps, UnaryOps, ReduceOps, TernaryOps, BufferOps, MemBuffer, ConstBuffer, MetaOps
from tinygrad.shape.shapetracker import ShapeTracker
from tinygrad.shape.view import View

def helper_test_lin(lin: Kernel, opts, failed_platforms, rtol=1e-2, atol=1e-2):
  if any(b.dtype == dtypes.half for b in lin.membufs) and not is_dtype_supported(dtypes.half): return
  if any(b.dtype == dtypes.bfloat16 for b in lin.membufs) and not is_dtype_supported(dtypes.bfloat16): return

  for opt in opts:
    try:
      lin.apply_opt(opt)
    except KernelOptError:
      # it's considered fixed if we invalidated the opts
      assert Device.DEFAULT not in failed_platforms, f"unexpected success on {Device.DEFAULT}"
      return

  compare_result = compare_linearizer(lin, rtol=rtol, atol=atol)
  if compare_result[0] in ["PASS", "KernelOptError"]:
    # it's considered fixed if we invalidated the opts
    assert Device.DEFAULT not in failed_platforms, f"unexpected success on {Device.DEFAULT}"
  else:
    assert Device.DEFAULT in failed_platforms, f"failed on {Device.DEFAULT} with {compare_result[0]}"
  return lin

@unittest.skipIf(CI and Device.DEFAULT in {"CUDA", "NV"}, "failed on CUDA CI")
class TestLinearizerFailures(unittest.TestCase):
  def setUp(self):
    random.seed(42)
    np.random.seed(42)
    Tensor.manual_seed(42)

  def test_failure_1(self):
    ast = UOp(UOps.SINK, None, arg=None, src=(
      UOp(UOps.STORE, None, arg=None, src=(
        UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.float), arg=0, src=()),
        UOp(UOps.SHAPETRACKER, None, arg=ShapeTracker(views=(View(shape=(32, 16, 1), strides=(16, 1, 0), offset=0, mask=None, contiguous=True),)), src=()),
        UOp(UOps.ALU, dtypes.float, arg=BinaryOps.ADD, src=(
          UOp(UOps.ALU, dtypes.float, arg=BinaryOps.ADD, src=(
            UOp(UOps.REDUCE_AXIS, dtypes.float, arg=(ReduceOps.SUM, (2,)), src=(
              UOp(UOps.LOAD, dtypes.float, arg=None, src=(
                UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.float), arg=1, src=()),
                UOp(UOps.SHAPETRACKER, None, arg=ShapeTracker(views=(View(shape=(32, 16, 16), strides=(16, 1, 0), offset=0, mask=None, contiguous=False),)), src=()),)),)),
            UOp(UOps.LOAD, dtypes.float, arg=None, src=(
              UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.float), arg=2, src=()),
              UOp(UOps.SHAPETRACKER, None, arg=ShapeTracker(views=(View(shape=(32, 16, 1), strides=(0, 1, 0), offset=0, mask=None, contiguous=False),)), src=()),)),)),
          UOp(UOps.LOAD, dtypes.float, arg=None, src=(
            UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.float), arg=1, src=()),
            UOp(UOps.SHAPETRACKER, None, arg=ShapeTracker(views=(View(shape=(32, 16, 1), strides=(16, 1, 0), offset=0, mask=None, contiguous=True),)), src=()),)),)),)),))
    helper_test_lin(Kernel(ast), [], failed_platforms=[])

  def test_failure_2(self):
    ast = UOp(UOps.SINK, None, arg=None, src=(
      UOp(UOps.STORE, None, arg=None, src=(
        UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.float), arg=0, src=()),
        UOp(UOps.SHAPETRACKER, None, arg=ShapeTracker(views=(View(shape=(32, 2, 37, 9, 1, 1), strides=(666, 333, 9, 1, 0, 0), offset=0, mask=None, contiguous=True),)), src=()),
        UOp(UOps.REDUCE_AXIS, dtypes.float, arg=(ReduceOps.MAX, (4, 5)), src=(
          UOp(UOps.LOAD, dtypes.float, arg=None, src=(
            UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.float), arg=1, src=()),
            UOp(UOps.SHAPETRACKER, None, arg=ShapeTracker(views=(View(shape=(32, 2, 111, 27), strides=(6160, 3080, 28, 1), offset=0, mask=((0, 32), (0, 2), (0, 110), (0, 27)), contiguous=False), View(shape=(32, 2, 37, 9, 2, 2), strides=(5994, 2997, 81, 3, 27, 1), offset=0, mask=None, contiguous=False))), src=()),)),)),)),))
    opts = [Opt(op=OptOps.LOCAL, axis=0, amt=32)]
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  def test_failure_3(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32, 8, 16, 1), strides=(128, 16, 1, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(ReduceOps.SUM, arg=(3,), src=(
        LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32, 8, 16, 16), strides=(2048, 256, 16, 1), offset=0, mask=None, contiguous=True),))), src=()),)),))
    opts = [Opt(op=OptOps.GROUP, axis=0, amt=4), Opt(op=OptOps.UPCAST, axis=0, amt=4), Opt(op=OptOps.UPCAST, axis=0, amt=2), Opt(op=OptOps.UNROLL, axis=1, amt=0), Opt(op=OptOps.UPCAST, axis=0, amt=4), Opt(op=OptOps.LOCAL, axis=0, amt=2), Opt(op=OptOps.LOCAL, axis=0, amt=2), Opt(op=OptOps.UPCAST, axis=1, amt=0), Opt(op=OptOps.LOCAL, axis=0, amt=32)]
    # METAL: AssertionError: Error Domain=AGXMetalG13X Code=3 "Threadgroup memory size (65536) exceeds the maximum threadgroup memory allowed (32768)" UserInfo={NSLocalizedDescription=Threadgroup memory size (65536) exceeds the maximum threadgroup memory allowed (32768)}
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  def test_failure_5(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 1, 1, 1, 1, 1, 1), strides=(0, 0, 0, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(ReduceOps.SUM, arg=(0, 2, 4, 6), src=(
        LazyOp(BinaryOps.ADD, arg=None, src=(
          x2:=LazyOp(BinaryOps.MUL, arg=None, src=(
            LazyOp(BinaryOps.ADD, arg=None, src=(
              LazyOp(BufferOps.CONST, arg=ConstBuffer(val=0.1464405059814453, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 4, 1, 3, 1, 4, 1), strides=(0, 0, 0, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),
              LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 4, 1, 3, 1, 4, 1), strides=(0, 0, 0, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 4, 1, 3, 1, 4, 1), strides=(0, 0, 0, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
          x2,)),)),))
    opts = [Opt(op=OptOps.UNROLL, axis=0, amt=4), Opt(op=OptOps.UNROLL, axis=0, amt=0)]
    # EXEC_ERROR, it has no global_size
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  def test_failure_6(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(10, 1), strides=(1, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(BinaryOps.ADD, arg=None, src=(
        LazyOp(ReduceOps.SUM, arg=(1,), src=(
          LazyOp(BufferOps.CONST, arg=ConstBuffer(val=-1, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(11, 19), strides=(0, 0), offset=0, mask=((0, 11), (9, 19)), contiguous=False), View(shape=(10, 10), strides=(1, 20), offset=0, mask=None, contiguous=False)))), src=()),)),
        LazyOp(BufferOps.CONST, arg=ConstBuffer(val=10, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(10, 1), strides=(0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),))
    opts = [Opt(op=OptOps.UPCAST, axis=0, amt=2), Opt(op=OptOps.UPCAST, axis=0, amt=0)]
    # COMPILE FAILED, KeyError: UOps.CONST
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  def test_failure_7(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 32, 1, 34, 1, 34), strides=(36992, 1156, 0, 34, 0, 1), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(ReduceOps.SUM, arg=(2, 4), src=(
        LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 32, 6, 8, 4, 6, 8, 4), strides=(2048, 64, 6291456, 8, 0, 1048576, 1, 0), offset=0, mask=((0, 512), (0, 32), (0, 6), (0, 8), (0, 1), (0, 6), (0, 8), (0, 1)), contiguous=False), View(shape=(512, 32, 6, 35, 6, 35), strides=(1179648, 36864, 6144, 192, 32, 1), offset=0, mask=((0, 512), (0, 32), (0, 6), (0, 32), (0, 6), (0, 32)), contiguous=False), View(shape=(512, 32, 238, 238), strides=(1411200, 44100, 210, 1), offset=0, mask=((0, 512), (0, 32), (0, 210), (0, 210)), contiguous=False), View(shape=(512, 32, 7, 34, 7, 34), strides=(1812608, 56644, 8092, 238, 34, 1), offset=0, mask=None, contiguous=True)))), src=()),)),))
    opts = [Opt(op=OptOps.UPCAST, axis=0, amt=4)]
    # test/test_linearizer_failures.py Fatal Python error: Segmentation fault
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  def test_failure_8(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 1), strides=(0, 0, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(UnaryOps.SQRT, arg=None, src=(
        LazyOp(UnaryOps.RECIP, arg=None, src=(
          LazyOp(BinaryOps.ADD, arg=None, src=(
            LazyOp(BinaryOps.MUL, arg=None, src=(
              LazyOp(ReduceOps.SUM, arg=(2,), src=(
                LazyOp(BinaryOps.MUL, arg=None, src=(
                  x6:=LazyOp(BinaryOps.ADD, arg=None, src=(
                    LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 4096), strides=(0, 0, 1), offset=0, mask=None, contiguous=True),))), src=()),
                    LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 4096), strides=(0, 0, 1), offset=0, mask=None, contiguous=True),))), src=()),)),
                  x6,)),)),
              LazyOp(BufferOps.CONST, arg=ConstBuffer(val=0.000244140625, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 1), strides=(0, 0, 0), offset=0, mask=None, contiguous=True),))), src=()),)),
            LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1e-06, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 1), strides=(0, 0, 0), offset=0, mask=None, contiguous=True),))), src=()),)),)),)),))
    opts = [Opt(op=OptOps.UNROLL, axis=0, amt=4), Opt(op=OptOps.UNROLL, axis=0, amt=4), Opt(op=OptOps.UNROLL, axis=0, amt=4), Opt(op=OptOps.UNROLL, axis=0, amt=4)]
    # fatal error: bracket nesting level exceeded maximum of 256
    # note: use -fbracket-depth=N to increase maximum nesting level
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  def test_failure_9(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 1, 3, 1, 1, 1, 1, 5, 15, 5, 3, 4), strides=(0, 0, 0, 4500, 0, 0, 0, 0, 900, 60, 12, 4, 1), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(ReduceOps.SUM, arg=(1,), src=(
        LazyOp(BinaryOps.MUL, arg=None, src=(
          LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 2, 1, 3, 1, 1, 1, 1, 5, 15, 5, 3, 4), strides=(0, 3, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),
          LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 2, 1, 3, 1, 1, 1, 1, 5, 15, 5, 3, 4), strides=(0, 4500, 0, 0, 0, 0, 0, 0, 900, 60, 12, 4, 1), offset=0, mask=None, contiguous=False),))), src=()),)),)),))
    opts = [Opt(op=OptOps.UPCAST, axis=1, amt=2), Opt(op=OptOps.UPCAST, axis=0, amt=0), Opt(op=OptOps.PADTO, axis=0, amt=32)]
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  def test_failure_10(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(1, 1, 1024, 1), strides=(0, 0, 1, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(BinaryOps.ADD, arg=None, src=(
        LazyOp(ReduceOps.SUM, arg=(3,), src=(
          LazyOp(BinaryOps.MUL, arg=None, src=(
            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(1, 1, 1024, 50257), strides=(0, 0, 0, 1), offset=0, mask=None, contiguous=False),))), src=()),
            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(1, 1, 1024, 50257), strides=(0, 0, 1, 1024), offset=0, mask=None, contiguous=False),))), src=()),)),)),
        LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=3, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(1, 1, 1024, 1), strides=(0, 0, 1, 0), offset=0, mask=None, contiguous=True),))), src=()),)),))
    helper_test_lin(Kernel(ast), [], failed_platforms=[])

  def test_failure_11(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 64, 1, 1), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(UnaryOps.RECIP, arg=None, src=(
        LazyOp(ReduceOps.SUM, arg=(0, 2, 3), src=(
          LazyOp(BinaryOps.MUL, arg=None, src=(
            LazyOp(BinaryOps.MUL, arg=None, src=(
              LazyOp(BinaryOps.ADD, arg=None, src=(
                LazyOp(BinaryOps.MAX, arg=None, src=(
                  LazyOp(BinaryOps.ADD, arg=None, src=(
                    LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 64, 6, 6), strides=(2304, 36, 6, 1), offset=0, mask=None, contiguous=True),))), src=()),
                    LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 64, 6, 6), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
                  LazyOp(BufferOps.CONST, arg=ConstBuffer(val=0.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 64, 6, 6), strides=(0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
                LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=3, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 64, 6, 6), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
              LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 64, 6, 6), strides=(0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
            LazyOp(BinaryOps.MUL, arg=None, src=(
              LazyOp(BinaryOps.MUL, arg=None, src=(
                LazyOp(BinaryOps.ADD, arg=None, src=(
                  LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 64, 3, 3, 2, 2), strides=(0, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 2, 3, 2), strides=(2304, 36, 12, 2, 4, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 6, 6), strides=(2304, 36, 6, 1), offset=0, mask=None, contiguous=True)))), src=()),
                  LazyOp(UnaryOps.CAST, arg=dtypes.float, src=(
                    LazyOp(BinaryOps.CMPLT, arg=None, src=(
                      LazyOp(BinaryOps.ADD, arg=None, src=(
                        LazyOp(BinaryOps.MUL, arg=None, src=(
                          LazyOp(BinaryOps.MUL, arg=None, src=(
                            LazyOp(BinaryOps.ADD, arg=None, src=(
                              LazyOp(BinaryOps.MAX, arg=None, src=(
                                LazyOp(BinaryOps.ADD, arg=None, src=(
                                  LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 64, 6, 6), strides=(2304, 36, 6, 1), offset=0, mask=None, contiguous=True), View(shape=(512, 64, 3, 3, 2, 2), strides=(2304, 36, 12, 2, 6, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 2, 3, 2), strides=(2304, 36, 12, 2, 4, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 6, 6), strides=(2304, 36, 6, 1), offset=0, mask=None, contiguous=True)))), src=()),
                                  LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 64, 6, 6), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 3, 2, 2), strides=(2304, 36, 12, 2, 6, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 2, 3, 2), strides=(2304, 36, 12, 2, 4, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 6, 6), strides=(2304, 36, 6, 1), offset=0, mask=None, contiguous=True)))), src=()),)),
                                x28:=LazyOp(BufferOps.CONST, arg=ConstBuffer(val=0.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 64, 6, 6), strides=(0, 0, 0, 0), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 3, 2, 2), strides=(2304, 36, 12, 2, 6, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 2, 3, 2), strides=(2304, 36, 12, 2, 4, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 6, 6), strides=(2304, 36, 6, 1), offset=0, mask=None, contiguous=True)))), src=()),)),
                              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=3, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 64, 6, 6), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 3, 2, 2), strides=(2304, 36, 12, 2, 6, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 2, 3, 2), strides=(2304, 36, 12, 2, 4, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 6, 6), strides=(2304, 36, 6, 1), offset=0, mask=None, contiguous=True)))), src=()),)),
                            LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 64, 6, 6), strides=(0, 0, 0, 0), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 3, 2, 2), strides=(2304, 36, 12, 2, 6, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 2, 3, 2), strides=(2304, 36, 12, 2, 4, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 6, 6), strides=(2304, 36, 6, 1), offset=0, mask=None, contiguous=True)))), src=()),)),
                          LazyOp(UnaryOps.SQRT, arg=None, src=(
                            LazyOp(UnaryOps.CAST, arg=dtypes.float, src=(
                              LazyOp(UnaryOps.RECIP, arg=None, src=(
                                LazyOp(BinaryOps.ADD, arg=None, src=(
                                  LazyOp(BinaryOps.MUL, arg=None, src=(
                                    LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=4, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(64,), strides=(1,), offset=0, mask=None, contiguous=True), View(shape=(512, 64, 6, 6), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 3, 2, 2), strides=(2304, 36, 12, 2, 6, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 2, 3, 2), strides=(2304, 36, 12, 2, 4, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 6, 6), strides=(2304, 36, 6, 1), offset=0, mask=None, contiguous=True)))), src=()),
                                    LazyOp(BufferOps.CONST, arg=ConstBuffer(val=5.425347222222222e-05, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(64,), strides=(0,), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 6, 6), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 3, 2, 2), strides=(2304, 36, 12, 2, 6, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 2, 3, 2), strides=(2304, 36, 12, 2, 4, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 6, 6), strides=(2304, 36, 6, 1), offset=0, mask=None, contiguous=True)))), src=()),)),
                                  LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1e-05, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(64,), strides=(0,), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 6, 6), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 3, 2, 2), strides=(2304, 36, 12, 2, 6, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 2, 3, 2), strides=(2304, 36, 12, 2, 4, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 6, 6), strides=(2304, 36, 6, 1), offset=0, mask=None, contiguous=True)))), src=()),)),)),)),)),)),
                        x28,)),
                      LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=5, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 64, 3, 3, 2, 2), strides=(576, 9, 3, 1, 0, 0), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 2, 3, 2), strides=(2304, 36, 12, 2, 4, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 6, 6), strides=(2304, 36, 6, 1), offset=0, mask=None, contiguous=True)))), src=()),)),)),)),
                LazyOp(UnaryOps.RECIP, arg=None, src=(
                  LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=6, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 64, 3, 3, 2, 2), strides=(576, 9, 3, 1, 0, 0), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 2, 3, 2), strides=(2304, 36, 12, 2, 4, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 6, 6), strides=(2304, 36, 6, 1), offset=0, mask=None, contiguous=True)))), src=()),)),)),
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=7, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 64, 3, 3, 2, 2), strides=(576, 9, 3, 1, 0, 0), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 3, 2, 3, 2), strides=(2304, 36, 12, 2, 4, 1), offset=0, mask=None, contiguous=False), View(shape=(512, 64, 6, 6), strides=(2304, 36, 6, 1), offset=0, mask=None, contiguous=True)))), src=()),)),)),)),)),))
    helper_test_lin(Kernel(ast), [], failed_platforms=[])

  def test_failure_12(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 1, 1, 1, 4, 1, 6, 1, 3), strides=(0, 0, 0, 0, 0, 18, 0, 3, 0, 1), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(ReduceOps.SUM, arg=(0, 2, 4, 6), src=(
        LazyOp(BinaryOps.ADD, arg=None, src=(
          x2:=LazyOp(BinaryOps.MUL, arg=None, src=(
            LazyOp(BinaryOps.ADD, arg=None, src=(
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 4, 1, 3, 4, 2, 6, 1, 3), strides=(0, 0, 0, 0, 0, 18, 0, 3, 0, 1), offset=0, mask=None, contiguous=False),))), src=()),
              LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 4, 1, 3, 4, 2, 6, 1, 3), strides=(0, 0, 0, 0, 0, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 4, 1, 3, 4, 2, 6, 1, 3), strides=(0, 0, 0, 0, 0, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
          x2,)),)),))
    opts = [Opt(op=OptOps.PADTO, axis=0, amt=32), Opt(op=OptOps.GROUP, axis=0, amt=4)]
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  @unittest.skip("AST has implicit movement ops")
  def test_failure_12_multireduce(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 1, 1, 1, 4, 1, 6, 1, 3), strides=(0, 0, 0, 0, 0, 18, 0, 3, 0, 1), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(ReduceOps.SUM, arg=(0, 2, 4, 8), src=(
        LazyOp(BinaryOps.ADD, arg=None, src=(
          x2:=LazyOp(BinaryOps.ADD, arg=None, src=(
            x3:=LazyOp(BinaryOps.MUL, arg=None, src=(
              LazyOp(BinaryOps.ADD, arg=None, src=(
                LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 4, 1, 3, 4, 2, 6, 1, 3), strides=(0, 0, 0, 0, 0, 18, 0, 3, 0, 1), offset=0, mask=None, contiguous=False),))), src=()),
                LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 4, 1, 3, 4, 2, 6, 1, 3), strides=(0, 0, 0, 0, 0, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 4, 1, 3, 4, 2, 6, 1, 3), strides=(0, 0, 0, 0, 0, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
            x3,)),
          LazyOp(ReduceOps.SUM, arg=(0, 2, 4, 8), src=(
            x2,)),)),)),))
    opts = [Opt(op=OptOps.PADTO, axis=0, amt=32), Opt(op=OptOps.GROUP, axis=0, amt=4)]
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  # both kernels are correct from a code standpoint, but generate different results due to precision errors (switching to float results in output matches)
  @unittest.skip("AST has implicit movement ops")
  def test_failure_13(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(2, 1, 384, 1), strides=(384, 0, 1, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(BinaryOps.ADD, arg=None, src=(
        LazyOp(ReduceOps.SUM, arg=(3,), src=(
          LazyOp(BinaryOps.MUL, arg=None, src=(
            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(2, 1, 384, 51864), strides=(51864, 0, 0, 1), offset=0, mask=None, contiguous=False),))), src=()),
            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(2, 1, 384, 51864), strides=(0, 0, 1, 384), offset=0, mask=None, contiguous=False),))), src=()),)),)),
        LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=3, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(2, 1, 384, 1), strides=(0, 0, 1, 0), offset=19584, mask=None, contiguous=False),))), src=()),)),))
    opts = [Opt(op=OptOps.GROUP, axis=0, amt=4)]
    helper_test_lin(Kernel(ast), opts, failed_platforms=["METAL", "GPU", "CUDA"])

  def test_failure_14(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 1, 1, 1, 4, 1, 6, 1, 3), strides=(0, 0, 0, 0, 0, 18, 0, 3, 0, 1), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(ReduceOps.SUM, arg=(0, 2, 4, 6), src=(
        LazyOp(BinaryOps.ADD, arg=None, src=(
          x2:=LazyOp(BinaryOps.MUL, arg=None, src=(
            LazyOp(BinaryOps.ADD, arg=None, src=(
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 4, 1, 3, 4, 2, 6, 1, 3), strides=(0, 0, 0, 0, 0, 18, 0, 3, 0, 1), offset=0, mask=None, contiguous=False),))), src=()),
              LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 4, 1, 3, 4, 2, 6, 1, 3), strides=(0, 0, 0, 0, 0, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 4, 1, 3, 4, 2, 6, 1, 3), strides=(0, 0, 0, 0, 0, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
          x2,)),)),))
    opts = [Opt(op=OptOps.PADTO, axis=0, amt=32), Opt(op=OptOps.UPCAST, axis=0, amt=4), Opt(op=OptOps.UPCAST, axis=0, amt=4)]
    # COMPILE_ERROR on METAL in fuzz_linearizer: unused variables and undeclared variables
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  def test_failure_15(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 112, 14, 14, 1, 1, 1), strides=(0, 0, 196, 14, 1, 0, 0, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(BinaryOps.ADD, arg=None, src=(
        LazyOp(BinaryOps.MUL, arg=None, src=(
          LazyOp(BinaryOps.MUL, arg=None, src=(
            LazyOp(BinaryOps.ADD, arg=None, src=(
              LazyOp(ReduceOps.SUM, arg=(5,), src=(
                LazyOp(BinaryOps.MUL, arg=None, src=(
                  LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 112, 14, 14, 480, 1, 1), strides=(0, 0, 0, 14, 1, 196, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),
                  LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 112, 14, 14, 480, 1, 1), strides=(0, 0, 480, 0, 0, 1, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=3, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 112, 14, 14, 1, 1, 1), strides=(0, 0, 1, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=4, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 112, 14, 14, 1, 1, 1), strides=(0, 0, 1, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
          LazyOp(UnaryOps.SQRT, arg=None, src=(
            LazyOp(UnaryOps.RECIP, arg=None, src=(
              LazyOp(BinaryOps.ADD, arg=None, src=(
                LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=5, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 112, 14, 14, 1, 1, 1), strides=(0, 0, 1, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),
                LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1e-05, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 112, 14, 14, 1, 1, 1), strides=(0, 0, 0, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),)),)),
        LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=6, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 112, 14, 14, 1, 1, 1), strides=(0, 0, 1, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),))
    opts = [Opt(op=OptOps.UPCAST, axis=1, amt=4), Opt(op=OptOps.UPCAST, axis=0, amt=2), Opt(op=OptOps.PADTO, axis=1, amt=32), Opt(op=OptOps.LOCAL, axis=0, amt=4), Opt(op=OptOps.LOCAL, axis=0, amt=2), Opt(op=OptOps.UPCAST, axis=1, amt=2), Opt(op=OptOps.UPCAST, axis=3, amt=0), Opt(op=OptOps.GROUP, axis=0, amt=8), Opt(op=OptOps.UPCAST, axis=1, amt=2), Opt(op=OptOps.LOCAL, axis=1, amt=16)]
    # COMPILE_ERROR on METAL in fuzz_linearizer ast 115: Error Domain=AGXMetalG14X Code=3 "Compiler encountered an internal error"
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  def test_failure_16(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 13, 1), strides=(0, 1, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(BinaryOps.MUL, arg=None, src=(
        LazyOp(ReduceOps.SUM, arg=(2,), src=(
          LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 13, 1024), strides=(0, 1024, 1), offset=0, mask=None, contiguous=True),))), src=()),)),
        LazyOp(BufferOps.CONST, arg=ConstBuffer(val=0.0009765625, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 13, 1), strides=(0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),))
    opts = [Opt(op=OptOps.GROUP, axis=0, amt=4), Opt(op=OptOps.UNROLL, axis=0, amt=0), Opt(op=OptOps.UNROLL, axis=0, amt=4), Opt(op=OptOps.GROUP, axis=0, amt=8), Opt(op=OptOps.UNROLL, axis=0, amt=4), Opt(op=OptOps.UNROLL, axis=1, amt=4)]
    # COMPILE_ERROR on METAL/GPU (probably HIP/CUDA too) in fuzz_linearizer ast 154: bracket nesting level exceeded maximum of 256
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  def test_failure_17(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 40, 1, 28, 28, 1, 1), strides=(31360, 0, 784, 0, 28, 1, 0, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(ReduceOps.SUM, arg=(3,), src=(
        LazyOp(BinaryOps.MUL, arg=None, src=(
          LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 40, 240, 28, 28, 1, 1), strides=(0, 0, 1, 40, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),
          LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 40, 240, 28, 28, 1, 1), strides=(188160, 0, 0, 784, 28, 1, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),))
    opts = [Opt(op=OptOps.UPCAST, axis=1, amt=4), Opt(op=OptOps.UPCAST, axis=0, amt=0), Opt(op=OptOps.PADTO, axis=1, amt=32), Opt(op=OptOps.LOCAL, axis=0, amt=2), Opt(op=OptOps.UPCAST, axis=1, amt=4), Opt(op=OptOps.UPCAST, axis=1, amt=4), Opt(op=OptOps.UPCAST, axis=1, amt=2), Opt(op=OptOps.GROUPTOP, axis=0, amt=16), Opt(op=OptOps.PADTO, axis=1, amt=32), Opt(op=OptOps.LOCAL, axis=1, amt=4)]
    # COMPILE_ERROR on METAL in fuzz_linearizer ast 178: Error Domain=AGXMetalG14X Code=3 "Compiler encountered an internal error"
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  def test_failure_18(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 384, 1), strides=(384, 0, 1, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(BinaryOps.ADD, arg=None, src=(
        LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 384, 1), strides=(384, 0, 1, 0), offset=0, mask=None, contiguous=True),))), src=()),
        LazyOp(BinaryOps.ADD, arg=None, src=(
          LazyOp(ReduceOps.SUM, arg=(3,), src=(
            LazyOp(BinaryOps.MUL, arg=None, src=(
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 384, 1536), strides=(1536, 0, 0, 1), offset=0, mask=None, contiguous=False),))), src=()),
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=3, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 384, 1536), strides=(0, 0, 1536, 1), offset=0, mask=None, contiguous=False),))), src=()),)),)),
          LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=4, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 384, 1), strides=(0, 0, 1, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),))
    opts = [Opt(op=OptOps.UPCAST, axis=1, amt=4), Opt(op=OptOps.UPCAST, axis=0, amt=0), Opt(op=OptOps.GROUPTOP, axis=0, amt=256), Opt(op=OptOps.UPCAST, axis=0, amt=4), Opt(op=OptOps.UPCAST, axis=0, amt=3)]
    # COMPILE_ERROR on METAL in fuzz_linearizer ast 239: Error Domain=AGXMetalG14X Code=3 "Compiler encountered an internal error"
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  def test_failure_19(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 4, 1, 9, 7, 3, 3), strides=(2268, 0, 567, 0, 63, 9, 3, 1), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(ReduceOps.SUM, arg=(3,), src=(
        LazyOp(BinaryOps.MUL, arg=None, src=(
          LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 4, 4, 9, 7, 3, 3), strides=(0, 0, 36, 9, 0, 0, -3, -1), offset=8, mask=None, contiguous=False),))), src=()),
          LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 4, 4, 9, 7, 3, 3), strides=(252, 0, 0, 63, 7, 1, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),))
    opts = [Opt(op=OptOps.LOCAL, axis=2, amt=3), Opt(op=OptOps.UPCAST, axis=1, amt=2), Opt(op=OptOps.UPCAST, axis=0, amt=0), Opt(op=OptOps.GROUP, axis=0, amt=4), Opt(op=OptOps.UPCAST, axis=1, amt=7), Opt(op=OptOps.UPCAST, axis=2, amt=3), Opt(op=OptOps.UPCAST, axis=1, amt=0), Opt(op=OptOps.LOCAL, axis=0, amt=2), Opt(op=OptOps.LOCAL, axis=0, amt=3)]
    # COMPILE_ERROR on METAL in fuzz_linearizer ast 379: Error Domain=AGXMetalG14X Code=3 "Compiler encountered an internal error"
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  def test_failure_20(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(4, 4), strides=(4, 1), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(BinaryOps.MUL, arg=None, src=(
        LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(4, 4), strides=(0, 1), offset=0, mask=None, contiguous=False),))), src=()),
        LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(4, 4), strides=(0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),))
    opts = [Opt(op=OptOps.UPCAST, axis=1, amt=0), Opt(op=OptOps.UPCAST, axis=0, amt=0)]
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  def test_failure_21(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(45, 65), strides=(65, 1), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(45, 65), strides=(0, 0), offset=0, mask=None, contiguous=False),))), src=()),))
    opts = [Opt(op=OptOps.PADTO, axis=0, amt=32)]
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  @unittest.skipIf(Device.DEFAULT in ("LLVM", "METAL", "CLANG"), "flaky")
  def test_failure_22(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 96, 1, 1), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(BinaryOps.MUL, arg=None, src=(
        x1:=LazyOp(BufferOps.CONST, arg=ConstBuffer(val=0.000244140625, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 96, 1, 1), strides=(0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),
        LazyOp(BinaryOps.MUL, arg=None, src=(
          LazyOp(BinaryOps.MUL, arg=None, src=(
            LazyOp(BinaryOps.MUL, arg=None, src=(
              LazyOp(ReduceOps.SUM, arg=(0, 2, 3), src=(
                LazyOp(BinaryOps.MUL, arg=None, src=(
                  LazyOp(BinaryOps.MUL, arg=None, src=(
                    LazyOp(BinaryOps.ADD, arg=None, src=(
                      LazyOp(BinaryOps.ADD, arg=None, src=(
                        LazyOp(BinaryOps.MUL, arg=None, src=(
                          LazyOp(BinaryOps.MUL, arg=None, src=(
                            LazyOp(BinaryOps.ADD, arg=None, src=(
                              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32, 96, 8, 16), strides=(12288, 128, 16, 1), offset=0, mask=None, contiguous=True),))), src=()),
                              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32, 96, 8, 16), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
                            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=3, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32, 96, 8, 16), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
                          LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=4, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32, 96, 8, 16), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
                        LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=5, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32, 96, 8, 16), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
                      LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=6, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32, 96, 8, 16), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
                    LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=7, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32, 96, 8, 16), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
                  LazyOp(BinaryOps.ADD, arg=None, src=(
                    LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=8, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 32, 48, 8, 16), strides=(0, 8640, 180, 18, 1), offset=19, mask=((1, 2), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(32, 96, 8, 16), strides=(12288, 128, 16, 1), offset=0, mask=None, contiguous=True)))), src=()),
                    LazyOp(BinaryOps.ADD, arg=None, src=(
                      LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=9, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 32, 48, 8, 16), strides=(0, 8640, 180, 18, 1), offset=19, mask=((1, 2), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(32, 96, 8, 16), strides=(12288, 128, 16, 1), offset=0, mask=None, contiguous=True)))), src=()),
                      LazyOp(BinaryOps.ADD, arg=None, src=(
                        LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=10, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 32, 48, 8, 16), strides=(0, 8640, 180, 18, 1), offset=19, mask=((1, 2), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(32, 96, 8, 16), strides=(12288, 128, 16, 1), offset=0, mask=None, contiguous=True)))), src=()),
                        LazyOp(BinaryOps.ADD, arg=None, src=(
                          LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=11, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 32, 48, 8, 16), strides=(0, 8640, 180, 18, 1), offset=19, mask=((1, 2), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(32, 96, 8, 16), strides=(12288, 128, 16, 1), offset=0, mask=None, contiguous=True)))), src=()),
                          LazyOp(BinaryOps.ADD, arg=None, src=(
                            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=12, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 32, 48, 8, 16), strides=(0, 8640, 180, 18, 1), offset=19, mask=((1, 2), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(32, 96, 8, 16), strides=(12288, 128, 16, 1), offset=0, mask=None, contiguous=True)))), src=()),
                            LazyOp(BinaryOps.ADD, arg=None, src=(
                              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=13, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 32, 48, 8, 16), strides=(0, 8640, 180, 18, 1), offset=19, mask=((1, 2), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(32, 96, 8, 16), strides=(12288, 128, 16, 1), offset=0, mask=None, contiguous=True)))), src=()),
                              LazyOp(BinaryOps.ADD, arg=None, src=(
                                LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=14, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 32, 48, 8, 16), strides=(0, 8640, 180, 18, 1), offset=19, mask=((1, 2), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(32, 96, 8, 16), strides=(12288, 128, 16, 1), offset=0, mask=None, contiguous=True)))), src=()),
                                LazyOp(BinaryOps.ADD, arg=None, src=(
                                  LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=15, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 32, 48, 8, 16), strides=(0, 8640, 180, 18, 1), offset=19, mask=((1, 2), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(32, 96, 8, 16), strides=(12288, 128, 16, 1), offset=0, mask=None, contiguous=True)))), src=()),
                                  LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=16, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 32, 48, 8, 16), strides=(0, 17280, 180, 18, 1), offset=19, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(2, 32, 48, 8, 16), strides=(0, 12288, 128, 16, 1), offset=0, mask=((0, 1), (0, 32), (0, 48), (0, 8), (0, 16)), contiguous=False), View(shape=(1536, 2, 128), strides=(128, 196608, 1), offset=0, mask=None, contiguous=False), View(shape=(32, 96, 8, 16), strides=(12288, 128, 16, 1), offset=0, mask=None, contiguous=True)))), src=()),)),)),)),)),)),)),)),)),)),)),
              LazyOp(UnaryOps.RECIP, arg=None, src=(
                LazyOp(BinaryOps.MUL, arg=None, src=(
                  LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=17, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 96, 1, 1), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=True),))), src=()),
                  LazyOp(BufferOps.CONST, arg=ConstBuffer(val=2.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 96, 1, 1), strides=(0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),)),
            x44:=LazyOp(UnaryOps.RECIP, arg=None, src=(
              LazyOp(BinaryOps.ADD, arg=None, src=(
                LazyOp(BinaryOps.MUL, arg=None, src=(
                  LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=18, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 96, 1, 1), strides=(0, 1, 0, 0), offset=0, mask=None, contiguous=True),))), src=()),
                  x1,)),
                LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1e-05, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 96, 1, 1), strides=(0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),)),
          x44,)),)),))
    opts = []
    helper_test_lin(Kernel(ast), opts, failed_platforms=["METAL", "CUDA"])

  def test_failure_23(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(240, 40, 1, 1), strides=(40, 1, 0, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(240, 40, 1, 1), strides=(1, 240, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),))
    opts = [Opt(op=OptOps.UPCAST, axis=1, amt=4), Opt(op=OptOps.LOCAL, axis=0, amt=16), Opt(op=OptOps.LOCAL, axis=1, amt=2), Opt(op=OptOps.UPCAST, axis=3, amt=2)]
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  def test_failure_24(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(8, 32, 1, 1), strides=(32, 1, 0, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(8, 32, 1, 1), strides=(1, 8, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),))
    opts = [Opt(op=OptOps.LOCAL, axis=1, amt=4), Opt(op=OptOps.UPCAST, axis=2, amt=2), Opt(op=OptOps.LOCAL, axis=1, amt=8), Opt(op=OptOps.UPCAST, axis=2, amt=0), Opt(op=OptOps.UPCAST, axis=1, amt=4), Opt(op=OptOps.LOCAL, axis=0, amt=8), Opt(op=OptOps.UPCAST, axis=1, amt=0), Opt(op=OptOps.UPCAST, axis=0, amt=2)]
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  # this is the cause of the GPT2 BEAM instability. bisects to PR#3530 O(n) arange attempt
  def test_failure_25(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(1024, 1), strides=(1, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(BinaryOps.ADD, arg=None, src=(
        LazyOp(ReduceOps.SUM, arg=(1,), src=(
          LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(1025, 2047), strides=(0, 0), offset=0, mask=((0, 1025), (1023, 2047)), contiguous=False), View(shape=(1024, 1024), strides=(1, 2048), offset=0, mask=None, contiguous=False)))), src=()),)),
        LazyOp(BufferOps.CONST, arg=ConstBuffer(val=-1, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(1024, 1), strides=(0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),))
    opts = [Opt(op=OptOps.GROUP, axis=0, amt=16), Opt(op=OptOps.UNROLL, axis=0, amt=4)]
    helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  # COMPARE_ERROR from GPT2 kernel - stems from uops.py self.simplify_phi_loops
  def test_failure_26(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(128, 1), strides=(1, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(BinaryOps.ADD, arg=None, src=(
        LazyOp(ReduceOps.SUM, arg=(1,), src=(
          LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(129, 255), strides=(0, 0), offset=0, mask=((0, 129), (127, 255)), contiguous=False), View(shape=(128, 128), strides=(1, 256), offset=0, mask=None, contiguous=False)))), src=()),)),
        LazyOp(BufferOps.CONST, arg=ConstBuffer(val=-1, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(128, 1), strides=(0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),))
    all_failing_opts = [
      [Opt(op=OptOps.UPCAST, axis=0, amt=4), Opt(op=OptOps.GROUPTOP, axis=0, amt=32), Opt(op=OptOps.UNROLL, axis=0, amt=0)],
      [Opt(op=OptOps.GROUPTOP, axis=0, amt=32), Opt(op=OptOps.UNROLL, axis=0, amt=0), Opt(op=OptOps.UPCAST, axis=0, amt=4)],
      [Opt(op=OptOps.UNROLL, axis=0, amt=4), Opt(op=OptOps.UNROLL, axis=0, amt=4), Opt(op=OptOps.LOCAL, axis=0, amt=16), Opt(op=OptOps.UPCAST, axis=0, amt=0)],
      [Opt(op=OptOps.UNROLL, axis=0, amt=4), Opt(op=OptOps.LOCAL, axis=0, amt=4), Opt(op=OptOps.UPCAST, axis=0, amt=4), Opt(op=OptOps.UPCAST, axis=0, amt=0)],
      [Opt(op=OptOps.UNROLL, axis=0, amt=4), Opt(op=OptOps.LOCAL, axis=0, amt=16), Opt(op=OptOps.UPCAST, axis=0, amt=0), Opt(op=OptOps.UNROLL, axis=0, amt=4)],
      [Opt(op=OptOps.LOCAL, axis=0, amt=4), Opt(op=OptOps.UPCAST, axis=0, amt=4), Opt(op=OptOps.UNROLL, axis=0, amt=4), Opt(op=OptOps.UPCAST, axis=0, amt=0)],
      [Opt(op=OptOps.LOCAL, axis=0, amt=16), Opt(op=OptOps.UPCAST, axis=0, amt=0), Opt(op=OptOps.UNROLL, axis=0, amt=4), Opt(op=OptOps.UNROLL, axis=0, amt=4)],
      [Opt(op=OptOps.LOCAL, axis=0, amt=16), Opt(op=OptOps.UPCAST, axis=0, amt=0), Opt(op=OptOps.GROUP, axis=0, amt=8), Opt(op=OptOps.UNROLL, axis=1, amt=4)],
      [Opt(op=OptOps.LOCAL, axis=0, amt=16), Opt(op=OptOps.GROUP, axis=0, amt=16), Opt(op=OptOps.UPCAST, axis=0, amt=0), Opt(op=OptOps.UNROLL, axis=1, amt=4)],
      [Opt(op=OptOps.LOCAL, axis=0, amt=16), Opt(op=OptOps.GROUP, axis=0, amt=16), Opt(op=OptOps.UNROLL, axis=1, amt=4), Opt(op=OptOps.UPCAST, axis=0, amt=0)],
      [Opt(op=OptOps.GROUP, axis=0, amt=8), Opt(op=OptOps.UNROLL, axis=1, amt=4), Opt(op=OptOps.LOCAL, axis=0, amt=16), Opt(op=OptOps.UPCAST, axis=0, amt=0)],
    ]
    for opts in all_failing_opts:
      helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  # COMPARE_ERROR from GPT2 kernel - just the first element off
  # testing ast 41
  # 0 ━┳ STORE MemBuffer(idx=0, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(1, 16, 13, 1), strides=(0, 13, 1, 0), offset=0, mask=None, contiguous=True),)))
  # 1  ┗━┳ MAX (3,)
  # 2    ┗━━ LOAD MemBuffer(idx=1, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(1, 16, 13, 13), strides=(0, 169, 13, 1), offset=0, mask=None, contiguous=True),)))
  # 208   13
  # ...
  # Mismatched elements: 1 / 1232 (0.0812%)
  # Max absolute difference: 0.8687
  # Max relative difference: 1.
  #  x: array([0.   , 0.996, 0.829, ..., 0.   , 0.   , 0.   ], dtype=float16)
  #  y: array([0.8687, 0.996 , 0.829 , ..., 0.    , 0.    , 0.    ], dtype=float16)
  # COMPARE FAILED!!
  def test_failure_27(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(1, 16, 13, 1), strides=(0, 13, 1, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(ReduceOps.MAX, arg=(3,), src=(
        LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(1, 16, 13, 13), strides=(0, 169, 13, 1), offset=0, mask=None, contiguous=True),))), src=()),)),))
    all_failing_opts = [
      [Opt(op=OptOps.PADTO, axis=0, amt=32), Opt(op=OptOps.UPCAST, axis=0, amt=4), Opt(op=OptOps.UPCAST, axis=0, amt=7), Opt(op=OptOps.UPCAST, axis=0, amt=0)],
    ]
    for opts in all_failing_opts:
      helper_test_lin(Kernel(ast), opts, failed_platforms=[])

  def test_failure_28(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.bfloat16, st=ShapeTracker(views=(View(shape=(1,), strides=(0,), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(TernaryOps.WHERE, arg=None, src=(
        LazyOp(BinaryOps.CMPLT, arg=None, src=(
          x2:=LazyOp(UnaryOps.CAST, arg=dtypes.bfloat16, src=(
            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(1,), strides=(0,), offset=0, mask=None, contiguous=True),))), src=()),)),
          x4:=LazyOp(BufferOps.CONST, arg=ConstBuffer(val=230.0, dtype=dtypes.bfloat16, st=ShapeTracker(views=(View(shape=(1,), strides=(0,), offset=0, mask=None, contiguous=True),))), src=()),)),
        LazyOp(BinaryOps.ADD, arg=None, src=(
          LazyOp(BinaryOps.MUL, arg=None, src=(
            LazyOp(BinaryOps.MUL, arg=None, src=(
              x2,
              LazyOp(BufferOps.CONST, arg=ConstBuffer(val=0.004347826086956522, dtype=dtypes.bfloat16, st=ShapeTracker(views=(View(shape=(1,), strides=(0,), offset=0, mask=None, contiguous=True),))), src=()),)),
            LazyOp(BufferOps.CONST, arg=ConstBuffer(val=0.199374800625, dtype=dtypes.bfloat16, st=ShapeTracker(views=(View(shape=(1,), strides=(0,), offset=0, mask=None, contiguous=True),))), src=()),)),
          LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1.99375e-07, dtype=dtypes.bfloat16, st=ShapeTracker(views=(View(shape=(1,), strides=(0,), offset=0, mask=None, contiguous=True),))), src=()),)),
        LazyOp(BinaryOps.ADD, arg=None, src=(
          LazyOp(BinaryOps.MUL, arg=None, src=(
            LazyOp(BinaryOps.MUL, arg=None, src=(
              LazyOp(BinaryOps.ADD, arg=None, src=(
                x2,
                x4,)),
              LazyOp(BufferOps.CONST, arg=ConstBuffer(val=0.0012987012987012987, dtype=dtypes.bfloat16, st=ShapeTracker(views=(View(shape=(1,), strides=(0,), offset=0, mask=None, contiguous=True),))), src=()),)),
            LazyOp(BufferOps.CONST, arg=ConstBuffer(val=-0.19439062499999998, dtype=dtypes.bfloat16, st=ShapeTracker(views=(View(shape=(1,), strides=(0,), offset=0, mask=None, contiguous=True),))), src=()),)),
          LazyOp(BufferOps.CONST, arg=ConstBuffer(val=0.199375, dtype=dtypes.bfloat16, st=ShapeTracker(views=(View(shape=(1,), strides=(0,), offset=0, mask=None, contiguous=True),))), src=()),)),)),))
    helper_test_lin(Kernel(ast), opts=[], failed_platforms=[])

  def test_failure_29(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(128, 1, 64, 56, 56, 1, 1, 1), strides=(200704, 0, 3136, 56, 1, 0, 0, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(UnaryOps.CAST, arg=dtypes.half, src=(
        LazyOp(ReduceOps.SUM, arg=(7, 6, 5), src=(
          LazyOp(UnaryOps.CAST, arg=dtypes.float, src=(
            LazyOp(BinaryOps.MUL, arg=None, src=(
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(1, 128, 1, 64, 4, 58, 4, 58), strides=(0, 200704, 0, 3136, 0, 56, 0, 1), offset=-57, mask=((0, 1), (0, 128), (0, 1), (0, 64), (0, 4), (1, 57), (0, 4), (1, 57)), contiguous=False), View(shape=(128, 1, 64, 56, 56, 64, 3, 3), strides=(3444736, 0, 0, 232, 1, 53824, 13688, 59), offset=0, mask=None, contiguous=False)))), src=()),
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(128, 1, 64, 56, 56, 64, 3, 3), strides=(0, 0, 576, 0, 0, 9, 3, 1), offset=0, mask=None, contiguous=False),))), src=()),)),)),)),)),))
    opts = [Opt(op=OptOps.TC, axis=0, amt=1), Opt(op=OptOps.PADTO, axis=2, amt=32)]
    helper_test_lin(Kernel(ast), opts, failed_platforms=[], atol=1.0)

  def test_failure_30(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(256, 1, 12, 31, 31, 1, 1, 1), strides=(11532, 0, 961, 31, 1, 0, 0, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(UnaryOps.CAST, arg=dtypes.half, src=(
        LazyOp(ReduceOps.SUM, arg=(7, 6, 5), src=(
          LazyOp(UnaryOps.CAST, arg=dtypes.float, src=(
            LazyOp(BinaryOps.MUL, arg=None, src=(
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(256, 1, 12, 31, 31, 3, 2, 2), strides=(3072, 0, 0, 32, 1, 1024, 32, 1), offset=0, mask=None, contiguous=False),))), src=()),
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(256, 1, 12, 31, 31, 3, 2, 2), strides=(0, 0, 12, 0, 0, 4, 2, 1), offset=0, mask=None, contiguous=False),))), src=()),)),)),)),)),))
    opts = [Opt(op=OptOps.PADTO, axis=3, amt=32), Opt(op=OptOps.LOCAL, axis=3, amt=32), Opt(op=OptOps.UPCAST, axis=3, amt=4), Opt(op=OptOps.UPCAST, axis=3, amt=0)]
    helper_test_lin(Kernel(ast), opts=opts, failed_platforms=[])

  # from METAL=1 fuzz_linearizer command in test.yml
  def test_failure_31(self):
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 16, 13, 1), strides=(0, 13, 1, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(ReduceOps.SUM, arg=(3,), src=(
        LazyOp(UnaryOps.EXP2, arg=None, src=(
          LazyOp(BinaryOps.MUL, arg=None, src=(
            LazyOp(BinaryOps.ADD, arg=None, src=(
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 16, 13, 13), strides=(0, 169, 13, 1), offset=0, mask=None, contiguous=True),))), src=()),
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 16, 13, 13), strides=(0, 13, 1, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
            LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1.4426950408889634, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 16, 13, 13), strides=(0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),)),))
    opts = [Opt(op=OptOps.UNROLL, axis=0, amt=0), Opt(op=OptOps.PADTO, axis=1, amt=32)]
    helper_test_lin(Kernel(ast), opts=opts, failed_platforms=[])

  @unittest.skipIf(CI, "for real AMD GPU")
  def test_failure_32(self):
    # kernel from beaming resnet
    # Memory access fault on tinybox red
    ast = LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(256, 1, 256, 14, 14, 1, 1, 1), strides=(50176, 0, 196, 14, 1, 0, 0, 0), offset=0, mask=None, contiguous=True),))), src=(
      LazyOp(UnaryOps.CAST, arg=dtypes.half, src=(
        LazyOp(ReduceOps.SUM, arg=(7, 6, 5), src=(
          LazyOp(UnaryOps.CAST, arg=dtypes.float, src=(
            LazyOp(BinaryOps.MUL, arg=None, src=(
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(1, 256, 1, 256, 4, 16, 4, 16), strides=(0, 50176, 0, 196, 0, 14, 0, 1), offset=-15, mask=((0, 1), (0, 256), (0, 1), (0, 256), (0, 4), (1, 15), (0, 4), (1, 15)), contiguous=False), View(shape=(256, 1, 256, 14, 14, 256, 3, 3), strides=(1048576, 0, 0, 64, 1, 4096, 1088, 17), offset=0, mask=None, contiguous=False)))), src=()),
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(256, 1, 256, 14, 14, 256, 3, 3), strides=(0, 0, 2304, 0, 0, 9, 3, 1), offset=0, mask=None, contiguous=False),))), src=()),)),)),)),)),))
    opts = [Opt(op=OptOps.TC, axis=2, amt=2), Opt(op=OptOps.UPCAST, axis=2, amt=7), Opt(op=OptOps.UNROLL, axis=1, amt=0), Opt(op=OptOps.LOCAL, axis=1, amt=16)]
    helper_test_lin(Kernel(ast), opts=opts, failed_platforms=[], atol=0.1, rtol=0.05)

  def test_failure_33(self):
    # UOps.UNMUL left after linearize
    ast = LazyOp(op=MetaOps.KERNEL, src=(
            LazyOp(op=BufferOps.STORE, src=(
              LazyOp(op=ReduceOps.SUM, src=(
                LazyOp(op=BinaryOps.MUL, src=(
                  LazyOp(op=BufferOps.LOAD, src=(), arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32640,), strides=(1,), offset=0, mask=((0, 26040),), contiguous=False),)))),
                  LazyOp(op=TernaryOps.WHERE, src=(
                    LazyOp(op=BinaryOps.CMPNE, src=(
                      LazyOp(op=BufferOps.LOAD, src=(), arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32640,), strides=(1,), offset=0, mask=((0, 26040),), contiguous=False),)))),
                      LazyOp(op=BufferOps.CONST, src=(), arg=ConstBuffer(val=0.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32640,), strides=(0,), offset=0, mask=None, contiguous=False),))))), arg=None),
                    LazyOp(op=TernaryOps.WHERE, src=(
                      LazyOp(op=BinaryOps.CMPLT, src=(
                        LazyOp(op=BinaryOps.ADD, src=(
                          LazyOp(op=BinaryOps.ADD, src=(
                            LazyOp(op=BinaryOps.MUL, src=(
                              LazyOp(op=BufferOps.CONST, src=(), arg=ConstBuffer(val=0.06788442333021306, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32640,), strides=(0,), offset=0, mask=((0, 26040),), contiguous=False),)))),
                              LazyOp(op=BufferOps.LOAD, src=(), arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32640,), strides=(1,), offset=0, mask=((0, 26040),), contiguous=False),))))), arg=None),
                            LazyOp(op=BufferOps.CONST, src=(), arg=ConstBuffer(val=-0.03394221166510653, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32640,), strides=(0,), offset=0, mask=((0, 26040),), contiguous=False),))))), arg=None),
                          LazyOp(op=BinaryOps.ADD, src=(
                            LazyOp(op=BufferOps.LOAD, src=(), arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32640,), strides=(1,), offset=-26040, mask=((26040, 32640),), contiguous=False),)))),
                            LazyOp(op=BufferOps.CONST, src=(), arg=ConstBuffer(val=-0.18257418583505536, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32640,), strides=(0,), offset=0, mask=((26040, 32640),), contiguous=False),))))), arg=None)), arg=None),
                          LazyOp(op=BufferOps.CONST, src=(), arg=ConstBuffer(val=0.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32640,), strides=(0,), offset=0, mask=None, contiguous=False),))))), arg=None),
                        LazyOp(op=BufferOps.CONST, src=(), arg=ConstBuffer(val=-1.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32640,), strides=(0,), offset=0, mask=None, contiguous=False),)))),
                      LazyOp(op=BufferOps.CONST, src=(), arg=ConstBuffer(val=1.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32640,), strides=(0,), offset=0, mask=None, contiguous=False),))))), arg=None),
                    LazyOp(op=BufferOps.CONST, src=(), arg=ConstBuffer(val=0.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(32640,), strides=(0,), offset=0, mask=None, contiguous=False),))))), arg=None)), arg=None),), arg=(0,)),), arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1,), strides=(0,), offset=0, mask=None, contiguous=True),)))),), arg=None)
    opts = [Opt(op=OptOps.GROUPTOP, axis=0, amt=16)]
    helper_test_lin(Kernel(ast), opts=opts, failed_platforms=[])

  # from fuzzing on metal
  def test_failure_34(self, unroll=False):
    ast = LazyOp(op=MetaOps.KERNEL, src=(
      LazyOp(op=BufferOps.STORE, src=(
        LazyOp(op=BinaryOps.MAX, src=(
          LazyOp(op=ReduceOps.SUM, src=(
            LazyOp(op=BinaryOps.MUL, src=(
              LazyOp(op=BufferOps.LOAD, src=(), arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(4, 1, 6, 10, 3, 1, 2, 5), strides=(77, 0, 0, 7, 1, 0, 7, 1), offset=0, mask=None, contiguous=False),)))),
              LazyOp(op=BufferOps.LOAD, src=(), arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(4, 1, 6, 10, 3, 1, 2, 5), strides=(0, 0, 10, 0, 0, 0, 5, 1), offset=0, mask=None, contiguous=False),))))), arg=None),),
            arg=(6, 7)), LazyOp(op=BufferOps.CONST, src=(), arg=ConstBuffer(val=0.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(4, 1, 6, 10, 3, 1, 1, 1), strides=(0, 0, 0, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))))), arg=None),), arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(4, 1, 6, 10, 3, 1, 1, 1), strides=(180, 0, 30, 3, 1, 0, 0, 0), offset=0, mask=None, contiguous=True),)))),), arg=None)
    opts = [Opt(op=OptOps.TC, axis=0, amt=2), Opt(op=OptOps.UNROLL, axis=0, amt=0)] if unroll else [Opt(op=OptOps.TC, axis=0, amt=2)]
    helper_test_lin(Kernel(ast), opts=opts, failed_platforms=[])

  def test_failure_35(self): self.test_failure_34(True)

  # from world fuzz_linearizer: PYTHONPATH=. METAL=1 FUZZ_ALL_ACTIONS=1 DEPTH=1 FUZZ_N=100 FUZZ_NTH=84 python3 ./test/external/fuzz_linearizer.py
  def test_failure_36(self):
    # UOps.UNMUL left after linearize
    ast = LazyOp(MetaOps.KERNEL, arg=None, src=(
      LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.uchar, st=ShapeTracker(views=(View(shape=(5, 1), strides=(1, 0), offset=0, mask=None, contiguous=True),))), src=(
        LazyOp(UnaryOps.CAST, arg=dtypes.uchar, src=(
          LazyOp(BinaryOps.ADD, arg=None, src=(
            LazyOp(ReduceOps.SUM, arg=(1,), src=(
              LazyOp(UnaryOps.CAST, arg=dtypes.uint, src=(
                LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1, dtype=dtypes.uchar, st=ShapeTracker(views=(View(shape=(6, 9), strides=(0, 0), offset=0, mask=((0, 6), (4, 9)), contiguous=False), View(shape=(5, 5), strides=(1, 10), offset=0, mask=None, contiguous=False)))), src=()),)),)),
            LazyOp(BufferOps.CONST, arg=ConstBuffer(val=-1, dtype=dtypes.uint, st=ShapeTracker(views=(View(shape=(5, 1), strides=(0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),)),))
    opts = [Opt(op=OptOps.UPCAST, axis=0, amt=0)]
    helper_test_lin(Kernel(ast), opts=opts, failed_platforms=[])

  # BEGIN METAL=1 ./examples/beautiful_mnist.py failures
  # log : PYTHONPATH=. LOGKERNS=/tmp/beautiful_mnist.kernels.txt METAL=1 python3 ./examples/beautiful_mnist.py
  def test_failure_37(self):
    # beautiful mnist kernel number 28: 6 possible TC axis_choices (3 for axis_buf1 and 2 reduce) and all fail
    # fuzz: PYTHONPATH=. METAL=1 FUZZ_ALL_ACTIONS=1 DEPTH=1 FUZZ_NTH=28 DEBUG=2 python3 ./test/external/fuzz_linearizer.py --logfile /tmp/beautiful_mnist.kernels.txt
    ast = LazyOp(MetaOps.KERNEL, arg=None, src=(
      LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 1, 32, 24, 24, 1, 1, 1), strides=(18432, 0, 576, 24, 1, 0, 0, 0), offset=0, mask=None, contiguous=True),))), src=(
        LazyOp(BinaryOps.MAX, arg=None, src=(
          LazyOp(BinaryOps.ADD, arg=None, src=(
            LazyOp(ReduceOps.SUM, arg=(6, 7), src=(
              LazyOp(BinaryOps.MUL, arg=None, src=(
                LazyOp(UnaryOps.CAST, arg=dtypes.float, src=(
                  LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.uchar, st=ShapeTracker(views=(View(shape=(512, 1, 32, 24, 24, 1, 5, 5), strides=(784, 0, 0, 28, 1, 0, 28, 1), offset=0, mask=None, contiguous=False),))), src=()),)),
                LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 1, 32, 24, 24, 1, 5, 5), strides=(0, 0, 25, 0, 0, 0, 5, 1), offset=0, mask=None, contiguous=False),))), src=()),)),)),
            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=3, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 1, 32, 24, 24, 1, 1, 1), strides=(0, 0, 1, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
          LazyOp(BufferOps.CONST, arg=ConstBuffer(val=0.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 1, 32, 24, 24, 1, 1, 1), strides=(0, 0, 0, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),))
    for axis in [0,1,2,3,4,5]:
      opts = [Opt(op=OptOps.TC, axis=axis, amt=2)]
      helper_test_lin(Kernel(ast), opts=opts, failed_platforms=[])

  def test_failure_38(self):
    # beautiful mnist kernel number 87: 6 possible TC axis_choices (2 for axis_buf1 and 3 reduce) and first/second reduce axis fail for both axis_buf1 choices
    # fuzz: PYTHONPATH=. METAL=1 FUZZ_ALL_ACTIONS=1 DEPTH=1 FUZZ_NTH=87 DEBUG=2 python3 ./test/external/fuzz_linearizer.py --logfile /tmp/beautiful_mnist.kernels.txt
    ast = LazyOp(op=MetaOps.KERNEL, src=(
      LazyOp(op=BufferOps.STORE, src=(
        LazyOp(op=ReduceOps.SUM, src=(
          LazyOp(op=BinaryOps.MUL, src=(LazyOp(op=UnaryOps.CAST, src=(
            LazyOp(op=BufferOps.LOAD, src=(), arg=MemBuffer(idx=1, dtype=dtypes.uchar, st=ShapeTracker(views=(View(shape=(2, 1, 32, 24, 24, 1, 5, 5, 256), strides=(784, 0, 0, 28, 1, 0, 28, 1, 1568), offset=0, mask=None, contiguous=False),)))),), arg=dtypes.float),
            LazyOp(op=BufferOps.LOAD, src=(), arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 1, 32, 24, 24, 1, 5, 5, 256), strides=(18432, 0, 576, 24, 1, 0, 0, 0, 36864), offset=0, mask=None, contiguous=False),))))), arg=None),),
        arg=(0, 3, 4)),), arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 32, 1, 1, 1, 5, 5, 256), strides=(0, 0, 6400, 0, 0, 0, 1280, 256, 1), offset=0, mask=None, contiguous=True),)))),), arg=None)
    for axis in [0,1,3,4]:
      opts = [Opt(op=OptOps.TC, axis=axis, amt=2)]
      helper_test_lin(Kernel(ast), opts=opts, failed_platforms=[])

  @unittest.skipIf(CI, "very slow, similar to test_failure_37")
  def test_failure_39(self):
    # beautiful mnist kernel number 127: 6 possible TC axis_choices (3 for axis_buf1 and 2 reduce) and all fail
    # fuzz: PYTHONPATH=. METAL=1 FUZZ_ALL_ACTIONS=1 DEPTH=1 FUZZ_NTH=127 DEBUG=2 python3 ./test/external/fuzz_linearizer.py --logfile /tmp/beautiful_mnist.kernels.txt
    ast = LazyOp(MetaOps.KERNEL, arg=None, src=(
      LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(10000, 1, 32, 24, 24, 1, 1, 1), strides=(18432, 0, 576, 24, 1, 0, 0, 0), offset=0, mask=None, contiguous=True),))), src=(
        LazyOp(BinaryOps.MAX, arg=None, src=(
          LazyOp(BinaryOps.ADD, arg=None, src=(
            LazyOp(ReduceOps.SUM, arg=(6, 7), src=(
              LazyOp(BinaryOps.MUL, arg=None, src=(
                LazyOp(UnaryOps.CAST, arg=dtypes.float, src=(
                  LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.uchar, st=ShapeTracker(views=(View(shape=(10000, 1, 32, 24, 24, 1, 5, 5), strides=(784, 0, 0, 28, 1, 0, 28, 1), offset=0, mask=None, contiguous=False),))), src=()),)),
                LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(10000, 1, 32, 24, 24, 1, 5, 5), strides=(0, 0, 25, 0, 0, 0, 5, 1), offset=0, mask=None, contiguous=False),))), src=()),)),)),
            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=3, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(10000, 1, 32, 24, 24, 1, 1, 1), strides=(0, 0, 1, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
          LazyOp(BufferOps.CONST, arg=ConstBuffer(val=0.0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(10000, 1, 32, 24, 24, 1, 1, 1), strides=(0, 0, 0, 0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),))
    for axis in [0,1,2,3,4,5]:
      opts = [Opt(op=OptOps.TC, axis=axis, amt=2)]
      helper_test_lin(Kernel(ast), opts=opts, failed_platforms=[])

  def test_failure_40(self):
    # beautiful mnist kernel number 3:
    # fuzz: PYTHONPATH=. METAL=1 FUZZ_ALL_ACTIONS=1 DEPTH=2 DEBUG=2 FUZZ_NTH=3 python3 ./test/external/fuzz_linearizer.py --logfile /tmp/beautiful_mnist.kernels.txt
    ast = LazyOp(MetaOps.KERNEL, arg=None, src=(
      LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(60000, 1), strides=(1, 0), offset=0, mask=None, contiguous=True),))), src=(
        LazyOp(BinaryOps.ADD, arg=None, src=(
          LazyOp(ReduceOps.SUM, arg=(1,), src=(
            LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(60001, 119999), strides=(0, 0), offset=0, mask=((0, 60001), (59999, 119999)), contiguous=False), View(shape=(60000, 60000), strides=(1, 120000), offset=0, mask=None, contiguous=False)))), src=()),)),
          LazyOp(BufferOps.CONST, arg=ConstBuffer(val=-1, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(60000, 1), strides=(0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),))
    for amt in [16,32]:
      opts = [Opt(op=OptOps.GROUPTOP, axis=0, amt=amt), Opt(op=OptOps.UNROLL, axis=0, amt=0)]
      helper_test_lin(Kernel(ast), opts=opts, failed_platforms=[])
  # END METAL=1 ./examples/beautiful_mnist.py failures

  @unittest.skipIf(CI, "for real AMD GPU")
  def test_failure_41(self):
    # One more resnet crash with a page fault on AMD. Checked on rocm6.1.3, -O1 works, -O2 fails
    ast = LazyOp(MetaOps.KERNEL, arg=None, src=(LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(256, 1, 128, 28, 28, 1, 1, 1), strides=(100352, 0, 784, 28, 1, 0, 0, 0), offset=0, mask=None, contiguous=True),))), src=(LazyOp(UnaryOps.CAST, arg=dtypes.half, src=(
       LazyOp(ReduceOps.SUM, arg=(5, 6, 7), src=(
         LazyOp(UnaryOps.CAST, arg=dtypes.float, src=(
           LazyOp(BinaryOps.MUL, arg=None, src=(
             LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(1, 256, 1, 128, 4, 58, 4, 58), strides=(0, 401408, 0, 3136, 0, 56, 0, 1), offset=-57, mask=((0, 1), (0, 256), (0, 1), (0, 128), (0, 4), (1, 57), (0, 4), (1, 57)), contiguous=False), View(shape=(256, 1, 128, 28, 28, 128, 3, 3), strides=(6889472, 0, 0, 464, 2, 53824, 13688, 59), offset=0, mask=None, contiguous=False)))), src=()), LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(256, 1, 128, 28, 28, 128, 3, 3), strides=(0, 0, 1152, 0, 0, 9, 3, 1), offset=0, mask=None, contiguous=False),))), src=()),)),)),)),)),)),))
    opts=[Opt(op=OptOps.TC, axis=5, amt=2), Opt(op=OptOps.UNROLL, axis=0, amt=0)]
    helper_test_lin(Kernel(ast), opts=opts, failed_platforms=["AMD", "HIP", "METAL"])

  # llama3 8B failure with BEAM=2 https://github.com/tinygrad/tinygrad/actions/runs/10150118124/job/28066519425#step:14:1, these don't compile
  @unittest.skipUnless(Device[Device.DEFAULT].renderer.has_local, "test needs local")
  @unittest.skipUnless(Device[Device.DEFAULT].renderer.has_shared, "test needs shared")
  def test_failure_42(self):
    ast = LazyOp(MetaOps.KERNEL, arg=None, src=(
  LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(25, 1), strides=(1, 0), offset=0, mask=None, contiguous=True),))), src=(
    LazyOp(ReduceOps.SUM, arg=(1,), src=(
      LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(26, 49), strides=(0, -1), offset=48, mask=((0, 26), (24, 49)), contiguous=False), View(shape=(25, 25), strides=(1, 50), offset=0, mask=None, contiguous=False)))), src=()),)),)),))
    opts = [Opt(op=OptOps.GROUP, axis=0, amt=0), Opt(op=OptOps.PADTO, axis=0, amt=32), Opt(op=OptOps.UPCAST, axis=0, amt=2), Opt(op=OptOps.PADTO, axis=0, amt=32)]
    helper_test_lin(Kernel(ast), opts=opts, failed_platforms=[])

  @unittest.skipUnless(Device[Device.DEFAULT].renderer.has_local, "test needs local")
  @unittest.skipUnless(Device[Device.DEFAULT].renderer.has_shared, "test needs shared")
  def test_failure_43(self):
    ast = LazyOp(MetaOps.KERNEL, arg=None, src=(
  LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(25, 1), strides=(1, 0), offset=0, mask=None, contiguous=True),))), src=(
    LazyOp(ReduceOps.SUM, arg=(1,), src=(
      LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(26, 49), strides=(0, -1), offset=48, mask=((0, 26), (24, 49)), contiguous=False), View(shape=(25, 25), strides=(1, 50), offset=0, mask=None, contiguous=False)))), src=()),)),)),))
    opts = [Opt(op=OptOps.GROUP, axis=0, amt=0), Opt(op=OptOps.PADTO, axis=0, amt=32), Opt(op=OptOps.LOCAL, axis=0, amt=4), Opt(op=OptOps.UPCAST, axis=0, amt=0)]
    helper_test_lin(Kernel(ast), opts=opts, failed_platforms=[])

  @unittest.skipUnless(Device[Device.DEFAULT].renderer.has_local, "test needs local")
  @unittest.skipUnless(Device[Device.DEFAULT].renderer.has_shared, "test needs shared")
  def test_failure_44(self):
    ast = LazyOp(MetaOps.KERNEL, arg=None, src=(
  LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(25, 1), strides=(1, 0), offset=0, mask=None, contiguous=True),))), src=(
    LazyOp(ReduceOps.SUM, arg=(1,), src=(
      LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(26, 49), strides=(0, -1), offset=48, mask=((0, 26), (24, 49)), contiguous=False), View(shape=(25, 25), strides=(1, 50), offset=0, mask=None, contiguous=False)))), src=()),)),)),))
    opts = [Opt(op=OptOps.GROUP, axis=0, amt=0), Opt(op=OptOps.PADTO, axis=0, amt=32), Opt(op=OptOps.LOCAL, axis=0, amt=4), Opt(op=OptOps.UPCAST, axis=0, amt=4)]
    k = helper_test_lin(Kernel(ast), opts=opts, failed_platforms=[])
    assert k is not None
    ifs = [u for u in k.uops if u.op is UOps.IF]
    self.assertEqual(len(ifs), 1)
    #for st in k.uops.sink.src: self.assertEqual(len(st.src), 4)
    self.assertLessEqual(len(ifs[0].src[0].sparents), 17)

  def test_failure_45(self):
    ast = LazyOp(MetaOps.KERNEL, arg=None, src=(
      LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 3, 1, 1, 1), strides=(3, 1, 0, 0, 0), offset=0, mask=None, contiguous=True),))), src=(
        LazyOp(ReduceOps.SUM, arg=(2, 3), src=(
          LazyOp(BinaryOps.MUL, arg=None, src=(
            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(2, 3, 2, 3, 1), strides=(0, 0, 3, 1, 0), offset=0, mask=None, contiguous=False),))), src=()),
            LazyOp(UnaryOps.CAST, arg=dtypes.float, src=(
              LazyOp(BinaryOps.MUL, arg=None, src=(
                LazyOp(BinaryOps.CMPNE, arg=None, src=(
                  LazyOp(BinaryOps.CMPNE, arg=None, src=(
                    LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(2, 3, 2, 3, 1), strides=(0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),
                    LazyOp(BinaryOps.ADD, arg=None, src=(
                      LazyOp(ReduceOps.SUM, arg=(4,), src=(
                        LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(3, 3), strides=(0, 0), offset=0, mask=((0, 3), (1, 3)), contiguous=False), View(shape=(2, 3, 2, 3, 3), strides=(0, 0, 1, 0, 4), offset=0, mask=((0, 2), (0, 3), (0, 2), (0, 3), (0, 2)), contiguous=False)))), src=()),)),
                      x12:=LazyOp(BufferOps.CONST, arg=ConstBuffer(val=-1, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(2, 3, 2, 3, 1), strides=(0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),
                  x13:=LazyOp(BufferOps.CONST, arg=ConstBuffer(val=True, dtype=dtypes.bool, st=ShapeTracker(views=(View(shape=(2, 3, 2, 3, 1), strides=(0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
                LazyOp(BinaryOps.CMPNE, arg=None, src=(
                  LazyOp(BinaryOps.CMPNE, arg=None, src=(
                    LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=3, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(2, 3, 2, 3, 1), strides=(3, 1, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),
                    LazyOp(BinaryOps.ADD, arg=None, src=(
                      LazyOp(ReduceOps.SUM, arg=(4,), src=(
                        LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(4, 5), strides=(0, 0), offset=0, mask=((0, 4), (2, 5)), contiguous=False), View(shape=(2, 3, 2, 3, 3), strides=(0, 0, 0, 1, 6), offset=0, mask=None, contiguous=False)))), src=()),)),
                       x12,)),)),
                   x13,)),)),)),)),)),)),))
    # ValueError: size mismatched, can't reshape self.shape=(6, 2, 3, 3) -> new_shape=(6, 2, 3, 1, 2)
    opts = [Opt(op=OptOps.UNROLL, axis=2, amt=0)]
    helper_test_lin(Kernel(ast), opts=opts, failed_platforms=[])

  def test_failure_46(self):
    ast = LazyOp(MetaOps.KERNEL, arg=None, src=(
      LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 1), strides=(1, 0), offset=0, mask=None, contiguous=True),))), src=(
        LazyOp(BinaryOps.MUL, arg=None, src=(
          LazyOp(ReduceOps.SUM, arg=(1,), src=(
            LazyOp(BinaryOps.MUL, arg=None, src=(
              LazyOp(UnaryOps.CAST, arg=dtypes.float, src=(
                LazyOp(BinaryOps.MUL, arg=None, src=(
                  LazyOp(BinaryOps.CMPNE, arg=None, src=(
                    LazyOp(BinaryOps.CMPNE, arg=None, src=(
                      LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(512, 10), strides=(0, 1), offset=0, mask=None, contiguous=False),))), src=()),
                      LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(512, 10), strides=(1, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
                    LazyOp(BufferOps.CONST, arg=ConstBuffer(val=True, dtype=dtypes.bool, st=ShapeTracker(views=(View(shape=(512, 10), strides=(0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
                  LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=3, dtype=dtypes.bool, st=ShapeTracker(views=(View(shape=(512, 10), strides=(1, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=4, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 10), strides=(0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),
          LazyOp(UnaryOps.RECIP, arg=None, src=(
            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=5, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(512, 1), strides=(1, 0), offset=0, mask=None, contiguous=True),))), src=()),)),)),)),))
    opts = [Opt(op=OptOps.UPCAST, axis=0, amt=2)]
    helper_test_lin(Kernel(ast), opts=opts, failed_platforms=[])

  def test_failure_47(self):
    # upcast an arange, failed with UOP_IS_SYMBOLIC=1 (fixed!)
    ast = LazyOp(MetaOps.KERNEL, arg=None, src=(
      LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(60000, 1), strides=(1, 0), offset=0, mask=None, contiguous=True),))), src=(
        LazyOp(BinaryOps.ADD, arg=None, src=(
          LazyOp(ReduceOps.SUM, arg=(1,), src=(
            LazyOp(BufferOps.CONST, arg=ConstBuffer(val=1, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(60001, 119999), strides=(0, 0), offset=0, mask=((0, 60001), (59999, 119999)), contiguous=False), View(shape=(60000, 60000), strides=(1, 120000), offset=0, mask=None, contiguous=False)))), src=()),)),
          LazyOp(BufferOps.CONST, arg=ConstBuffer(val=-1, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(60000, 1), strides=(0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),))
    opts = [Opt(op=OptOps.UPCAST, axis=0, amt=3)]
    helper_test_lin(Kernel(ast), opts=opts, failed_platforms=[])

  @unittest.skipUnless(not CI and Device.DEFAULT in ("NV", "CUDA"), "for real NV")
  def test_failure_48(self):
    # with UOP_IS_SYMBOLIC=1, generates the wrong IDIV (fixed!)
    ast = LazyOp(MetaOps.KERNEL, arg=None, src=(
      LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(1, 1, 64, 1, 1, 256, 1, 1, 256), strides=(0, 0, 65536, 0, 0, 256, 0, 0, 1), offset=0, mask=None, contiguous=True),))), src=(
        LazyOp(ReduceOps.SUM, arg=(3, 4), src=(
          LazyOp(UnaryOps.CAST, arg=dtypes.float, src=(
            LazyOp(BinaryOps.MUL, arg=None, src=(
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(1, 1, 64, 56, 56, 256, 1, 1, 256), strides=(0, 0, 0, 56, 1, 3136, 0, 0, 802816), offset=0, mask=None, contiguous=False),))), src=()),
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.half, st=ShapeTracker(views=(View(shape=(1, 1, 64, 56, 56, 256, 1, 1, 256), strides=(0, 0, 3136, 56, 1, 0, 0, 0, 200704), offset=0, mask=None, contiguous=False),))), src=()),)),)),)),)),))
    opts = [Opt(op=OptOps.TC, axis=0, amt=0), Opt(op=OptOps.UPCAST, axis=1, amt=4), Opt(op=OptOps.UPCAST, axis=0, amt=4), Opt(op=OptOps.LOCAL, axis=0, amt=2)]
    helper_test_lin(Kernel(ast, opts=Device[Device.DEFAULT].renderer), opts=opts, failed_platforms=[])

  def test_failure_49(self):
    # with UOP_IS_SYMBOLIC=1, on METAL it breaks store fusion and has A+B and B+A being two different UOp
    ast = LazyOp(MetaOps.KERNEL, arg=None, src=(
      LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(10, 6, 1), strides=(6, 1, 0), offset=0, mask=None, contiguous=True),))), src=(
        LazyOp(ReduceOps.SUM, arg=(2,), src=(
          LazyOp(BinaryOps.MUL, arg=None, src=(
            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(10, 6, 10), strides=(10, 0, 1), offset=0, mask=None, contiguous=False),))), src=()),
            LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.float, st=ShapeTracker(views=(View(shape=(10, 6, 10), strides=(0, 1, 6), offset=0, mask=None, contiguous=False),))), src=()),)),)),)),))
    opts = [Opt(op=OptOps.TC, axis=0, amt=2), Opt(op=OptOps.UPCAST, axis=0, amt=2)]
    helper_test_lin(Kernel(ast, opts=Device[Device.DEFAULT].renderer), opts=opts, failed_platforms=[])

  def test_failure_50(self):
    # from BEAM_COMPARE=2 running tinyphysics.onnx model
    ast = LazyOp(MetaOps.KERNEL, arg=None, src=(
      LazyOp(BufferOps.STORE, arg=MemBuffer(idx=0, dtype=dtypes.bool, st=ShapeTracker(views=(View(shape=(1, 1, 20, 1, 20), strides=(0, 0, 20, 0, 1), offset=0, mask=None, contiguous=True),))), src=(
        LazyOp(BinaryOps.CMPNE, arg=None, src=(
          LazyOp(ReduceOps.SUM, arg=(3,), src=(
            LazyOp(BinaryOps.MUL, arg=None, src=(
              LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=1, dtype=dtypes.bool, st=ShapeTracker(views=(View(shape=(1, 1, 20, 20, 20), strides=(0, 0, 0, 20, 1), offset=0, mask=None, contiguous=False),))), src=()),
              LazyOp(BinaryOps.CMPNE, arg=None, src=(
                LazyOp(BinaryOps.CMPNE, arg=None, src=(
                  LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=2, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(1, 1, 20, 20, 20), strides=(0, 0, 1, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),
                  LazyOp(BufferOps.LOAD, arg=MemBuffer(idx=3, dtype=dtypes.int, st=ShapeTracker(views=(View(shape=(1, 1, 20, 20, 20), strides=(0, 0, 0, 1, 0), offset=0, mask=None, contiguous=False),))), src=()),)),
                LazyOp(BufferOps.CONST, arg=ConstBuffer(val=True, dtype=dtypes.bool, st=ShapeTracker(views=(View(shape=(1, 1, 20, 20, 20), strides=(0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),)),
          LazyOp(BufferOps.CONST, arg=ConstBuffer(val=True, dtype=dtypes.bool, st=ShapeTracker(views=(View(shape=(1, 1, 20, 1, 20), strides=(0, 0, 0, 0, 0), offset=0, mask=None, contiguous=False),))), src=()),)),)),))
    opts = [Opt(op=OptOps.UPCAST, axis=1, amt=2)]
    helper_test_lin(Kernel(ast, opts=Device[Device.DEFAULT].renderer), opts=opts, failed_platforms=[])

if __name__ == '__main__':
  unittest.main()
