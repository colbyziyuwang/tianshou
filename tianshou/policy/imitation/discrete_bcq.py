import math
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

import gymnasium as gym
import numpy as np
import torch
import torch.nn.functional as F

from tianshou.data import Batch, ReplayBuffer, to_torch
from tianshou.data.types import (
    BatchWithReturnsProtocol,
    ImitationBatchProtocol,
    ObsBatchProtocol,
    RolloutBatchProtocol,
)
from tianshou.policy.base import (
    LaggedNetworkFullUpdateAlgorithmMixin,
    OfflineAlgorithm,
    Policy,
)
from tianshou.policy.modelfree.dqn import DQNTrainingStats
from tianshou.policy.optim import OptimizerFactory

float_info = torch.finfo(torch.float32)
INF = float_info.max


@dataclass(kw_only=True)
class DiscreteBCQTrainingStats(DQNTrainingStats):
    q_loss: float
    i_loss: float
    reg_loss: float


TDiscreteBCQTrainingStats = TypeVar("TDiscreteBCQTrainingStats", bound=DiscreteBCQTrainingStats)


class DiscreteBCQPolicy(Policy):
    def __init__(
        self,
        *,
        model: torch.nn.Module,
        imitator: torch.nn.Module,
        target_update_freq: int = 8000,
        unlikely_action_threshold: float = 0.3,
        action_space: gym.spaces.Discrete,
        observation_space: gym.Space | None = None,
    ) -> None:
        """
        :param model: a model following the rules (s_B -> action_values_BA)
        :param imitator: a model following the rules in
            :class:`~tianshou.policy.BasePolicy`. (s -> imitation_logits)
        :param target_update_freq: the target network update frequency.
        :param unlikely_action_threshold: the threshold (tau) for unlikely
            actions, as shown in Equ. (17) in the paper.
        :param target_update_freq: the target network update frequency (0 if
            you do not use the target network).
        :param action_space: the environment's action space.
        :param observation_space: the environment's observation space.
        """
        super().__init__(
            action_space=action_space,
            observation_space=observation_space,
        )
        self.model = model
        self.imitator = imitator
        assert (
            target_update_freq > 0
        ), f"BCQ needs target_update_freq>0 but got: {target_update_freq}."
        assert (
            0.0 <= unlikely_action_threshold < 1.0
        ), f"unlikely_action_threshold should be in [0, 1) but got: {unlikely_action_threshold}"
        if unlikely_action_threshold > 0:
            self._log_tau = math.log(unlikely_action_threshold)
        else:
            self._log_tau = -np.inf
        self.max_action_num: int | None = None

    def forward(  # type: ignore
        self,
        batch: ObsBatchProtocol,
        state: dict | Batch | np.ndarray | None = None,
        **kwargs: Any,
    ) -> ImitationBatchProtocol:
        q_value, state = self.model(batch.obs, state=state, info=batch.info)
        if self.max_action_num is None:
            self.max_action_num = q_value.shape[1]
        imitation_logits, _ = self.imitator(batch.obs, state=state, info=batch.info)

        # mask actions for argmax
        ratio = imitation_logits - imitation_logits.max(dim=-1, keepdim=True).values
        mask = (ratio < self._log_tau).float()
        act = (q_value - INF * mask).argmax(dim=-1)

        result = Batch(act=act, state=state, q_value=q_value, imitation_logits=imitation_logits)
        return cast(ImitationBatchProtocol, result)


