import numpy as np
from tqdm import trange
from tinygrad.helpers import dtypes, getenv
from tinygrad.tensor import Tensor

def lr_warmup(optimizer, init_lr, lr, current_epoch, warmup_epochs):
  scale = current_epoch / warmup_epochs
  new_lr = init_lr + (lr - init_lr) * scale
  optimizer.lr.assign([new_lr])

def train(model, X_train, Y_train, optim, steps, BS=128, lossfn=lambda out,y: out.sparse_categorical_crossentropy(y),
        transform=lambda x: x, target_transform=lambda x: x, noloss=False):
  Tensor.training = True
  losses, accuracies = [], []
  for i in (t := trange(steps, disable=getenv('CI', False))):
    samp = np.random.randint(0, X_train.shape[0], size=(BS))
    x = Tensor(transform(X_train[samp]), requires_grad=False)
    y = Tensor(target_transform(Y_train[samp]))

    # network
    out = model.forward(x) if hasattr(model, 'forward') else model(x)

    loss = lossfn(out, y)
    optim.zero_grad()
    loss.backward()
    if noloss: del loss
    optim.step()

    # printing
    if not noloss:
      cat = out.argmax(axis=-1)
      accuracy = (cat == y).mean().numpy()

      loss = loss.detach().numpy()
      losses.append(loss)
      accuracies.append(accuracy)
      t.set_description("loss %.2f accuracy %.2f" % (loss, accuracy))
  return [losses, accuracies]


def evaluate(model, X_test, Y_test, num_classes=None, BS=128, return_predict=False, transform=lambda x: x,
             target_transform=lambda y: y):
  Tensor.training = False
  def numpy_eval(Y_test, num_classes):
    Y_test_preds_out = np.zeros(list(Y_test.shape)+[num_classes])
    for i in trange((len(Y_test)-1)//BS+1, disable=getenv('CI', False)):
      x = Tensor(transform(X_test[i*BS:(i+1)*BS]))
      out = model.forward(x) if hasattr(model, 'forward') else model(x)
      Y_test_preds_out[i*BS:(i+1)*BS] = out.numpy()
    Y_test_preds = np.argmax(Y_test_preds_out, axis=-1)
    Y_test = target_transform(Y_test)
    return (Y_test == Y_test_preds).mean(), Y_test_preds

  if num_classes is None: num_classes = Y_test.max().astype(int)+1
  acc, Y_test_pred = numpy_eval(Y_test, num_classes)
  print("test set accuracy is %f" % acc)
  return (acc, Y_test_pred) if return_predict else acc