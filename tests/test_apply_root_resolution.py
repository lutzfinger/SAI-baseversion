"""Regression test for the 2026-05-04 bot apply-path bug.

The slack_bot's apply_in_background passed `private_root =
settings.root_dir` to apply_proposal. When the bot ran from the
merged runtime tree (~/.sai-runtime), settings.root_dir IS
~/.sai-runtime — the merger then refused with `--out cannot be
inside --private`.

This test exercises the OVERLAY VALIDATOR directly with the
exact failure mode (private == runtime) so the regression is
caught at the framework level, not just at the bot level.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.runtime.overlay import InputError, _validate_paths


def test_validator_refuses_when_out_equals_private(tmp_path):
    public = tmp_path / "public"
    private = tmp_path / "private"
    public.mkdir()
    private.mkdir()
    out = private  # The exact failure mode: out path == private path.
    with pytest.raises(InputError, match="--out cannot be inside --private"):
        _validate_paths(public=public, private=private, out=out, clean=True)


def test_validator_refuses_when_out_inside_private(tmp_path):
    public = tmp_path / "public"
    private = tmp_path / "private"
    public.mkdir()
    private.mkdir()
    out = private / "nested" / "runtime"
    with pytest.raises(InputError, match="--out cannot be inside --private"):
        _validate_paths(public=public, private=private, out=out, clean=True)


def test_validator_accepts_sibling_out(tmp_path):
    public = tmp_path / "public"
    private = tmp_path / "private"
    out = tmp_path / "runtime"
    public.mkdir()
    private.mkdir()
    # Should not raise.
    _validate_paths(public=public, private=private, out=out, clean=True)


def test_validator_refuses_when_out_inside_public(tmp_path):
    public = tmp_path / "public"
    private = tmp_path / "private"
    public.mkdir()
    private.mkdir()
    out = public / "runtime"
    with pytest.raises(InputError, match="--out cannot be inside --public"):
        _validate_paths(public=public, private=private, out=out, clean=True)
