from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

import gymnasium as gym
import numpy as np
import torch
from sensai.util.helper import mark_used

from tianshou.data import Batch, ReplayBuffer, to_numpy, to_torch_as
from tianshou.data.batch import BatchProtocol
from tianshou.data.types import (
    ActBatchProtocol,
    BatchWithReturnsProtocol,
    ModelOutputBatchProtocol,
    ObsBatchProtocol,
    RolloutBatchProtocol,
)
from tianshou.policy.base import (
    LaggedNetworkFullUpdateAlgorithmMixin,
    OffPolicyAlgorithm,
    Policy,
    TArrOrActBatch,
    TrainingStats,
    TTrainingStats,
)
from tianshou.policy.optim import OptimizerFactory
from tianshou.utils.net.common import Net

mark_used(ActBatchProtocol)


@dataclass(kw_only=True)
class DQNTrainingStats(TrainingStats):
    loss: float


TDQNTrainingStats = TypeVar("TDQNTrainingStats", bound=DQNTrainingStats)
TModel = TypeVar("TModel", bound=torch.nn.Module | Net)


class DQNPolicy(Policy, Generic[TModel]):
    def __init__(
        self,
        *,
        model: TModel,
        action_space: gym.spaces.Space,
        observation_space: gym.Space | None = None,
        eps_training: float = 0.0,
        eps_inference: float = 0.0,
    ) -> None:
        """
        :param model: a model mapping (obs, state, info) to action_values_BA.
        :param action_space: the environment's action space
        :param observation_space: the environment's observation space.
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
        super().__init__(
            action_space=action_space,
            observation_space=observation_space,
            action_scaling=False,
            action_bound_method=None,
        )
        self.model = model
        self.max_action_num: int | None = None
        self.eps_training = eps_training
        self.eps_inference = eps_inference

    def set_eps_training(self, eps: float) -> None:
        """
        Sets the epsilon value for epsilon-greedy exploration during training.

        :param eps: the epsilon value for epsilon-greedy exploration during training.
            When collecting data for training, this is the probability of choosing a random action
            instead of the action chosen by the policy.
            A value of 0.0 means no exploration (fully greedy) and a value of 1.0 means full
            exploration (fully random).
        """
        self.eps_training = eps

    def set_eps_inference(self, eps: float) -> None:
        """
        Sets the epsilon value for epsilon-greedy exploration during inference.

        :param eps: the epsilon value for epsilon-greedy exploration during inference,
            i.e. non-training cases (such as evaluation during test steps).
            The epsilon value is the probability of choosing a random action instead of the action
            chosen by the policy.
            A value of 0.0 means no exploration (fully greedy) and a value of 1.0 means full
            exploration (fully random).
        """
        self.eps_inference = eps

    def forward(
        self,
        batch: ObsBatchProtocol,
        state: dict | BatchProtocol | np.ndarray | None = None,
        model: torch.nn.Module | None = None,
        **kwargs: Any,
    ) -> ModelOutputBatchProtocol:
        """Compute action over the given batch data.

        If you need to mask the action, please add a "mask" into batch.obs, for
        example, if we have an environment that has "0/1/2" three actions:
        ::

            batch == Batch(
                obs=Batch(
                    obs="original obs, with batch_size=1 for demonstration",
                    mask=np.array([[False, True, False]]),
                    # action 1 is available
                    # action 0 and 2 are unavailable
                ),
                ...
            )

        :return: A :class:`~tianshou.data.Batch` which has 3 keys:

            * ``act`` the action.
            * ``logits`` the network's raw output.
            * ``state`` the hidden state.

        .. seealso::

            Please refer to :meth:`~tianshou.policy.BasePolicy.forward` for
            more detailed explanation.
        """
        if model is None:
            model = self.model
        obs = batch.obs
        # TODO: this is convoluted! See also other places where this is done.
        obs_next = obs.obs if hasattr(obs, "obs") else obs
        action_values_BA, hidden_BH = model(obs_next, state=state, info=batch.info)
        q = self.compute_q_value(action_values_BA, getattr(obs, "mask", None))
        if self.max_action_num is None:
            self.max_action_num = q.shape[1]
        act_B = to_numpy(q.argmax(dim=1))
        result = Batch(logits=action_values_BA, act=act_B, state=hidden_BH)
        return cast(ModelOutputBatchProtocol, result)

    def compute_q_value(self, logits: torch.Tensor, mask: np.ndarray | None) -> torch.Tensor:
        """Compute the q value based on the network's raw output and action mask."""
        if mask is not None:
            # the masked q value should be smaller than logits.min()
            min_value = logits.min() - logits.max() - 1.0
            logits = logits + to_torch_as(1 - mask, logits) * min_value
        return logits

    def add_exploration_noise(
        self,
        act: TArrOrActBatch,
        batch: ObsBatchProtocol,
    ) -> TArrOrActBatch:
        eps = self.eps_training if self.is_within_training_step else self.eps_inference
        # TODO: This looks problematic; the non-array case is silently ignored
        if isinstance(act, np.ndarray) and not np.isclose(eps, 0.0):
            batch_size = len(act)
            rand_mask = np.random.rand(batch_size) < eps
            assert (
                self.max_action_num is not None
            ), "Can't call this method before max_action_num was set in first forward"
            q = np.random.rand(batch_size, self.max_action_num)  # [0, 1]
            if hasattr(batch.obs, "mask"):
                q += batch.obs.mask
            rand_act = q.argmax(axis=1)
            act[rand_mask] = rand_act[rand_mask]
        return act


