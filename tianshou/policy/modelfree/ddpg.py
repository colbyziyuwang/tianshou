import warnings
from abc import ABC
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar, cast

import gymnasium as gym
import numpy as np
import torch
from sensai.util.helper import mark_used

from tianshou.data import Batch, ReplayBuffer
from tianshou.data.batch import BatchProtocol
from tianshou.data.types import (
    ActBatchProtocol,
    ActStateBatchProtocol,
    BatchWithReturnsProtocol,
    ObsBatchProtocol,
    RolloutBatchProtocol,
)
from tianshou.exploration import BaseNoise, GaussianNoise
from tianshou.policy import Algorithm
from tianshou.policy.base import (
    LaggedNetworkPolyakUpdateAlgorithmMixin,
    OffPolicyAlgorithm,
    Policy,
    TArrOrActBatch,
    TPolicy,
    TrainingStats,
    TTrainingStats,
)
from tianshou.policy.optim import OptimizerFactory
from tianshou.utils.net.continuous import ContinuousActorDeterministic, ContinuousCritic

mark_used(ActBatchProtocol)


@dataclass(kw_only=True)
class DDPGTrainingStats(TrainingStats):
    actor_loss: float
    critic_loss: float


TDDPGTrainingStats = TypeVar("TDDPGTrainingStats", bound=DDPGTrainingStats)


class ContinuousPolicyWithExplorationNoise(Policy, ABC):
    def __init__(
        self,
        *,
        exploration_noise: BaseNoise | Literal["default"] | None = None,
        action_space: gym.Space,
        observation_space: gym.Space | None = None,
        action_scaling: bool = True,
        action_bound_method: Literal["clip"] | None = "clip",
    ):
        """
        :param exploration_noise: noise model for adding noise to continuous actions
            for exploration. This is useful when solving "hard exploration" problems.
            "default" is equivalent to GaussianNoise(sigma=0.1).
        :param action_space: Env's action space.
        :param observation_space: Env's observation space.
        :param action_scaling: if True, scale the action from [-1, 1] to the range
            of action_space. Only used if the action_space is continuous.
        :param action_bound_method: method to bound action to range [-1, 1].
        """
        super().__init__(
            action_space=action_space,
            observation_space=observation_space,
            action_scaling=action_scaling,
            action_bound_method=action_bound_method,
        )
        if exploration_noise == "default":
            exploration_noise = GaussianNoise(sigma=0.1)
        self.exploration_noise = exploration_noise

    def set_exploration_noise(self, noise: BaseNoise | None) -> None:
        """Set the exploration noise."""
        self.exploration_noise = noise

    def add_exploration_noise(
        self,
        act: TArrOrActBatch,
        batch: ObsBatchProtocol,
    ) -> TArrOrActBatch:
        if self.exploration_noise is None:
            return act
        if isinstance(act, np.ndarray):
            return act + self.exploration_noise(act.shape)
        warnings.warn("Cannot add exploration noise to non-numpy_array action.")
        return act


class DDPGPolicy(ContinuousPolicyWithExplorationNoise):
    def __init__(
        self,
        *,
        actor: torch.nn.Module | ContinuousActorDeterministic,
        exploration_noise: BaseNoise | Literal["default"] | None = None,
        action_space: gym.Space,
        observation_space: gym.Space | None = None,
        action_scaling: bool = True,
        action_bound_method: Literal["clip"] | None = "clip",
    ):
        """
        :param actor: The actor network following the rules (s -> actions)
        :param exploration_noise: add noise to continuous actions for exploration;
            set to None for discrete action spaces.
            This is useful when solving "hard exploration" problems.
            "default" is equivalent to GaussianNoise(sigma=0.1).
        :param action_space: Env's action space.
        :param tau: Param for soft update of the target network.
        :param observation_space: Env's observation space.
        :param action_scaling: if True, scale the action from [-1, 1] to the range
            of action_space. Only used if the action_space is continuous.
        :param action_bound_method: method to bound action to range [-1, 1].
        """
        if action_scaling and not np.isclose(actor.max_action, 1.0):
            warnings.warn(
                "action_scaling and action_bound_method are only intended to deal"
                "with unbounded model action space, but find actor model bound"
                f"action space with max_action={actor.max_action}."
                "Consider using unbounded=True option of the actor model,"
                "or set action_scaling to False and action_bound_method to None.",
            )
        super().__init__(
            exploration_noise=exploration_noise,
            action_space=action_space,
            observation_space=observation_space,
            action_scaling=action_scaling,
            action_bound_method=action_bound_method,
        )
        self.actor = actor

    def forward(
        self,
        batch: ObsBatchProtocol,
        state: dict | BatchProtocol | np.ndarray | None = None,
        model: torch.nn.Module | None = None,
        **kwargs: Any,
    ) -> ActStateBatchProtocol:
        """Compute action over the given batch data.

        :return: A :class:`~tianshou.data.Batch` which has 2 keys:

            * ``act`` the action.
            * ``state`` the hidden state.

        .. seealso::

            Please refer to :meth:`~tianshou.policy.BasePolicy.forward` for
            more detailed explanation.
        """
        if model is None:
            model = self.actor
        actions, hidden = model(batch.obs, state=state, info=batch.info)
        return cast(ActStateBatchProtocol, Batch(act=actions, state=hidden))


