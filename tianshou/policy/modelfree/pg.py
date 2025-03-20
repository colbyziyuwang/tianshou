import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar, cast

import gymnasium as gym
import numpy as np
import torch

from tianshou.data import (
    Batch,
    ReplayBuffer,
    SequenceSummaryStats,
    to_torch,
    to_torch_as,
)
from tianshou.data.batch import BatchProtocol
from tianshou.data.types import (
    BatchWithReturnsProtocol,
    DistBatchProtocol,
    ObsBatchProtocol,
    RolloutBatchProtocol,
)
from tianshou.policy import Algorithm
from tianshou.policy.base import (
    OnPolicyAlgorithm,
    Policy,
    TrainingStats,
)
from tianshou.policy.optim import OptimizerFactory
from tianshou.utils import RunningMeanStd
from tianshou.utils.net.continuous import ContinuousActorProb
from tianshou.utils.net.discrete import DiscreteActor, dist_fn_categorical_from_logits

# Dimension Naming Convention
# B - Batch Size
# A - Action
# D - Dist input (usually 2, loc and scale)
# H - Dimension of hidden, can be None

TDistFnContinuous = Callable[
    [tuple[torch.Tensor, torch.Tensor]],
    torch.distributions.Distribution,
]
TDistFnDiscrete = Callable[[torch.Tensor], torch.distributions.Categorical]

TDistFnDiscrOrCont = TDistFnContinuous | TDistFnDiscrete


@dataclass(kw_only=True)
class PGTrainingStats(TrainingStats):
    loss: SequenceSummaryStats


TPGTrainingStats = TypeVar("TPGTrainingStats", bound=PGTrainingStats)


class ActorPolicy(Policy):
    def __init__(
        self,
        *,
        actor: torch.nn.Module | ContinuousActorProb | DiscreteActor,
        dist_fn: TDistFnDiscrOrCont,
        deterministic_eval: bool = False,
        action_space: gym.Space,
        observation_space: gym.Space | None = None,
        # TODO: why change the default from the base?
        action_scaling: bool = True,
        action_bound_method: Literal["clip", "tanh"] | None = "clip",
    ) -> None:
        """
        :param actor: the actor network following the rules:
            If `self.action_type == "discrete"`: (`s_B` ->`action_values_BA`).
            If `self.action_type == "continuous"`: (`s_B` -> `dist_input_BD`).
        :param dist_fn: distribution class for computing the action.
            Maps model_output -> distribution. Typically, a Gaussian distribution
            taking `model_output=mean,std` as input for continuous action spaces,
            or a categorical distribution taking `model_output=logits`
            for discrete action spaces. Note that as user, you are responsible
            for ensuring that the distribution is compatible with the action space.
        :param deterministic_eval: if True, will use deterministic action (the dist's mode)
            instead of stochastic one during evaluation. Does not affect training.
        :param action_space: env's action space.
        :param observation_space: Env's observation space.
        :param action_scaling: if True, scale the action from [-1, 1] to the range
            of action_space. Only used if the action_space is continuous.
        :param action_bound_method: method to bound action to range [-1, 1].
            Only used if the action_space is continuous.
        """
        super().__init__(
            action_space=action_space,
            observation_space=observation_space,
            action_scaling=action_scaling,
            action_bound_method=action_bound_method,
        )
        if action_scaling and not np.isclose(actor.max_action, 1.0):
            warnings.warn(
                "action_scaling and action_bound_method are only intended"
                "to deal with unbounded model action space, but find actor model"
                f"bound action space with max_action={actor.max_action}."
                "Consider using unbounded=True option of the actor model,"
                "or set action_scaling to False and action_bound_method to None.",
            )
        self.actor = actor
        self.dist_fn = dist_fn
        self._eps = 1e-8
        self.deterministic_eval = deterministic_eval

    def forward(
        self,
        batch: ObsBatchProtocol,
        state: dict | BatchProtocol | np.ndarray | None = None,
        **kwargs: Any,
    ) -> DistBatchProtocol:
        """Compute action over the given batch data by applying the actor.

        Will sample from the dist_fn, if appropriate.
        Returns a new object representing the processed batch data
        (contrary to other methods that modify the input batch inplace).

        .. seealso::

            Please refer to :meth:`~tianshou.policy.BasePolicy.forward` for
            more detailed explanation.
        """
        # TODO - ALGO: marked for algorithm refactoring
        action_dist_input_BD, hidden_BH = self.actor(batch.obs, state=state, info=batch.info)
        # in the case that self.action_type == "discrete", the dist should always be Categorical, and D=A
        # therefore action_dist_input_BD is equivalent to logits_BA
        # If discrete, dist_fn will typically map loc, scale to a distribution (usually a Gaussian)
        # the action_dist_input_BD in that case is a tuple of loc_B, scale_B and needs to be unpacked
        dist = self.dist_fn(action_dist_input_BD)

        act_B = (
            dist.mode
            if self.deterministic_eval and not self.is_within_training_step
            else dist.sample()
        )
        # act is of dimension BA in continuous case and of dimension B in discrete
        result = Batch(logits=action_dist_input_BD, act=act_B, state=hidden_BH, dist=dist)
        return cast(DistBatchProtocol, result)


