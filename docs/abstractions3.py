# abstractions2 goes from back to front, here we will go from front to back
from typing import List
from tqdm import tqdm
from tinygrad.helpers import DEBUG

# *****
# 0. Load mnist on the device

from tinygrad.features.datasets import mnist
X_train, Y_train, _, _ = mnist()
X_train = X_train.float()
X_train -= X_train.mean()

# *****
# 1. Define an MNIST model.

from tinygrad import Tensor

l1 = Tensor.kaiming_uniform(128, 784)
l2 = Tensor.kaiming_uniform(10, 128)
def model(x): return x.flatten(1).dot(l1.T).relu().dot(l2.T)
l1n, l2n = l1.numpy(), l2.numpy()

# *****
# 2. Choose a batch for training and do the backward pass.

from tinygrad.nn.optim import SGD
optim = SGD([l1, l2])

X, Y = X_train[samples:=Tensor.randint(128, high=X_train.shape[0])], Y_train[samples]
optim.zero_grad()
model(X).sparse_categorical_crossentropy(Y).backward()
optim._step()

# *****
# 3. Create a schedule.

# The weight Tensors have been assigned to, but not yet realized. Everything is still lazy at this point
# l1.lazydata and l2.lazydata define a computation graph

from tinygrad.engine.schedule import create_schedule_with_vars
schedule, var_vals = create_schedule_with_vars([l1.lazydata, l2.lazydata])

print(f"The schedule contains {len(schedule)} items.")
for si in schedule: print(str(si)[:80])

# *****
# 4. Lower a schedule.

from tinygrad.engine.realize import lower_schedule_item, ExecItem
lowered: List[ExecItem] = [ExecItem(lower_schedule_item(si), list(si.outputs+si.inputs)) for si in tqdm(schedule)]

# *****
# 5. Run the schedule

for ei in lowered: ei.run(var_vals)

# *****
# 6. Print the weight change

print("first weight change\n", l1.numpy()-l1n)
print("second weight change\n", l2.numpy()-l2n)
