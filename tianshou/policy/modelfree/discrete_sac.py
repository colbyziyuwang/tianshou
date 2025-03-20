from dataclasses import dataclass
from typing import Any, TypeVar, cast

import gymnasium as gym
import numpy as np
import torch
from torch.distributions import Categorical

from tianshou.data import Batch, to_torch
from tianshou.data.types import (
    DistBatchProtocol,
    ObsBatchProtocol,
    RolloutBatchProtocol,
)
from tianshou.policy.base import Policy
from tianshou.policy.modelfree.sac import Alpha, SACTrainingStats
from tianshou.policy.modelfree.td3 import ActorDualCriticsOffPolicyAlgorithm
from tianshou.policy.optim import OptimizerFactory
from tianshou.utils.net.discrete import DiscreteCritic


@dataclass
class DiscreteSACTrainingStats(SACTrainingStats):
    pass


TDiscreteSACTrainingStats = TypeVar("TDiscreteSACTrainingStats", bound=DiscreteSACTrainingStats)


# TODO: This is a vanilla discrete actor policy; we may not need this "specific" class.
class DiscreteSACPolicy(Policy):
    def __init__(
        self,
        *,
        actor: torch.nn.Module,
        deterministic_eval: bool = True,
        action_space: gym.Space,
        observation_space: gym.Space | None = None,
    ):
        """
        :param actor: the actor network following the rules (s -> dist_input_BD),
            where the distribution input is for a `Categorical` distribution.
        :param deterministic_eval: whether, in evaluation/inference mode, to use always
            use the most probable action instead of sampling an action from the
            categorical distribution. This setting does not affect data collection
            for training, where actions are always sampled.
        :param action_space: the action space of the environment
        :param observation_space: the observation space of the environment
        """
        assert isinstance(action_space, gym.spaces.Discrete)
        super().__init__(
            action_space=action_space,
            observation_space=observation_space,
        )
        self.actor = actor
        self.deterministic_eval = deterministic_eval

    def forward(
        self,
        batch: ObsBatchProtocol,
        state: dict | Batch | np.ndarray | None = None,
        **kwargs: Any,
    ) -> Batch:
        logits_BA, hidden_BH = self.actor(batch.obs, state=state, info=batch.info)
        dist = Categorical(logits=logits_BA)
        act_B = (
            dist.mode
            if self.deterministic_eval and not self.is_within_training_step
            else dist.sample()
        )
        return Batch(logits=logits_BA, act=act_B, state=hidden_BH, dist=dist)


class DiscreteSAC(
    ActorDualCriticsOffPolicyAlgorithm[
        DiscreteSACPolicy, TDiscreteSACTrainingStats, DistBatchProtocol
    ]
):
    """Implementation of SAC for Discrete Action Settings. arXiv:1910.07207."""

    def __init__(
        self,
        *,
        policy: DiscreteSACPolicy,
        policy_optim: OptimizerFactory,
        critic: torch.nn.Module | DiscreteCritic,
        critic_optim: OptimizerFactory,
        critic2: torch.nn.Module | DiscreteCritic | None = None,
        critic2_optim: OptimizerFactory | None = None,
        tau: float = 0.005,
        gamma: float = 0.99,
        alpha: float | Alpha = 0.2,
        estimation_step: int = 1,
    ) -> None:
        """
        :param policy: the policy
        :param policy_optim: the optimizer for actor network.
        :param critic: the first critic network. (s -> <Q(s, a_1), ..., Q(s, a_N)>).
        :param critic_optim: the optimizer for the first critic network.
        :param critic2: the second critic network. (s -> <Q(s, a_1), ..., Q(s, a_N)>).
            If None, use the same network as critic (via deepcopy).
        :param critic2_optim: the optimizer for the second critic network.
            If None, clone critic_optim to use for critic2.parameters().
        :param tau: param for soft update of the target network.
        :param gamma: discount factor, in [0, 1].
        :param alpha: the entropy regularization coefficient alpha or an object
            which can be used to automatically tune it (e.g. an instance of `AutoAlpha`).
        :param estimation_step: the number of steps to look ahead for calculating
        :param lr_scheduler: a learning rate scheduler that adjusts the learning rate
            in optimizer in each policy.update()
        """
        super().__init__(
            policy=policy,
            policy_optim=policy_optim,
            critic=critic,
            critic_optim=critic_optim,
            critic2=critic2,
            critic2_optim=critic2_optim,
            tau=tau,
            gamma=gamma,
            estimation_step=estimation_step,
        )
        self.alpha = Alpha.from_float_or_instance(alpha)

    def _target_q_compute_value(
        self, obs_batch: Batch, act_batch: DistBatchProtocol
    ) -> torch.Tensor:
        dist = cast(Categorical, act_batch.dist)
        target_q = dist.probs * torch.min(
            self.critic_old(obs_batch.obs),
            self.critic2_old(obs_batch.obs),
        )
        return target_q.sum(dim=-1) + self.alpha.value * dist.entropy()

    def _update_with_batch(self, batch: RolloutBatchProtocol) -> TDiscreteSACTrainingStats:  # type: ignore
        weight = batch.pop("weight", 1.0)
        target_q = batch.returns.flatten()
        act = to_torch(batch.act[:, np.newaxis], device=target_q.device, dtype=torch.long)

        # critic 1
        current_q1 = self.critic(batch.obs).gather(1, act).flatten()
        td1 = current_q1 - target_q
        critic1_loss = (td1.pow(2) * weight).mean()
        self.critic_optim.step(critic1_loss)

        # critic 2
        current_q2 = self.critic2(batch.obs).gather(1, act).flatten()
        td2 = current_q2 - target_q
        critic2_loss = (td2.pow(2) * weight).mean()
        self.critic2_optim.step(critic2_loss)

        batch.weight = (td1 + td2) / 2.0  # prio-buffer

        # actor
        dist = self.policy(batch).dist
        entropy = dist.entropy()
        with torch.no_grad():
            current_q1a = self.critic(batch.obs)
            current_q2a = self.critic2(batch.obs)
            q = torch.min(current_q1a, current_q2a)
        actor_loss = -(self.alpha.value * entropy + (dist.probs * q).sum(dim=-1)).mean()
        self.policy_optim.step(actor_loss)

        alpha_loss = self.alpha.update(entropy.detach())

        self._update_lagged_network_weights()

        return DiscreteSACTrainingStats(  # type: ignore[return-value]
            actor_loss=actor_loss.item(),
            critic1_loss=critic1_loss.item(),
            critic2_loss=critic2_loss.item(),
            alpha=self.alpha.value,
            alpha_loss=alpha_loss,
        )
