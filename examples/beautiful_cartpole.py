from typing import Tuple
from tinygrad import Tensor, TinyJit, nn
from tinygrad.helpers import dtypes  # TODO: wouldn't need this if argmax returned the right dtype
import gymnasium as gym
from tqdm import trange
import numpy as np  # TODO: remove numpy import

class ActorCritic:
  def __init__(self, in_features, out_features, hidden_state=32):
    self.l1 = nn.Linear(in_features, hidden_state)
    self.l2 = nn.Linear(hidden_state, out_features)

    self.c1 = nn.Linear(in_features, hidden_state)
    self.c2 = nn.Linear(hidden_state, 1)

  def __call__(self, obs:Tensor) -> Tensor:
    x = self.l1(obs).relu()
    act = self.l2(x).log_softmax()
    x = self.c1(obs).relu()
    return act, self.c2(x)

def evaluate(model:ActorCritic, test_env:gym.Env) -> float:
  (obs, _), terminated, truncated = test_env.reset(), False, False
  total_rew = 0.0
  while not terminated and not truncated:
    act = model(Tensor(obs))[0].argmax().cast(dtypes.int32).item()
    obs, rew, terminated, truncated, _ = test_env.step(act)
    total_rew += rew
  return total_rew

# TODO: time should be < 5s on M1 Max
if __name__ == "__main__":
  env = gym.make('CartPole-v1')

  model = ActorCritic(env.observation_space.shape[0], int(env.action_space.n))
  opt = nn.optim.Adam(nn.state.get_parameters(model), lr=1e-2)

  @TinyJit
  def train_step(x:Tensor, rtg:Tensor, mask:Tensor) -> Tuple[Tensor, Tensor, Tensor]:
    log_dist, value = model(x)
    advantage = rtg - value
    action_loss = -(log_dist * mask * advantage.detach()).sum(-1).mean()
    entropy_loss = (log_dist.exp() * log_dist).sum(-1).mean()   # this encourages diversity
    critic_loss = advantage.square().mean()
    (action_loss + entropy_loss*0.001 + critic_loss).backward()
    opt.step()
    return action_loss.realize(), entropy_loss.realize(), critic_loss.realize()

  @TinyJit
  def get_action_dist(obs:Tensor) -> Tensor: return model(obs)[0].exp().realize()

  BS = 128
  for i in (t:=trange(100)):
    Xn, Rn, Mn = [], [], []
    ep_rews = []
    get_action_dist.reset()   # NOTE: if you don't reset the jit here it captures the wrong model on the first run through
    while len(Xn) < BS:
      obs:np.ndarray = env.reset()[0]
      acts, rews, terminated, truncated = [], [], False, False
      # NOTE: we don't want to early stop since then the rewards are wrong for the last episode
      while not terminated and not truncated:
        # pick actions
        # TODO: move the multinomial into jitted tinygrad when JIT rand works
        # TODO: what's the temperature here?
        act = get_action_dist(Tensor(obs)).multinomial().item()

        # save this state action pair
        # TODO: don't use np.copy here on the CPU, what's the tinygrad way to do this and keep on device? need __setitem__ assignment
        Xn.append(np.copy(obs))
        acts.append(act)

        obs, rew, terminated, truncated, _ = env.step(act)
        rews.append(rew)
      ep_rews.append(sum(rews))

      # reward to go
      # TODO: move this into tinygrad
      for i, act in enumerate(acts):
        rew, discount = 0, 1.0
        for r in rews[i:]:
          rew += r * discount
          discount *= 0.9
        Rn.append([rew])
        act_mask = np.zeros((env.action_space.n), dtype=np.float32)
        act_mask[act] = 1.0
        Mn.append(act_mask)

    # TODO: this shouldn't be numpy
    X, R, M = np.array(Xn, dtype=np.float32), np.array(Rn, dtype=np.float32), np.array(Mn, dtype=np.float32)
    samples = Tensor.randint(BS, high=X.shape[0], device="CPU").numpy()
    action_loss, entropy_loss, critic_loss = train_step(Tensor(X[samples]), Tensor(R[samples]), Tensor(M[samples]))
    t.set_description(f"action_loss: {action_loss.item():6.2f} entropy_loss: {entropy_loss.item():6.2f} critic_loss: {critic_loss.item():6.2f} ep_count: {len(ep_rews):2d} avg_ep_rew: {sum(ep_rews)/len(ep_rews):6.2f}")

  test_rew = evaluate(model, gym.make('CartPole-v1', render_mode='human'))
  print(f"test reward: {test_rew}")
