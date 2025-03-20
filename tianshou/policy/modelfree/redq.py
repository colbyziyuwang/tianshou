from dataclasses import dataclass
from typing import Any, Literal, TypeVar, cast

import gymnasium as gym
import numpy as np
import torch
from torch.distributions import Independent, Normal

from tianshou.data import Batch
from tianshou.data.types import (
    DistLogProbBatchProtocol,
    ObsBatchProtocol,
    RolloutBatchProtocol,
)
from tianshou.exploration import BaseNoise
from tianshou.policy.modelfree.ddpg import (
    ActorCriticOffPolicyAlgorithm,
    ContinuousPolicyWithExplorationNoise,
    DDPGTrainingStats,
)
from tianshou.policy.modelfree.sac import Alpha
from tianshou.policy.optim import OptimizerFactory
from tianshou.utils.net.continuous import ContinuousActorProb


@dataclass
class REDQTrainingStats(DDPGTrainingStats):
    """A data structure for storing loss statistics of the REDQ learn step."""

    alpha: float | None = None
    alpha_loss: float | None = None


TREDQTrainingStats = TypeVar("TREDQTrainingStats", bound=REDQTrainingStats)


class REDQPolicy(ContinuousPolicyWithExplorationNoise):
    def __init__(
        self,
        *,
        actor: torch.nn.Module | ContinuousActorProb,
        exploration_noise: BaseNoise | Literal["default"] | None = None,
        action_space: gym.spaces.Space,
        deterministic_eval: bool = True,
        action_scaling: bool = True,
        action_bound_method: Literal["clip"] | None = "clip",
        observation_space: gym.Space | None = None,
    ):
        """
        :param actor: The actor network following the rules in
            :class:`~tianshou.policy.BasePolicy`. (s -> model_output)
        :param action_space: Env's action space.
        :param deterministic_eval: whether, in evaluation/inference mode, to use always
            use the most probable action instead of sampling an action from the
            categorical distribution. This setting does not affect data collection
            for training, where actions are always sampled.
        :param observation_space: Env's observation space.
        :param action_scaling: if True, scale the action from [-1, 1] to the range
            of action_space. Only used if the action_space is continuous.
        :param action_bound_method: method to bound action to range [-1, 1].
            Only used if the action_space is continuous.
        """
        super().__init__(
            exploration_noise=exploration_noise,
            action_space=action_space,
            observation_space=observation_space,
            action_scaling=action_scaling,
            action_bound_method=action_bound_method,
        )
        self.actor = actor
        self.deterministic_eval = deterministic_eval
        self._eps = np.finfo(np.float32).eps.item()

    def forward(  # type: ignore
        self,
        batch: ObsBatchProtocol,
        state: dict | Batch | np.ndarray | None = None,
        **kwargs: Any,
    ) -> DistLogProbBatchProtocol:
        (loc_B, scale_B), h_BH = self.actor(batch.obs, state=state, info=batch.info)
        dist = Independent(Normal(loc_B, scale_B), 1)
        if self.deterministic_eval and not self.is_within_training_step:
            act_B = dist.mode
        else:
            act_B = dist.rsample()
        log_prob = dist.log_prob(act_B).unsqueeze(-1)
        # apply correction for Tanh squashing when computing logprob from Gaussian
        # You can check out the original SAC paper (arXiv 1801.01290): Eq 21.
        # in appendix C to get some understanding of this equation.
        squashed_action = torch.tanh(act_B)
        log_prob = log_prob - torch.log((1 - squashed_action.pow(2)) + self._eps).sum(
            -1,
            keepdim=True,
        )
        result = Batch(
            logits=(loc_B, scale_B),
            act=squashed_action,
            state=h_BH,
            dist=dist,
            log_prob=log_prob,
        )
        return cast(DistLogProbBatchProtocol, result)


