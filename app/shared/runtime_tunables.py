"""Runtime tunables loader.

Reads ``config/sai_runtime_tunables.yaml`` (in the merged runtime
tree) and exposes typed accessors. Values are operator-editable
ONLY via Claude Code per principle #16e + the channel-allowed-
discussion principle — Slack / web / other surfaces cannot edit them.

Loaded once at import; callers that need fresh values during a long-
running daemon should call ``reload()``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


DEFAULT_TUNABLES_PATH = Path(__file__).resolve().parents[2] / "config" / "sai_runtime_tunables.yaml"


_DEFAULTS: dict[str, Any] = {
    "intent_idle_timeout_hours": 96,
    "intent_max_history_events": 50,
    "agent_max_iterations": 8,
    "agent_max_cost_per_invocation_usd": 0.10,
    "edge_case_soft_cap": 50,
    "true_north_max_cost_per_run_usd": 2.00,
    "apply_llm_p_r_drop_threshold": 0.10,
    "course_policy_max_age_days": 180,
    "ta_roster_max_age_days": 180,
}


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    if not DEFAULT_TUNABLES_PATH.exists():
        return dict(_DEFAULTS)
    try:
        raw = yaml.safe_load(DEFAULT_TUNABLES_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return dict(_DEFAULTS)
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in raw.items() if k in _DEFAULTS})
    return out


def reload() -> None:
    """Force re-read on the next get(). Use sparingly; mostly for tests."""
    _load.cache_clear()


def get(key: str) -> Any:
    """Return the operator-tuned value or its default."""
    return _load().get(key, _DEFAULTS.get(key))


def all_tunables() -> dict[str, Any]:
    """Snapshot of every loaded tunable + its current value (for sai-health)."""
    return dict(_load())
