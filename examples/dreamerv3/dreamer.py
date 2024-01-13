import functools
import pathlib

import distributions
import models
import numpy as np
import utils
from env import count_steps, load_episodes, make_dataset, make_envs, simulate

from tinygrad import Tensor
from tinygrad.nn.state import get_state_dict, load_state_dict, safe_load, safe_save


class Dreamer:
    def __init__(self, obs_space, act_space, config, logger, dataset):
        self._config = config
        self._logger = logger
        self._log_every = config.log_every
        batch_steps = config.batch_size * config.batch_length
        self._num_train_steps = batch_steps // config.train_ratio
        self._reset_every = config.reset_every
        self._expl_until = config.expl_until
        self.pretrained = False

        self._metrics = {}
        # this is update step
        self._step = logger.step // config.action_repeat
        self._update_count = 0
        self._dataset = dataset
        self._wm = models.WorldModel(obs_space, act_space, self._step, config)
        self._task_behavior = models.ImagBehavior(config, self._wm)
        # greedy exploration
        self._expl_behavior = self._task_behavior

    def __call__(self, obs, reset, state=None, training=True):
        if training:
            num_steps = self._config.pretrain if not self.pretrained else self._num_train_steps
            self.pretrained = True
            for _ in range(num_steps):
                self._train(next(self._dataset))
                self._update_count += 1
                self._metrics["update_count"] = self._update_count
            if self._step % self._log_every == 0:
                for name, values in self._metrics.items():
                    self._logger.scalar(name, float(np.mean(values)))
                    self._metrics[name] = []
                if self._config.video_pred_log:
                    openl = self._wm.video_pred(next(self._dataset))
                    self._logger.video("train_openl", openl)
                self._logger.write(fps=True)

        policy_output, state = self._policy(obs, state, training)

        if training:
            self._step += len(reset)
            self._logger.step = self._config.action_repeat * self._step
        return policy_output, state

    def _policy(self, obs, state, training):
        if state is None:
            latent = action = None
        else:
            latent, action = state
        obs = self._wm.preprocess(obs)
        embed = self._wm.encoder({k: v[:, None] for k, v in obs.items()})[:, 0]
        latent, _ = self._wm.dynamics.obs_step(latent, action, embed, obs["is_first"])
        if self._config.eval_state_mean:
            latent["stoch"] = latent["mean"]
        feat = self._wm.dynamics.get_feat(latent)
        if not training:
            actor = self._task_behavior.actor(feat)
            action = actor.mode
        elif self._step <= self._expl_until:
            actor = self._expl_behavior.actor(feat)
            action = actor.sample()
        else:
            actor = self._task_behavior.actor(feat)
            action = actor.sample()
        logprob = actor.log_prob(action)
        latent = {k: v.detach() for k, v in latent.items()}
        action = action.detach()
        policy_output = {"action": action, "logprob": logprob}
        state = (latent, action)
        return policy_output, state

    def _train(self, data):
        metrics = {}
        post, context, mets = self._wm._train(data)
        metrics.update(mets)
        start = post

        def reward(f, s, a):
            embed = self._wm.dynamics.get_feat(s)
            return self._wm.heads["reward"](embed).mode.squeeze(-1)

        metrics.update(self._task_behavior._train(start, reward)[-1])
        for name, value in metrics.items():
            if name not in self._metrics.keys():
                self._metrics[name] = [value]
            else:
                self._metrics[name].append(value)


def main():
    config = utils.load_config()
    utils.set_seed_everywhere(config.seed)
    logdir = pathlib.Path(config.logdir).expanduser()
    config.traindir = config.traindir or logdir / "train_eps"
    config.evaldir = config.evaldir or logdir / "eval_eps"

    print("Logdir", logdir)
    logdir.mkdir(parents=True, exist_ok=True)
    config.traindir.mkdir(parents=True, exist_ok=True)
    config.evaldir.mkdir(parents=True, exist_ok=True)
    step = count_steps(config.traindir)
    # step in logger is environmental step
    logger = utils.Logger(logdir, config.action_repeat * step)

    print("Create envs.")
    if config.offline_traindir:
        directory = config.offline_traindir.format(**vars(config))
    else:
        directory = config.traindir
    train_eps = load_episodes(directory, limit=config.dataset_size)
    if config.offline_evaldir:
        directory = config.offline_evaldir.format(**vars(config))
    else:
        directory = config.evaldir
    eval_eps = load_episodes(directory, limit=1)

    train_envs = make_envs(config)
    eval_envs = make_envs(config)
    act_space = train_envs[0].action_space
    print("Action Space", act_space)

    state = None
    if not config.offline_traindir:
        prefill = max(0, config.prefill - count_steps(config.traindir))
        print(f"Prefill dataset ({prefill} steps).")

        def random_agent(o, d, s):
            if config.actor["dist"] == "onehot":
                random_actor = distributions.OneHotCategorical(Tensor.zeros(int(act_space.n)).repeat((config.num_envs, 1)).to(config.device))
            else:
                random_actor = distributions.Independent(
                    distributions.Uniform(
                        Tensor(act_space.low).repeat((config.num_envs, 1)).to(config.device),
                        Tensor(act_space.high).repeat((config.num_envs, 1)).to(config.device),
                    ),
                    1,
                )
            action = random_actor.sample()
            logprob = random_actor.log_prob(action)
            return {"action": action, "logprob": logprob}, None

        state = simulate(random_agent, train_envs, train_eps, config.traindir, logger, limit=config.dataset_size, steps=prefill)
        logger.step += prefill * config.action_repeat
        print(f"Logger: ({logger.step} steps).")

    print("Simulate agent.")
    train_dataset = make_dataset(train_eps, config)
    eval_dataset = make_dataset(eval_eps, config)
    agent = Dreamer(train_envs[0].observation_space, train_envs[0].action_space, config, logger, train_dataset)
    if (logdir / "latest.safetensors").exists():
        state_dict = safe_load(logdir / "latest.safetensors")
        load_state_dict(agent, state_dict)
        agent.pretrained = True

    # make sure eval will be executed once after config.steps
    while agent._step < config.steps + config.eval_every:
        logger.write()
        if config.eval_episode_num > 0:
            print("Start evaluation.")
            eval_policy = functools.partial(agent, training=False)
            simulate(eval_policy, eval_envs, eval_eps, config.evaldir, logger, is_eval=True, episodes=config.eval_episode_num)
            if config.video_pred_log:
                video_pred = agent._wm.video_pred(next(eval_dataset))
                logger.video("eval_openl", video_pred)
        print("Start training.")
        state = simulate(agent, train_envs, train_eps, config.traindir, logger, limit=config.dataset_size, steps=config.eval_every, state=state)
        state_dict = get_state_dict(agent)
        safe_save(state_dict, logdir / "latest.safetensors")
    for env in train_envs + eval_envs:
        env.close()


if __name__ == "__main__":
    main()
