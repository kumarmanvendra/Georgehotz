from __future__ import annotations
from typing import Dict, Tuple, Sequence, Callable, Any, Union, Optional, List, cast, Literal
import functools, io, math
from tinygrad.tensor import Tensor, Device, _broadcast_shape, ConstType
from tinygrad.helpers import getenv, CI, OSX, prod, flatten, make_pair
from tinygrad.dtype import dtypes, DType
from onnx import AttributeProto, ModelProto, TensorProto, ValueInfoProto

# TODO try to remove this np stuff later
import numpy as np
try:
  from onnx.helper import tensor_dtype_to_np_dtype
except ImportError:
  # for onnx < 1.13
  from onnx.mapping import TENSOR_TYPE_TO_NP_TYPE
  def tensor_dtype_to_np_dtype(tensor_dtype:int) -> np.dtype: return TENSOR_TYPE_TO_NP_TYPE[tensor_dtype]

# ========== helpers
# Tensor -> python value cache for arguments
@functools.lru_cache(None)
def to_python_const(t:Tensor): return t if not isinstance(t, Tensor) else t.data().tobytes() if t.dtype is dtypes.uint8 else t.tolist()

# copied from helpers.py
def supported_device_dtypes(dtype, device):
  if dtype is dtypes.bfloat16: return dtypes.default_float
  if dtype is dtypes.half and (CI and device in {"GPU", "LLVM", "CUDA"}): return dtypes.default_float
  if dtype is dtypes.float64 and (device == "METAL" or (OSX and device == "GPU")): return dtypes.default_float
  return dtype

# ======= parsers
# src: onnx/mapping.py  https://onnx.ai/onnx/api/mapping.html#l-mod-onnx-mapping
DTYPE_MAP: Dict[int, DType] = {
  TensorProto.FLOAT:dtypes.float, TensorProto.UINT8:dtypes.uint8, TensorProto.INT8:dtypes.int8, TensorProto.UINT16:dtypes.uint16,
  TensorProto.INT16:dtypes.int16, TensorProto.INT32:dtypes.int32, TensorProto.INT64:dtypes.int64, TensorProto.BOOL:dtypes.bool,
  TensorProto.FLOAT16:dtypes.float16, TensorProto.DOUBLE:dtypes.double, TensorProto.UINT32:dtypes.uint32, TensorProto.UINT64:dtypes.uint64,
  TensorProto.BFLOAT16:dtypes.bfloat16, TensorProto.FLOAT8E4M3FN:dtypes.float, TensorProto.FLOAT8E4M3FNUZ:dtypes.float,
  TensorProto.FLOAT8E5M2:dtypes.float, TensorProto.FLOAT8E5M2FNUZ:dtypes.float}
def parse_dtype(onnx_dtype: int) -> DType:
  if onnx_dtype in DTYPE_MAP: return supported_device_dtypes(DTYPE_MAP[onnx_dtype], Device.DEFAULT)
  raise NotImplementedError(f"onnx dtype {TensorProto.DataType.Name(onnx_dtype)} is not supported")

def parse_buffer(inp: TensorProto) -> Tensor:
  if dat := list(inp.float_data) or list(inp.int32_data) or list(inp.int64_data):
    return Tensor(dat, dtype=parse_dtype(inp.data_type), requires_grad=False).reshape(tuple(inp.dims))
  if len(inp.raw_data) > 0:
    return Tensor(np.frombuffer(inp.raw_data, dtype=tensor_dtype_to_np_dtype(inp.data_type)).copy().reshape(tuple(inp.dims)), requires_grad=False)
  raise NotImplementedError(f"buffer with data type {TensorProto.DataType.Name(inp.data_type)} is not supported")

# src: onnx/onnx_ml_pb2.pyi
# NOTE: this is not a complete list
# torch's parser at onnx2torch/onnx_node.py: `OnnxNode._parse_attribute_value()`
ATTRS_MAP = {AttributeProto.FLOAT: lambda a: float(a.f), AttributeProto.INT: lambda a: int(a.i), AttributeProto.STRING: lambda a: a.s.decode("utf-8"),
         AttributeProto.TENSOR: lambda a: parse_buffer(a.t), AttributeProto.FLOATS: lambda a: tuple(float(x) for x in a.floats),
         AttributeProto.INTS: lambda a: tuple(int(x) for x in a.ints), AttributeProto.STRINGS: lambda a: tuple(x.decode("utf-8") for x in a.strings)}
def parse_attribute(a: AttributeProto):
  if a.type in ATTRS_MAP: return ATTRS_MAP[a.type](a)
  raise NotImplementedError(f"attribute with type {AttributeProto.AttributeType.Name(a.type)} is not supported")

