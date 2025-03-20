# Changelog

## Release 2.0.0

* `Trainer` abstraction (formerly `BaseTrainer`):
    * The trainer logic and configuration is now properly separated between the three cases of on-policy, off-policy
      and offline learning: The base class is no longer a "God" class which does it all; logic and functionality has moved
      to the respective subclasses (`OnPolicyTrainer`, `OffPolicyTrainer` and `OfflineTrainer`, with `OnlineTrainer`
      being introduced as a base class for the two former specialisations).
    * The trainers now use configuration objects with central documentation (which has been greatly improved to enhance
      clarity and usability in general); every type of trainer now has a dedicated configuration class which provides
      precisely the options that are applicable.
    * The interface has been streamlined with improved naming of functions/parameters and limiting the public interface to purely
      the methods and attributes a user should reasonably access.
    * Further changes potentially affecting usage:
        * We dropped the iterator semantics: Method `__next__` has been replaced by `execute_epoch`.
        * We no longer report outdated statistics (e.g. on rewards/returns when a training step does not collect any full
          episodes)
        * See also "Issues resolved" below (as issue resolution can result in usage changes) 
        * The default value for `test_in_train` was changed from True to False (updating all usage sites to explicitly
          set the parameter), because False is the more natural default, which does not make assumptions about
          returns/score values computed for the data from a collection step being at all meaningful for early stopping
    * Further internal changes unlikely to affect usage:
        * Module `trainer.utils` was removed and the functions therein where moved to class `Trainer`
        * The two places that collected and evaluated test episodes (`_test_in_train` and `_reset`) in addition to 
          `_test_step` were unified to use `_test_step` (with some minor parametrisation) and now log the results 
          of the test step accordingly.
    * Issues resolved:
        * Methods `run` and `reset`: Parameter `reset_prior_to_run` of `run` was never respected if it was set to `False`,
          because the implementation of `__iter__` (now removed) would call `reset` regardless - and calling `reset`
          is indeed necessary, because it initializes the training. The parameter was removed and replaced by
          `reset_collectors` (such that `run` now replicates the parameters of `reset`).
        * Inconsistent configuration options now raise exceptions rather than silently ignoring the issue in the 
          hope that default behaviour will achieve what the user intended.
          One condition where `test_in_train` was silently set to `False` was removed and replaced by a warning.
        * The stop criterion `stop_fn` did not consider scores as computed by `compute_score_fn` but instead always used
          mean returns (i.e. it was assumed that the default implementation of `compute_score_fn` applies).
          This is an inconsistency which has been resolved.
        * The `gradient_step` counter was flawed (as it made assumptions about the underlying algorithms, which were 
          not valid). It has been replaced with an update step counter.
          Members of `InfoStats` and parameters of `Logger` (and subclasses) were changed accordingly. 
    * Migration information at a glance:
        * Training parameters are now passed via instances of configuration objects instead of directly as keyword arguments:
          `OnPolicyTrainerParams`, `OffPolicyTrainerParams`, `OfflineTrainerParams`.
        * Changed parameter default: Default for `test_in_train` was changed from True to False.
        * Trainer classes have been renamed:
            * `OnpolicyTrainer` -> `OnPolicyTrainer`
            * `OffpolicyTrainer` -> `OffPolicyTrainer`
        * Method `run`: The parameter `reset_prior_to_run` was removed and replaced by `reset_collectors` (see above).
        * Methods `run` and `reset`: The parameter `reset_buffer` was renamed to `reset_collector_buffers` for clarity
        * Trainers are no longer iterators; manual usage (not using `run`) should simply call `reset` followed by
          calls of `execute_epoch`.