class DiscreteBCQ(
    OfflineAlgorithm[DiscreteBCQPolicy, TDiscreteBCQTrainingStats],
    LaggedNetworkFullUpdateAlgorithmMixin,
    Generic[TDiscreteBCQTrainingStats],
):
    """Implementation of the discrete batch-constrained deep Q-learning (BCQ) algorithm. arXiv:1910.01708."""

    def __init__(
        self,
        *,
        policy: DiscreteBCQPolicy,
        optim: OptimizerFactory,
        discount_factor: float = 0.99,
        estimation_step: int = 1,
        target_update_freq: int = 8000,
        eval_eps: float = 1e-3,
        imitation_logits_penalty: float = 1e-2,
        reward_normalization: bool = False,
        is_double: bool = True,
        clip_loss_grad: bool = False,
    ) -> None:
        """
        :param policy: the policy
        :param optim: a torch.optim for optimizing the model.
        :param discount_factor: in [0, 1].
        :param estimation_step: the number of steps to look ahead
        :param target_update_freq: the target network update frequency.
        :param eval_eps: the epsilon-greedy noise added in evaluation.
        :param imitation_logits_penalty: regularization weight for imitation
            logits.
        :param estimation_step: the number of steps to look ahead.
        :param target_update_freq: the target network update frequency (0 if
            you do not use the target network).
        :param reward_normalization: normalize the **returns** to Normal(0, 1).
            TODO: rename to return_normalization?
        :param is_double: use double dqn.
        :param clip_loss_grad: clip the gradient of the loss in accordance
            with nature14236; this amounts to using the Huber loss instead of
            the MSE loss.
        """
        super().__init__(
            policy=policy,
        )
        LaggedNetworkFullUpdateAlgorithmMixin.__init__(self)
        self.optim = self._create_optimizer(self.policy, optim)
        assert (
            0.0 <= discount_factor <= 1.0
        ), f"discount factor should be in [0, 1] but got: {discount_factor}"
        self.gamma = discount_factor
        assert (
            estimation_step > 0
        ), f"estimation_step should be greater than 0 but got: {estimation_step}"
        self.n_step = estimation_step
        self._target = target_update_freq > 0
        self.freq = target_update_freq
        self._iter = 0
        if self._target:
            self.model_old = self._add_lagged_network(self.policy.model)
        self.rew_norm = reward_normalization
        self.is_double = is_double
        self.clip_loss_grad = clip_loss_grad
        assert 0.0 <= eval_eps < 1.0
        self.eps = eval_eps
        self._weight_reg = imitation_logits_penalty

    def preprocess_batch(
        self,
        batch: RolloutBatchProtocol,
        buffer: ReplayBuffer,
        indices: np.ndarray,
    ) -> BatchWithReturnsProtocol:
        return self.compute_nstep_return(
            batch=batch,
            buffer=buffer,
            indices=indices,
            target_q_fn=self._target_q,
            gamma=self.gamma,
            n_step=self.n_step,
            rew_norm=self.rew_norm,
        )

    def _target_q(self, buffer: ReplayBuffer, indices: np.ndarray) -> torch.Tensor:
        batch = buffer[indices]  # batch.obs_next: s_{t+n}
        next_obs_batch = Batch(obs=batch.obs_next, info=[None] * len(batch))
        # target_Q = Q_old(s_, argmax(Q_new(s_, *)))
        act = self.policy(next_obs_batch).act
        target_q, _ = self.model_old(batch.obs_next)
        return target_q[np.arange(len(act)), act]

    def _update_with_batch(
        self,
        batch: RolloutBatchProtocol,
    ) -> TDiscreteBCQTrainingStats:
        if self._iter % self.freq == 0:
            self._update_lagged_network_weights()
        self._iter += 1

        target_q = batch.returns.flatten()
        result = self.policy(batch)
        imitation_logits = result.imitation_logits
        current_q = result.q_value[np.arange(len(target_q)), batch.act]
        act = to_torch(batch.act, dtype=torch.long, device=target_q.device)
        q_loss = F.smooth_l1_loss(current_q, target_q)
        i_loss = F.nll_loss(F.log_softmax(imitation_logits, dim=-1), act)
        reg_loss = imitation_logits.pow(2).mean()
        loss = q_loss + i_loss + self._weight_reg * reg_loss

        self.optim.step(loss)

        return DiscreteBCQTrainingStats(  # type: ignore[return-value]
            loss=loss.item(),
            q_loss=q_loss.item(),
            i_loss=i_loss.item(),
            reg_loss=reg_loss.item(),
        )