TActBatchProtocol = TypeVar("TActBatchProtocol", bound=ActBatchProtocol)


class ActorCriticOffPolicyAlgorithm(
    OffPolicyAlgorithm[TPolicy, TTrainingStats],
    LaggedNetworkPolyakUpdateAlgorithmMixin,
    Generic[TPolicy, TTrainingStats, TActBatchProtocol],
    ABC,
):
    """Base class for actor-critic off-policy algorithms that use a lagged critic
    as a target network.

    Its implementation of `process_fn` adds the n-step return to the batch, using the
    Q-values computed by the target network (lagged critic, `critic_old`) in order to compute the
    reward-to-go.

    Specializations can override the action computation (`_target_q_compute_action`) or the
    Q-value computation based on these actions (`_target_q_compute_value`) to customize the
    target Q-value computation.
    The default implementation assumes a continuous action space where a critic receives a
    state/observation and an action; for discrete action spaces, where the critic receives only
    a state/observation, the method `_target_q_compute_value` must be overridden.
    """

    def __init__(
        self,
        *,
        policy: Any,
        policy_optim: OptimizerFactory,
        critic: torch.nn.Module,
        critic_optim: OptimizerFactory,
        tau: float = 0.005,
        gamma: float = 0.99,
        estimation_step: int = 1,
    ) -> None:
        """
        :param policy: the policy
        :param policy_optim: the optimizer for actor network.
        :param critic: the critic network.
            For continuous action spaces: (s, a -> Q(s, a)).
            For discrete action spaces: (s -> <Q(s, a_1), ..., Q(s, a_N)>).
            NOTE: The default implementation of `_target_q_compute_value` assumes
                a continuous action space; override this method if using discrete actions.
        :param critic_optim: the optimizer for the critic network.
        :param tau: param for soft update of the target network.
        :param gamma: discount factor, in [0, 1].
        :param lr_scheduler: a learning rate scheduler that adjusts the learning rate
            in optimizer in each policy.update()
        """
        assert 0.0 <= tau <= 1.0, f"tau should be in [0, 1] but got: {tau}"
        assert 0.0 <= gamma <= 1.0, f"gamma should be in [0, 1] but got: {gamma}"
        super().__init__(
            policy=policy,
        )
        LaggedNetworkPolyakUpdateAlgorithmMixin.__init__(self, tau=tau)
        self.policy_optim = self._create_optimizer(policy, policy_optim)
        self.critic = critic
        self.critic_old = self._add_lagged_network(self.critic)
        self.critic_optim = self._create_optimizer(self.critic, critic_optim)
        self.gamma = gamma
        self.estimation_step = estimation_step

    @staticmethod
    def _minimize_critic_squared_loss(
        batch: RolloutBatchProtocol,
        critic: torch.nn.Module,
        optimizer: Algorithm.Optimizer,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Takes an optimizer step to minimize the squared loss of the critic given a batch of data.

        :param batch: the batch containing the observations, actions, returns, and (optionally) weights.
        :param critic: the critic network to minimize the loss for.
        :param optimizer: the optimizer for the critic's parameters.
        :return: a pair (`td`, `loss`), where `td` is the tensor of errors (current - target) and `loss` is the MSE loss.
        """
        weight = getattr(batch, "weight", 1.0)
        current_q = critic(batch.obs, batch.act).flatten()
        target_q = batch.returns.flatten()
        td = current_q - target_q
        critic_loss = (td.pow(2) * weight).mean()
        optimizer.step(critic_loss)
        return td, critic_loss

    def preprocess_batch(
        self,
        batch: RolloutBatchProtocol,
        buffer: ReplayBuffer,
        indices: np.ndarray,
    ) -> RolloutBatchProtocol | BatchWithReturnsProtocol:
        # add the n-step return to the batch, which the critic (Q-functions) seeks to match,
        # based the Q-values computed by the target network (lagged critic)
        return self.compute_nstep_return(
            batch=batch,
            buffer=buffer,
            indices=indices,
            target_q_fn=self._target_q,
            gamma=self.gamma,
            n_step=self.estimation_step,
        )

    def _target_q_compute_action(self, obs_batch: Batch) -> TActBatchProtocol:
        """
        Computes the action to be taken for the given batch (containing the observations)
        within the context of Q-value target computation.

        :param obs_batch: the batch containing the observations.
        :return: batch containing the actions to be taken.
        """
        return self.policy(obs_batch)

    def _target_q_compute_value(
        self, obs_batch: Batch, act_batch: TActBatchProtocol
    ) -> torch.Tensor:
        """
        Computes the target Q-value given a batch with observations and actions taken.

        :param obs_batch: the batch containing the observations.
        :param act_batch: the batch containing the actions taken.
        :return: a tensor containing the target Q-values.
        """
        # compute the target Q-value using the lagged critic network (target network)
        return self.critic_old(obs_batch.obs, act_batch.act)

    def _target_q(self, buffer: ReplayBuffer, indices: np.ndarray) -> torch.Tensor:
        """
        Computes the target Q-value for the given buffer and indices.

        :param buffer: the replay buffer
        :param indices: the indices within the buffer to compute the target Q-value for
        """
        obs_next_batch = Batch(
            obs=buffer[indices].obs_next,
            info=[None] * len(indices),
        )  # obs_next: s_{t+n}
        act_batch = self._target_q_compute_action(obs_next_batch)
        return self._target_q_compute_value(obs_next_batch, act_batch)


class DDPG(
    ActorCriticOffPolicyAlgorithm[DDPGPolicy, TDDPGTrainingStats, ActBatchProtocol],
    Generic[TDDPGTrainingStats],
):
    """Implementation of Deep Deterministic Policy Gradient. arXiv:1509.02971."""

    def __init__(
        self,
        *,
        policy: DDPGPolicy,
        policy_optim: OptimizerFactory,
        critic: torch.nn.Module | ContinuousCritic,
        critic_optim: OptimizerFactory,
        tau: float = 0.005,
        gamma: float = 0.99,
        estimation_step: int = 1,
    ) -> None:
        """
        :param policy: the policy
        :param policy_optim: The optimizer for actor network.
        :param critic: The critic network. (s, a -> Q(s, a))
        :param critic_optim: The optimizer for critic network.
        :param tau: Param for soft update of the target network.
        :param gamma: Discount factor, in [0, 1].
        :param estimation_step: The number of steps to look ahead.
        :param lr_scheduler: if not None, will be called in `policy.update()`.
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
        self.actor_old = self._add_lagged_network(self.policy.actor)

    def _target_q_compute_action(self, obs_batch: Batch) -> ActBatchProtocol:
        # compute the action using the lagged actor network
        return self.policy(obs_batch, model=self.actor_old)

    def _update_with_batch(self, batch: RolloutBatchProtocol) -> TDDPGTrainingStats:  # type: ignore
        # critic
        td, critic_loss = self._minimize_critic_squared_loss(batch, self.critic, self.critic_optim)
        batch.weight = td  # prio-buffer
        # actor
        actor_loss = -self.critic(batch.obs, self.policy(batch).act).mean()
        self.policy_optim.step(actor_loss)
        self._update_lagged_network_weights()

        return DDPGTrainingStats(actor_loss=actor_loss.item(), critic_loss=critic_loss.item())  # type: ignore[return-value]