# ========== runner
ONNXLIMIT = getenv("ONNXLIMIT", -1)
def get_run_onnx(onnx_model: ModelProto):
  # initialization data
  model_parameters = {inp.name:parse_buffer(inp) for inp in onnx_model.graph.initializer}
  model_attributes = {num:{x.name:parse_attribute(x) for x in n.attribute} for num,n in enumerate(onnx_model.graph.node)}

  def run_onnx(inputs=None, debug=0):
    """
    Run the ONNX model with the provided inputs.

    `debug` parameter is used to control the logging verbosity for onnx.
    `debug` can be used with `DEBUGONNX` environment variable or passed in as an argument
    verbosity levels for `debug`:
      - 0: No debug output (default).
      - 1: Prints each op with input shapes.
      - 2: Prints intermediate outputs with names for each op.
      - 3: Prints the input for each op along with whether or not they are turned into a python const.
      - 4: Prints the details of `handle_arguments` and `dispatch`
    NOTE: debug level 5 greatly hinders performance!
      - 5: Runs correctness verification using `torch` for initialization, input, and output data (must have `torch` and `onnx2torch` installed)
    """
    debug, inputs = getenv("DEBUGONNX") or debug, inputs or {}
    if debug >= 5:
      from extra.onnx_verifier import verify_initialization, verify_op
      verify_initialization(onnx_model, inputs, model_parameters, model_attributes)

    # TODO: we can also infer output data types and verify that too. Torch does this, not sure we should
    # src: https://onnx.ai/onnx/repo-docs/IR.html#input-output-data-types
    # we're doing a get_input_metadata like thing when we prep onnx inputs and then we check inputs again using a get_input_metadata like thing ....
    # dynamically load inputs to correct dtype and validate shape when possible
    def parse_input(model_input:ValueInfoProto):
      if model_input.name not in inputs: raise RuntimeError(f"no data for {model_input=}")
      inp, type_proto = inputs[model_input.name], model_input.type
      if type_proto.HasField("map_type"): raise NotImplementedError(f"model input {model_input.name} has map type")
      if type_proto.HasField("optional_type"):
        if inp is None: return Tensor(None)
        type_proto = type_proto.optional_type.elem_type
      if type_proto.HasField("sequence_type"):
        if not isinstance(inp, Sequence): raise RuntimeError(f"model input has to be a sequence type {model_input.name}: {inp}")
        # the element_type of tensor_type of a sequence_type determines the dtype for all tensors in the sequence
        dtype = parse_dtype(type_proto.sequence_type.elem_type.tensor_type.elem_type)
        ret = [Tensor(i, dtype=dtype, requires_grad=False) if not isinstance(i, Tensor) else i for i in inp]
        # TODO: compile2.py is in conflict with dtype verification for input
        # either we compile2.py test half or we don't verify dtype for input, orrrr maybe add a strict parameter to enable tighter checking
        # if not all(t.dtype is dtype for t in ret): raise RuntimeError(f"{model_input.name}: parsed dtype {dtype} input {ret}")
        return ret
      assert type_proto.HasField("tensor_type"), f"{model_input=}"
      dtype = parse_dtype(type_proto.tensor_type.elem_type)
      inp = Tensor(inp, dtype=dtype, requires_grad=False) if not isinstance(inp, Tensor) else inp
      # if dtype is not inp.dtype: raise RuntimeError(f"{model_input.name}: has wrong input dtype, parsed dtype {dtype} input dtype {inp.dtype}")
      # if dim_value is missing, it's a variable dim_value, e.g. dim {dim_param: "N"}, so we skip validation for those
      for i,d in enumerate(type_proto.tensor_type.shape.dim):
        if not d.dim_param and inp.shape[i] != d.dim_value:
          raise RuntimeError(f"{model_input.name}: tensor proto shape {type_proto.tensor_type.shape} input shape {inp.shape}")
      return inp

    input_tensors = {model_input.name: parse_input(model_input) for model_input in onnx_model.graph.input if model_input.name not in model_parameters}
    intermediate_tensors: Dict[str,Tensor] = {}

    def fetch_tensor(x: str):
      if x in model_parameters: return model_parameters[x]
      if x in intermediate_tensors: return intermediate_tensors[x]
      if x != "": return input_tensors[x]
      return None

    # inputs we need to turn into a python const to compute
    to_python_const_inps: Dict[str, Tuple[int, ...]] = \
    {"Tile": (1,), "Range": (0,1,2), "Expand": (1,), "Reshape": (1,), "Squeeze": (1,), "Unsqueeze": (1,), "Trilu": (1,), "ConstantOfShape": (0,),
      "CumSum": (1,), "Pad": (1,2,3), "MaxUnpool": (2,), "Dropout": (1,2), "CenterCropPad": (1,), "OneHot": (1,), "Compress": (1,),
      "ImageDecoder": (0,), "AffineGrid": (1,), "Resize": (1,2,3), "Upsample": (1,), "Split": (1,), "Slice": (1,2,3,4)}

    for num,n in enumerate(onnx_model.graph.node):
      # preperation
      tensor_inp, opt = [fetch_tensor(x) for x in n.input], model_attributes[num]
      if debug >= 1: print(f"{num}: op \"{n.op_type}\" input shapes {[x.shape if isinstance(x, Tensor) else x for x in tensor_inp]} opt {opt}")
      # to python consts
      if debug >= 3: print("\tinputs:\n" + "\n".join(f"\t\t{x} - {t}" + (" -> *python const*" if i in to_python_const_inps.get(n.op_type,()) else "")
                                                      for i,(x,t) in enumerate(zip(n.input, tensor_inp))))
      # TODO: maybe combine handle_arguments and to_python_const
      inp = [to_python_const(x) if i in to_python_const_inps.get(n.op_type, ()) else x for i,x in enumerate(tensor_inp)]

      # running the op
      if debug >= 4: print(f"\tbefore `handle_arguments`: {inp=}, {opt=}")
      inp, opt = handle_arguments(n.op_type, inp, opt, n=n, intermediate_tensors=intermediate_tensors)
      if debug >= 4: print(f"\tafter `handle_arguments`: {inp=}, {opt=}")
      ret = dispatch(n.op_type, inp, opt)

      # finalization after the op finishes running
      if not isinstance(ret, tuple): ret = (ret,)
      if len(n.output) > len(ret): raise RuntimeError(f"expected output size must be less than {len(ret)}, it's {n.output}")
      for i in range(len(n.output)): intermediate_tensors[n.output[i]] = ret[i]
      if debug >= 2: print("\toutputs:\n" + "\n".join(f"\t\t{n.output[i]} - {ret[i]}" for i in range(len(n.output))))
      if debug >= 5: verify_op(num, n, tuple(tensor_inp), opt, ret)

      if num == ONNXLIMIT: return {name:intermediate_tensors[name] for name in n.output}
    return {x.name:intermediate_tensors[x.name] for x in onnx_model.graph.output}
  return run_onnx

# =========== ops lol
# this is such a cluster fk
# I think I'm trying too hard with abstractions here
# the tradeoff is linecount + maybe readablilty vs maintainablilty + good errors
# actually maybe readability also suffers, crap.

# https://github.com/onnx/onnx/blob/main/docs/Operators.md
def handle_arguments(op:str, inps, opts, **kwargs):
  def rewrite_opt_names(inps, opts, mapping, **_): return inps, {mapping.get(k, k): v for k, v in opts.items()}
  def set_defaults(inps, opts, defaults, **_): return inps, {**defaults, **opts}

  op_handler = {
    **{op: functools.partial(set_defaults, defaults=d) for op, d in {"HardSigmoid": {"alpha": 0.2, "beta": 0.5}}.items()},
    **{op: functools.partial(rewrite_opt_names, mapping=amap) for op, amap in
       {"Concat": {"axis": "dim"}, "LeakyRelu": {"alpha": "neg_slope"}, "Selu": {"gamma": "scale"}}.items()},
    # -> opts y: Tensor
    "Gradient": lambda inps, opts, intermediate_tensor, **_: (inps, {"y": intermediate_tensor[opts["y"]]}),
    # -> inp: List[int, int | None], opt[]
    "Split": lambda inps, opts, n, **_: (inps + ([opts["split"]] if "split" in opts else []), {**{"num_outputs": len(n.output)},
                                                                                               **{k:v for k,v in opts.items() if k != "split"}}),
    # only onnx_model_version < 10 has opt, we just unload the opt into inp to match other versions
    "Slice": lambda inps, opts, **_: (inps + [list(v) for v in reversed(opts.values())], {}), # axes, ends, starts -> starts, ends, axes
    }
  return op_handler.get(op, lambda inps, opts, **_: (inps, opts))(inps, opts, **kwargs)

