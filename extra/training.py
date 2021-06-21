import os
import numpy as np
from tqdm import trange
from extra.utils import get_parameters
from tinygrad.tensor import Tensor, GPU, Device
from tqdm import tqdm

def sparse_categorical_crossentropy(out, Y):
  num_classes = out.shape[-1]
  YY = Y.flatten()
  y = np.zeros((YY.shape[0], num_classes), np.float32)
  # correct loss for NLL, torch NLL loss returns one per row
  y[range(y.shape[0]),YY] = -1.0*num_classes
  y = y.reshape(list(Y.shape)+[num_classes])
  y = Tensor(y)
  return out.mul(y).mean()

def train(model, X_train, Y_train, optim, steps, BS=128, lossfn=sparse_categorical_crossentropy, 
        transform=lambda x: x, target_transform=lambda x: x):
  Tensor.training = True
  losses, accuracies = [], []
  for i in (t := trange(steps, disable=os.getenv('CI') is not None)):
    samp = np.random.randint(0, X_train.shape[0], size=(BS))
    x = Tensor(transform(X_train[samp]))
    y = target_transform(Y_train[samp])

    # network
    out = model.forward(x)

    loss = lossfn(out, y)
    optim.zero_grad()
    loss.backward()
    optim.step()

    cat = np.argmax(out.cpu().data, axis=-1)
    accuracy = (cat == y).mean()

    # printing
    loss = loss.cpu().data
    losses.append(loss)
    accuracies.append(accuracy)
    t.set_description("loss %.2f accuracy %.2f" % (loss, accuracy))

def evaluate(model, X_test, Y_test, num_classes=None, BS=128, return_predict=False, transform=lambda x: x, 
             target_transform=lambda y: y):
  Tensor.training = False
  def numpy_eval(Y_test, num_classes):
    Y_test_preds_out = np.zeros(list(Y_test.shape)+[num_classes])
    for i in trange((len(Y_test)-1)//BS+1, disable=os.getenv('CI') is not None):
      x = Tensor(transform(X_test[i*BS:(i+1)*BS]))
      Y_test_preds_out[i*BS:(i+1)*BS] = model.forward(x).cpu().data
    Y_test_preds = np.argmax(Y_test_preds_out, axis=-1)
    Y_test = target_transform(Y_test)
    return (Y_test == Y_test_preds).mean(), Y_test_preds

  if num_classes is None: num_classes = Y_test.max().astype(int)+1
  acc, Y_test_pred = numpy_eval(Y_test, num_classes)
  print("test set accuracy is %f" % acc)
  return (acc, Y_test_pred) if return_predict else acc

def train_w_dataloaders(model, dl_train, dl_test, optim, epochs, lossfn=sparse_categorical_crossentropy):
  losses_tr, accuracies_tr = [], []
  accuracies_te = []
  for epoch in range(epochs):
    loss, accuracy = single_datalaoder_pass(model, dl_train, optim, mode='train', lossfn=lossfn)
    losses_tr += [loss]
    accuracies_tr += [accuracy]

    accuracy = single_datalaoder_pass(model, dl_test, optim, mode='eval', lossfn=lossfn)
    accuracies_te += [accuracy]
    print(f'test set accuracy is {accuracy}')


def evaluate_w_dataloader(model, dataloader, optim, epochs, lossfn=sparse_categorical_crossentropy):
  return single_datalaoder_pass(model, dataloader, optim, mode='eval', lossfn=lossfn)

def single_datalaoder_pass(model, dataloader, optim, mode='train', return_predict=False,
                           lossfn=sparse_categorical_crossentropy):
  if mode == 'train':
    Tensor.training = True
  else:
    Tensor.training = False
  preds = []
  losses, accuracies = [], []
  for x, y in (t := tqdm(dataloader, desc=mode.capitalize())):
    x = Tensor(x)
    out = model.forward(x)
    if return_predict:
      preds += [out]
    loss = lossfn(out, y)
    optim.zero_grad()
    loss.backward()
    optim.step()

    cat = np.argmax(out.cpu().data, axis=-1)
    accuracy = (cat == y).mean()

    # printing
    loss = loss.cpu().data
    losses.append(loss[0])
    accuracies.append(accuracy)
    t.set_description(f'loss {loss[0]:.2f} accuracy {accuracy:.2f}')
  if mode == 'train':
    if return_predict:
      return np.mean(losses), np.mean(accuracies), np.concatenate(preds, 0)
    else:
      return np.mean(losses), np.mean(accuracies)
  else:
    if return_predict:
      return np.mean(accuracies), np.concatenate(preds, 0)
    else:
      return np.mean(accuracies)

