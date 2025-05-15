import argparse
import os
from test.determinism_test import AlgorithmDeterminismTest

import gymnasium as gym
import numpy as np
import torch
from gymnasium.spaces import Box
from torch.utils.tensorboard import SummaryWriter

from tianshou.data import Collector, CollectStats, VectorReplayBuffer
from tianshou.env import DummyVectorEnv
from tianshou.policy import A2C, OffPolicyImitationLearning
from tianshou.policy.base import Algorithm
from tianshou.policy.imitation.base import ImitationPolicy
from tianshou.policy.modelfree.pg import ActorPolicyProbabilistic
from tianshou.policy.optim import AdamOptimizerFactory
from tianshou.trainer.base import OffPolicyTrainerParams, OnPolicyTrainerParams
from tianshou.utils import TensorboardLogger
from tianshou.utils.net.common import Net
from tianshou.utils.net.discrete import DiscreteActor, DiscreteCritic

try:
    import envpool
except ImportError:
    envpool = None


def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=str, default="CartPole-v1")
    parser.add_argument("--reward-threshold", type=float, default=None)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--buffer-size", type=int, default=20000)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--il-lr", type=float, default=1e-3)
    parser.add_argument("--gamma", type=float, default=0.9)
    parser.add_argument("--epoch", type=int, default=10)
    parser.add_argument("--step-per-epoch", type=int, default=50000)
    parser.add_argument("--il-step-per-epoch", type=int, default=1000)
    parser.add_argument("--episode-per-collect", type=int, default=16)
    parser.add_argument("--step-per-collect", type=int, default=16)
    parser.add_argument("--update-per-step", type=float, default=1 / 16)
    parser.add_argument("--repeat-per-collect", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--hidden-sizes", type=int, nargs="*", default=[64, 64])
    parser.add_argument("--imitation-hidden-sizes", type=int, nargs="*", default=[128])
    parser.add_argument("--training-num", type=int, default=16)
    parser.add_argument("--test-num", type=int, default=100)
    parser.add_argument("--logdir", type=str, default="log")
    parser.add_argument("--render", type=float, default=0.0)
    parser.add_argument(
        "--device",
        type=str,
        default="cuda" if torch.cuda.is_available() else "cpu",
    )
    # a2c special
    parser.add_argument("--vf-coef", type=float, default=0.5)
    parser.add_argument("--ent-coef", type=float, default=0.0)
    parser.add_argument("--max-grad-norm", type=float, default=None)
    parser.add_argument("--gae-lambda", type=float, default=1.0)
    parser.add_argument("--rew-norm", action="store_true", default=False)
    return parser.parse_known_args()[0]


def test_a2c_with_il(
    args: argparse.Namespace = get_args(),
    enable_assertions: bool = True,
    skip_il: bool = False,
) -> None:
    # seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    if envpool is not None:
        train_envs = env = envpool.make(
            args.task,
            env_type="gymnasium",
            num_envs=args.training_num,
            seed=args.seed,
        )
        test_envs = envpool.make(
            args.task,
            env_type="gymnasium",
            num_envs=args.test_num,
            seed=args.seed,
        )
    else:
        env = gym.make(args.task)
        train_envs = DummyVectorEnv([lambda: gym.make(args.task) for _ in range(args.training_num)])
        test_envs = DummyVectorEnv([lambda: gym.make(args.task) for _ in range(args.test_num)])
        train_envs.seed(args.seed)
        test_envs.seed(args.seed)
    args.state_shape = env.observation_space.shape or env.observation_space.n
    args.action_shape = env.action_space.shape or env.action_space.n
    if args.reward_threshold is None:
        default_reward_threshold = {"CartPole-v1": 195}
        args.reward_threshold = default_reward_threshold.get(args.task, env.spec.reward_threshold)
    # model
    net = Net(state_shape=args.state_shape, hidden_sizes=args.hidden_sizes)
    actor = DiscreteActor(preprocess_net=net, action_shape=args.action_shape).to(args.device)
    critic = DiscreteCritic(preprocess_net=net).to(args.device)
    optim = AdamOptimizerFactory(lr=args.lr)
    dist = torch.distributions.Categorical
    policy = ActorPolicyProbabilistic(
        actor=actor,
        dist_fn=dist,
        action_scaling=isinstance(env.action_space, Box),
        action_space=env.action_space,
    )
    algorithm: A2C = A2C(
        policy=policy,
        critic=critic,
        optim=optim,
        gamma=args.gamma,
        gae_lambda=args.gae_lambda,
        vf_coef=args.vf_coef,
        ent_coef=args.ent_coef,
        max_grad_norm=args.max_grad_norm,
        return_scaling=args.rew_norm,
    )
    # collector
    train_collector = Collector[CollectStats](
        algorithm,
        train_envs,
        VectorReplayBuffer(args.buffer_size, len(train_envs)),
    )
    train_collector.reset()
    test_collector = Collector[CollectStats](algorithm, test_envs)
    test_collector.reset()
    # log
    log_path = os.path.join(args.logdir, args.task, "a2c")
    writer = SummaryWriter(log_path)
    logger = TensorboardLogger(writer)

    def save_best_fn(policy: Algorithm) -> None:
        torch.save(policy.state_dict(), os.path.join(log_path, "policy.pth"))

    def stop_fn(mean_rewards: float) -> bool:
        return mean_rewards >= args.reward_threshold

    # trainer
    result = algorithm.run_training(
        OnPolicyTrainerParams(
            train_collector=train_collector,
            test_collector=test_collector,
            max_epoch=args.epoch,
            step_per_epoch=args.step_per_epoch,
            repeat_per_collect=args.repeat_per_collect,
            episode_per_test=args.test_num,
            batch_size=args.batch_size,
            episode_per_collect=args.episode_per_collect,
            step_per_collect=None,
            stop_fn=stop_fn,
            save_best_fn=save_best_fn,
            logger=logger,
            test_in_train=True,
        )
    )

    if enable_assertions:
        assert stop_fn(result.best_reward)

    if skip_il:
        return

    # here we define an imitation collector with a trivial policy
    # if args.task == 'CartPole-v1':
    #     env.spec.reward_threshold = 190  # lower the goal
    net = Net(state_shape=args.state_shape, hidden_sizes=args.hidden_sizes)
    actor = DiscreteActor(preprocess_net=net, action_shape=args.action_shape).to(args.device)
    optim = AdamOptimizerFactory(lr=args.il_lr)
    il_policy = ImitationPolicy(
        actor=actor,
        action_space=env.action_space,
    )
    il_algorithm: OffPolicyImitationLearning = OffPolicyImitationLearning(
        policy=il_policy,
        optim=optim,
    )
    if envpool is not None:
        il_env = envpool.make(
            args.task,
            env_type="gymnasium",
            num_envs=args.test_num,
            seed=args.seed,
        )
    else:
        il_env = DummyVectorEnv(
            [lambda: gym.make(args.task) for _ in range(args.test_num)],
        )
        il_env.seed(args.seed)

    il_test_collector = Collector[CollectStats](
        il_algorithm,
        il_env,
    )
    train_collector.reset()
    result = il_algorithm.run_training(
        OffPolicyTrainerParams(
            train_collector=train_collector,
            test_collector=il_test_collector,
            max_epoch=args.epoch,
            step_per_epoch=args.il_step_per_epoch,
            step_per_collect=args.step_per_collect,
            episode_per_test=args.test_num,
            batch_size=args.batch_size,
            stop_fn=stop_fn,
            save_best_fn=save_best_fn,
            logger=logger,
            test_in_train=True,
        )
    )

    if enable_assertions:
        assert stop_fn(result.best_reward)


def test_ppo_determinism() -> None:
    main_fn = lambda args: test_a2c_with_il(args, enable_assertions=False, skip_il=True)
    AlgorithmDeterminismTest("discrete_a2c", main_fn, get_args()).run()
