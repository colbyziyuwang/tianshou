from dataclasses import dataclass
from typing import Any, TypeVar, cast

import gymnasium as gym
import numpy as np
import torch
import torch.nn.functional as F

from tianshou.data import Batch, to_numpy
from tianshou.data.batch import BatchProtocol
from tianshou.data.types import (
    ObsBatchProtocol,
    QuantileRegressionBatchProtocol,
    RolloutBatchProtocol,
)
from tianshou.policy import QRDQN
from tianshou.policy.modelfree.qrdqn import QRDQNPolicy, QRDQNTrainingStats
from tianshou.policy.optim import OptimizerFactory


@dataclass(kw_only=True)
class IQNTrainingStats(QRDQNTrainingStats):
    pass


TIQNTrainingStats = TypeVar("TIQNTrainingStats", bound=IQNTrainingStats)


class IQNPolicy(QRDQNPolicy):
    def __init__(
        self,
        *,
        model: torch.nn.Module,
        action_space: gym.spaces.Space,
        sample_size: int = 32,
        online_sample_size: int = 8,
        target_sample_size: int = 8,
        observation_space: gym.Space | None = None,
        eps_training: float = 0.0,
        eps_inference: float = 0.0,
    ) -> None:
        """
        :param model:
        :param action_space: the environment's action space
        :param sample_size:
        :param online_sample_size:
        :param target_sample_size:
        :param observation_space: the environment's observation space
        :param eps_training: the epsilon value for epsilon-greedy exploration during training.
            When collecting data for training, this is the probability of choosing a random action
            instead of the action chosen by the policy.
            A value of 0.0 means no exploration (fully greedy) and a value of 1.0 means full
            exploration (fully random).
        :param eps_inference: the epsilon value for epsilon-greedy exploration during inference,
            i.e. non-training cases (such as evaluation during test steps).
            The epsilon value is the probability of choosing a random action instead of the action
            chosen by the policy.
            A value of 0.0 means no exploration (fully greedy) and a value of 1.0 means full
            exploration (fully random).
        """
        assert isinstance(action_space, gym.spaces.Discrete)
        assert sample_size > 1, f"sample_size should be greater than 1 but got: {sample_size}"
        assert (
            online_sample_size > 1
        ), f"online_sample_size should be greater than 1 but got: {online_sample_size}"
        assert (
            target_sample_size > 1
        ), f"target_sample_size should be greater than 1 but got: {target_sample_size}"
        super().__init__(
            model=model,
            action_space=action_space,
            observation_space=observation_space,
            eps_training=eps_training,
            eps_inference=eps_inference,
        )
        self.sample_size = sample_size
        self.online_sample_size = online_sample_size
        self.target_sample_size = target_sample_size

    def forward(
        self,
        batch: ObsBatchProtocol,
        state: dict | BatchProtocol | np.ndarray | None = None,
        model: torch.nn.Module | None = None,
        **kwargs: Any,
    ) -> QuantileRegressionBatchProtocol:
        is_model_old = model is not None
        if is_model_old:
            sample_size = self.target_sample_size
        elif self.training:
            sample_size = self.online_sample_size
        else:
            sample_size = self.sample_size
        if model is None:
            model = self.model
        obs = batch.obs
        # TODO: this seems very contrived!
        obs_next = obs.obs if hasattr(obs, "obs") else obs
        (logits, taus), hidden = model(
            obs_next,
            sample_size=sample_size,
            state=state,
            info=batch.info,
        )
        q = self.compute_q_value(logits, getattr(obs, "mask", None))
        if self.max_action_num is None:  # type: ignore
            # TODO: see same thing in DQNPolicy!
            self.max_action_num = q.shape[1]
        act = to_numpy(q.max(dim=1)[1])
        result = Batch(logits=logits, act=act, state=hidden, taus=taus)
        return cast(QuantileRegressionBatchProtocol, result)


class IQN(QRDQN[IQNPolicy, TIQNTrainingStats]):
    """Implementation of Implicit Quantile Network. arXiv:1806.06923."""

    def __init__(
        self,
        *,
        policy: IQNPolicy,
        optim: OptimizerFactory,
        discount_factor: float = 0.99,
        num_quantiles: int = 200,
        estimation_step: int = 1,
        target_update_freq: int = 0,
        reward_normalization: bool = False,
    ) -> None:
        """
        :param policy: the policy
        :param optim: the optimizer for the policy's model
        :param discount_factor: in [0, 1].
        :param num_quantiles: the number of quantile midpoints in the inverse
            cumulative distribution function of the value.
        :param estimation_step: the number of steps to look ahead.
        :param target_update_freq: the target network update frequency (0 if
            you do not use the target network).
        :param reward_normalization: normalize the **returns** to Normal(0, 1).
            TODO: rename to return_normalization?
        """
        super().__init__(
            policy=policy,
            optim=optim,
            discount_factor=discount_factor,
            num_quantiles=num_quantiles,
            estimation_step=estimation_step,
            target_update_freq=target_update_freq,
            reward_normalization=reward_normalization,
        )

    def _update_with_batch(
        self,
        batch: RolloutBatchProtocol,
    ) -> TIQNTrainingStats:
        self._periodically_update_lagged_network_weights()
        weight = batch.pop("weight", 1.0)
        action_batch = self.policy(batch)
        curr_dist, taus = action_batch.logits, action_batch.taus
        act = batch.act
        curr_dist = curr_dist[np.arange(len(act)), act, :].unsqueeze(2)
        target_dist = batch.returns.unsqueeze(1)
        # calculate each element's difference between curr_dist and target_dist
        dist_diff = F.smooth_l1_loss(target_dist, curr_dist, reduction="none")
        huber_loss = (
            (
                dist_diff
                * (taus.unsqueeze(2) - (target_dist - curr_dist).detach().le(0.0).float()).abs()
            )
            .sum(-1)
            .mean(1)
        )
        loss = (huber_loss * weight).mean()
        # ref: https://github.com/ku2482/fqf-iqn-qrdqn.pytorch/
        # blob/master/fqf_iqn_qrdqn/agent/qrdqn_agent.py L130
        batch.weight = dist_diff.detach().abs().sum(-1).mean(1)  # prio-buffer
        self.optim.step(loss)

        return IQNTrainingStats(loss=loss.item())  # type: ignore[return-value]