def dispatch(op:str, inps: List, opts: Dict):
  # tensor methods
  tensor_methods = {"Less": "__lt__", "Greater": "__gt__", "LessOrEqual": "__le__", "GreaterOrEqual": "__ge__", "Equal": "__eq__",
    "LogSoftmax": "log_softmax", "Not": "logical_not", "Tile": "repeat", "Range": "arange", "NegativeLogLikelihoodLoss": "nll_loss", "Concat": "cat",
    **{n:n.lower() for n in ("Neg", "Reciprocal", "Pow", "Sqrt", "Sign", "Abs", "Exp", "Log", "Mish", "Sin", "Cos", "Tan", "Relu",
    "Sigmoid", "MatMul", "Floor", "Ceil", "Softplus", "HardSwish", "Where", "Mul", "Sinh", "Cosh", "Tanh", "Softsign", "Asinh", "Acosh", "Atanh",
    "Elu", "Celu", "Xor", "Round", "Softmax", "LeakyRelu", "Selu", "HardSigmoid")}}

  # easy methods
  lambda_methods: Dict[str, Callable[..., Any]] = {"Identity": lambda x:x, "Add": lambda x,y,*_,**__: x+y, "Sub": lambda x,y,*_:x-y,
  "IsNaN": lambda x: x != x}

  # dispatch!
  # op rewrite
  if op in tensor_methods: return getattr(Tensor, tensor_methods[op])(*inps, **opts)
  # lambda
  if op in lambda_methods: return lambda_methods[op](*inps, **opts)
  # implemented
  if hasattr(OnnxOps, op): return getattr(OnnxOps, op)(*inps, **opts)

  raise NotImplementedError(f"Operation '{op}' is not implemented.")