* `Policy` and `Algorithm` abstractions (formerly unified in `BasePolicy`):
  * We now conceptually differentiate between the learning algorithm and the policy being optimised:
    * The abstraction `BasePolicy` is thus replaced by `Algorithm` and `Policy`.  
      Migration information: The instantiation of a policy is replaced by the instantiation of an `Algorithm`,
      which is passed a `Policy`. In most cases, the former policy class name `<Name>Policy` is replaced by algorithm
      class `<Name>`; exceptions are noted below.
        * `ImitationPolicy` -> `OffPolicyImitationLearning`, `OfflineImitationLearning` 
        * `PGPolicy` -> `Reinforce` 
        * `MultiAgentPolicyManager` -> `MultiAgentOnPolicyAlgorithm`, `MultiAgentOffPolicyAlgorithm` 
        * `MARLRandomPolicy` -> `MARLRandomDiscreteMaskedOffPolicyAlgorithm`
      For the respective subtype of `Policy` to use, see the respective algorithm class' constructor.
  * Interface changes/improvements:
      * Core methods have been renamed:
          * `process_fn` -> `preprocess_batch`
          * `post_process_fn` -> `postprocess_batch`
          * `learn` -> `_update_with_batch` (no longer in public interface)
      * The updating interface has been cleaned up:
          * Functions `update` and `_update_with_batch` (formerly `learn`) no longer have `*args` and `**kwargs`.
          * Instead, the interfaces for the offline, off-policy and on-policy cases are properly differentiated.
      * New method `run_training`: The `Algorithm` abstraction can now directly initiate the learning process via this method.
      * `Algorithms` no longer require `torch.optim.Optimizer` instances and instead require `OptimizerFactory` 
        instances, which create the actual optimizers internally.
        The new `OptimizerFactory` abstraction simultaneously handles the creation of learning rate schedulers
        for the optimizers created (via method `with_lr_scheduler_factory` and accompanying factory abstraction 
        `LRSchedulerFactory`).
        The parameter `lr_scheduler` has thus been removed from all algorithm constructors.
      * The flag `updating` has been removed (no internal usage, general usefulness questionable).
  * Internal design improvements:
      * Introduced an abstraction for the alpha parameter (coefficient of the entropy term) 
        in `SAC`, `DiscreteSAC` and other algorithms.
          * Class hierarchy:
              * Abstract base class `Alpha` base class with value property and update method
              * `FixedAlpha` for constant entropy coefficients
              * `AutoAlpha` for automatic entropy tuning (replaces the old tuple-based representation)
          * The (auto-)updating logic is now completely encapsulated, reducing the complexity of the algorithms.
          * Implementations for continuous and discrete cases now share the same abstraction,
            making the codebase more consistent while preserving the original functionality.
      * Introduced a policy base class `ContinuousPolicyWithExplorationNoise` which encapsulates noise generation 
        for continuous action spaces (e.g. relevant to `DDPG`, `SAC` and `REDQ`). 
      * Multi-agent RL methods are now differentiated by the type of the sub-algorithms being employed
        (`MultiAgentOnPolicyAlgorithm`, `MultiAgentOffPolicyAlgorithm`), which renders all interfaces clean.
        Helper class `MARLDispatcher` has been factored out to manage the dispatching of data to the respective agents.
      * Algorithms now internally use a wrapper (`Algorithm.Optimizer`) around the optimizers; creation is handled
        by method `_create_optimizer`. 
          * This facilitates backpropagation steps with gradient clipping.  
          * The optimizers of an Algorithm instance are now centrally tracked, such that we can ensure that the 
            optimizers' states are handled alongside the model parameters when calling `state_dict` or `load_state_dict` 
            on the `Algorithm` instance.
            Special handling of the restoration of optimizers' state dicts was thus removed from examples and tests.
  * Fixed issues in the class hierarchy (particularly critical violations of the Liskov substitution principle): 
      * Introduced base classes (to retain factorization without abusive inheritance):
          * `ActorCriticOnPolicyAlgorithm`
          * `ActorCriticOffPolicyAlgorithm`
          * `ActorDualCriticsOffPolicyAlgorithm` (extends `ActorCriticOffPolicyAlgorithm`)
          * `QLearningOffPolicyAlgorithm`
      * `A2C`: Inherit from `ActorCriticOnPolicyAlgorithm` instead of `Reinforce`
      * `BDQN`:
          * Inherit from `QLearningOffPolicyAlgorithm` instead of `DQN`
          * Remove parameter `clip_loss_grad` (unused; only passed on to former base class)
      * `C51`:
          * Inherit from `QLearningOffPolicyAlgorithm` instead of `DQN`
          * Remove parameters `clip_loss_grad` and `is_double` (unused; only passed on to former base class)
      * `CQL`:
          * Inherit directly from `OfflineAlgorithm` instead of `SAC` (off-policy).
          * Remove parameter `estimation_step`, which was not actually used (it was only passed it on to its
            superclass).
      * `DiscreteCQL`: Remove unused parameters `clip_loss_grad` and `is_double`, which were only passed on to
        base class `QRDQN` (and unused by it).
      * `DiscreteCRR`: Inherit directly from `OfflineAlgorithm` instead of `Reinforce` (on-policy)
      * `FQF`: Remove unused parameters `clip_loss_grad` and `is_double`, which were only passed on to
        base class `QRDQN` (and unused by it).
      * `IQN`: Remove unused parameters `clip_loss_grad` and `is_double`, which were only passed on to 
        base class `QRDQN` (and unused by it).
      * `NPG`: Inherit from `ActorCriticOnPolicyAlgorithm` instead of `A2C`
      * `QRDQN`: 
          * Inherit from `QLearningOffPolicyAlgorithm` instead of `DQN`
          * Remove parameters `clip_loss_grad` and `is_double` (unused; only passed on to former base class) 
      * `REDQ`: Inherit from `ActorCriticOffPolicyAlgorithm` instead of `DDPG`
      * `SAC`: Inherit from `ActorDualCriticsOffPolicyAlgorithm` instead of `DDPG`
      * `TD3`: Inherit from `ActorDualCriticsOffPolicyAlgorithm` instead of `DDPG`
