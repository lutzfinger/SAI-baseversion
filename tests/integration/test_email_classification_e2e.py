"""End-to-end integration: email_classification through the AI Stack.

Composes every layer built in steps 1–7 and verifies the eval-centric
contract holds:

  - Cascade with early-stop: ONE tier resolves the easy case.
  - Cascade escalation: rules abstain → local_llm → cloud_llm.
  - Full abstain → HumanTier posts an ask via SlackAskUI; record links it.
  - Ask reply reconciliation: human reply lands → record gets ground truth
    via SLACK_ASK; is_ground_truth flips True.
  - Gmail label reconciliation: user re-tags thread → record gets ground
    truth via HUMAN_LABEL.
  - Orchestrator: pending record in low-coverage bucket gets asked;
    saturated-bucket record gets skipped.

No real Slack, OpenAI, or Gmail. Tiers are scripted; clients are stubs.
The test is the proof that the pieces compose.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.connectors.slack_ask_ui import SlackAskUI
from app.eval.ask import AskStatus, AskStore
from app.eval.orchestrator import AskOrchestrator
from app.eval.reconciler import ReconciliationOutcome
from app.eval.reconcilers import AskReplyReconciler, GmailLabelReconciler
from app.eval.record import (
    EvalRecord,
    Prediction,
    RealitySource,
    RealityStatus,
)
from app.eval.storage import EvalRecordStore
from app.runtime.ai_stack import (
    HumanTier,
    Task,
    TaskConfig,
    TieredTaskRunner,
)
from app.runtime.ai_stack.tier import TierKind


def _now() -> datetime:
    return datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


# ─── scripted tiers (avoid real LLM/network) ──────────────────────────


class _ScriptedTier:
    def __init__(
        self,
        *,
        tier_id: str,
        tier_kind: TierKind,
        prediction: Prediction,
        confidence_threshold: float = 0.85,
    ) -> None:
        self.tier_id = tier_id
        self.tier_kind = tier_kind
        self.prediction = prediction
        self.confidence_threshold = confidence_threshold
        self.calls = 0

    def predict(self, _input: dict[str, Any]) -> Prediction:
        self.calls += 1
        return self.prediction


class _StubSlack:
    def __init__(self, *, ts: str = "1111111111.000100") -> None:
        self._ts = ts
        self.posts: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        return {"ok": True, "ts": self._ts, "channel": kwargs.get("channel", "")}

    def conversations_replies(self, **_kwargs: Any) -> dict[str, Any]:
        return {"messages": self.replies}


# ─── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def stores(tmp_path: Path) -> tuple[EvalRecordStore, AskStore]:
    return (
        EvalRecordStore(root=tmp_path / "eval"),
        AskStore(root=tmp_path / "eval"),
    )


@pytest.fixture
def task_config_yaml() -> Path:
    """Loads the registry/tasks/email_classification.yaml example."""

    return Path("registry/tasks/email_classification.yaml")


# ─── tests ────────────────────────────────────────────────────────────


def test_yaml_config_loads(task_config_yaml: Path) -> None:
    config = TaskConfig.from_yaml(task_config_yaml)
    assert config.task_id == "email_classification"
    assert config.active_tier_id == "cloud_llm"
    assert config.reality_observation_window_days == 7
    assert TierKind.RULES in config.graduation_thresholds


def test_cascade_early_stop_at_rules(
    stores: tuple[EvalRecordStore, AskStore],
    task_config_yaml: Path,
) -> None:
    eval_store, _ = stores
    config = TaskConfig.from_yaml(task_config_yaml)

    rules = _ScriptedTier(
        tier_id="rules",
        tier_kind=TierKind.RULES,
        prediction=Prediction(
            tier_id="rules",
            output={"label": "newsletters"},
            confidence=0.97,
            abstained=False,
        ),
    )
    local_llm = _ScriptedTier(
        tier_id="local_llm",
        tier_kind=TierKind.LOCAL_LLM,
        prediction=Prediction(tier_id="local_llm", output={}, confidence=0.0, abstained=True),
    )
    cloud_llm = _ScriptedTier(
        tier_id="cloud_llm",
        tier_kind=TierKind.CLOUD_LLM,
        prediction=Prediction(tier_id="cloud_llm", output={}, confidence=0.0, abstained=True),
    )
    task = Task(config=config, tiers=[rules, local_llm, cloud_llm])

    runner = TieredTaskRunner(eval_store=eval_store, clock=_now)
    record = runner.run(
        task,
        input_id="msg-rules-resolved",
        input_data={"subject": "Weekly digest"},
    )

    # ONE tier ran. Rules resolved.
    assert record.escalation_chain == ["rules"]
    assert record.active_decision == {"label": "newsletters"}
    assert local_llm.calls == 0
    assert cloud_llm.calls == 0


def test_cascade_walks_through_to_cloud_when_cheaper_tiers_abstain(
    stores: tuple[EvalRecordStore, AskStore],
    task_config_yaml: Path,
) -> None:
    eval_store, _ = stores
    config = TaskConfig.from_yaml(task_config_yaml)

    rules = _ScriptedTier(
        tier_id="rules",
        tier_kind=TierKind.RULES,
        prediction=Prediction(tier_id="rules", output={}, confidence=0.0, abstained=True),
    )
    local_llm = _ScriptedTier(
        tier_id="local_llm",
        tier_kind=TierKind.LOCAL_LLM,
        prediction=Prediction(tier_id="local_llm", output={}, confidence=0.0, abstained=True),
        confidence_threshold=0.7,
    )
    cloud_llm = _ScriptedTier(
        tier_id="cloud_llm",
        tier_kind=TierKind.CLOUD_LLM,
        prediction=Prediction(
            tier_id="cloud_llm",
            output={"label": "personal", "confidence": 0.92},
            confidence=0.92,
            abstained=False,
            cost_usd=0.0014,
        ),
    )
    task = Task(config=config, tiers=[rules, local_llm, cloud_llm])

    runner = TieredTaskRunner(eval_store=eval_store, clock=_now)
    record = runner.run(
        task, input_id="msg-cloud-resolved", input_data={"subject": "Hi"}
    )
    assert record.escalation_chain == ["rules", "local_llm", "cloud_llm"]
    assert record.active_decision == {"label": "personal", "confidence": 0.92}
    assert record.tier_predictions["cloud_llm"].cost_usd == 0.0014


def test_full_abstain_posts_slack_ask_via_human_tier_and_links_record(
    stores: tuple[EvalRecordStore, AskStore],
    task_config_yaml: Path,
) -> None:
    eval_store, ask_store = stores
    config = TaskConfig.from_yaml(task_config_yaml)
    slack = _StubSlack(ts="1111111111.000200")
    slack_ui = SlackAskUI(client=slack, channel="#example", ask_store=ask_store)

    rules = _ScriptedTier(
        tier_id="rules",
        tier_kind=TierKind.RULES,
        prediction=Prediction(tier_id="rules", output={}, confidence=0.0, abstained=True),
    )
    cloud_llm = _ScriptedTier(
        tier_id="cloud_llm",
        tier_kind=TierKind.CLOUD_LLM,
        prediction=Prediction(tier_id="cloud_llm", output={}, confidence=0.0, abstained=True),
    )
    human = HumanTier(
        tier_id="human",
        ask_poster=slack_ui,
        task_id=config.task_id,
    )
    task = Task(config=config, tiers=[rules, cloud_llm, human])

    runner = TieredTaskRunner(eval_store=eval_store, clock=_now)
    record = runner.run(
        task, input_id="msg-asked", input_data={"subject": "Weird edge case"}
    )

    assert record.escalation_chain == ["rules", "cloud_llm", "human"]
    assert record.ask_id is not None
    assert len(slack.posts) == 1

    # Ask record persisted with status OPEN
    [ask] = ask_store.read_all(config.task_id)
    assert ask.ask_id == record.ask_id
    assert ask.status == AskStatus.OPEN


def test_slack_reply_reconciles_record_to_ground_truth(
    stores: tuple[EvalRecordStore, AskStore],
    task_config_yaml: Path,
) -> None:
    eval_store, ask_store = stores
    config = TaskConfig.from_yaml(task_config_yaml)
    slack = _StubSlack(ts="1111111111.000300")
    slack_ui = SlackAskUI(client=slack, channel="#example", ask_store=ask_store)

    rules = _ScriptedTier(
        tier_id="rules",
        tier_kind=TierKind.RULES,
        prediction=Prediction(tier_id="rules", output={}, confidence=0.0, abstained=True),
    )
    cloud_llm = _ScriptedTier(
        tier_id="cloud_llm",
        tier_kind=TierKind.CLOUD_LLM,
        prediction=Prediction(tier_id="cloud_llm", output={}, confidence=0.0, abstained=True),
    )
    human = HumanTier(tier_id="human", ask_poster=slack_ui, task_id=config.task_id)
    task = Task(config=config, tiers=[rules, cloud_llm, human])

    runner = TieredTaskRunner(eval_store=eval_store, clock=_now)
    runner.run(task, input_id="msg-asked-2", input_data={"subject": "?"})

    # Now simulate user replying in Slack thread.
    slack.replies = [
        {"user": "BOT", "text": "[email_classification] needs input"},
        {"user": "U_LUTZ", "text": "friends"},
    ]
    reconciler = AskReplyReconciler(
        task_id=config.task_id,
        client=slack,
        ask_store=ask_store,
        eval_store=eval_store,
        bot_user_id="BOT",
        clock=lambda: _now() + timedelta(hours=1),
    )
    counts = reconciler.poll_open_asks()
    assert counts["answered"] == 1

    # Latest fold of the record shows ground truth from SLACK_ASK
    records = eval_store.read_all(config.task_id)
    latest = next(r for r in reversed(records) if r.input_id == "msg-asked-2")
    assert latest.is_ground_truth is True
    assert latest.reality_status == RealityStatus.ANSWERED
    assert latest.reality is not None
    assert latest.reality.source == RealitySource.SLACK_ASK
    assert latest.reality.label == {"text": "friends", "valid": True}


def test_gmail_relabel_reconciles_record_to_human_label(
    stores: tuple[EvalRecordStore, AskStore],
    task_config_yaml: Path,
) -> None:
    eval_store, _ = stores
    config = TaskConfig.from_yaml(task_config_yaml)

    rules = _ScriptedTier(
        tier_id="rules",
        tier_kind=TierKind.RULES,
        prediction=Prediction(
            tier_id="rules",
            output={"label": "personal", "applied_labels": ["L1/Personal"]},
            confidence=0.9,
            abstained=False,
        ),
    )
    cloud_llm = _ScriptedTier(
        tier_id="cloud_llm",
        tier_kind=TierKind.CLOUD_LLM,
        prediction=Prediction(tier_id="cloud_llm", output={}, confidence=0.0, abstained=True),
    )
    task = Task(config=config, tiers=[rules, cloud_llm])

    runner = TieredTaskRunner(eval_store=eval_store, clock=_now)
    record = runner.run(
        task, input_id="thread-1", input_data={"subject": "Hi"}
    )

    # Simulate user re-labeling in Gmail.
    user_relabeled = {"L1/Friends", "INBOX"}
    reconciler = GmailLabelReconciler(
        task_id=config.task_id,
        thread_labels_fn=lambda _tid: user_relabeled,
        applied_label_extractor=lambda decision: set(
            decision.get("applied_labels", [])
        ),
    )
    result = reconciler.reconcile_one(record, now=_now() + timedelta(days=2))
    assert result.outcome == ReconciliationOutcome.OBSERVED
    assert result.reality is not None
    assert result.reality.source == RealitySource.HUMAN_LABEL
    assert result.reality.label == {"labels": ["L1/Friends"]}


def test_orchestrator_asks_undercovered_pending_within_budget(
    stores: tuple[EvalRecordStore, AskStore],
    task_config_yaml: Path,
) -> None:
    eval_store, ask_store = stores
    config = TaskConfig.from_yaml(task_config_yaml)
    slack = _StubSlack(ts="1111111111.000400")
    slack_ui = SlackAskUI(client=slack, channel="#example", ask_store=ask_store)

    # Seed two pending records, no ground truth → both undercovered.
    for index, label in enumerate(["customers", "newsletters"]):
        eval_store.append(
            EvalRecord(
                task_id=config.task_id,
                input_id=f"msg-{index}",
                input={"subject": "x"},
                active_decision={"label": label},
                decided_at=_now() - timedelta(hours=1),
                reality_observation_window_ends_at=_now() + timedelta(days=6),
                tier_predictions={
                    "rules": Prediction(
                        tier_id="rules",
                        output={"label": "newsletters"},
                        confidence=0.55,
                    ),
                    "cloud_llm": Prediction(
                        tier_id="cloud_llm",
                        output={"label": label},
                        confidence=0.62,
                    ),
                },
            )
        )

    orchestrator = AskOrchestrator(
        task_id=config.task_id,
        ask_poster=slack_ui,
        eval_store=eval_store,
        ask_store=ask_store,
        bucketing_fn=lambda r: r.active_decision.get("label"),
        daily_budget=1,
        coverage_target=100,
        min_priority_threshold=0.10,
        clock=_now,
    )
    counts = orchestrator.review_pending()
    assert counts["asked"] == 1
    assert counts["waited"] == 1
    assert len(slack.posts) == 1


def test_full_pipeline_one_input_to_ground_truth(
    stores: tuple[EvalRecordStore, AskStore],
    task_config_yaml: Path,
) -> None:
    """The narrative test: one email enters the cascade, gets escalated,
    asked of human, answered, reconciled. End state: ground truth landed."""

    eval_store, ask_store = stores
    config = TaskConfig.from_yaml(task_config_yaml)
    slack = _StubSlack(ts="1111111111.000500")
    slack_ui = SlackAskUI(client=slack, channel="#example", ask_store=ask_store)

    cascade = [
        _ScriptedTier(
            tier_id="rules",
            tier_kind=TierKind.RULES,
            prediction=Prediction(tier_id="rules", output={}, confidence=0.0, abstained=True),
        ),
        _ScriptedTier(
            tier_id="cloud_llm",
            tier_kind=TierKind.CLOUD_LLM,
            prediction=Prediction(
                tier_id="cloud_llm",
                output={"label": "personal", "confidence": 0.6},
                confidence=0.6,
                abstained=True,
            ),
        ),
        HumanTier(tier_id="human", ask_poster=slack_ui, task_id=config.task_id),
    ]
    task = Task(config=config, tiers=cascade)

    runner = TieredTaskRunner(eval_store=eval_store, clock=_now)
    record = runner.run(
        task, input_id="msg-narrative", input_data={"subject": "edge case"}
    )
    assert record.ask_id is not None
    assert record.is_ground_truth is False
    assert record.reality_status == RealityStatus.ASKED

    # Lutz answers in Slack.
    slack.replies = [
        {"user": "BOT", "text": "needs input"},
        {"user": "U_LUTZ", "text": "personal"},
    ]
    reconciler = AskReplyReconciler(
        task_id=config.task_id,
        client=slack,
        ask_store=ask_store,
        eval_store=eval_store,
        bot_user_id="BOT",
        clock=lambda: _now() + timedelta(hours=2),
    )
    reconciler.poll_open_asks()

    # End state: record is ground truth, ask is ANSWERED, both via SLACK_ASK.
    records = eval_store.read_all(config.task_id)
    latest = next(r for r in reversed(records) if r.input_id == "msg-narrative")
    assert latest.is_ground_truth is True
    assert latest.reality_status == RealityStatus.ANSWERED
    assert latest.reality is not None
    assert latest.reality.label == {"text": "personal", "valid": True}

    [ask] = ask_store.latest_state(config.task_id).values()
    assert ask.status == AskStatus.ANSWERED
