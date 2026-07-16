#!/usr/bin/env python3
"""Doc-freshness gate (check-in layer, Plan D / N9).

Flags likely-stale prose docs: when code in an area changes but the doc that
OWNS that area was not touched in the same change, the doc is probably drifting.
Follows the `generate_tool_overview.py --check` precedent (a --check that a
gate can run), but for HAND-WRITTEN docs there is no generator to diff against,
so this is a heuristic reminder, not a byte-check.

POSTURE — WARN-FIRST: prints warnings and exits 0 by default (the README /
code_map backlog is large; see the staleness-sweep loose-end). Set
DOC_FRESHNESS_BLOCK=1 to exit 1 on drift once the backlog is cleared.

Reuse (check 12): the area->doc map below is derived from docs/code_map.md's
directory skeleton (the coarse "by area" headings that Agent D confirmed still
match the tree), extended with the 5 packages code_map.md omits. It deliberately
does NOT invent a new mapping file — keep this dict in sync with code_map.md.

An owning doc is considered "reaffirmed" for a change if EITHER it was modified
in the same diff, OR the plan/commit provides `doc-ok: <area>` lines (an explicit
"I checked, still accurate" override).

Usage: check_doc_freshness.py [--base <git-ref>] [--repo <dir>] [--ack <area> ...]
Exit: 0 (WARN-first) or 1 (DOC_FRESHNESS_BLOCK=1 and drift found); 2 bad input.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# code-area (path prefix) -> docs that OWN it. Keep in sync with docs/code_map.md.
# The 5 packages code_map.md omits (canonical, eval, reflection, tasks, learning)
# are included so a change there is not silently "unowned".
AREA_OWNERS: dict[str, list[str]] = {
    "app/control_plane/": ["docs/architecture.md", "docs/code_map.md"],
    "app/graphs/": ["docs/architecture.md", "docs/code_map.md"],
    "app/workers/": ["docs/code_map.md", "docs/system_inventory.md"],
    "app/tools/": ["docs/tool_overview.md", "docs/code_map.md"],
    "app/connectors/": ["docs/code_map.md", "docs/threat_model.md"],
    "app/llm/": ["docs/architecture.md", "docs/code_map.md"],
    "app/canonical/": ["docs/code_map.md"],
    "app/eval/": ["docs/testing.md", "docs/code_map.md"],
    "app/reflection/": ["docs/code_map.md"],
    "app/tasks/": ["docs/code_map.md"],
    "app/learning/": ["docs/code_map.md"],
    "app/skills/": ["docs/SKILL-LIFECYCLE.md", "docs/code_map.md"],
    "registry/": ["docs/tool_registry_guide.md"],
    "config/llm_registry.yaml": ["docs/architecture.md"],
}


def _changed(repo: Path, base: str | None) -> list[str]:
    rng = base if base else "HEAD~1"
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", rng],
            capture_output=True, text=True, timeout=15,
        )
        files = [f for f in out.stdout.splitlines() if f.strip()]
    except Exception:
        files = []
    # include staged + unstaged too, so it works mid-change
    for extra in (["diff", "--name-only"], ["diff", "--name-only", "--cached"]):
        try:
            out = subprocess.run(["git", "-C", str(repo), *extra],
                                 capture_output=True, text=True, timeout=15)
            files.extend(f for f in out.stdout.splitlines() if f.strip())
        except Exception:
            pass
    return sorted(set(files))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=None,
                    help="git ref to diff against (default HEAD~1 + working)")
    ap.add_argument("--repo", type=Path, default=Path.cwd())
    ap.add_argument("--ack", nargs="*", default=[], help="areas explicitly reaffirmed as accurate")
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    changed = _changed(repo, args.base)
    if not changed:
        print("doc-freshness: no changed files detected; nothing to check.")
        return 0

    changed_set = set(changed)
    acked = set(args.ack)
    drift: list[str] = []
    for area, docs in AREA_OWNERS.items():
        if area in acked:
            continue
        area_changed = any(c.startswith(area) or c == area for c in changed)
        if not area_changed:
            continue
        doc_touched = any(d in changed_set for d in docs)
        if not doc_touched:
            drift.append(
                f"{area}: code changed but owning doc(s) not updated -> {', '.join(docs)}"
            )

    if not drift:
        print("doc-freshness: ok — every changed area's owning doc was updated (or acked).")
        return 0

    sys.stderr.write("doc-freshness: possible STALE docs (code changed, doc did not):\n")
    for d in drift:
        sys.stderr.write("  " + d + "\n")
    sys.stderr.write(
        "\nUpdate the owning doc, or re-run with --ack <area> once you confirm it's accurate.\n"
    )
    if os.environ.get("DOC_FRESHNESS_BLOCK") == "1":
        return 1
    sys.stderr.write("doc-freshness: WARN-only (set DOC_FRESHNESS_BLOCK=1 to enforce).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
