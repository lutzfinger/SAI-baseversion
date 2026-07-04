#!/usr/bin/env python3
"""Different-vendor (OpenAI) adversarial code review - the second-opinion step.

Reads OPENAI_API_KEY from the environment (provide it through your 1Password
wrapper; do NOT put a secret reference in this repo - the boundary linter blocks
those). Before sending anything, it runs the boundary linter on the target file
and REFUSES if it is flagged, so private data is never sent to an external
vendor. Prints findings; makes no commits. It is advisory: a human triages the
output; it is not a gate.

Usage:
  OPENAI_API_KEY=... python scripts/cross_review.py \
      --file <path> --context "what this artifact IS" [--focus "..."]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_PREFERRED = ("gpt-5.2", "gpt-5.1", "gpt-5", "gpt-4.1", "gpt-4o")


def _pick_model(client) -> str:
    ids = [m.id for m in client.models.list().data]
    for want in _PREFERRED:
        cand = [m for m in ids if m == want] or [m for m in ids if m.startswith(want)]
        cand = [m for m in cand if not any(x in m for x in ("audio", "realtime", "image", "tts"))]
        if cand:
            return sorted(cand)[0]
    return "gpt-4o"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Different-vendor (OpenAI) adversarial review of a file."
    )
    ap.add_argument("--file", required=True, help="path to the artifact to review")
    ap.add_argument("--context", required=True,
                    help="what the artifact IS (prevents mis-framing)")
    ap.add_argument("--focus", default="correctness, fail-open/security, and edge-case bugs",
                    help="what to focus the review on")
    args = ap.parse_args(argv)

    # (a) readable file
    try:
        content = Path(args.file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cross_review: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2

    # (b) boundary pre-flight - never send private data to an external vendor.
    # Lint the EXACT bytes that will be sent (a temp copy of `content`), not the
    # file path, so the file cannot change between the lint and the send (TOCTOU).
    linter = REPO_ROOT / "scripts" / "boundary_check.py"
    if not linter.exists():
        print(f"cross_review: boundary linter missing at {linter}; refusing to send.",
              file=sys.stderr)
        return 2
    tmp_path = None
    try:
        # inside REPO_ROOT: boundary_check.py resolves paths relative to its root
        # and loads its private-terms from there, so an outside-repo temp would
        # crash it. A hidden, unique temp under the repo lints correctly.
        fd, tmp_path = tempfile.mkstemp(
            dir=str(REPO_ROOT), prefix=".crossrev_tmp_",
            suffix=Path(args.file).suffix or ".txt",
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        result = subprocess.run(
            [sys.executable, str(linter), "--paths", tmp_path],
            capture_output=True, text=True,
        )
    except OSError as exc:
        print(f"cross_review: boundary pre-flight could not run ({exc}); refusing to send.",
              file=sys.stderr)
        return 2
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    if result.returncode != 0:
        print("cross_review: REFUSING to send - the boundary linter flagged this file; "
              "private data must not go to an external vendor:", file=sys.stderr)
        print(result.stdout + result.stderr, file=sys.stderr)
        return 2

    # (c) key present
    if not os.environ.get("OPENAI_API_KEY"):
        print("cross_review: OPENAI_API_KEY is not set. Provide it via your 1Password "
              "wrapper before running.", file=sys.stderr)
        return 2

    # (d) call the vendor; fail closed on any API/auth/network error
    try:
        from openai import OpenAI

        client = OpenAI()
        model = _pick_model(client)
        prompt = (
            "You are an adversarial reviewer from a different vendor.\n"
            f"CONTEXT (what this artifact IS): {args.context}\n"
            f"FOCUS: {args.focus}\n"
            "Review the artifact below. Be concrete and terse; list findings as "
            "SEVERITY: issue -> fix. If it is sound, say so and name the strongest "
            f"residual risk.\n\n=== {args.file} ===\n{content}"
        )
        response = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}]
        )
    except Exception as exc:  # noqa: BLE001 - fail closed on any API/auth/network error
        print(f"cross_review: OpenAI call failed ({type(exc).__name__}: {exc}).",
              file=sys.stderr)
        return 3

    print(f"MODEL: {model}")
    print("=== REVIEW ===")
    print(response.choices[0].message.content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
