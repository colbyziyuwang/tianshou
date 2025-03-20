import warnings
from dataclasses import dataclass
from typing import Generic, TypeVar

import numpy as np
import torch
import torch.nn.functional as F

from tianshou.data import Batch, ReplayBuffer
from tianshou.data.types import RolloutBatchProtocol
from tianshou.policy.modelfree.dqn import (
    DQNPolicy,
    DQNTrainingStats,
    QLearningOffPolicyAlgorithm,
)
from tianshou.policy.optim import OptimizerFactory


@dataclass(kw_only=True)
class QRDQNTrainingStats(DQNTrainingStats):
    pass


TQRDQNTrainingStats = TypeVar("TQRDQNTrainingStats", bound=QRDQNTrainingStats)


class QRDQNPolicy(DQNPolicy):
    def compute_q_value(self, logits: torch.Tensor, mask: np.ndarray | None) -> torch.Tensor:
        return super().compute_q_value(logits.mean(2), mask)


TQRDQNPolicy = TypeVar("TQRDQNPolicy", bound=QRDQNPolicy)


class QRDQN(
    QLearningOffPolicyAlgorithm[TQRDQNPolicy, TQRDQNTrainingStats],
    Generic[TQRDQNPolicy, TQRDQNTrainingStats],
):
    """Implementation of Quantile Regression Deep Q-Network. arXiv:1710.10044."""

    def __init__(
        self,
        *,
        policy: TQRDQNPolicy,
        optim: OptimizerFactory,
        discount_factor: float = 0.99,
        num_quantiles: int = 200,
        estimation_step: int = 1,
        target_update_freq: int = 0,
        reward_normalization: bool = False,
    ) -> None:
        """
        :param policy: the policy
        :param optim: the optimizer for the policy
        :param discount_factor: in [0, 1].
        :param num_quantiles: the number of quantile midpoints in the inverse
            cumulative distribution function of the value.
        :param estimation_step: the number of steps to look ahead.
        :param target_update_freq: the target network update frequency (0 if
            you do not use the target network).
        :param reward_normalization: normalize the **returns** to Normal(0, 1).
            TODO: rename to return_normalization?
        """
        assert num_quantiles > 1, f"num_quantiles should be greater than 1 but got: {num_quantiles}"
        super().__init__(
            policy=policy,
            optim=optim,
            discount_factor=discount_factor,
            estimation_step=estimation_step,
            target_update_freq=target_update_freq,
            reward_normalization=reward_normalization,
        )
        self.num_quantiles = num_quantiles
        tau = torch.linspace(0, 1, self.num_quantiles + 1)
        self.tau_hat = torch.nn.Parameter(
            ((tau[:-1] + tau[1:]) / 2).view(1, -1, 1),
            requires_grad=False,
        )
        warnings.filterwarnings("ignore", message="Using a target size")

    def _target_q(self, buffer: ReplayBuffer, indices: np.ndarray) -> torch.Tensor:
        obs_next_batch = Batch(
            obs=buffer[indices].obs_next,
            info=[None] * len(indices),
        )  # obs_next: s_{t+n}
        if self.use_target_network:
            act = self.policy(obs_next_batch).act
            next_dist = self.policy(obs_next_batch, model=self.model_old).logits
        else:
            next_batch = self.policy(obs_next_batch)
            act = next_batch.act
            next_dist = next_batch.logits
        return next_dist[np.arange(len(act)), act, :]

    def _update_with_batch(
        self,
        batch: RolloutBatchProtocol,
    ) -> TQRDQNTrainingStats:
        self._periodically_update_lagged_network_weights()
        weight = batch.pop("weight", 1.0)
        curr_dist = self.policy(batch).logits
        act = batch.act
        curr_dist = curr_dist[np.arange(len(act)), act, :].unsqueeze(2)
        target_dist = batch.returns.unsqueeze(1)
        # calculate each element's difference between curr_dist and target_dist
        dist_diff = F.smooth_l1_loss(target_dist, curr_dist, reduction="none")
        huber_loss = (
            (dist_diff * (self.tau_hat - (target_dist - curr_dist).detach().le(0.0).float()).abs())
            .sum(-1)
            .mean(1)
        )
        loss = (huber_loss * weight).mean()
        # ref: https://github.com/ku2482/fqf-iqn-qrdqn.pytorch/
        # blob/master/fqf_iqn_qrdqn/agent/qrdqn_agent.py L130
        batch.weight = dist_diff.detach().abs().sum(-1).mean(1)  # prio-buffer
        self.optim.step(loss)

        return QRDQNTrainingStats(loss=loss.item())  # type: ignore[return-value]
