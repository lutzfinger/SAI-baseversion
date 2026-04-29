"""Generate docs/tool_overview.md from the runtime tool registry."""

from __future__ import annotations

import argparse

from app.shared.config import REPO_ROOT
from app.shared.tool_registry import render_tool_overview_markdown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit nonzero if docs/tool_overview.md is out of date.",
    )
    args = parser.parse_args()

    target = REPO_ROOT / "docs" / "tool_overview.md"
    rendered = render_tool_overview_markdown()
    if args.check:
        existing = target.read_text(encoding="utf-8")
        if existing != rendered:
            raise SystemExit("docs/tool_overview.md is out of date; run generate_tool_overview.py")
        return 0
    target.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