class REDQ(ActorCriticOffPolicyAlgorithm[REDQPolicy, TREDQTrainingStats, DistLogProbBatchProtocol]):
    """Implementation of REDQ. arXiv:2101.05982."""

    def __init__(
        self,
        *,
        policy: REDQPolicy,
        policy_optim: OptimizerFactory,
        critic: torch.nn.Module,
        critic_optim: OptimizerFactory,
        ensemble_size: int = 10,
        subset_size: int = 2,
        tau: float = 0.005,
        gamma: float = 0.99,
        alpha: float | Alpha = 0.2,
        estimation_step: int = 1,
        actor_delay: int = 20,
        deterministic_eval: bool = True,
        target_mode: Literal["mean", "min"] = "min",
    ) -> None:
        """
        :param policy: the policy
        :param policy_optim: The optimizer for actor network.
        :param critic: The critic network. (s, a -> Q(s, a))
        :param critic_optim: The optimizer for critic network.
        :param ensemble_size: Number of sub-networks in the critic ensemble.
        :param subset_size: Number of networks in the subset.
        :param tau: Param for soft update of the target network.
        :param gamma: Discount factor, in [0, 1].
        :param alpha: the entropy regularization coefficient alpha or an object
            which can be used to automatically tune it (e.g. an instance of `AutoAlpha`).
        :param estimation_step: The number of steps to look ahead.
        :param actor_delay: Number of critic updates before an actor update.
        """
        if target_mode not in ("min", "mean"):
            raise ValueError(f"Unsupported target_mode: {target_mode}")
        if not 0 < subset_size <= ensemble_size:
            raise ValueError(
                f"Invalid choice of ensemble size or subset size. "
                f"Should be 0 < {subset_size=} <= {ensemble_size=}",
            )
        super().__init__(
            policy=policy,
            policy_optim=policy_optim,
            critic=critic,
            critic_optim=critic_optim,
            tau=tau,
            gamma=gamma,
            estimation_step=estimation_step,
        )
        self.ensemble_size = ensemble_size
        self.subset_size = subset_size

        self.target_mode = target_mode
        self.critic_gradient_step = 0
        self.actor_delay = actor_delay
        self.deterministic_eval = deterministic_eval
        self.__eps = np.finfo(np.float32).eps.item()

        self._last_actor_loss = 0.0  # only for logging purposes

        self.alpha = Alpha.from_float_or_instance(alpha)

    def _target_q_compute_value(
        self, obs_batch: Batch, act_batch: DistLogProbBatchProtocol
    ) -> torch.Tensor:
        a_ = act_batch.act
        sample_ensemble_idx = np.random.choice(self.ensemble_size, self.subset_size, replace=False)
        qs = self.critic_old(obs_batch.obs, a_)[sample_ensemble_idx, ...]
        if self.target_mode == "min":
            target_q, _ = torch.min(qs, dim=0)
        elif self.target_mode == "mean":
            target_q = torch.mean(qs, dim=0)
        else:
            raise ValueError(f"Invalid target_mode: {self.target_mode}")

        target_q -= self.alpha.value * act_batch.log_prob

        return target_q

    def _update_with_batch(self, batch: RolloutBatchProtocol) -> TREDQTrainingStats:  # type: ignore
        # critic ensemble
        weight = getattr(batch, "weight", 1.0)
        current_qs = self.critic(batch.obs, batch.act).flatten(1)
        target_q = batch.returns.flatten()
        td = current_qs - target_q
        critic_loss = (td.pow(2) * weight).mean()
        self.critic_optim.step(critic_loss)
        batch.weight = torch.mean(td, dim=0)  # prio-buffer
        self.critic_gradient_step += 1

        alpha_loss = None
        # actor
        if self.critic_gradient_step % self.actor_delay == 0:
            obs_result = self.policy(batch)
            a = obs_result.act
            current_qa = self.critic(batch.obs, a).mean(dim=0).flatten()
            actor_loss = (self.alpha.value * obs_result.log_prob.flatten() - current_qa).mean()
            self.policy_optim.step(actor_loss)

            # The entropy of a Gaussian policy can be expressed as -log_prob + a constant (which we ignore)
            entropy = -obs_result.log_prob.detach()
            alpha_loss = self.alpha.update(entropy)

            self._last_actor_loss = actor_loss.item()

        self._update_lagged_network_weights()

        return REDQTrainingStats(  # type: ignore[return-value]
            actor_loss=self._last_actor_loss,
            critic_loss=critic_loss.item(),
            alpha=self.alpha.value,
            alpha_loss=alpha_loss,
        )
