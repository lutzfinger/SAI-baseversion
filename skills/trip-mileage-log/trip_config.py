"""Load the operator's private trip-mileage config (§17/§18 values).

The base skill is values-free; everything operator-specific (home, sheet URL,
tab gids, kill-switch name, place aliases) lives in a PRIVATE config that the
overlay merges to ``~/.sai-runtime/config/trip_mileage.yaml``. This loader
fail-closes (#6) on an absent file or any missing required key.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".sai-runtime" / "config" / "trip_mileage.yaml"

_REQUIRED_KEYS = (
    "home_label",
    "sheet_url",
    "time_tracking_gid",
    "distance_gid",
    "business_pct_default",
    "kill_switch_env",
)


def load_trip_config(path: str | Path | None = None) -> dict[str, Any]:
    """Return the validated trip config dict, or raise with a clear message.

    Raises FileNotFoundError if the file is absent, ValueError if a required
    key is missing or the YAML root is not a mapping.
    """
    cfg_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"trip-mileage-log config not found at {cfg_path}. "
            f"Create it (see SAI/config/trip_mileage.yaml) or pass --config."
        )
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{cfg_path}: config root must be a YAML mapping, got {type(raw).__name__}")
    missing = [k for k in _REQUIRED_KEYS if k not in raw or raw[k] in (None, "")]
    if missing:
        raise ValueError(f"{cfg_path}: missing required config key(s): {', '.join(missing)}")
    # Normalize optional maps so callers never None-check.
    raw.setdefault("place_aliases", {})
    raw.setdefault("seed_distances", {})
    if not isinstance(raw["place_aliases"], dict):
        raise ValueError(f"{cfg_path}: place_aliases must be a mapping")
    return raw