* High-Level API changes:
    * Detailed optimizer configuration (analogous to the procedural API) is now possible:
        * All optimizers can be configured in the respective algorithm-specific `Params` object by using
          `OptimizerFactoryFactory` instances as parameter values (e.g. for `optim`, `actor_optim`, `critic_optim`, etc.).
        * Learning rate schedulers remain separate parameters and now use `LRSchedulerFactoryFactory` 
          instances. The respective parameter names now use the suffix `lr_scheduler` instead of `lr_scheduler_factory`
          (as the precise nature need not be reflected in the name; brevity is preferable).
    * `SamplingConfig` is replaced by `TrainingConfig` and subclasses differentiating off-policy and on-policy cases 
      appropriately (`OnPolicyTrainingConfig`, `OffPolicyTrainingConfig`).
        * The `test_in_train` parameter is now exposed (default False).
        * Inapplicable arguments can no longer be set in the respective subclass (e.g. `OffPolicyTrainingConfig` does not
          contain parameter `repeat_per_collect`).
* Peripheral changes:
    * The `Actor` classes have been renamed for clarity:
        * `BaseActor` -> `Actor` 
        * `continuous.ActorProb` -> `ContinuousActorProb`
        * `coninuous.Actor` -> `ContinuousActorDeterministic`
        * `discrete.Actor` -> `DiscreteActor`
    * The `Critic` classes have been renamed for clarity:
        * `continuous.Critic` -> `ContinuousCritic`
        * `discrete.Critic` -> `DiscreteCritic`
    * Moved Atari helper modules `atari_network` and `atari_wrapper` to the library under `tianshou.env.atari`.
    * Fix issues pertaining to the torch device assignment of network components (#810):
        * Remove 'device' member (and the corresponding constructor argument) from the following classes:
          `BranchingNet`, `C51Net`, `ContinuousActorDeterministic`, `ContinuousActorProb`, `ContinuousCritic`, 
          `DiscreteActor`, `DiscreteCritic`, `DQNet`, `FullQuantileFunction`, `ImplicitQuantileNetwork`, 
          `IntrinsicCuriosityModule`, `Net`, `MLP`, `Perturbation`, `QRDQNet`, `Rainbow`, `Recurrent`, 
          `RecurrentActorProb`, `RecurrentCritic`, `VAE`
        * (Peripheral change:) Require the use of keyword arguments for the constructors of all of these classes 

## Unreleased

### Changes/Improvements

- trainer:
    - Custom scoring now supported for selecting the best model. #1202
- highlevel:
    - `DiscreteSACExperimentBuilder`: Expose method `with_actor_factory_default` #1248 #1250

### Breaking Changes

- data:
    - stats:
        - `InfoStats` has a new non-optional field `best_score` which is used
          for selecting the best model. #1202

## Release 1.1.0

### Highlights

#### Evaluation Package

This release introduces a new package `evaluation` that integrates best
practices for running experiments (seeding test and train environmets) and for
evaluating them using the [rliable](https://github.com/google-research/rliable)
library. This should be especially useful for algorithm developers for comparing
performances and creating meaningful visualizations. **This functionality is
currently in alpha state** and will be further improved in the next releases.
You will need to install tianshou with the extra `eval` to use it.

The creation of multiple experiments with varying random seeds has been greatly
facilitated. Moreover, the `ExpLauncher` interface has been introduced and
implemented with several backends to support the execution of multiple
experiments in parallel.

An example for this using the high-level interfaces can be found
[here](examples/mujoco/mujoco_ppo_hl_multi.py), examples that use low-level
interfaces will follow soon.

#### Improvements in Batch

Apart from that, several important
extensions have been added to internal data structures, most notably to `Batch`.
Batches now implement `__eq__` and can be meaningfully compared. Applying
operations in a nested fashion has been significantly simplified, and checking
for NaNs and dropping them is now possible.

One more notable change is that torch `Distribution` objects are now sliced when
slicing a batch. Previously, when a Batch with say 10 actions and a dist
corresponding to them was sliced to `[:3]`, the `dist` in the result would still
correspond to all 10 actions. Now, the dist is also "sliced" to be the
distribution of the first 3 actions.

A detailed list of changes can be found below.

### Changes/Improvements

- `evaluation`: New package for repeating the same experiment with multiple
  seeds and aggregating the results. #1074 #1141 #1183
- `data`:
    - `Batch`:
        - Add methods `to_dict` and `to_list_of_dicts`. #1063 #1098
        - Add methods `to_numpy_` and `to_torch_`. #1098, #1117
        - Add `__eq__` (semantic equality check). #1098
        - `keys()` deprecated in favor of `get_keys()` (needed to make iteration
          consistent with naming) #1105.
        - Major: new methods for applying functions to values, to check for NaNs
          and drop them, and to set values. #1181
        - Slicing a batch with a torch distribution now also slices the
          distribution. #1181
    - `data.collector`:
        - `Collector`:
            - Introduced `BaseCollector` as a base class for all collectors.
              #1123
            - Add method `close` #1063
            - Method `reset` is now more granular (new flags controlling
              behavior). #1063
        - `CollectStats`: Add convenience
          constructor `with_autogenerated_stats`. #1063
- `trainer`:
    - Trainers can now control whether collectors should be reset prior to
      training. #1063
- `policy`:
    - introduced attribute `in_training_step` that is controlled by the trainer.
      #1123
    - policy automatically set to `eval` mode when collecting and to `train`
      mode when updating. #1123
    - Extended interface of `compute_action` to also support array-like inputs
      #1169
- `highlevel`:
    - `SamplingConfig`:
        - Add support for `batch_size=None`. #1077
        - Add `training_seed` for explicit seeding of training and test
          environments, the `test_seed` is inferred from `training_seed`. #1074
    - `experiment`:
        - `Experiment` now has a `name` attribute, which can be set
          using `ExperimentBuilder.with_name` and
          which determines the default run name and therefore the persistence
          subdirectory.
          It can still be overridden in `Experiment.run()`, the new parameter
          name being `run_name` rather than
          `experiment_name` (although the latter will still be interpreted
          correctly). #1074 #1131
        - Add class `ExperimentCollection` for the convenient execution of
          multiple experiment runs #1131
        - The `World` object, containing all low-level objects needed for
          experimentation,
          can now be extracted from an `Experiment` instance. This enables
          customizing
          the experiment prior to its execution, bridging the low and high-level
          interfaces. #1187
        - `ExperimentBuilder`:
            - Add method `build_seeded_collection` for the sound creation of
              multiple
              experiments with varying random seeds #1131
            - Add method `copy` to facilitate the creation of multiple
              experiments from a single builder #1131
    - `env`:
        - Added new `VectorEnvType` called `SUBPROC_SHARED_MEM_AUTO` and used in
          for Atari and Mujoco venv creation. #1141
- `utils`:
    - `logger`:
        - Loggers can now restore the logged data into python by using the
          new `restore_logged_data` method. #1074
        - Wandb logger extended #1183
    - `net.continuous.Critic`:
        - Add flag `apply_preprocess_net_to_obs_only` to allow the
          preprocessing network to be applied to the observations only (without
          the actions concatenated), which is essential for the case where we
          want
          to reuse the actor's preprocessing network #1128
    - `torch_utils` (new module)
        - Added context managers `torch_train_mode`
          and `policy_within_training_step` #1123
    - `print`
        - `DataclassPPrintMixin` now supports outputting a string, not just
          printing the pretty repr. #1141

### Fixes

- `highlevel`:
    - `CriticFactoryReuseActor`: Enable the Critic
      flag `apply_preprocess_net_to_obs_only` for continuous critics,
      fixing the case where we want to reuse an actor's preprocessing network
      for the critic (affects usages
      of the experiment builder method `with_critic_factory_use_actor` with
      continuous environments) #1128
    - Policy parameter `action_scaling` value `"default"` was not correctly
      transformed to a Boolean value for
      algorithms SAC, DDPG, TD3 and REDQ. The value `"default"` being truthy
      caused action scaling to be enabled
      even for discrete action spaces. #1191
- `atari_network.DQN`:
    - Fix constructor input validation #1128
    - Fix `output_dim` not being set if `features_only`=True
      and `output_dim_added_layer` is not None #1128
- `PPOPolicy`:
    - Fix `max_batchsize` not being used in `logp_old` computation
      inside `process_fn` #1168
- Fix `Batch.__eq__` to allow comparing Batches with scalar array values #1185

### Internal Improvements

- `Collector`s rely less on state, the few stateful things are stored explicitly
  instead of through a `.data` attribute. #1063
- Introduced a first iteration of a naming convention for vars in `Collector`s.
  #1063
- Generally improved readability of Collector code and associated tests (still
  quite some way to go). #1063
- Improved typing for `exploration_noise` and within Collector. #1063
- Better variable names related to model outputs (logits, dist input etc.).
  #1032
- Improved typing for actors and critics, using Tianshou classes
  like `Actor`, `ActorProb`, etc.,
  instead of just `nn.Module`. #1032
- Added interfaces for most `Actor` and `Critic` classes to enforce the presence
  of `forward` methods. #1032
- Simplified `PGPolicy` forward by unifying the `dist_fn` interface (see
  associated breaking change). #1032
- Use `.mode` of distribution instead of relying on knowledge of the
  distribution type. #1032
- Exception no longer raised on `len` of empty `Batch`. #1084
- tests and examples are covered by `mypy`. #1077
- `NetBase` is more used, stricter typing by making it generic. #1077
- Use explicit multiprocessing context for creating `Pipe` in `subproc.py`.
  #1102

### Breaking Changes

- `data`:
    - `Collector`:
        - Removed `.data` attribute. #1063
        - Collectors no longer reset the environment on initialization.
          Instead, the user might have to call `reset` expicitly or
          pass `reset_before_collect=True` . #1063
        - Removed `no_grad` argument from `collect` method (was unused in
          tianshou). #1123
    - `Batch`:
        - Fixed `iter(Batch(...)` which now behaves the same way
          as `Batch(...).__iter__()`.
          Can be considered a bugfix. #1063
        - The methods `to_numpy` and `to_torch` in are not in-place anymore
          (use `to_numpy_` or `to_torch_` instead). #1098, #1117
        - The method `Batch.is_empty` has been removed. Instead, the user can
          simply check for emptiness of Batch by using `len` on dicts. #1144
        - Stricter `cat_`, only concatenation of batches with the same structure
          is allowed. #1181
        - `to_torch` and `to_numpy` are no longer static methods.
          So `Batch.to_numpy(batch)` should be replaced by `batch.to_numpy()`.
          #1200
- `utils`:
    - `logger`:
        - `BaseLogger.prepare_dict_for_logging` is now abstract. #1074
        - Removed deprecated and unused `BasicLogger` (only affects users who
          subclassed it). #1074
    - `utils.net`:
        - `Recurrent` now receives and returns
          a `RecurrentStateBatch` instead of a dict. #1077
    - Modules with code that was copied from sensAI have been replaced by
      imports from new dependency sensAI-utils:
        - `tianshou.utils.logging` is replaced with `sensai.util.logging`
        - `tianshou.utils.string` is replaced with `sensai.util.string`
        - `tianshou.utils.pickle` is replaced with `sensai.util.pickle`
- `env`:
    - All VectorEnvs now return a numpy array of info-dicts on reset instead of
      a list. #1063
- `policy`:
    - Changed interface of `dist_fn` in `PGPolicy` and all subclasses to take a
      single argument in both
      continuous and discrete cases. #1032
- `AtariEnvFactory` constructor (in examples, so not really breaking) now
  requires explicit train and test seeds. #1074
- `EnvFactoryRegistered` now requires an explicit `test_seed` in the
  constructor. #1074
- `highlevel`:
    - `params`: The parameter `dist_fn` has been removed from the parameter
      objects (`PGParams`, `A2CParams`, `PPOParams`, `NPGParams`, `TRPOParams`).
      The correct distribution is now determined automatically based on the
      actor factory being used, avoiding the possibility of
      misspecification. Persisted configurations/policies continue to work as
      expected, but code must not specify the `dist_fn` parameter.
      #1194 #1195
    - `env`:
        - `EnvFactoryRegistered`: parameter `seed` has been replaced by the pair
          of parameters `train_seed` and `test_seed`
          Persisted instances will continue to work correctly.
          Subclasses such as `AtariEnvFactory` are also affected requires
          explicit train and test seeds. #1074
        - `VectorEnvType`: `SUBPROC_SHARED_MEM` has been replaced
          by `SUBPROC_SHARED_MEM_DEFAULT`. It is recommended to
          use `SUBPROC_SHARED_MEM_AUTO` instead. However, persisted configs will
          continue working. #1141

### Tests

- Fixed env seeding it `test_sac_with_il.py` so that the test doesn't fail
  randomly. #1081
- Improved CI triggers and added telemetry (if requested by user) #1177
- Improved environment used in tests.
- Improved tests bach equality to check with scalar values #1185

### Dependencies

- [DeepDiff](https://github.com/seperman/deepdiff) added to help with diffs of
  batches in tests. #1098
- Bumped black, idna, pillow
- New extra "eval"
- Bumped numba to >=60.0.0, permitting installation on python 3.12 # 1177
- New dependency sensai-utils

Started after v1.0.0
