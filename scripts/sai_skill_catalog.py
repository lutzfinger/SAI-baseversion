"""Generate a markdown catalog of installed SAI skills.

Output is paste-ready into the cowork_skill_creator_prompt at the
"## Existing SAI skills (operator catalog)" anchor — Co-Work then
KNOWS what skills exist instead of having to discover them.

Usage:
    python -m scripts.sai_skill_catalog
    python -m scripts.sai_skill_catalog --skills-dir $SAI_PRIVATE/skills
    python -m scripts.sai_skill_catalog --paste-into docs/cowork_skill_creator_prompt.md
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import yaml


def _default_skills_dir() -> Path:
    """Default skill location.

    Honours ``$SAI_PRIVATE`` if set (operator's private overlay
    path), otherwise falls back to a vendor-neutral
    ``~/.sai/skills`` placeholder. Operator overrides via
    ``--skills-dir``.
    """
    private = os.environ.get("SAI_PRIVATE")
    if private:
        return Path(private) / "skills"
    return Path.home() / ".sai" / "skills"


DEFAULT_SKILLS_DIR = _default_skills_dir()
ANCHOR_BEGIN = "<!-- SAI_SKILL_CATALOG_BEGIN -->"
ANCHOR_END = "<!-- SAI_SKILL_CATALOG_END -->"


def _safe_load(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def render_catalog(skills_dir: Path) -> str:
    if not skills_dir.exists():
        return f"_No skills directory at `{skills_dir}` — install SAI first._\n"
    rows: list[str] = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        if skill_dir.name == "incoming":
            continue
        if ".bak" in skill_dir.name:
            continue
        manifest_path = skill_dir / "skill.yaml"
        if not manifest_path.exists():
            continue
        manifest = _safe_load(manifest_path)
        identity = manifest.get("identity", {})
        wf_id = identity.get("workflow_id", skill_dir.name)
        version = identity.get("version", "?")
        description = (identity.get("description") or "(no description)").strip()
        # Cascade summary
        cascade = manifest.get("cascade", []) or []
        tier_summary = " → ".join(
            f"{(t.get('tier_id') or '?')}({t.get('kind') or '?'})"
            for t in cascade
        ) or "(empty)"
        # Trigger
        trigger_kind = (manifest.get("trigger") or {}).get("kind", "?")
        # Outputs
        outputs = manifest.get("outputs", []) or []
        output_names = ", ".join(o.get("name", "?") for o in outputs) or "(none)"
        # Eval count
        eval_kinds = []
        eval_block = manifest.get("eval", {})
        for ds in (eval_block.get("datasets") or []):
            eval_kinds.append(ds.get("kind", "?"))
        for k in ("canaries", "edge_cases", "workflow_regression"):
            if k in eval_block:
                eval_kinds.append(k)
        eval_kinds_str = ", ".join(eval_kinds) or "(none)"

        try:
            display_path = skill_dir.relative_to(Path.home())
        except ValueError:
            display_path = skill_dir  # outside $HOME — show absolute path
        rows.append(
            f"### `{wf_id}` (v{version})\n\n"
            f"{description}\n\n"
            f"- **Trigger:** `{trigger_kind}`\n"
            f"- **Cascade:** {tier_summary}\n"
            f"- **Outputs:** {output_names}\n"
            f"- **Eval:** {eval_kinds_str}\n"
            f"- **Path:** `{display_path}` (private overlay)\n"
        )
    if not rows:
        return "_No installed skills found._\n"
    return "\n\n".join(rows) + "\n"


def paste_into(prompt_path: Path, catalog: str) -> bool:
    """Replace the SAI_SKILL_CATALOG anchor block with the new catalog.

    Returns True if the file was modified.
    """
    text = prompt_path.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(ANCHOR_BEGIN) + r".*?" + re.escape(ANCHOR_END),
        re.DOTALL,
    )
    replacement = f"{ANCHOR_BEGIN}\n\n{catalog}\n{ANCHOR_END}"
    new_text, n = pattern.subn(replacement, text)
    if n == 0:
        sys.stderr.write(
            f"Anchors not found in {prompt_path}. Add this block where you "
            f"want the catalog inserted:\n\n{ANCHOR_BEGIN}\n{ANCHOR_END}\n"
        )
        return False
    if new_text == text:
        return False
    prompt_path.write_text(new_text, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a markdown catalog of installed SAI skills.",
    )
    parser.add_argument(
        "--skills-dir", type=Path, default=DEFAULT_SKILLS_DIR,
    )
    parser.add_argument(
        "--paste-into", type=Path, default=None,
        help="Optional path to a markdown file with SAI_SKILL_CATALOG "
             "anchor markers; the catalog block is replaced in-place.",
    )
    args = parser.parse_args(argv)
    catalog = render_catalog(args.skills_dir)
    if args.paste_into:
        changed = paste_into(args.paste_into, catalog)
        print(f"{'updated' if changed else 'no change'}: {args.paste_into}")
    else:
        print(catalog)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
