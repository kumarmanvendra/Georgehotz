from tinygrad.codegen.linearizer import Linearizer
from tqdm import tqdm, trange
import math
import random
from tinygrad.tensor import Tensor
from tinygrad.nn import Linear
from tinygrad.nn.optim import Adam
from tinygrad.nn.state import get_parameters, get_state_dict, safe_save, safe_load, load_state_dict

# stuff needed to unpack a kernel
from tinygrad.ops import LazyOp, TernaryOps, BinaryOps, UnaryOps, ReduceOps, BufferOps, MemBuffer, ConstBuffer
from tinygrad.helpers import dtypes
from tinygrad.shape.shapetracker import ShapeTracker
from tinygrad.shape.view import View
from tinygrad.shape.symbolic import Variable
inf, nan = float('inf'), float('nan')
from tinygrad.codegen.optimizer import Opt, OptOps

from extra.optimization.helpers import lin_to_feats, MAX_DIMS

# NOTE: this is not real value of the state, it's just a prediction of the runtime
INNER = 256
class ValueNet:
  def __init__(self):
    self.l1 = Linear(240,INNER)
    self.l2 = Linear(INNER,INNER)
    self.l3 = Linear(INNER,1)
  def __call__(self, x):
    x = self.l1(x).relu()
    x = self.l2(x).relu()
    return self.l3(x)

if __name__ == "__main__":
  net = ValueNet()
  optim = Adam(get_parameters(net))

  TRAIN_SIZE = 2000
  TEST_SIZE = 128

  dset = open("/tmp/logtm").read().strip().split("\n")
  random.seed(1337)
  random.shuffle(dset)
  dset = dset[:TRAIN_SIZE+TEST_SIZE]

  X,Y = [], []
  for i,x in enumerate(tqdm(dset)):
    ast, opts, tms = eval(x)
    lin = Linearizer(ast)
    for o in opts: lin.apply_opt(o)
    if lin.shape_len >= MAX_DIMS: continue
    if min(tms) == float('inf'): continue
    X.append(lin_to_feats(lin))
    Y.append([math.log(min(tms)*1e6)])
  print(f"got {len(X)} samples")

  X_test,Y_test = Tensor(X[TRAIN_SIZE:]), Tensor(Y[TRAIN_SIZE:])
  X,Y = X[:TRAIN_SIZE], Y[:TRAIN_SIZE]

  def get_minibatch(X,Y,bs):
    xs, ys = [], []
    for _ in range(bs):
      sel = random.randint(0, len(X)-1)
      xs.append(X[sel])
      ys.append(Y[sel])
    return Tensor(xs), Tensor(ys)

  Tensor.no_grad, Tensor.training = False, True
  losses = []
  test_losses = []
  test_loss = float('inf')
  for i in (t:=trange(1000)):
    x,y = get_minibatch(X,Y,bs=128)
    out = net(x)
    loss = (out-y).square().mean()
    optim.zero_grad()
    loss.backward()
    optim.step()
    t.set_description(f"loss {loss.numpy():7.2f}, test loss {test_loss:7.2f}")
    losses.append(loss.numpy().item())
    test_losses.append(test_loss)
    if i % 10: test_loss = (net(X_test)-Y_test).square().mean().numpy().item()

  safe_save(get_state_dict(net), "/tmp/valuenet.safetensors")

  import matplotlib.pyplot as plt
  plt.plot(losses[50:])
  plt.plot(test_losses[50:])
  plt.show()
