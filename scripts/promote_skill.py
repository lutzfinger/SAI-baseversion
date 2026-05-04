"""CLI: promote a skill from `incoming/<draft_id>/` to `skills/<workflow_id>/`.

Validates the manifest, computes + writes the integrity hash, then
moves the directory. After promotion, run `sai-overlay merge` to make
the skill visible to the runtime.

Usage:
    python -m scripts.promote_skill \\
        --incoming-dir $SAI_PRIVATE/skills/incoming/some-draft/ \\
        --target-dir   $SAI_PRIVATE/skills/some-workflow/
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from app.skills.integrity import compute_skill_sha256, write_integrity_file
from app.skills.loader import load_skill_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate + integrity-stamp + promote a SAI skill.",
    )
    parser.add_argument(
        "--incoming-dir", type=Path, required=True,
        help="The skill draft to promote (typically under incoming/).",
    )
    parser.add_argument(
        "--target-dir", type=Path, required=True,
        help="Where to land the promoted skill (typically skills/<workflow_id>/).",
    )
    parser.add_argument(
        "--in-place", action="store_true",
        help="Skip the move; just validate + write integrity hash. Useful "
             "when re-stamping a skill after an in-place edit.",
    )
    args = parser.parse_args(argv)

    if not args.incoming_dir.is_dir():
        print(f"ERROR: incoming-dir does not exist: {args.incoming_dir}",
              file=sys.stderr)
        return 2

    # Step 1 — validate manifest (fail closed before stamping).
    print(f"validating manifest at {args.incoming_dir}…")
    manifest, report = load_skill_manifest(args.incoming_dir)
    if not report.ok:
        print(f"ERROR: manifest invalid:\n{report.summary()}", file=sys.stderr)
        return 3
    print(f"  ✓ {manifest.identity.workflow_id} v{manifest.identity.version}")

    # Step 2 — compute + write integrity hash.
    sha = compute_skill_sha256(args.incoming_dir)
    write_integrity_file(args.incoming_dir)
    print(f"  ✓ integrity hash recorded: {sha[:16]}…")

    # Step 3 — move (or skip if --in-place).
    if args.in_place:
        print(f"  ✓ in-place re-stamp; no move performed")
        return 0

    if args.target_dir.exists():
        print(f"ERROR: target-dir already exists: {args.target_dir}",
              file=sys.stderr)
        print("  Either back it up first or use --in-place to re-stamp.",
              file=sys.stderr)
        return 4

    args.target_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(args.incoming_dir), str(args.target_dir))
    print(f"  ✓ promoted to {args.target_dir}")
    print()
    print("Next step: re-merge the overlay so the runtime picks up the skill")
    print("    sai-overlay merge --public <public> --private <private> --out <runtime> --clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
