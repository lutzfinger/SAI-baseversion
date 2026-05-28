"""Validate skill.yaml v1 (SkillManifest, #33) and v2 (SkillManifestV2, profiles).

The gate every unified-skill-sync PR (PR 2 deploy, PR 4 cowork, PR 6 import,
PR 7 scaffold) calls before writing a single byte. Per PRINCIPLES.md §6a,
every value crossing a trust boundary must be validated against an explicit
allowed shape.

Public API (returns ``(manifest, report)`` tuples — matches loader.py
convention; does NOT raise on validation failure):

  validate_file(path, *, skill_dir=None) -> tuple[Optional[SkillManifest | SkillManifestV2], ValidationReport]
  validate_dict(data, *, skill_dir=None) -> tuple[Optional[SkillManifest | SkillManifestV2], ValidationReport]

CLI:

  python -m app.skills.manifest_validator <skill.yaml path>
    → exit 0 on valid, 2 on invalid (matches sai-overlay conventions)

  python -m app.skills.manifest_validator --smoke-test-all-real-skills
    → walks (SAI ∪ SAI-baseversion)/skills/ and validates every skill.yaml
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Optional, Union

import yaml
from pydantic import ValidationError as PydanticValidationError

from app.skills.manifest import (
    SkillManifest,
    SkillManifestV2,
    ValidationIssue,
    ValidationReport,
)

LOGGER = logging.getLogger(__name__)

SKILL_MANIFEST_FILENAME = "skill.yaml"
ALLOWED_DEPLOY_TARGETS: frozenset[str] = frozenset(
    {"sai_runtime", "claude_code", "cowork"}
)

ManifestType = Union[SkillManifest, SkillManifestV2]


# ─── public API ──────────────────────────────────────────────────────


def validate_file(
    path: Path,
    *,
    skill_dir: Optional[Path] = None,
) -> tuple[Optional[ManifestType], ValidationReport]:
    """Load ``path`` as YAML and validate it as a SAI skill manifest.

    ``skill_dir`` (defaults to ``path.parent``) is the directory that
    files[] entries are resolved against for path-safety + glob
    expansion. Pass ``None`` to skip filesystem checks (schema-only).
    """

    if skill_dir is None:
        skill_dir = path.parent

    if not path.exists():
        return None, ValidationReport(
            workflow_id="(missing)",
            errors=[ValidationIssue(
                severity="error",
                rule="manifest.missing",
                message=f"No file at {path}",
            )],
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, ValidationReport(
            workflow_id="(unparseable)",
            errors=[ValidationIssue(
                severity="error",
                rule="manifest.yaml_parse_error",
                message=str(exc),
            )],
        )

    return validate_dict(raw, skill_dir=skill_dir)


def validate_dict(
    data: Any,
    *,
    skill_dir: Optional[Path] = None,
) -> tuple[Optional[ManifestType], ValidationReport]:
    """Validate an already-parsed YAML dict as a SAI skill manifest.

    Dispatches on ``schema_version`` and presence of ``profiles``.
    """

    workflow_id = "(unknown)"
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    # Step 12 — root must be a dict.
    if not isinstance(data, dict):
        errors.append(ValidationIssue(
            severity="error",
            rule="manifest.root_not_a_dict",
            message=(
                f"skill.yaml root must be a YAML mapping (dict); got "
                f"{type(data).__name__}. A list at the root is never valid."
            ),
        ))
        return None, ValidationReport(
            workflow_id=workflow_id, errors=errors, warnings=warnings,
        )

    # Pull workflow_id / skill_id early for nicer error messages.
    identity = data.get("identity") or {}
    if isinstance(identity, dict):
        workflow_id = (
            identity.get("workflow_id")
            or identity.get("skill_id")
            or workflow_id
        )

    schema_version = str(data.get("schema_version", "1"))

    # Step 3 dispatch: v2 if (schema_version == "2") OR (profiles: key present).
    has_profiles_key = "profiles" in data
    is_v2 = schema_version == "2" or has_profiles_key

    if is_v2:
        manifest, report = _validate_v2(data, skill_dir, workflow_id)
    else:
        manifest, report = _validate_v1(data, skill_dir, workflow_id)

    # Candidate recognition (PR 1a): provenance ALONE doesn't make a skill a
    # candidate — a clean, schema-valid skill may legitimately record who
    # designed it. A skill is a CANDIDATE only when it FAILS the registered
    # schema AND carries a `provenance:` block — i.e. it's raw designer-surface
    # output (cowork / claude_code) awaiting the registration ceremony (#33b).
    # Reclassify such failures from error → informational so they don't count
    # as validation failures; the fix is the ceremony, not schema edits.
    if not report.ok and "provenance" in data:
        return None, ValidationReport(
            workflow_id=workflow_id,
            errors=[],
            warnings=[ValidationIssue(
                severity="warning",
                rule="manifest.candidate",
                message=(
                    "pre-registration CANDIDATE (fails registered schema but "
                    "carries provenance:); awaiting registration ceremony "
                    "(skill_critique_reviewer + make validate-skill). Original "
                    f"first error: {report.errors[0].rule}: {report.errors[0].message}"
                ),
            )],
        )

    return manifest, report


# ─── v1 path ─────────────────────────────────────────────────────────


def _validate_v1(
    data: dict[str, Any],
    skill_dir: Optional[Path],
    workflow_id: str,
) -> tuple[Optional[SkillManifest], ValidationReport]:
    """v1 schema (single sai_workflow, no profiles:)."""

    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    try:
        manifest = SkillManifest.model_validate(data)
    except PydanticValidationError as exc:
        errors.extend(_pydantic_errors_to_issues(exc, "manifest.v1_schema"))
        return None, ValidationReport(
            workflow_id=workflow_id, errors=errors, warnings=warnings,
        )

    # v1 doesn't have files[]; no path-safety check applies.
    return manifest, ValidationReport(
        workflow_id=manifest.identity.workflow_id,
        errors=errors,
        warnings=warnings,
    )


# ─── v2 path ─────────────────────────────────────────────────────────


def _validate_v2(
    data: dict[str, Any],
    skill_dir: Optional[Path],
    workflow_id: str,
) -> tuple[Optional[SkillManifestV2], ValidationReport]:
    """v2 schema (multi-profile container)."""

    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    # Step 3 — reject empty / missing profiles before pydantic, to give a
    # clearer error than pydantic's default.
    profiles_raw = data.get("profiles")
    if profiles_raw is None or profiles_raw == {}:
        errors.append(ValidationIssue(
            severity="error",
            rule="manifest.v2_no_profile",
            message=(
                "v2 manifest must declare at least one enabled profile "
                "under `profiles:` (sai_workflow or claude_code)"
            ),
        ))
        return None, ValidationReport(
            workflow_id=workflow_id, errors=errors, warnings=warnings,
        )

    # Force schema_version to "2" so the Literal["2"] field accepts even
    # when callers wrote schema_version: "1" but with profiles: present.
    data_normalized = dict(data)
    data_normalized["schema_version"] = "2"

    try:
        manifest = SkillManifestV2.model_validate(data_normalized)
    except PydanticValidationError as exc:
        errors.extend(_pydantic_errors_to_issues(exc, "manifest.v2_schema"))
        return None, ValidationReport(
            workflow_id=workflow_id, errors=errors, warnings=warnings,
        )

    # Step 6 — path-safety + glob expansion across every enabled profile's
    # files[]. Mutates a copy of files in place (after model_dump so we
    # don't fight pydantic's frozen-ish semantics).
    if skill_dir is not None:
        per_profile_errors, per_profile_warnings = _check_files(
            manifest=manifest,
            skill_dir=skill_dir,
        )
        errors.extend(per_profile_errors)
        warnings.extend(per_profile_warnings)

    # Step 7 — deploy_to[] subset of allowed targets is enforced by
    # the Pydantic Literal type, but if callers ever wrote dynamic
    # values that slipped past Literal, this is a belt-and-suspenders
    # check. Currently the Literal does the work; no extra code here.

    if errors:
        return None, ValidationReport(
            workflow_id=workflow_id, errors=errors, warnings=warnings,
        )

    return manifest, ValidationReport(
        workflow_id=manifest.identity.workflow_id,
        errors=errors,
        warnings=warnings,
    )


# ─── files[] / path-safety / glob expansion ──────────────────────────


def _check_files(
    *,
    manifest: SkillManifestV2,
    skill_dir: Path,
) -> tuple[list[ValidationIssue], list[ValidationIssue]]:
    """Walk every profile's files[]: refuse traversal, expand globs.

    Returns (errors, warnings). Mutates the manifest in place: globs are
    replaced with their concrete expansion in files[].
    """

    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    skill_dir_resolved = skill_dir.resolve()

    for profile_name in ("sai_workflow", "claude_code"):
        profile = getattr(manifest.profiles, profile_name, None)
        if profile is None or not profile.enabled:
            continue

        expanded: list[str] = []
        for entry in profile.files:
            # ── path-safety: refuse absolute, refuse `..` ───────────────
            if Path(entry).is_absolute():
                errors.append(ValidationIssue(
                    severity="error",
                    rule="files.path_absolute",
                    message=(
                        f"profile {profile_name!r}: file path {entry!r} is "
                        f"absolute; only paths relative to the skill dir are "
                        f"allowed"
                    ),
                ))
                continue
            if ".." in Path(entry).parts:
                errors.append(ValidationIssue(
                    severity="error",
                    rule="files.path_traversal",
                    message=(
                        f"profile {profile_name!r}: file path {entry!r} "
                        f"contains `..` (path traversal); refuse"
                    ),
                ))
                continue

            # ── glob expansion ──────────────────────────────────────────
            if any(c in entry for c in "*?["):
                matches = sorted(skill_dir_resolved.glob(entry))
                if not matches:
                    warnings.append(ValidationIssue(
                        severity="warning",
                        rule="files.glob_no_match",
                        message=(
                            f"profile {profile_name!r}: glob {entry!r} "
                            f"matched no files under {skill_dir}"
                        ),
                    ))
                    continue
                for m in matches:
                    rel = m.relative_to(skill_dir_resolved).as_posix()
                    # Re-apply path-safety after resolution.
                    abs_m = m.resolve()
                    try:
                        abs_m.relative_to(skill_dir_resolved)
                    except ValueError:
                        errors.append(ValidationIssue(
                            severity="error",
                            rule="files.glob_escaped_skill_dir",
                            message=(
                                f"profile {profile_name!r}: glob {entry!r} "
                                f"resolved to {abs_m}, outside skill dir "
                                f"{skill_dir_resolved}"
                            ),
                        ))
                        continue
                    expanded.append(rel)
                continue

            # ── concrete relpath: check it stays inside skill_dir ───────
            target = (skill_dir_resolved / entry).resolve()
            try:
                target.relative_to(skill_dir_resolved)
            except ValueError:
                errors.append(ValidationIssue(
                    severity="error",
                    rule="files.path_outside_skill_dir",
                    message=(
                        f"profile {profile_name!r}: file {entry!r} resolves "
                        f"to {target}, outside skill dir {skill_dir_resolved}"
                    ),
                ))
                continue

            # File-existence check is informational only at this layer;
            # the deploy command (PR 2) makes it hard.
            if not target.exists():
                warnings.append(ValidationIssue(
                    severity="warning",
                    rule="files.missing_on_disk",
                    message=(
                        f"profile {profile_name!r}: file {entry!r} listed in "
                        f"manifest but missing on disk at {target}"
                    ),
                ))
            expanded.append(entry)

        # Replace files[] in-place with the resolved set.
        profile.files = expanded

    return errors, warnings


# ─── pydantic error → ValidationIssue conversion ─────────────────────


def _pydantic_errors_to_issues(
    exc: PydanticValidationError,
    rule_prefix: str,
) -> list[ValidationIssue]:
    """Convert pydantic's structured errors to flat ValidationIssue rows.

    Each pydantic error becomes one ValidationIssue. The `rule` field
    encodes the location (e.g. "manifest.v2_schema.profiles.claude_code.cascade").
    """

    issues: list[ValidationIssue] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        rule = f"{rule_prefix}.{loc}" if loc else rule_prefix
        msg = err.get("msg") or str(err)
        # Stringify input for friendlier output, capped at 80 chars.
        input_repr = repr(err.get("input"))
        if len(input_repr) > 80:
            input_repr = input_repr[:77] + "..."
        issues.append(ValidationIssue(
            severity="error",
            rule=rule,
            message=f"{msg} (got {input_repr})",
        ))
    return issues


# ─── smoke test over every real skill in (SAI ∪ SAI-baseversion) ─────


_REAL_SKILL_ROOTS: tuple[Path, ...] = (
    Path.home() / "Lutz_Dev" / "SAI" / "skills",
    Path.home() / "Lutz_Dev" / "SAI-baseversion" / "skills",
)


def _is_candidate(report: ValidationReport) -> bool:
    return any(w.rule == "manifest.candidate" for w in report.warnings)


def smoke_test_all_real_skills(
    roots: tuple[Path, ...] = _REAL_SKILL_ROOTS,
) -> tuple[int, int, int, list[tuple[Path, ValidationReport]]]:
    """Walk every roots/<skill-id>/skill.yaml; validate; collect results.

    Skips:
      - dotfiles / dotdirs (".git", ".pytest_cache")
      - underscore-prefixed entries ("__init__.py", "__pycache__")
      - anything that isn't a directory (per fixture-walker rule)

    Returns (pass_count, fail_count, candidate_count, failed_reports).
    Candidates (skill.yaml with a `provenance:` block) are counted
    separately — they are not failures (#33b: awaiting registration).
    """

    pass_count = 0
    fail_count = 0
    candidate_count = 0
    failures: list[tuple[Path, ValidationReport]] = []

    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if entry.name.startswith(("_", ".")):
                continue
            if not entry.is_dir():
                continue
            manifest_path = entry / SKILL_MANIFEST_FILENAME
            if not manifest_path.is_file():
                # Not every dir under skills/ ships a manifest yet (e.g.,
                # `incoming/` is a folder of sub-skills). Skip silently.
                continue
            _, report = validate_file(manifest_path, skill_dir=entry)
            if _is_candidate(report):
                candidate_count += 1
            elif report.ok:
                pass_count += 1
            else:
                fail_count += 1
                failures.append((manifest_path, report))

    return pass_count, fail_count, candidate_count, failures


# ─── CLI ─────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.skills.manifest_validator",
        description=(
            "Validate a skill.yaml (v1 or v2). Exit 0 = valid, 2 = invalid. "
            "Matches sai-overlay CLI conventions."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        help="Path to skill.yaml to validate.",
    )
    parser.add_argument(
        "--smoke-test-all-real-skills",
        action="store_true",
        help=(
            "Walk every skill.yaml under "
            "(~/Lutz_Dev/SAI ∪ ~/Lutz_Dev/SAI-baseversion)/skills/ and report. "
            "Exit 0 if all pass, 1 if any fail."
        ),
    )

    args = parser.parse_args(argv)

    if args.smoke_test_all_real_skills:
        passed, failed, candidates, reports = smoke_test_all_real_skills()
        print(
            f"smoke test: {passed} passed, {failed} failed, "
            f"{candidates} candidate"
        )
        if reports:
            print()
            print("Failed manifests:")
            for path, rep in reports:
                print(f"  {path}")
                for issue in rep.errors:
                    print(f"    ❌ {issue.rule}: {issue.message}")
        return 1 if failed else 0

    if args.path is None:
        parser.error("either a path or --smoke-test-all-real-skills is required")

    _, report = validate_file(args.path)
    if _is_candidate(report):
        # Candidate: print the informational warning, exit 0 (not a failure).
        print(f"{report.workflow_id}: CANDIDATE (awaiting registration)")
        for w in report.warnings:
            print(f"  ⓘ {w.rule}: {w.message}")
        return 0
    print(report.summary())
    return 0 if report.ok else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