TDQNPolicy = TypeVar("TDQNPolicy", bound=DQNPolicy)


class QLearningOffPolicyAlgorithm(
    OffPolicyAlgorithm[TDQNPolicy, TTrainingStats], LaggedNetworkFullUpdateAlgorithmMixin, ABC
):
    """
    Base class for Q-learning off-policy algorithms that use a Q-function to compute the
    n-step return.
    It optionally uses a lagged model, which is used as a target network and which is
    fully updated periodically.
    """

    def __init__(
        self,
        *,
        policy: TDQNPolicy,
        optim: OptimizerFactory,
        discount_factor: float = 0.99,
        estimation_step: int = 1,
        target_update_freq: int = 0,
        reward_normalization: bool = False,
    ) -> None:
        """
        :param policy: the policy
        :param optim: the optimizer for the policy
        :param discount_factor: in [0, 1].
        :param estimation_step: the number of steps to look ahead.
        :param target_update_freq: the frequency with which to update the weights of the target network;
            0 if a target network shall not be used.
        :param reward_normalization: normalize the **returns** to Normal(0, 1).
            TODO: rename to return_normalization?
        """
        super().__init__(
            policy=policy,
        )
        self.optim = self._create_policy_optimizer(optim)
        LaggedNetworkFullUpdateAlgorithmMixin.__init__(self)
        assert (
            0.0 <= discount_factor <= 1.0
        ), f"discount factor should be in [0, 1] but got: {discount_factor}"
        self.gamma = discount_factor
        assert (
            estimation_step > 0
        ), f"estimation_step should be greater than 0 but got: {estimation_step}"
        self.n_step = estimation_step
        self.rew_norm = reward_normalization
        self.target_update_freq = target_update_freq
        # TODO: 1 would be a more reasonable initialization given how it is incremented
        self._iter = 0
        self.model_old = (
            self._add_lagged_network(self.policy.model) if self.use_target_network else None
        )

    def _create_policy_optimizer(self, optim: OptimizerFactory) -> torch.optim.Optimizer:
        return self._create_optimizer(self.policy, optim)

    @property
    def use_target_network(self) -> bool:
        return self.target_update_freq > 0

    @abstractmethod
    def _target_q(self, buffer: ReplayBuffer, indices: np.ndarray) -> torch.Tensor:
        pass

    def preprocess_batch(
        self,
        batch: RolloutBatchProtocol,
        buffer: ReplayBuffer,
        indices: np.ndarray,
    ) -> BatchWithReturnsProtocol:
        """Compute the n-step return for Q-learning targets.

        More details can be found at
        :meth:`~tianshou.policy.BasePolicy.compute_nstep_return`.
        """
        return self.compute_nstep_return(
            batch=batch,
            buffer=buffer,
            indices=indices,
            target_q_fn=self._target_q,
            gamma=self.gamma,
            n_step=self.n_step,
            rew_norm=self.rew_norm,
        )

    def _periodically_update_lagged_network_weights(self) -> None:
        """
        Periodically updates the parameters of the lagged target network (if any), i.e.
        every n-th call (where n=`target_update_freq`), the target network's parameters
        are fully updated with the model's parameters.
        """
        if self.use_target_network and self._iter % self.target_update_freq == 0:
            self._update_lagged_network_weights()
        self._iter += 1


