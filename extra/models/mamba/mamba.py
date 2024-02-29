import math
import torch
# import torch.nn as nn
# import torch.nn.functional as F
from typing import List, Callable
from tinygrad import Tensor, TinyJit, nn, GlobalCounters
# from tinygrad.tensor import Tensor

# TODO: use Tensor.reshape for einops.rearrange


# TODO: do we need all of these params?
class Mamba:
  def __init__(self, 
    d_model, 
    d_state=16, 
    d_conv=4, 
    expand=2, 
    dt_rank="auto", 
    conv_bias=True, 
    bias=False, 
    layer_idx=None):
        
    self.d_model = d_model
    self.d_state = d_state
    self.d_conv = d_conv
    self.expand = expand
    self.d_inner = int(self.expand * self.d_model)
    self.dt_rank = math.ceil(self.d_model / 16) if dt_rank == "auto" else dt_rank
    self.layer_index = layer_idx

    self.in_proj = nn.Linear(self.d_model, self.d_inner * 2, bias=bias)
    self.conv2d == nn.Conv2d(
      in_channels=self.d_inner,
      out_channels=self.d_inner, 
      kernel_size=d_conv, 
      groups=self.d_inner, 
      padding=d_conv - 1
    )
      


# class Mamba(nn.Module):
#     def __init__(
#         self,
#         d_model,
#         d_state=16,
#         d_conv=4,
#         expand=2,
#         dt_rank="auto",
#         conv_bias=True,
#         bias=False,
#         layer_idx=None,
#     ):
#         super().__init__()
#         self.d_model = d_model
#         self.d_state = d_state
#         self.d_conv = d_conv
#         self.expand = expand
#         self.d_inner = int(self.expand * self.d_model)
#         self.dt_rank = math.ceil(self.d_model / 16) if dt_rank == "auto" else dt_rank
#         self.layer_idx = layer_idx

#         self.in_proj = nn.Linear(self.d_model, self.d_inner * 2, bias=bias)
#         self.conv1d = nn.Conv1d(
#             in_channels=self.d_inner,
#             out_channels=self.d_inner,
#             bias=conv_bias,
#             kernel_size=d_conv,
#             groups=self.d_inner,
#             padding=d_conv - 1,
#         )
#         self.x_proj = nn.Linear(self.d_inner, self.dt_rank + self.d_state * 2, bias=False)
#         self.dt_proj = nn.Linear(self.dt_rank, self.d_inner, bias=True)
#         self.A_log = nn.Parameter(torch.empty(self.d_inner, self.d_state))
#         self.A = None
#         self.D = nn.Parameter(torch.empty(self.d_inner))
#         self.out_proj = nn.Linear(self.d_inner, self.d_model, bias=bias)

#     def forward(self, hidden_states, inference_params=None):
#         """
#         hidden_states: (L, D)
#         Returns: same shape as hidden_states
#         """
#         seqlen, dim = hidden_states.shape
#         assert seqlen == 1, "Can decode only 1 token at a time"

#         conv_state, ssm_state = self._get_states_from_cache(inference_params)

#         xz = self.in_proj(hidden_states)  # (l 2d)
#         x, z = xz.chunk(2, dim=-1)  # (l d)

#         # Conv step
#         conv_state.copy_(torch.roll(conv_state, shifts=-1, dims=-1))  # Update state (d w)
#         conv_state[:, -1] = x
#         # x = torch.sum(conv_state * rearrange(self.conv1d.weight, "d 1 w -> d w"), dim=-1)  # (d)
#         x = torch.sum(conv_state * rearrange(self.conv1d.weight, "d 1 w -> d w"), dim=-1)
#         x = x + self.conv1d.bias
#         x = F.silu(x)

#         x_db = self.x_proj(x)  # (dt_rank+2*d_state)
#         dt, B, C = torch.split(x_db, [self.dt_rank, self.d_state, self.d_state], dim=-1)
#         dt = self.dt_proj(dt)  # (d_inner)
#         dt = F.softplus(dt)

#         # Initialize A only once per layer
#         if self.A is None:
#             self.A = -torch.exp(self.A_log.float())  # (d_inner, d_state)

#         # SSM step
#         # Discretize A and B
#         dA = torch.exp(torch.einsum("d,dn->dn", dt, self.A))
#         dB = torch.einsum("d,n->dn", dt, B)
#         ssm_state.copy_(ssm_state * dA + rearrange(x, "d -> d 1") * dB)
#         y = torch.einsum("dn,n->d", ssm_state, C)
#         y = y + self.D * x
#         y = y * F.silu(z)  # (d)

#         out = self.out_proj(y)
#         return out

#     def _get_states_from_cache(self, inference_params):
#         assert self.layer_idx is not None
#         if self.layer_idx not in inference_params.key_value_memory_dict:
#             conv_state = torch.zeros(self.d_inner, self.d_conv)
#             ssm_state  = torch.zeros(self.d_inner, self.d_state)
#             inference_params.key_value_memory_dict[self.layer_idx] = (conv_state, ssm_state)
#         else:
#             conv_state, ssm_state = inference_params.key_value_memory_dict[self.layer_idx]
#         return conv_state, ssm_state


# class Block(nn.Module):
#     def __init__(self, dim, mixer_cls, norm_cls):
#         """ Simple block wrapping a mixer class with RMSNorm and residual connection """
#         super().__init__()
#         self.mixer = mixer_cls(dim)
#         self.norm = norm_cls(dim)

#     def forward(self, hidden_states: Tensor, inference_params=None):
#         """ Pass the input through the encoder layer """
#         residual = hidden_states
#         hidden_states = self.norm(hidden_states)
#         hidden_states = self.mixer(hidden_states, inference_params=inference_params)
#         hidden_states += residual
#         return hidden_states