"""Runner for podcast-rag-index — atomic SAI skill.

Adds transcript Markdown files (with YAML frontmatter) to an existing ChromaDB
RAG index by delegating to a `rag-index-updater`-style `index.py` that the
operator already runs. This skill is pure *mechanism*: it scopes a scan to one
directory, hash-diffs against a manifest so unchanged files are skipped, and
shells out to the operator's indexer. It bakes in NO operator-specific paths or
collection names — those are values the caller supplies (directly or via a
`config_path` YAML on the operator's private side).

Public API
----------
- locate_indexer_handler  → resolve the indexer scripts dir (input/config)
- scan_inputs_handler     → build to_index file list from input_dir
- run_indexer_handler     → shell out to index.py
- run(inputs)             → cascade entry
- main()                  → CLI

Cascade inputs
--------------
  input_dir:        str (required). Directory of .md transcripts to index.
  force:            bool (optional). Re-index even if hash unchanged.
  indexer_scripts:  str (optional*). Path to the indexer scripts dir
                    (the dir containing index.py). *Required unless supplied
                    by config_path. Fail-closed if unresolved.
  manifest_path:    str (optional). Manifest JSON used for hash-diff resume.
                    If absent, every .md is (re)indexed.
  config_path:      str (optional). YAML on the operator's private side that
                    supplies defaults for indexer_scripts / manifest_path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


WORKFLOW_ID = "podcast-rag-index"


def _expand(p: str | None) -> Path | None:
    return Path(os.path.expanduser(p)) if p else None


def _resolve_config(state: dict) -> None:
    """If config_path is given, fill any unset input from the YAML. The loader
    is the mechanism; the YAML's values stay on the operator's private side."""
    cfg_path = _expand(state.get("config_path"))
    if not cfg_path or not cfg_path.exists():
        return
    try:
        import yaml
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:
        return
    rag = cfg.get("rag_index", cfg) if isinstance(cfg, dict) else {}
    for key in ("indexer_scripts", "manifest_path"):
        if not state.get(key) and rag.get(key):
            state[key] = rag[key]


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest(manifest_path: Path | None) -> dict[str, Any]:
    if not manifest_path or not manifest_path.exists():
        return {}
    try:
        return json.loads(manifest_path.read_text())
    except json.JSONDecodeError:
        return {}


def _existing_hash(manifest_entry: Any) -> str | None:
    if manifest_entry is None:
        return None
    if isinstance(manifest_entry, str):
        return manifest_entry
    if isinstance(manifest_entry, dict):
        return manifest_entry.get("hash")
    return None


# ─── handlers ─────────────────────────────────────────────────────────

def locate_indexer_handler(state: dict) -> dict:
    explicit = _expand(state.get("indexer_scripts"))
    if not explicit:
        raise FileNotFoundError(
            "indexer_scripts not set; pass it in inputs or via config_path"
        )
    if explicit.exists() and (explicit / "index.py").exists():
        state["_indexer_dir"] = explicit
        return {"indexer_scripts": str(explicit)}
    raise FileNotFoundError(
        f"index.py not found under indexer_scripts: {explicit}"
    )


def scan_inputs_handler(state: dict) -> dict:
    input_dir = Path(os.path.expanduser(state["input_dir"]))
    if not input_dir.exists():
        raise FileNotFoundError(f"input_dir_missing: {input_dir}")
    force = bool(state.get("force"))
    manifest = _load_manifest(_expand(state.get("manifest_path")))
    to_index: list[dict] = []
    skipped: list[str] = []
    for p in sorted(input_dir.rglob("*.md")):
        h = _file_hash(p)
        prev = _existing_hash(manifest.get(str(p)))
        if not force and prev == h:
            skipped.append(p.name)
            continue
        status = "new" if prev is None else "changed"
        to_index.append({
            "path": str(p),
            "type": "md",
            "hash": h,
            "status": status,
            "name": p.name,
        })
    scan_path = Path("/tmp") / f"podcast_rag_scan_{int(datetime.utcnow().timestamp())}.json"
    scan_path.write_text(json.dumps(to_index, indent=2))
    state["_scan_path"] = scan_path
    state["_to_index_count"] = len(to_index)
    state["_skipped_count"] = len(skipped)
    return {
        "to_index": len(to_index),
        "skipped_unchanged": len(skipped),
        "scan_file": str(scan_path),
    }


def run_indexer_handler(state: dict) -> dict:
    if state.get("_to_index_count", 0) == 0:
        return {"indexed": 0, "stdout": "nothing to index"}
    indexer_dir: Path = state["_indexer_dir"]
    scan_path: Path = state["_scan_path"]
    cmd = [sys.executable, str(indexer_dir / "index.py"), "--files", str(scan_path)]
    if state.get("force"):
        cmd.append("--force")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"index_py_failed: {proc.stderr[-500:]}")
    return {
        "indexed": state["_to_index_count"],
        "stdout_tail": proc.stdout[-1000:],
    }


# ─── orchestrator ─────────────────────────────────────────────────────

def run(inputs: dict) -> dict:
    state = dict(inputs)
    _resolve_config(state)
    audit: list[dict] = []
    for name, fn in [
        ("locate_indexer", locate_indexer_handler),
        ("scan_inputs", scan_inputs_handler),
        ("run_indexer", run_indexer_handler),
    ]:
        try:
            out = fn(state)
            audit.append({"tier": name, "status": "ok", "out": out})
        except Exception as exc:
            audit.append({"tier": name, "status": "error", "error": str(exc)})
            return {
                "final_verdict": "escalate",
                "reason": f"{name}_failed",
                "audit": audit,
            }
    return {
        "final_verdict": "ready_to_propose",
        "audit": audit,
        "indexed_count": state.get("_to_index_count", 0),
        "skipped_count": state.get("_skipped_count", 0),
    }


def main() -> None:
    p = argparse.ArgumentParser(prog="podcast-rag-index")
    p.add_argument("subcmd", choices=["run"])
    p.add_argument("--inputs-json", required=True)
    args = p.parse_args()
    inputs = json.loads(args.inputs_json)
    result = run(inputs)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("final_verdict") == "ready_to_propose" else 1)


if __name__ == "__main__":
    main()
