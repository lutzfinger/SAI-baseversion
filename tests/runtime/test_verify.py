"""Phase 1 tests: hash-verifying loader.

Each test prepares a tiny merged runtime tree using the existing
`sai-overlay merge` machinery, then exercises one failure path of the
verifier.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.runtime.manifest import (
    MANIFEST_FILENAME,
    Manifest,
    ManifestCorruptError,
    ManifestNotFoundError,
)
from app.runtime.overlay import merge
from app.runtime.verify import (
    DEFAULT_MODE,
    ENV_VAR,
    HashMismatchError,
    MissingFileError,
    OverlayVerifyError,
    UnregisteredFileError,
    UnverifiableModeError,
    Verifier,
    VerificationFailureRecord,
    build_verifier_for_runtime,
    resolve_mode,
)


# ---------- helpers ----------


def _make_runtime(tmp_path: Path, *, mode: str = "copy") -> Path:
    """Create a tiny public/private pair, merge, return the runtime root."""

    public = tmp_path / "public"
    private = tmp_path / "private"
    runtime = tmp_path / "runtime"

    (public / "workflows").mkdir(parents=True)
    (public / "policies").mkdir(parents=True)
    (private / "workflows").mkdir(parents=True)

    (public / "workflows" / "alpha.yaml").write_text("workflow_id: alpha\n")
    (public / "workflows" / "beta.yaml").write_text("workflow_id: beta\n")
    (public / "policies" / "alpha.yaml").write_text("policy_id: alpha\n")
    # Private overrides one workflow
    (private / "workflows" / "alpha.yaml").write_text("workflow_id: alpha-override\n")

    merge(public=public, private=private, out=runtime, mode=mode)  # type: ignore[arg-type]
    return runtime


# ---------- happy path ----------


def test_clean_runtime_passes_strict(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    verifier = Verifier(runtime, mode="strict")
    assert verifier.verify_all() == []


def test_clean_runtime_verify_each_file(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    verifier = Verifier(runtime, mode="strict")
    for path in runtime.rglob("*"):
        if path.is_file() and path.name != MANIFEST_FILENAME:
            verifier.verify(path)  # should not raise


# ---------- failure paths (the spec list) ----------


def test_tampering_raises_hash_mismatch(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    verifier = Verifier(runtime, mode="strict")
    target = runtime / "workflows" / "alpha.yaml"
    target.write_text("tampered\n")
    with pytest.raises(HashMismatchError) as exc_info:
        verifier.verify(target)
    assert exc_info.value.relpath == "workflows/alpha.yaml"
    assert exc_info.value.expected != exc_info.value.actual


def test_unregistered_file_raises(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    verifier = Verifier(runtime, mode="strict")
    stray = runtime / "workflows" / "stray.yaml"
    stray.write_text("workflow_id: stray\n")
    with pytest.raises(UnregisteredFileError) as exc_info:
        verifier.verify(stray)
    assert exc_info.value.relpath == "workflows/stray.yaml"


def test_missing_file_raises_via_verify_all(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    target = runtime / "workflows" / "beta.yaml"
    target.unlink()
    verifier = Verifier(runtime, mode="strict")
    problems = verifier.verify_all()
    assert any(isinstance(p, MissingFileError) for p in problems)
    assert any(p.relpath == "workflows/beta.yaml" for p in problems)


def test_symlink_mode_strict_raises_unverifiable(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, mode="symlink")
    with pytest.raises(UnverifiableModeError):
        Verifier(runtime, mode="strict")


def test_symlink_mode_warn_logs_and_loads(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    runtime = _make_runtime(tmp_path, mode="symlink")
    with caplog.at_level("WARNING", logger="app.runtime.verify"):
        verifier = Verifier(runtime, mode="warn")
        # warn-mode constructor records the unverifiable mode but does not raise
        assert verifier.manifest.mode == "symlink"
    assert any("UnverifiableModeError" in rec.message or "symlink" in rec.message.lower()
               for rec in caplog.records)


def test_off_mode_skips_all_checks(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    target = runtime / "workflows" / "alpha.yaml"
    target.write_text("tampered\n")
    verifier = Verifier(runtime, mode="off")
    verifier.verify(target)  # tampering ignored
    assert verifier.verify_all() == []


# ---------- multi-problem aggregation ----------


def test_verify_all_aggregates_multiple_problems(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    # Tamper one, add a stray, delete one
    (runtime / "workflows" / "alpha.yaml").write_text("tampered\n")
    (runtime / "workflows" / "stray.yaml").write_text("stray\n")
    (runtime / "policies" / "alpha.yaml").unlink()
    verifier = Verifier(runtime, mode="strict")
    problems = verifier.verify_all()
    error_types = {type(p).__name__ for p in problems}
    assert "HashMismatchError" in error_types
    assert "UnregisteredFileError" in error_types
    assert "MissingFileError" in error_types


# ---------- audit callback ----------


def test_failure_callback_invoked(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    captured: list[VerificationFailureRecord] = []
    verifier = Verifier(
        runtime, mode="strict", on_failure=captured.append
    )
    target = runtime / "workflows" / "alpha.yaml"
    target.write_text("tampered\n")
    with pytest.raises(HashMismatchError):
        verifier.verify(target)
    assert len(captured) == 1
    rec = captured[0]
    assert rec.error_type == "HashMismatchError"
    assert rec.relpath == "workflows/alpha.yaml"
    assert rec.expected_sha256 is not None
    assert rec.actual_sha256 is not None
    assert rec.mode == "strict"


def test_failure_callback_exceptions_do_not_mask_error(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)

    def boom(_record: VerificationFailureRecord) -> None:
        raise RuntimeError("callback exploded")

    verifier = Verifier(runtime, mode="strict", on_failure=boom)
    target = runtime / "workflows" / "alpha.yaml"
    target.write_text("tampered\n")
    # Original verify error must still propagate; callback failure logged
    with pytest.raises(HashMismatchError):
        verifier.verify(target)


# ---------- manifest loading errors ----------


def test_manifest_not_found_raises(tmp_path: Path) -> None:
    empty_runtime = tmp_path / "empty"
    empty_runtime.mkdir()
    with pytest.raises(ManifestNotFoundError):
        Manifest.load(empty_runtime)
    with pytest.raises(ManifestNotFoundError):
        Verifier(empty_runtime, mode="strict")


def test_manifest_corrupt_raises(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    (runtime_root / MANIFEST_FILENAME).write_text("not valid json")
    with pytest.raises(ManifestCorruptError):
        Manifest.load(runtime_root)


# ---------- env var resolution ----------


def test_resolve_mode_default() -> None:
    assert resolve_mode({}) == DEFAULT_MODE


def test_resolve_mode_uppercase_env() -> None:
    assert resolve_mode({ENV_VAR: "STRICT"}) == "strict"
    assert resolve_mode({ENV_VAR: "Warn"}) == "warn"
    assert resolve_mode({ENV_VAR: "off"}) == "off"


def test_resolve_mode_invalid_raises() -> None:
    with pytest.raises(ValueError):
        resolve_mode({ENV_VAR: "very-strict"})


# ---------- build_verifier_for_runtime ----------


def test_build_verifier_returns_none_when_runtime_root_unset() -> None:
    assert build_verifier_for_runtime(None) is None


def test_build_verifier_returns_none_when_no_manifest(tmp_path: Path) -> None:
    runtime = tmp_path / "no-manifest"
    runtime.mkdir()
    assert build_verifier_for_runtime(runtime) is None


def test_build_verifier_returns_none_when_off(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    assert build_verifier_for_runtime(runtime, mode="off") is None


def test_build_verifier_returns_verifier_when_manifest_present(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    verifier = build_verifier_for_runtime(runtime)
    assert isinstance(verifier, Verifier)
    assert verifier.manifest.mode == "copy"


# ---------- loader integration: PromptStore / PolicyStore / WorkflowStore ----------


def test_workflow_store_with_verifier_rejects_tampered_yaml(tmp_path: Path) -> None:
    """The plan's headline test: 'Loading a tampered tree fails closed.'"""

    runtime = _make_runtime(tmp_path)
    verifier = Verifier(runtime, mode="strict")
    # Tamper the workflow YAML file
    (runtime / "workflows" / "alpha.yaml").write_text("workflow_id: tampered\n")

    from app.control_plane.loaders import WorkflowStore

    store = WorkflowStore(runtime / "workflows", verifier=verifier)
    with pytest.raises(HashMismatchError):
        store.load("alpha.yaml")


def test_workflow_store_without_verifier_loads_anything(tmp_path: Path) -> None:
    """Backward compat: stores without verifier behave exactly as before."""

    runtime = _make_runtime(tmp_path)
    (runtime / "workflows" / "alpha.yaml").write_text(
        "workflow_id: alpha\nversion: 1\ndescription: x\nworker: w\nconnector: c\npolicy: alpha.yaml\n"
    )
    from app.control_plane.loaders import WorkflowStore

    # Construction without verifier: no hash check happens.
    store = WorkflowStore(runtime / "workflows")
    # Just check the constructor accepted no verifier; full load needs a real
    # tool registry which is out of scope for this test.
    assert store.verifier is None
