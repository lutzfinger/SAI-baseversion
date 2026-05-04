"""Tests for the hash-verifying prompt loader (#23 + #24c)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from app.shared.prompt_loader import (
    PromptHashMismatch,
    _strip_frontmatter,
    load_hashed_prompt,
    reload_locks,
)


@pytest.fixture(autouse=True)
def _clear_lock_cache() -> None:
    reload_locks()
    yield
    reload_locks()


def _setup_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str], locks: dict[str, str],
) -> Path:
    """Build a fake repo at tmp_path with prompts/<files...> + prompt-locks.yaml.

    Patches REPO_ROOT to tmp_path so the loader looks here. Returns the
    `prompts_root` (tmp_path/prompts).
    """

    prompts_root = tmp_path / "prompts"
    prompts_root.mkdir(parents=True, exist_ok=True)
    for relpath, body in files.items():
        target = prompts_root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    (prompts_root / "prompt-locks.yaml").write_text(
        yaml.safe_dump({"prompts": locks}), encoding="utf-8"
    )
    monkeypatch.setattr("app.shared.prompt_loader.REPO_ROOT", tmp_path)
    reload_locks()
    return prompts_root


def test_loads_hash_matching_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = "---\nprompt_id: test\nversion: \"1\"\n---\nHello body.\n"
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    _setup_repo(tmp_path, monkeypatch, {"agents/test.md": body}, {"agents/test.md": sha})

    out = load_hashed_prompt("agents/test.md")
    assert out.startswith("Hello body.")


def test_raises_on_missing_lock_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = "Hello body.\n"
    _setup_repo(tmp_path, monkeypatch, {"agents/test.md": body}, {})

    with pytest.raises(PromptHashMismatch, match="no entry"):
        load_hashed_prompt("agents/test.md")


def test_raises_on_hash_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = "Hello body.\n"
    _setup_repo(tmp_path, monkeypatch, {"agents/test.md": body}, {"agents/test.md": "deadbeef"})

    with pytest.raises(PromptHashMismatch, match="hash mismatch"):
        load_hashed_prompt("agents/test.md")


def test_raises_on_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_repo(tmp_path, monkeypatch, {}, {"agents/missing.md": "deadbeef"})

    with pytest.raises(PromptHashMismatch, match="missing"):
        load_hashed_prompt("agents/missing.md")


def test_strip_frontmatter_handles_no_frontmatter() -> None:
    out = _strip_frontmatter("Just body.\n")
    assert out == "Just body.\n"


def test_strip_frontmatter_strips_yaml_block() -> None:
    text = "---\nprompt_id: x\n---\nBody starts here.\n"
    out = _strip_frontmatter(text)
    assert out == "Body starts here.\n"


def test_real_sai_eval_agent_prompt_loads_with_real_lock() -> None:
    """End-to-end check that the shipped agent prompt + lock match."""

    body = load_hashed_prompt("agents/sai_eval_agent.md")
    assert "sai-eval agent" in body
    assert "HARD RULES" in body
    assert "FIRST EXTERNAL SENDER" in body
