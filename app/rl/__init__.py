"""RL-from-human-feedback layer for SAI.

Converts existing human signals (Slack approvals, eval record reality
observations, feedback actions) into structured training data for
downstream fine-tuning via DPO, SFT, or PPO/GRPO.

Public surface:
    models          — ScoredTrajectory, PreferencePair, RewardSignal
    trajectory      — RawTrajectory, TrajectoryStore, ScoredTrajectoryStore
    reward          — HumanRewardScorer
    preference_pairs — PreferencePairBuilder
    batch_runner    — BatchTrajectoryRunner
    exporters       — HuggingFaceExporter
"""
