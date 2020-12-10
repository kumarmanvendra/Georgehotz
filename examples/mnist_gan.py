#!/usr/bin/env python
import os
import sys
import numpy as np
from tqdm import tqdm
sys.path.append(os.getcwd())
sys.path.append(os.path.join(os.getcwd(), 'test'))

from tinygrad.tensor import Tensor, Function, register
from tinygrad.utils import get_parameters
import tinygrad.optim as optim
from test_mnist import X_train
from torchvision.utils import make_grid, save_image
import torch

GPU = os.getenv("GPU") is not None

class LinearGen:
  def __init__(self):
    lv = 128
    self.l1 = Tensor.uniform(128, 256)
    self.l2 = Tensor.uniform(256, 512)
    self.l3 = Tensor.uniform(512, 1024)
    self.l4 = Tensor.uniform(1024, 784)

  def forward(self, x):
    x = x.dot(self.l1).leakyrelu(0.2)
    x = x.dot(self.l2).leakyrelu(0.2)
    x = x.dot(self.l3).leakyrelu(0.2)
    x = x.dot(self.l4).tanh()
    return x

class LinearDisc:
  def __init__(self):
    in_sh = 784
    self.l1 = Tensor.uniform(784, 1024)
    self.l2 = Tensor.uniform(1024, 512)
    self.l3 = Tensor.uniform(512, 256)
    self.l4 = Tensor.uniform(256, 1)

  def forward(self, x, train=True):
    x = x.dot(self.l1).leakyrelu(0.2)
    if train:
        x = x.dropout(0.3)
    x = x.dot(self.l2).leakyrelu(0.2)
    if train:
        x = x.dropout(0.3)
    x = x.dot(self.l3).leakyrelu(0.2)
    if train:
        x = x.dropout(0.3)
    x = x.dot(self.l4).sigmoid()
    return x

if __name__ == "__main__":
    generator = LinearGen()
    discriminator = LinearDisc()
    batch_size = 64
    k = 1
    epochs = 100
    generator_params = get_parameters(generator)
    discriminator_params = get_parameters(discriminator)
    gen_loss = []
    disc_loss = []
    train_data_size = len(X_train)
    if GPU:
      [x.cuda_() for x in generator_params+discriminator_params]
    # optimizers
    optim_g = optim.Adam(generator_params, lr=0.001)
    optim_d = optim.Adam(discriminator_params, lr=0.001)

    def train_loader():
        for _ in range(int(train_data_size/batch_size)):
            idx =np.random.randint(0, X_train.shape[0], size=(batch_size))
            X = Tensor(X_train[idx].reshape((-1,28*28)).astype(np.float32), gpu=GPU)
            yield X

    def real_label(bs):
        y = np.zeros((bs,2), np.float32)
        y[range(bs), [1]*bs] = -2.0
        real_labels = Tensor(y, gpu=GPU)
        return real_labels

    def fake_label(bs):
        y = np.zeros((bs,2), np.float32)
        y[range(bs), [0]*bs] = -2.0
        fake_labels = Tensor(y, gpu=GPU)
        return fake_labels

    def train_discriminator(optimizer, data_real, data_fake):
        real_labels = real_label(batch_size)
        fake_labels = fake_label(batch_size)

        optimizer.zero_grad()

        output_real = discriminator.forward(data_real)
        loss_real = (output_real.logsoftmax() * real_labels).mean()

        output_fake = discriminator.forward(data_fake)
        loss_fake = (output_fake.logsoftmax() * fake_labels).mean()

        loss_real.backward()
        loss_fake.backward()
        optimizer.step()

        return loss_real + loss_fake

    def train_generator(optimizer, data_fake):
        real_labels = real_label(batch_size)
        optimizer.zero_grad()
        output = discriminator.forward(data_fake)
        loss = (output.logsoftmax() * real_labels).mean()
        loss.backward()
        optimizer.step()
        return loss
    ds_noise = Tensor(np.random.uniform(size=(10,128)), gpu=GPU)

    for epoch in range(epochs):
        loss_g = 0.0
        loss_d = 0.0
        print(f"Epoch {epoch} of {epochs}")
        for i, image in tqdm(enumerate(train_loader()), total=int(60000/batch_size)):
            for step in range(k):
                noise = Tensor(np.random.uniform(size=(batch_size,128)), gpu=GPU)
                data_fake = generator.forward(noise).detach()
                data_real = image
                loss_d += train_discriminator(optim_d, data_real, data_fake)
            noise = Tensor(np.random.uniform(size=(batch_size,128)), gpu=GPU)
            data_fake = generator.forward(noise)
            loss_g += train_generator(optim_g, data_fake)
        fake_images = generator.forward(ds_noise).cpu().data
        fake_images = make_grid(torch.tensor(fake_images))
        save_image(fake_images, f"image_{epoch}.jpg")
        epoch_loss_g = loss_g.cpu().data / i
        epoch_loss_d = loss_d.cpu().data / i
        print(f"Generator loss: {epoch_loss_g}, Discriminator loss: {epoch_loss_d}")
    else:
        print("Training Completed!")