class DQN(
    QLearningOffPolicyAlgorithm[TDQNPolicy, TDQNTrainingStats],
    Generic[TDQNPolicy, TDQNTrainingStats],
):
    """Implementation of Deep Q Network. arXiv:1312.5602.

    Implementation of Double Q-Learning. arXiv:1509.06461.

    Implementation of Dueling DQN. arXiv:1511.06581 (the dueling DQN is
    implemented in the network side, not here).
    """

    def __init__(
        self,
        *,
        policy: TDQNPolicy,
        optim: OptimizerFactory,
        discount_factor: float = 0.99,
        estimation_step: int = 1,
        target_update_freq: int = 0,
        reward_normalization: bool = False,
        is_double: bool = True,
        clip_loss_grad: bool = False,
    ) -> None:
        """
        :param policy: the policy
        :param optim: the optimizer for the policy
        :param discount_factor: in [0, 1].
        :param estimation_step: the number of steps to look ahead.
        :param target_update_freq: the frequency with which to update the weights of the target network;
            0 if a target network shall not be used.
        :param reward_normalization: normalize the **returns** to Normal(0, 1).
            TODO: rename to return_normalization?
        :param is_double: use double dqn.
        :param clip_loss_grad: clip the gradient of the loss in accordance
            with nature14236; this amounts to using the Huber loss instead of
            the MSE loss.
        """
        super().__init__(
            policy=policy,
            optim=optim,
            discount_factor=discount_factor,
            estimation_step=estimation_step,
            target_update_freq=target_update_freq,
            reward_normalization=reward_normalization,
        )
        self.is_double = is_double
        self.clip_loss_grad = clip_loss_grad

    def _target_q(self, buffer: ReplayBuffer, indices: np.ndarray) -> torch.Tensor:
        obs_next_batch = Batch(
            obs=buffer[indices].obs_next,
            info=[None] * len(indices),
        )  # obs_next: s_{t+n}
        result = self.policy(obs_next_batch)
        if self.use_target_network:
            # target_Q = Q_old(s_, argmax(Q_new(s_, *)))
            target_q = self.policy(obs_next_batch, model=self.model_old).logits
        else:
            target_q = result.logits
        if self.is_double:
            return target_q[np.arange(len(result.act)), result.act]
        # Nature DQN, over estimate
        return target_q.max(dim=1)[0]

    def _update_with_batch(
        self,
        batch: RolloutBatchProtocol,
    ) -> TDQNTrainingStats:
        self._periodically_update_lagged_network_weights()
        weight = batch.pop("weight", 1.0)
        q = self.policy(batch).logits
        q = q[np.arange(len(q)), batch.act]
        returns = to_torch_as(batch.returns.flatten(), q)
        td_error = returns - q

        if self.clip_loss_grad:
            y = q.reshape(-1, 1)
            t = returns.reshape(-1, 1)
            loss = torch.nn.functional.huber_loss(y, t, reduction="mean")
        else:
            loss = (td_error.pow(2) * weight).mean()

        batch.weight = td_error  # prio-buffer
        self.optim.step(loss)

        return DQNTrainingStats(loss=loss.item())  # type: ignore[return-value]
