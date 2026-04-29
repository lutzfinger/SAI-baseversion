"""`sai verify` — fail-fast walker over the merged runtime tree.

Walks the runtime tree, hashes every file, compares each against
`.sai-overlay-manifest.json`. Reports every problem (does not stop at the
first). Exit codes:

  0 — clean
  1 — verification problems found
  2 — bad input (e.g. missing manifest, runtime root does not exist)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.runtime.manifest import ManifestCorruptError, ManifestNotFoundError
from app.runtime.verify import (
    DEFAULT_MODE,
    OverlayVerifyError,
    Verifier,
    VerifyMode,
    resolve_mode,
)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sai-verify",
        description=__doc__,
    )
    parser.add_argument(
        "--runtime",
        type=Path,
        required=True,
        help="path to the merged runtime tree (created by `sai-overlay merge`)",
    )
    parser.add_argument(
        "--mode",
        choices=("strict", "warn", "off"),
        default=None,
        help=(
            f"override SAI_OVERLAY_VERIFY env var (default: {DEFAULT_MODE} when unset)"
        ),
    )
    args = parser.parse_args(argv)

    runtime_root = args.runtime.resolve()
    if not runtime_root.exists() or not runtime_root.is_dir():
        print(f"runtime path does not exist or is not a directory: {runtime_root}",
              file=sys.stderr)
        return 2

    mode: VerifyMode = args.mode if args.mode else resolve_mode()

    try:
        verifier = Verifier(runtime_root, mode=mode)
    except (ManifestNotFoundError, ManifestCorruptError) as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return 2
    except OverlayVerifyError as exc:
        # e.g. UnverifiableModeError raised in __init__ for strict + symlink
        print(f"verify FAILED: {exc}", file=sys.stderr)
        return 1

    problems = verifier.verify_all()
    if not problems:
        print(f"verify ok: {runtime_root}  (mode={mode}, "
              f"manifest_mode={verifier.manifest.mode}, "
              f"files={len(verifier.manifest.files)})")
        return 0

    print(f"verify FAILED: {len(problems)} problem(s)", file=sys.stderr)
    for err in problems:
        print(f"  {type(err).__name__}: {err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(cli())