# this is not properly linted. WIP
class OnnxOps:
  # implemented methods
  def Gradient(x, y):
    y.backward()
    return tuple([t.grad for t in x])

  def Split(*xs, axis=0, num_outputs):
    if len(xs) == 2: return xs[0].split(xs[1], axis)
    # split has to be inferred
    print(xs[0].shape)
    print(axis)
    size = xs[0].shape[axis]
    return xs[0].split([size // num_outputs + (1 if i < size % num_outputs else 0) for i in range(num_outputs)], axis)

  def Slice(data, starts, ends, axes=None, steps=None):
    axes, steps = axes or list(range(data.ndim)), steps or [1]*data.ndim
    slices = [slice(0,x,1) for x in data.shape]
    for i, axis in enumerate(axes): slices[axis] = slice(starts[i], ends[i], steps[i])
    return data[slices]

  # TODO maybe don't cast hack things
  # TODO maybe implement meshgrid utility
  # TODO maybe write a helper function for patterns like
  # axes, real_pads  = axes or list(range(x.ndim)), [0] * (x.ndim*2)
  # for i,axis in enumerate(axes): real_pads[axis%x.ndim], real_pads[axis%x.ndim+x.ndim] = pads[i], pads[i+len(axes)]
  # **************** Free Ops ****************

  def Max(*data_0): return functools.reduce(Tensor.maximum, data_0)
  def Min(*data_0): return functools.reduce(Tensor.minimum, data_0)
  def Sum(*data_0): return functools.reduce(Tensor.add, data_0)
  def Squeeze(data: Tensor, axes): return functools.reduce(lambda d, dim: d.squeeze(dim), sorted(axes, reverse=True), data)
  def Unsqueeze(data: Tensor, axes): return functools.reduce(lambda d, dim: d.unsqueeze(dim), sorted(axes), data)
  def Mean(*data_0): return OnnxOps.Sum(*data_0) / len(data_0)

  # TODO: saturate controls float8 casting behavior
  def Cast(x: Tensor, to: int, saturate=1): return x.cast(parse_dtype(to))
  def CastLike(x: Tensor, target_type: Tensor, saturate=1): return x.cast(target_type.dtype)

  # **************** Simple Ops ****************

  # https://github.com/onnx/onnx/blob/main/onnx/reference/ops/op_div.py
  def Div(x: Tensor, other: Tensor): return (x/other).cast(x.dtype)

  def Constant(value:Optional[Tensor]=None, value_float=None, value_floats=None, value_int=None,value_ints=None,value_string=None,value_strings=None):
    if value is not None: return value
    if value_float is not None: return Tensor(value_float, dtype=dtypes.float32, requires_grad=False)
    if value_floats is not None: return Tensor(list(value_floats), dtype=dtypes.float32, requires_grad=False)
    if value_int is not None: return Tensor(value_int, dtype=dtypes.int64, requires_grad=False)
    if value_ints is not None: return Tensor(list(value_ints), dtype=dtypes.int64, requires_grad=False)
    if value_string is not None or value_strings is not None: raise NotImplementedError('value_string or value_strings not implemented for Constant op') # noqa: E501
  def ConstantOfShape(shape:List[ConstType], value:Tensor): return Tensor.ones(*shape, dtype=value.dtype) * (value if shape != [0] else 1)

  # need default values for hardsigmoid
  def HardSigmoid(x: Tensor, alpha=0.2, beta=0.5): return x.hardsigmoid(alpha, beta)
  def Gelu(x:Tensor, approximate=None): return x.gelu() if approximate == "tanh" else 0.5 * x * (1 + OnnxOps.Erf(x/math.sqrt(2)))
  def PRelu(X:Tensor, slope:Tensor):
    # HACK OnnxBackendPyTorchConvertedModelTest HAS WEIRD SLOPE WHERE IT'S [0.25, 0.25, 0.25] FOR ANY X.SHAPE
    slope = slope[0] if slope.size(-1) != X.size(-1) else slope
    return (X > 0).where(X, X * slope)
  def ThresholdedRelu(X: Tensor, alpha=1.0): return (X > alpha).where(X, 0)
  # cuz onnx just uses min and max as attribute names
  def Clip(x: Tensor, min=None, max=None): # noqa: A002 # pylint: disable=redefined-builtin
    return x.clip(float('-inf') if min is None else min,float('inf') if max is None else max).cast(x.dtype)

  def _axes(axes, noop_with_empty_axes):
    if axes is not None and not (isinstance(axes, Tensor) and axes.shape == (0,)): return to_python_const(axes)
    return [] if noop_with_empty_axes else None
  def ReduceMax(data: Tensor, axes=None, keepdims=1, noop_with_empty_axes=0): return data.max(OnnxOps._axes(axes, noop_with_empty_axes), keepdim=keepdims) # noqa: E501
  def ReduceMin(data: Tensor, axes=None, keepdims=1, noop_with_empty_axes=0): return data.min(OnnxOps._axes(axes, noop_with_empty_axes), keepdim=keepdims) # noqa: E501
  def ReduceSum(data: Tensor, axes=None, keepdims=1, noop_with_empty_axes=0): return data.sum(OnnxOps._axes(axes, noop_with_empty_axes), keepdim=keepdims) # noqa: E501
  def ReduceMean(data: Tensor, axes=None, keepdims=1, noop_with_empty_axes=0): return data.mean(OnnxOps._axes(axes, noop_with_empty_axes), keepdim=keepdims) # noqa: E501
  def ReduceSumSquare(data: Tensor, axes=None, keepdims=1, noop_with_empty_axes=0): return OnnxOps.ReduceSum(data.square(), axes, keepdims,noop_with_empty_axes) # noqa: E501
  def ReduceProd(data: Tensor, axes=None, keepdims=1, noop_with_empty_axes=0): return data.prod(OnnxOps._axes(axes, noop_with_empty_axes), keepdim=keepdims) # noqa: E501
  def ReduceL1(data: Tensor, axes=None, keepdims=1, noop_with_empty_axes=0): return OnnxOps.ReduceSum(data.abs(), axes, keepdims, noop_with_empty_axes) # noqa: E501
  def ReduceL2(data: Tensor, axes=None, keepdims=1, noop_with_empty_axes=0): return OnnxOps.ReduceSumSquare(data, axes, keepdims, noop_with_empty_axes).sqrt() # noqa: E501
  def ReduceLogSum(data: Tensor, axes=None, keepdims=1, noop_with_empty_axes=0): return OnnxOps.ReduceSum(data, axes, keepdims, noop_with_empty_axes).log() # noqa: E501
  def ReduceLogSumExp(data: Tensor, axes=None,keepdims=1,noop_with_empty_axes=0): return OnnxOps.ReduceSum(data.exp(),axes,keepdims,noop_with_empty_axes).log()  # noqa: E501

  def GlobalAveragePool(X: Tensor): return X.mean(axis=tuple(range(2, X.ndim)), keepdim=True)
  def GlobalMaxPool(X: Tensor): return X.max(axis=tuple(range(2, X.ndim)), keepdim=True)
  def OptionalHasElement(x: Optional[Tensor]=None): return Tensor(x is not None and x.numel() > 0)
  def OptionalGetElement(x: Optional[Tensor]=None): return x if x is not None else Tensor([])

  def Shape(data: Tensor, end=None, start=0): return Tensor(data.shape[start:end], dtype=dtypes.int64)
  def Size(data: Union[Tensor, List]): return prod(data if isinstance(data, list) else data.shape)
  def Flatten(x: Tensor, axis=1): return x.reshape(prod(x.shape[0:axis]), -1)
  def Reshape(data: Tensor, shape, allowzero=0): return data.reshape([int(x) or (0 if allowzero else data.size(i)) for i, x in enumerate(shape)])
  def Expand(x: Tensor, shape:List): return x.expand(_broadcast_shape(x.shape, tuple(shape)))
  def Shrink(x: Tensor, bias=0.0, lambd=0.5): return (x < -lambd)*(x+bias) + (x > lambd)*(x-bias)
  def And(x:Tensor, y:Tensor): return (x==y).where(x, False)
  def Or(x:Tensor, y:Tensor): return (x==y).where(x, True)

  def Asin(x): return OnnxOps.Atan(x / (1 - x * x).sqrt())
  def Acos(x: Tensor):
    negate = (x < 0)
    x = x.abs()
    ret = ((((-0.0187293 * x) + 0.0742610)*x - 0.2121144) * x + 1.5707288) * (1.0 - x).sqrt()
    ret = ret - 2 * negate * ret
    return negate * math.pi + ret
  def Atan(y: Tensor):
    t1 = y.abs()
    t3 = (1 > t1).where(t1, t1.reciprocal())
    t4 = t3 * t3
    t0 = ((((-0.013480470 * t4 + 0.057477314) * t4 - 0.121239071) * t4 + 0.195635925) * t4 - 0.332994597) * t4 + 0.999995630
    t3 = t0 * t3
    t3 = (t1 > 1).where(1.570796327 - t3, t3)
    return y.sign() * t3

  def Trilu(x: Tensor, k:int=0, upper=1): return x.triu(k) if upper else x.tril(k)

  def Binarizer(x:Tensor, threshold=0.0): return (x > threshold).float()

  def ArgMax(x: Tensor, axis=0, keepdims=1, select_last_index=0):
    if select_last_index: return ((x.shape[axis]-1) - x.flip(axis).argmax(axis, keepdim=keepdims)).cast(dtypes.int64)
    return x.argmax(axis, keepdim=keepdims).cast(dtypes.int64)
  def ArgMin(x, axis=0, keepdims=1, select_last_index=0): return OnnxOps.ArgMax(-x, axis=axis, keepdims=keepdims, select_last_index=select_last_index)

  def Transpose(x: Tensor, perm=None): return x.permute(order=list(range(x.ndim)[::-1]) if perm is None else perm)

  # **************** Complex Ops ****************

  def Gemm(A: Tensor, B: Tensor, C: Optional[Tensor] = None, alpha=1.0, beta=1.0, transA=0, transB=0, broadcast=0):
    ret = alpha * (A.transpose(transA) @ B.transpose(transB))
    if C is not None: ret = ret + beta * (C if broadcast == 0 else C.reshape([-1 if i < len(C.shape) else 1 for i in range(ret.ndim)][::-1]))
    return ret

  def Einsum(*Inputs: List[Tensor], equation): return Tensor.einsum(equation, Inputs)

  def CumSum(X:Tensor, axis:int, exclusive=0, reverse=0):
    if axis < 0: axis += X.ndim
    if reverse: X = X.flip(axis)
    if exclusive: X = X.pad(tuple((1,0) if i == axis else None for i in range(X.ndim)))\
                        .shrink(tuple((0,X.shape[axis]) if i == axis else None for i in range(X.ndim)))
    return X.cumsum(axis).flip(axis) if reverse else X.cumsum(axis)

  # TODO: this is copied from tinygrad/nn/__init__.py
  # spatial is from opset 7 and has since been removed
  def BatchNormalization(X: Tensor, scale, B, input_mean, input_var, epsilon=1e-05, momentum=0.9, training_mode=0, spatial=1, is_test=0):
    if training_mode:
      batch_mean = X.mean(axis=(reduce_axes:=tuple(x for x in range(X.ndim) if x != 1)))
      y = (X - batch_mean.detach().reshape(shape=[1, -1, *([1]*(X.ndim-2))]))  # d(var)/d(mean) = 0
      batch_var = (y*y).mean(axis=reduce_axes)
      running_mean, running_var = input_mean * momentum + batch_mean * (1 - momentum), input_var * momentum + batch_var * (1 - momentum)
      return X.batchnorm(scale, B, batch_mean, batch_var.add(epsilon).rsqrt()), running_mean, running_var
    return X.batchnorm(scale, B, input_mean, (input_var + epsilon).rsqrt())

  def InstanceNormalization(x: Tensor, scale: Tensor, bias: Tensor, epsilon=1e-05):
    axis = tuple(range(2, x.ndim))
    mean = x.mean(axis=axis, keepdim=True)
    invstd = x.sub(mean).square().mean(axis=axis, keepdim=True).add(epsilon).rsqrt()
    return x.sub(mean).mul(scale.reshape(shape=[-1, 1, 1])).mul(invstd).add(bias.reshape(shape=[-1, 1, 1]))

  def LayerNormalization(x: Tensor, scale, bias, axis=-1, epsilon=1e-05, stash_type=1):
    assert stash_type == 1, "only float32 is supported"
    axis = tuple(i for i in range(axis if axis >= 0 else x.ndim + axis, x.ndim))
    mean = x.mean(axis=axis, keepdim=True)
    return x.layernorm(axis, epsilon).mul(scale).add(bias), mean, (x.sub(mean)).square().mean(axis=axis, keepdim=True).add(epsilon).rsqrt()

  def GroupNormalization(x: Tensor, scale: Tensor, bias: Tensor, num_groups, epsilon=1e-05):
    return x.reshape(x.size(0), num_groups, -1).layernorm(axis=-1, eps=epsilon).mul(scale.unsqueeze(-1)).add(bias.unsqueeze(-1)).reshape(x.shape)

  # **************** Ops with Padding ****************
  # helpers
  # (x1_begin, x2_begin, ..., x1_end, x2_end, ...) -> (..., x2_start, x2_end, x1_start, x1_end)
  def _onnx_pads_to_pad2d_pads(pads): return flatten(reversed(list((pb, pe) for pb, pe in zip(pads, pads[len(pads)//2:]))))

  # (H_pad, W_pad) -> (U_pad, L_pad, D_pad, R_pad) aka (x1_begin, x2_begin, ..., x1_end, x2_end, ...)
  def _auto_pad(pads, auto_pad: Literal["SAME_UPPER", "SAME_LOWER"]):
    return [pads[i]//2 for i in range(len(pads))] + [pads[i]-pads[i]//2 for i in range(len(pads))] if auto_pad == "SAME_UPPER" else \
            [pads[i]-pads[i]//2 for i in range(len(pads))] + [pads[i]//2 for i in range(len(pads))]

  # resolve auto_pad
  def _resolve_pool_pad(i_, k_, d_, s_, p_, auto_pad):
    s_, d_, p_ = (make_pair(x, len(k_)*2) for x in (s_, d_, p_))
    if auto_pad == "NOTSET": return p_ if len(p_) == len(k_)*2 else p_*2
    o_ = [(math.floor((i - (1 if auto_pad in ("SAME_UPPER", "SAME_LOWER") else k)) / s) + 1) for i,k,s in zip(i_, k_, s_)]
    p_ = OnnxOps._auto_pad([(o-1)*s+k-i for o,i,k,s in zip(o_, i_, k_, s_)], auto_pad)
    return p_

  def Pad(x:Tensor, pads:List[int], constant_value:Optional[ConstType]=None, axes:Optional[List[int]]=None, mode="constant", value:float=0.):
    constant_value, mode = value if constant_value is None else float(constant_value), {"edge": "replicate", "wrap":"circular"}.get(mode, mode)
    axes, real_pads  = axes or list(range(x.ndim)), [0] * (x.ndim*2)
    for i,axis in enumerate(axes): real_pads[axis%x.ndim], real_pads[axis%x.ndim+x.ndim] = pads[i], pads[i+len(axes)]
    return x.pad2d(OnnxOps._onnx_pads_to_pad2d_pads(real_pads), constant_value, mode)

  def AveragePool(X: Tensor, kernel_shape, auto_pad="NOTSET", ceil_mode=False, count_include_pad=False, dilations=1, pads=0, strides=1):
    pads = OnnxOps._onnx_pads_to_pad2d_pads(OnnxOps._resolve_pool_pad(X.shape[-len(kernel_shape):], kernel_shape, dilations, strides, pads, auto_pad))
    ret = X.pad2d(pads).avg_pool2d(kernel_shape, strides, dilations, ceil_mode=ceil_mode)
    return ret if count_include_pad else ret / X.ones_like().pad2d(pads).avg_pool2d(kernel_shape, strides, dilations, ceil_mode=ceil_mode)

  def MaxPool(X: Tensor, kernel_shape, auto_pad="NOTSET", ceil_mode=False, dilations=1, pads=0, storage_order=0, strides=1):
    pads = OnnxOps._onnx_pads_to_pad2d_pads(OnnxOps._resolve_pool_pad(X.shape[-len(kernel_shape):], kernel_shape, dilations, strides, pads, auto_pad))
    ret = X.pad2d(pads, float('-inf')).max_pool2d(kernel_shape, strides, dilations, ceil_mode=ceil_mode)
    indices = ((ret.reshape(-1, 1) == X.reshape(1, -1)) * Tensor.arange(X.numel(), dtype=dtypes.int64).unsqueeze(0)).sum(1).reshape(ret.shape)
    return ret.cast(X.dtype), indices.transpose(-2, -1) if storage_order else indices

  def MaxUnpool(xT: Tensor, xI: Tensor, outshape: Optional[Tensor]=None, kernel_shape=None, pads=None, strides=None):
    assert pads is None, "no tests covering pads"
    out_sh = [(ks//2)*2 + st * inps for inps, st, ks in zip(xI.shape, strides, kernel_shape)]
    ret = ((xI.reshape(-1, 1) == Tensor.arange(prod(out_sh))) * xT.reshape(-1, 1)).sum(0).reshape(1, 1, *out_sh)
    if outshape is not None and outshape != ret.shape:
      ret = ret.pad2d(OnnxOps._onnx_pads_to_pad2d_pads(OnnxOps._auto_pad([outshape[-2] - ret.shape[-2], outshape[-1] - ret.shape[-1]], "SAME_UPPER")))
    return ret

  def Conv(X: Tensor, W: Tensor, B:Optional[Tensor]=None, auto_pad="NOTSET", dilations=1, group=1, kernel_shape=None, pads=0, strides=1):
    pads = OnnxOps._resolve_pool_pad(X.shape[-len(kernel_shape):], kernel_shape, dilations, strides, pads, auto_pad)
    return X.conv2d(W, B, stride=strides, groups=group, dilation=dilations, padding=OnnxOps._onnx_pads_to_pad2d_pads(pads))

  # TODO: their reference implementation and their documentation have different information
  # ref: https://github.com/onnx/onnx/blob/main/onnx/reference/ops/op_conv_transpose.py
  # doc: https://github.com/onnx/onnx/blob/main/docs/Operators.md#ConvTranspose
  # the current implementation makes sense to geohotstan and passes tests, but differs from both ref and doc
  def ConvTranspose(X: Tensor, W: Tensor, B:Optional[Tensor]=None, auto_pad="NOTSET", dilations=1, group=1, kernel_shape=None, pads=None,
                    output_shape=None, output_padding=0, strides=1):
    input_shape, kernel_shape = X.shape[2:], (kernel_shape or W.shape[2:])
    strides, dilations, output_padding = (make_pair(x, len(input_shape)) for x in (strides, dilations, output_padding))
    if output_shape is not None: # we pad according to output_shape
      X = X.conv_transpose2d(W, B, stride=strides, groups=group, dilation=dilations, padding=0, output_padding=0)
      return X.pad((None, None, *((0, out-xs) for out, xs in zip(output_shape, X.shape[2:]))))  # TODO: unsure about this
    # NOTE the pads either from args or auto_pad have the format [x1_begin, x2_begin, ..., x1_end, x2_end, ...]
    # this is asymmetrical padding and conv_transpose2d does not support it natively
    # padding for conv_transpose2d effectively "shrinks" the padding that goes into conv2d, so we can get around this asymmetry by shrinking it after
    if pads is None: # we generate asymmetrical pads
      output_shape = [X.shape[i+2] * strides[i] for i in range(len(strides))]
      pads = [strides[i]*(input_shape[i]-1) + output_padding[i] + ((kernel_shape[i]-1)*dilations[i]+1)-output_shape[i] for i in range(len(input_shape))] # noqa: E501
      pads = [0,0] * len(input_shape) if auto_pad == "NOTSET" else OnnxOps._auto_pad(pads, auto_pad)
    X = X.conv_transpose2d(W, B, stride=strides, groups=group, dilation=dilations, padding=0, output_padding=output_padding)
    return X.pad2d(OnnxOps._onnx_pads_to_pad2d_pads([-p for p in pads])) # neg it since we shrink
    # return X if pads is None else X.shrink((None, None, *((pl, X.size(i+2)-pr) for i,(pl,pr) in enumerate(zip(pads, pads[len(pads)//2:])))))

  def DepthToSpace(X:Tensor, blocksize:int, mode:str="DCR"):
    return X.rearrange("b (c h1 w1) h w -> b c (h h1) (w w1)" if mode=="CRD" else "b (h1 w1 c) h w -> b c (h h1) (w w1)", h1=blocksize, w1=blocksize)

  def SpaceToDepth(X:Tensor, blocksize:int): return X.rearrange("b c (h h1) (w w1) -> b (h1 w1 c) h w", h1=blocksize, w1=blocksize)

  # Reimplemented here because you need legacy RNG for passing ONNX tests.
  def Dropout(data: Tensor, ratio=0.5, training_mode=False, seed=None):
    if not training_mode: return data, Tensor.ones(data.shape, dtype=dtypes.bool)  # if mask is requested as output it will contain all True's.
    mask = Tensor(np.random.RandomState(seed).random(cast(Tuple[int,...], data.shape)) >= ratio, requires_grad=False, device=data.device)
    return data * mask * (1/(1.0 - ratio)), mask

  def LRN(x: Tensor, size, alpha=1e-4, beta=0.75, bias=1.0):
    bs, c, iy, ix = x.shape
    ret = x/x.mul(x).reshape(bs,1,c,iy*ix).pad2d((0,0,(size-1)//2, size//2)).avg_pool2d((size, 1), 1).reshape(bs,c,iy,ix).mul(alpha).add(bias).pow(beta)  # noqa: E501
    return ret

  def MeanVarianceNormalization(x: Tensor, axis=(0, 2, 3)): return (x - x.mean(axis, keepdim=True)) / (x.std(axis, keepdim=True, correction=0) + 1e-9)

  def SoftmaxCrossEntropyLoss(scores: Tensor, labels: Tensor, weights=None, ignore_index=None, reduction="mean"):
    log_probs = scores.log_softmax(1)
    return log_probs.nll_loss(labels, weights, ignore_index, reduction), log_probs

  # TODO: is fuse_arange stuff working for this?
  def Gather(x: Tensor, indices: Tensor, axis=0):
    if indices.numel() < 9: # NOTE lessor kernels for smaller indices but kernel number increases depending on size of indices
      x_sh = list(x.shape)
      ret_shape = x_sh[:axis] + list(indices.shape) + x_sh[axis+1:]
      if indices.ndim > 1: indices = indices.flatten()
      python_indices = cast(Union[List[int], int], to_python_const(indices))
      normalized_python_indices = [python_indices] if not isinstance(python_indices, list) else [x_sh[axis]+x if x<0 else x for x in python_indices]
      args = [[(0,x) if j != axis else (i,i+1) for j, x in enumerate(x_sh)] for i in normalized_python_indices]
      return x.shrink(arg=tuple(args[0])).cat(*[x.shrink(arg=tuple(arg)) for arg in args[1:]], dim=axis).reshape(ret_shape)
    # NOTE faster gather, fixed number of kernels, but exceeds limited kernels for openpilot
    return x[tuple([slice(None) if i != axis else indices for i in range(x.ndim)])]
  def ArrayFeatureExtractor(x: Tensor, indices: Tensor): return x[..., indices]
  def GatherElements(x: Tensor, indices: Tensor, axis):
    indices = (indices < 0).where(x.shape[axis], 0) + indices
    return x.gather(axis, indices)

  def Resize(X:Tensor, roi=None, scales=None, sizes=None, antialias=0, axes=None, coordinate_transformation_mode='half_pixel',
              cubic_coeff_a=-0.75, exclude_outside=0, extrapolation_value=0.0, keep_aspect_ratio_policy='stretch',
              mode='nearest', nearest_mode='round_prefer_floor'):
    def _apply_nearest_mode(index: Tensor, input_dim, mode: str):
      if mode == "round_prefer_floor": index = (index - 0.5).ceil()
      elif mode == "round_prefer_ceil": index = (index + 0.5).floor()
      elif mode in ["floor", "ceil"]: index = getattr(index, mode)()
      else: raise ValueError(f"invalid {nearest_mode=}")
      return index.cast(dtypes.int32).clip(0, input_dim-1)
    def _apply_transformation(index: Tensor, input_dim, scale_dim, roi_dim, sizes_frac, mode):
      # TODO: needs more testing, not confident in this
      # NOTE: their reference implementation differ from the implementation in their reference docs
      # https://github.com/onnx/onnx/blob/main/onnx/reference/ops/op_resize.py
      # https://github.com/onnx/onnx/blob/main/docs/Operators.md#Resize
      output_dim = scale_dim * input_dim
      if mode == "half_pixel": index = (index + 0.5) / scale_dim - 0.5
      elif mode == "align_corners": index = index * (input_dim - 1) / (output_dim - 1) if output_dim != 1 else Tensor([0])
      elif mode == "asymmetric": index = index / scale_dim
      elif mode == "pytorch_half_pixel": index = (index + 0.5) / scale_dim - 0.5 if output_dim != 1 else Tensor([-0.5])
      elif mode == "half_pixel_symmetric": index = input_dim / 2 * (1 - int(output_dim) / sizes_frac) + (index + 0.5) / scale_dim - 0.5
      elif mode == "tf_crop_and_resize": index = roi_dim[0] * (input_dim - 1) + index * ((roi_dim[1] - roi_dim[0]) * (input_dim - 1) / (output_dim - 1)) # noqa: E501
      else: raise ValueError(f"invalid {coordinate_transformation_mode=}")
      return index.clip(0, input_dim-1)

    scales, sizes = (None if scales is None else scales[-2:]), (None if sizes is None else sizes[-2:])
    # we pre permute the axes and permute back after resize
    axes, input_shape, = (axes or list(range(X.ndim))), X.shape[2:],
    perm = [a for a in range(len(X.shape)) if a not in axes] + list(axes)
    X = X.permute(*perm)

    if sizes is not None:
      if keep_aspect_ratio_policy in ["not_larger", "not_smaller"]:
        scale_fxn = min if keep_aspect_ratio_policy == "not_larger" else max
        scales = scale_fxn([sizes[i] / input_shape[i] for i in range(X.ndim-2) if i+2 in axes])
        sizes = [int((scales * input_shape[i]) + 0.5) if i+2 in axes else input_shape[i] for i in range(X.ndim-2)]
      else: scales = [sizes[-2] / X.size(-2), sizes[-1] / X.size(-1)]
    else: sizes = [int(sc*sh) for sc, sh in zip(scales, input_shape)]
    scales = [scales] * 2 if not isinstance(scales, list) else scales
    roi = [[st, ed] for st, ed in zip(roi, roi[len(roi)//2:])] if isinstance(roi, list) else [None] * (X.ndim-2)

    # NOTE: this transformation makes it so that we can't just call Tensor.interpolate
    indexes = []
    for shape, size, scale, region in zip(input_shape, sizes, scales, roi):
      indexes.append(_apply_transformation(Tensor.arange(size), shape,scale, region, shape * scale, coordinate_transformation_mode))

    if mode == "nearest":
      indexes = [_apply_nearest_mode(index, shape, nearest_mode) for (index, shape) in zip(indexes, input_shape)]
      # meshgrid
      X = X[(..., *[idx.reshape(*(-1 if i == dim else 1 for i in range(len(sizes)))).expand(sizes) for dim, idx in enumerate(indexes)])]
    if mode == "linear":
      expand = list(X.shape)
      for i in range(-len(sizes), 0):
        reshape, index = [1] * X.ndim, indexes[i]
        reshape[i] = expand[i] = sizes[i]
        low, high, perc = [y.reshape(reshape).expand(expand) for y in (index.floor(), index.ceil(), index - index.floor())]
        X = X.gather(i, low).lerp(X.gather(i, high), perc)
    if mode == "cubic": raise NotImplementedError("cubic interpolation is not implemented")
    return X.permute(*[perm.index(i) for i in range(len(perm))]) if perm else X
  def Upsample(X, scales, mode): return OnnxOps.Resize(X=X, scales=scales, mode=mode)

  def CenterCropPad(t: Tensor, shape, axes=None):
    shrink_arg = [None] * t.ndim
    pad_arg = [None] * t.ndim
    for s, x in zip(shape, axes or range(t.ndim)):
      tx = t.shape[x]
      if s < tx: shrink_arg[x] = (tx//2 - (s+1)//2, tx//2 + s//2)
      elif s > tx: pad_arg[x] = ((s-tx)//2, (s-tx+1)//2)
    return t.shrink(tuple(shrink_arg)).pad(tuple(pad_arg))

  def OneHot(indices: Tensor, depth, values, axis=-1):
    # Scalar or Rank 1 tensor containing exactly one element
    depth = depth[0] if isinstance(depth, list) else depth
    indices, rank = (indices < 0).where(indices+depth, indices), indices.ndim
    if axis < 0: axis += rank + 1
    ls, rs = indices.shape[0:axis], indices.shape[axis: rank]
    cond = indices[:,None] == Tensor.arange(int(depth)).reshape((1,) * len(ls) + (int(depth),) + (1,) * len(rs))
    return cond.where(values[1], values[0])

  def Erf(x: Tensor):
    t = 1.0 / (1.0 + 0.3275911 * x.abs())
    y = (0.254829592 * t + -0.284496736 * t ** 2 + 1.421413741 * t ** 3 + -1.453152027 * t ** 4 + 1.061405429 * t ** 5)
    z = 1.0 - y * (-x * x).exp()
    return (x > 0).where(z, -z)

  def Compress(inp: Tensor, condition, axis=None):
    if axis is None:
      inp = inp.flatten()
      axis = 0
    if axis < 0: axis += inp.ndim
    con = Tensor(np.arange(len(condition))[condition]) # TODO no boolean indexing in Tensor, pretty sure it's possible maybe?
    return inp[tuple(con if i == axis else slice(None) for i in range(inp.ndim))]

  def EyeLike(x: Tensor, dtype: Optional[int]=None, k=0):
    tiny_dtype = x.dtype if dtype is None else parse_dtype(dtype)
    dim = cast(int, min(x.shape))
    if x.size(0) == x.size(1): return Tensor.eye(dim, dtype=tiny_dtype)
    padarg = tuple(None if d == dim else (k, d-dim-k) for d in x.shape)
    return Tensor.eye(dim, dtype=tiny_dtype).pad(padarg)

  def IsInf(x: Tensor, detect_negative=1, detect_positive=1):
    return (x == float("inf")) * bool(detect_positive) + (x == float("-inf")) * bool(detect_negative)

  def DequantizeLinear(x: Tensor, x_scale: Tensor, x_zero_point: Union[Tensor, int] = 0, axis=1, block_size=0):
    if axis < 0: axis += x.ndim
    if not isinstance(x_zero_point, Tensor): x_zero_point = Tensor(x_zero_point)
    if block_size: x_zer, x_sc = x_zero_point.repeat_interleave(block_size, axis), x_scale.repeat_interleave(block_size, axis)
    else:
      shape = (*[1]*axis, *x_scale.shape, *[1]*(x.ndim - axis - x_scale.ndim))
      x_sc, x_zer = x_scale.reshape(shape), x_zero_point.reshape(shape)
    return ((x.float() - x_zer) * x_sc).cast(x_scale.dtype)

  # copied from https://github.com/onnx/onnx/blob/main/onnx/reference/ops/op_image_decoder.py
  # without importing PIL we'll have to manually decode a bunch of image formats like PNG, JPEG, WebP, etc
  # TODO maybe uint8 stuff may work?
  def ImageDecoder(encoded_stream: bytes, pixel_format="RGB"):
    try: import PIL.Image
    except ImportError as e: raise ImportError("Pillow must be installed to use the reference implementation of the ImageDecoder operator") from e
    img = PIL.Image.open(io.BytesIO(encoded_stream))
    if pixel_format == "BGR": return Tensor(np.array(img))[:, :, ::-1]
    if pixel_format == "RGB": return Tensor(np.array(img))
    if pixel_format == "Grayscale": return Tensor(np.array(img.convert("L"))).unsqueeze(-1) # (H, W) to (H, W, 1)
    raise ValueError(f"pixel_format={pixel_format!r} is not supported.")

  # TODO: can this be cleaned up? This can use linspace and meshgrid but idk about line save
  def AffineGrid(theta: Tensor, size, align_corners=0):
    _, _, *data_sz = size
    size_zeros, original_grid = Tensor.zeros(data_sz), Tensor.ones(data_sz)
    stackable = [original_grid]
    for dim, dim_sz in enumerate(data_sz):
      a = Tensor.arange(-1, 1.0001, 2/(dim_sz-1)) if align_corners == 1 else Tensor.arange(-1+1/dim_sz, 1, 2/dim_sz)
      if dim == 0: stackable = [a.reshape(dim_sz, *[1]*(len(data_sz)-1)) + size_zeros, *stackable]
      elif dim == 1: stackable = [a.reshape(1, dim_sz, *[1]*(len(data_sz)-2)) + size_zeros, *stackable]
      else: stackable = [a.reshape(1, dim_sz) + size_zeros, *stackable]
    original_grid = Tensor.stack(*stackable, dim=len(data_sz))
    transformed_grid = theta.matmul(original_grid.reshape(-1, len(data_sz)+1).transpose()).transpose(1, 2)
    return transformed_grid.reshape(size[0], *data_sz, theta.size(1))

  # **************** com.microsoft Ops ****************

  def SkipLayerNormalization(x:Tensor, skip:Tensor, gamma, beta:Optional[Tensor]=None, bias:Optional[Tensor]=None, epsilon=None):
    if epsilon is None: epsilon=1e-12
    x = x + skip + bias
    return x.layernorm(eps=epsilon) * gamma + beta, None, None, x

  def FastGelu(x:Tensor, bias:Optional[Tensor]=None):
    # this is tanh approximated
    return (x + bias).gelu()

  # TODO: how to simplify these haha, I don't actually understand ML, IM A FRAUD
  def EmbedLayerNormalization(input_ids: Tensor, segment_ids: Tensor, word_embedding:Tensor,
                              position_embedding:Tensor, segment_embedding:Tensor, gamma=None, beta=None,
                              mask:Optional[Tensor]=None, position_ids:Optional[Tensor]=None, epsilon=None, mask_index_type=None):
    # https://github.com/microsoft/onnxruntime/blob/main/docs/ContribOperators.md#com.microsoft.EmbedLayerNormalization
    assert (segment_ids is None) is (segment_embedding is None)
    assert (mask is None) is (mask_index_type is None)
    assert mask is None, "functionality not supported yet"  # TODO
    input_shape = input_ids.shape
    seq_length = input_shape[1]
    compute_seg_emb = (segment_embedding is not None and segment_ids is not None)
    vocab_size, max_position_embeddings, type_vocab_size = word_embedding.shape[0], position_embedding.shape[0], (segment_embedding.shape[0]
                                                                                                                  if compute_seg_emb else None)

    def embedding(x, vocab_size, weight) -> Tensor:
      vocab_counter = Tensor.arange(vocab_size, dtype=x.dtype, requires_grad=False).reshape(1, 1, vocab_size).expand(*x.shape, vocab_size)
      return (vocab_counter == x.unsqueeze(2).expand(*x.shape, vocab_size)) @ weight

    # bert embedding layer
    if epsilon is None: epsilon = 1e-12
    if position_ids is None: position_ids = Tensor.arange(seq_length, requires_grad=False).unsqueeze(0).expand(*input_shape)
    wrd_embedding_res = embedding(input_ids, vocab_size, word_embedding)
    pos_embedding_res = embedding(position_ids, max_position_embeddings, position_embedding)
    seg_embedding_res = embedding(segment_ids, type_vocab_size, segment_embedding) if compute_seg_emb else None

    embedding_sum = wrd_embedding_res + pos_embedding_res
    if seg_embedding_res is not None: embedding_sum = embedding_sum + seg_embedding_res
    out = embedding_sum.layernorm(eps=epsilon) * gamma + beta
    return out, None, embedding_sum

  # TODO I gotta learn this
  def Attention(x:Tensor, weights, bias:Tensor, mask_index:Optional[Tensor]=None, past:Optional[Tensor]=None,
                relative_position_bias:Optional[Tensor]=None, past_sequence_length:Optional[Tensor]=None, do_rotary=None, mask_filter_value=None,
                num_heads=None, past_present_share_buffer=None, qkv_hidden_sizes=None, scale=None, unidirectional=None):
    # https://github.com/microsoft/onnxruntime/blob/main/docs/ContribOperators.md#com.microsoft.Attention
    assert num_heads is not None  # required
    assert (qkv_hidden_sizes is None and past is not None) or (qkv_hidden_sizes is not None)
    assert relative_position_bias is do_rotary is past_sequence_length is mask_filter_value is past_present_share_buffer is scale is None, \
    "functionality not supported yet"  # TODO strange params
    hidden_size, v_hidden_size = qkv_hidden_sizes[1:] if qkv_hidden_sizes is not None else 2*(weights.size(1) // 3,)

    if unidirectional:  # gpt-style
      assert hidden_size == v_hidden_size
      xqkv = x.linear(weights, bias)
      xq, xk, xv = [xqkv.shrink([None, None, (i*hidden_size, (i+1)*hidden_size)]) for i in range(3)]
    else:  # bert-style
      wq, wk, wv = weights[:,:hidden_size], weights[:,hidden_size:hidden_size+v_hidden_size], weights[:,hidden_size+v_hidden_size:]
      bq, bk, bv = (bias[:hidden_size], bias[hidden_size:hidden_size+v_hidden_size], bias[hidden_size+v_hidden_size]) if bias is not None else None
      xq, xk, xv = [x.linear(w, b) for w, b in zip((wq, wk, wv), (bq, bk, bv))]
    xq, xk, xv = [x.reshape(x.shape[0], x.shape[1], num_heads, -1).transpose(1, 2) for x in (xq, xk, xv)]

    present = None
    if past is not None:
      xk, xv = Tensor.cat(past[0], xk, dim=-2), Tensor.cat(past[1], xv, dim=-2)
      present = Tensor.cat(xk.unsqueeze(0), xv.unsqueeze(0))

    def attn(query, key, value, attn_mask):
      query_length, key_length = query.shape[-2], key.shape[-2]
      cdim = max(query_length, key_length) + 1
      attn_weights = query @ key.transpose(-1, -2) / math.sqrt(value.shape[-1])
      # This is where Tensor.scaled_dot_product_attention differs:
      causal_mask = Tensor.ones((cdim, cdim), requires_grad=False, dtype=dtypes.bool).tril(0)[key_length - query_length : key_length, :key_length]
      masked = Tensor.where(causal_mask, attn_weights, -math.inf)
      if attn_mask is not None: masked = masked + attn_mask
      return masked.softmax(-1) @ value

    bsz, _, seq_len, _ = xq.shape
    out = attn(xq, xk, xv, mask_index).transpose(1, 2).reshape(bsz, seq_len, -1)
    return out, present