class DiscreteActorPolicy(ActorPolicy):
    def __init__(
        self,
        *,
        actor: torch.nn.Module | DiscreteActor,
        dist_fn: TDistFnDiscrete = dist_fn_categorical_from_logits,
        deterministic_eval: bool = False,
        action_space: gym.Space,
        observation_space: gym.Space | None = None,
    ) -> None:
        """
        :param actor: the actor network following the rules: (`s_B` -> `dist_input_BD`).
        :param dist_fn: distribution class for computing the action.
            Maps model_output -> distribution, typically a categorical distribution
            taking `model_output=logits`.
        :param deterministic_eval: if True, will use deterministic action (the dist's mode)
            instead of stochastic one during evaluation. Does not affect training.
        :param action_space: the environment's (discrete) action space.
        :param observation_space: the environment's observation space.
        """
        if not isinstance(action_space, gym.spaces.Discrete):
            raise ValueError(f"Action space must be an instance of Discrete; got {action_space}")
        super().__init__(
            actor=actor,
            dist_fn=dist_fn,
            deterministic_eval=deterministic_eval,
            action_space=action_space,
            observation_space=observation_space,
            action_scaling=False,
            action_bound_method=None,
        )


TActorPolicy = TypeVar("TActorPolicy", bound=ActorPolicy)


class DiscountedReturnComputation:
    def __init__(
        self,
        discount_factor: float = 0.99,
        reward_normalization: bool = False,
    ):
        """
        :param discount_factor: the future reward discount factor gamma in [0, 1].
        :param reward_normalization: if True, will normalize the *returns*
            by subtracting the running mean and dividing by the running standard deviation.
            Can be detrimental to performance!
        """
        assert 0.0 <= discount_factor <= 1.0, "discount factor should be in [0, 1]"
        self.gamma = discount_factor
        self.rew_norm = reward_normalization
        self.ret_rms = RunningMeanStd()
        self.eps = 1e-8

    def add_discounted_returns(
        self, batch: RolloutBatchProtocol, buffer: ReplayBuffer, indices: np.ndarray
    ) -> BatchWithReturnsProtocol:
        r"""Compute the discounted returns (Monte Carlo estimates) for each transition.

        They are added to the batch under the field `returns`.
        Note: this function will modify the input batch!

        .. math::
            G_t = \sum_{i=t}^T \gamma^{i-t}r_i

        where :math:`T` is the terminal time step, :math:`\gamma` is the
        discount factor, :math:`\gamma \in [0, 1]`.

        :param batch: a data batch which contains several episodes of data in
            sequential order. Mind that the end of each finished episode of batch
            should be marked by done flag, unfinished (or collecting) episodes will be
            recognized by buffer.unfinished_index().
        :param buffer: the corresponding replay buffer.
        :param numpy.ndarray indices: tell batch's location in buffer, batch is equal
            to buffer[indices].
        """
        v_s_ = np.full(indices.shape, self.ret_rms.mean)
        # gae_lambda = 1.0 means we use Monte Carlo estimate
        unnormalized_returns, _ = Algorithm.compute_episodic_return(
            batch,
            buffer,
            indices,
            v_s_=v_s_,
            gamma=self.gamma,
            gae_lambda=1.0,
        )
        # TODO: overridden in A2C, where mean is not subtracted. Subtracting mean
        #  can be very detrimental! It also has no theoretical grounding.
        #  This should be addressed soon!
        if self.rew_norm:
            batch.returns = (unnormalized_returns - self.ret_rms.mean) / np.sqrt(
                self.ret_rms.var + self.eps,
            )
            self.ret_rms.update(unnormalized_returns)
        else:
            batch.returns = unnormalized_returns
        batch: BatchWithReturnsProtocol
        return batch


class Reinforce(OnPolicyAlgorithm[ActorPolicy, TPGTrainingStats], Generic[TPGTrainingStats]):
    """Implementation of the REINFORCE (a.k.a. vanilla policy gradient) algorithm.

    .. seealso::

        Please refer to :class:`~tianshou.policy.BasePolicy` for more detailed explanation.
    """

    def __init__(
        self,
        *,
        policy: TActorPolicy,
        discount_factor: float = 0.99,
        reward_normalization: bool = False,
        optim: OptimizerFactory,
    ) -> None:
        """
        :param policy: the policy
        :param optim: optimizer for the policy's actor network.
        :param discount_factor: in [0, 1].
        :param reward_normalization: if True, will normalize the *returns*
            by subtracting the running mean and dividing by the running standard deviation.
            Can be detrimental to performance!
        """
        super().__init__(
            policy=policy,
        )
        self.discounted_return_computation = DiscountedReturnComputation(
            discount_factor=discount_factor,
            reward_normalization=reward_normalization,
        )
        self.optim = self._create_optimizer(self.policy, optim)

    def preprocess_batch(
        self,
        batch: RolloutBatchProtocol,
        buffer: ReplayBuffer,
        indices: np.ndarray,
    ) -> BatchWithReturnsProtocol:
        return self.discounted_return_computation.add_discounted_returns(
            batch,
            buffer,
            indices,
        )

    # TODO: why does mypy complain?
    def _update_with_batch(  # type: ignore
        self,
        batch: BatchWithReturnsProtocol,
        batch_size: int | None,
        repeat: int,
    ) -> TPGTrainingStats:
        losses = []
        split_batch_size = batch_size or -1
        for _ in range(repeat):
            for minibatch in batch.split(split_batch_size, merge_last=True):
                result = self.policy(minibatch)
                dist = result.dist
                act = to_torch_as(minibatch.act, result.act)
                ret = to_torch(minibatch.returns, torch.float, result.act.device)
                log_prob = dist.log_prob(act).reshape(len(ret), -1).transpose(0, 1)
                loss = -(log_prob * ret).mean()
                self.optim.step(loss)
                losses.append(loss.item())

        loss_summary_stat = SequenceSummaryStats.from_sequence(losses)

        return PGTrainingStats(loss=loss_summary_stat)  # type: ignore[return-value]
