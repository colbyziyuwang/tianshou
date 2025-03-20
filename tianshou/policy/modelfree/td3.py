from abc import ABC
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import torch

from tianshou.data import Batch
from tianshou.data.types import (
    ActStateBatchProtocol,
    RolloutBatchProtocol,
)
from tianshou.policy.base import (
    TPolicy,
    TrainingStats,
    TTrainingStats,
)
from tianshou.policy.modelfree.ddpg import (
    ActorCriticOffPolicyAlgorithm,
    DDPGPolicy,
    TActBatchProtocol,
)
from tianshou.policy.optim import OptimizerFactory


@dataclass(kw_only=True)
class TD3TrainingStats(TrainingStats):
    actor_loss: float
    critic1_loss: float
    critic2_loss: float


TTD3TrainingStats = TypeVar("TTD3TrainingStats", bound=TD3TrainingStats)


class ActorDualCriticsOffPolicyAlgorithm(
    ActorCriticOffPolicyAlgorithm[TPolicy, TTrainingStats, TActBatchProtocol],
    Generic[TPolicy, TTrainingStats, TActBatchProtocol],
    ABC,
):
    """A base class for off-policy algorithms with two critics, where the target Q-value is computed as the minimum
    of the two lagged critics' values.
    """

    def __init__(
        self,
        *,
        policy: Any,
        policy_optim: OptimizerFactory,
        critic: torch.nn.Module,
        critic_optim: OptimizerFactory,
        critic2: torch.nn.Module | None = None,
        critic2_optim: OptimizerFactory | None = None,
        tau: float = 0.005,
        gamma: float = 0.99,
        estimation_step: int = 1,
    ) -> None:
        """
        :param policy: the policy
        :param policy_optim: the optimizer for actor network.
        :param critic: the first critic network.
            For continuous action spaces: (s, a -> Q(s, a)).
            NOTE: The default implementation of `_target_q_compute_value` assumes
                a continuous action space; override this method if using discrete actions.
        :param critic_optim: the optimizer for the first critic network.
        :param critic2: the second critic network (analogous functionality to the first).
            If None, use the same network as the first critic (via deepcopy).
        :param critic2_optim: the optimizer for the second critic network.
            If None, use critic_optim.
        :param tau: param for soft update of the target network.
        :param gamma: discount factor, in [0, 1].
        :param lr_scheduler: a learning rate scheduler that adjusts the learning rate
            in optimizer in each policy.update()
        """
        super().__init__(
            policy=policy,
            policy_optim=policy_optim,
            critic=critic,
            critic_optim=critic_optim,
            tau=tau,
            gamma=gamma,
            estimation_step=estimation_step,
        )
        self.critic2 = critic2 or deepcopy(critic)
        self.critic2_old = self._add_lagged_network(self.critic2)
        self.critic2_optim = self._create_optimizer(self.critic2, critic2_optim or critic_optim)

    def _target_q_compute_value(
        self, obs_batch: Batch, act_batch: TActBatchProtocol
    ) -> torch.Tensor:
        # compute the Q-value as the minimum of the two lagged critics
        act = act_batch.act
        return torch.min(
            self.critic_old(obs_batch.obs, act),
            self.critic2_old(obs_batch.obs, act),
        )


class TD3(
    ActorDualCriticsOffPolicyAlgorithm[DDPGPolicy, TTD3TrainingStats, ActStateBatchProtocol],
    Generic[TTD3TrainingStats],
):
    """Implementation of TD3, arXiv:1802.09477."""

    def __init__(
        self,
        *,
        policy: DDPGPolicy,
        policy_optim: OptimizerFactory,
        critic: torch.nn.Module,
        critic_optim: OptimizerFactory,
        critic2: torch.nn.Module | None = None,
        critic2_optim: OptimizerFactory | None = None,
        tau: float = 0.005,
        gamma: float = 0.99,
        policy_noise: float = 0.2,
        update_actor_freq: int = 2,
        noise_clip: float = 0.5,
        estimation_step: int = 1,
    ) -> None:
        """
        :param policy: the policy
        :param policy_optim: the optimizer for actor network.
        :param critic: the first critic network. (s, a -> Q(s, a))
        :param critic_optim: the optimizer for the first critic network.
        :param critic2: the second critic network. (s, a -> Q(s, a)).
            If None, use the same network as critic (via deepcopy).
        :param critic2_optim: the optimizer for the second critic network.
            If None, clone critic_optim to use for critic2.parameters().
        :param tau: param for soft update of the target network.
        :param gamma: discount factor, in [0, 1].
        :param policy_noise: the noise used in updating policy network.
        :param update_actor_freq: the update frequency of actor network.
        :param noise_clip: the clipping range used in updating policy network.
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
        self.actor_old = self._add_lagged_network(self.policy.actor)
        self.policy_noise = policy_noise
        self.update_actor_freq = update_actor_freq
        self.noise_clip = noise_clip
        self._cnt = 0
        self._last = 0

    def _target_q_compute_action(self, obs_batch: Batch) -> ActStateBatchProtocol:
        # compute action using lagged actor
        act_batch = self.policy(obs_batch, model=self.actor_old)
        act_ = act_batch.act

        # add noise
        noise = torch.randn(size=act_.shape, device=act_.device) * self.policy_noise
        if self.noise_clip > 0.0:
            noise = noise.clamp(-self.noise_clip, self.noise_clip)
        act_ += noise

        act_batch.act = act_
        return act_batch

    def _update_with_batch(self, batch: RolloutBatchProtocol) -> TTD3TrainingStats:  # type: ignore
        # critic 1&2
        td1, critic1_loss = self._minimize_critic_squared_loss(
            batch, self.critic, self.critic_optim
        )
        td2, critic2_loss = self._minimize_critic_squared_loss(
            batch, self.critic2, self.critic2_optim
        )
        batch.weight = (td1 + td2) / 2.0  # prio-buffer

        # actor
        if self._cnt % self.update_actor_freq == 0:
            actor_loss = -self.critic(batch.obs, self.policy(batch, eps=0.0).act).mean()
            self._last = actor_loss.item()
            self.policy_optim.step(actor_loss)
            self._update_lagged_network_weights()
        self._cnt += 1

        return TD3TrainingStats(  # type: ignore[return-value]
            actor_loss=self._last,
            critic1_loss=critic1_loss.item(),
            critic2_loss=critic2_loss.item(),
        )
