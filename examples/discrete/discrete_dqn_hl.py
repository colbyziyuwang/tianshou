from sensai.util.logging import run_main

from tianshou.highlevel.config import OffPolicyTrainingConfig
from tianshou.highlevel.env import (
    EnvFactoryRegistered,
    VectorEnvType,
)
from tianshou.highlevel.experiment import DQNExperimentBuilder, ExperimentConfig
from tianshou.highlevel.params.policy_params import DQNParams
from tianshou.highlevel.trainer import (
    EpochStopCallbackRewardThreshold,
)


def main() -> None:
    experiment = (
        DQNExperimentBuilder(
            EnvFactoryRegistered(
                task="CartPole-v1",
                venv_type=VectorEnvType.DUMMY,
                train_seed=0,
                test_seed=10,
            ),
            ExperimentConfig(
                persistence_enabled=False,
                watch=True,
                watch_render=1 / 35,
                watch_num_episodes=100,
            ),
            OffPolicyTrainingConfig(
                num_epochs=10,
                step_per_epoch=10000,
                batch_size=64,
                num_train_envs=10,
                num_test_envs=100,
                buffer_size=20000,
                step_per_collect=10,
                update_per_step=1 / 10,
            ),
        )
        .with_dqn_params(
            DQNParams(
                lr=1e-3,
                discount_factor=0.9,
                estimation_step=3,
                target_update_freq=320,
                eps_training=0.3,
                eps_inference=0.0,
            ),
        )
        .with_model_factory_default(hidden_sizes=(64, 64))
        .with_epoch_stop_callback(EpochStopCallbackRewardThreshold(195))
        .build()
    )
    experiment.run()


if __name__ == "__main__":
    run_main(main)
