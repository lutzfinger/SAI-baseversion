"""Runner for local-transcribe — atomic SAI skill.

Transcribes one or many audio files using a fully-local Whisper model. Defaults
to mlx-whisper (Metal-accelerated on Apple Silicon); falls back to faster-whisper
on other platforms. Writes one Markdown file per input with YAML frontmatter
compatible with Lutz's existing ChromaDB RAG index pipeline.

No audio leaves the machine. No API keys required.

Public API
----------
- pick_backend_handler        → choose mlx-whisper / faster-whisper / fail
- collect_inputs_handler      → expand a manifest / directory into audio paths
- transcribe_each_handler     → run Whisper, write .md with YAML frontmatter
- summarize_handler           → emit run summary
- run(inputs)                 → CLI / cascade entry
- main()                      → CLI

Cascade inputs
--------------
  audio_path:      str (optional). Single audio file.
  audio_dir:       str (optional). Directory; transcribes every audio file.
  manifest_path:   str (optional). podcast-download manifest.json.
                   When given, episode metadata is injected into frontmatter.
  output_dir:      str (required). Where .md transcripts land.
  model:           str (optional). mlx-community/whisper-large-v3-mlx by default.
  language:        str (optional). Default "en".
  source_label:    str (optional). Goes into YAML `source:` field (e.g. "Podcast").
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


WORKFLOW_ID = "local-transcribe"
AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".wav", ".ogg", ".aac", ".flac", ".webm"}

DEFAULT_MLX_MODEL = "mlx-community/whisper-large-v3-mlx"
DEFAULT_FASTER_MODEL = "large-v3"


# ─── helpers ──────────────────────────────────────────────────────────

def _slugify(text: str, max_len: int = 80) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:max_len].strip("-") or "item"


def _yaml_escape(value: Any) -> str:
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _collapse_whisper_repeats(text: str, max_repeats: int = 3) -> str:
    """Whisper sometimes hallucinates long runs of the same short token on
    near-silent stretches ("Yeah. Yeah. Yeah." x 50). Collapse any run of
    the same word repeated >max_repeats times down to max_repeats."""
    import re as _re
    return _re.sub(
        r"(\b\w{1,8}\b[.!?,]?)(?:\s+\1){%d,}" % max_repeats,
        lambda m: " ".join([m.group(1)] * max_repeats),
        text,
    )


def _write_transcript_md(
    dest: Path,
    text: str,
    frontmatter: dict[str, Any],
) -> None:
    lines = ["---"]
    for k, v in frontmatter.items():
        if v in (None, ""):
            continue
        lines.append(f"{k}: {_yaml_escape(v)}")
    lines.append("---")
    lines.append("")
    lines.append(text.strip())
    lines.append("")
    dest.write_text("\n".join(lines))


# ─── backend selection ────────────────────────────────────────────────

class _MlxBackend:
    name = "mlx-whisper"

    def __init__(self, model: str) -> None:
        import mlx_whisper  # noqa: F401
        self.model = model

    def transcribe(self, audio_path: str, language: str) -> str:
        import mlx_whisper
        result = mlx_whisper.transcribe(
            audio_path,
            path_or_hf_repo=self.model,
            language=language or None,
        )
        return result.get("text", "").strip()


class _FasterBackend:
    name = "faster-whisper"

    def __init__(self, model: str) -> None:
        from faster_whisper import WhisperModel  # noqa: F401
        self.model = WhisperModel(model, device="cpu", compute_type="int8")

    def transcribe(self, audio_path: str, language: str) -> str:
        segments, _ = self.model.transcribe(audio_path, language=language or None)
        return " ".join(s.text.strip() for s in segments).strip()


def _build_backend(model_hint: Optional[str]) -> Any:
    try:
        return _MlxBackend(model_hint or DEFAULT_MLX_MODEL)
    except ImportError:
        pass
    return _FasterBackend(model_hint or DEFAULT_FASTER_MODEL)


# ─── handlers ─────────────────────────────────────────────────────────

def pick_backend_handler(state: dict) -> dict:
    backend = _build_backend(state.get("model"))
    state["_backend"] = backend
    return {"backend": backend.name}


def collect_inputs_handler(state: dict) -> dict:
    items: list[dict] = []
    seen: set[str] = set()

    mpath = state.get("manifest_path")
    if mpath:
        manifest = json.loads(Path(os.path.expanduser(mpath)).read_text())
        for ep in manifest.get("episodes", []):
            ap = ep.get("audio_path")
            if not ap or not Path(ap).exists():
                continue
            if ap in seen:
                continue
            seen.add(ap)
            items.append({
                "audio_path": ap,
                "title": ep.get("title", Path(ap).stem),
                "date": ep.get("pub_date", ""),
                "url": ep.get("audio_url", ""),
                "show_title": ep.get("show_title", ""),
                "show_url": ep.get("show_url", ""),
                "description": ep.get("description", ""),
                "episode_id": ep.get("episode_id", ""),
            })

    single = state.get("audio_path")
    if single:
        ap = os.path.expanduser(single)
        if ap not in seen and Path(ap).exists():
            items.append({"audio_path": ap, "title": Path(ap).stem})
            seen.add(ap)

    adir = state.get("audio_dir")
    if adir:
        for p in sorted(Path(os.path.expanduser(adir)).rglob("*")):
            if p.suffix.lower() in AUDIO_EXTS and str(p) not in seen:
                items.append({"audio_path": str(p), "title": p.stem})
                seen.add(str(p))

    if not items:
        raise ValueError("no_audio_inputs: provide manifest_path, audio_path, or audio_dir")
    state["items"] = items
    return {"item_count": len(items)}


def transcribe_each_handler(state: dict) -> dict:
    out_dir = Path(os.path.expanduser(state["output_dir"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    backend = state["_backend"]
    language = state.get("language") or "en"
    source_label = state.get("source_label") or "Podcast"

    transcribed, skipped, failed = [], [], []
    for item in state["items"]:
        ap = item["audio_path"]
        date = (item.get("date") or "")[:10]
        prefix = f"{date}-" if date else ""
        out_name = f"{prefix}{_slugify(item['title'])}.md"
        dest = out_dir / out_name
        if dest.exists() and dest.stat().st_size > 200:
            skipped.append(out_name)
            item["transcript_path"] = str(dest)
            continue
        t0 = time.time()
        try:
            text = backend.transcribe(ap, language)
            if not text.strip():
                raise RuntimeError("empty_transcript")
            text = _collapse_whisper_repeats(text)
            frontmatter = {
                "title": item.get("title", Path(ap).stem),
                "date": date,
                "url": item.get("url", ""),
                "source": source_label,
                "show_title": item.get("show_title", ""),
                "show_url": item.get("show_url", ""),
                "episode_id": item.get("episode_id", ""),
                "language": language,
                "transcribed_by": backend.name,
                "transcribed_at": datetime.utcnow().isoformat() + "Z",
                "audio_source": ap,
            }
            _write_transcript_md(dest, text, frontmatter)
            dur = round(time.time() - t0, 1)
            transcribed.append({"name": out_name, "chars": len(text), "secs": dur})
            item["transcript_path"] = str(dest)
        except Exception as exc:
            failed.append({"name": out_name, "audio": ap, "error": str(exc)})
            item["transcript_path"] = None

    state["transcribe_results"] = {
        "transcribed": transcribed, "skipped": skipped, "failed": failed,
    }
    return state["transcribe_results"]


def summarize_handler(state: dict) -> dict:
    r = state["transcribe_results"]
    return {
        "transcribed_count": len(r["transcribed"]),
        "skipped_count": len(r["skipped"]),
        "failed_count": len(r["failed"]),
        "output_dir": state["output_dir"],
    }


# ─── orchestrator ─────────────────────────────────────────────────────

def run(inputs: dict) -> dict:
    state = dict(inputs)
    audit: list[dict] = []
    for name, fn in [
        ("pick_backend", pick_backend_handler),
        ("collect_inputs", collect_inputs_handler),
        ("transcribe_each", transcribe_each_handler),
        ("summarize", summarize_handler),
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
        "items": [{"audio_path": i["audio_path"],
                    "transcript_path": i.get("transcript_path")}
                   for i in state["items"]],
    }


def main() -> None:
    p = argparse.ArgumentParser(prog="local-transcribe")
    p.add_argument("subcmd", choices=["run"])
    p.add_argument("--inputs-json", required=True)
    args = p.parse_args()
    inputs = json.loads(args.inputs_json)
    result = run(inputs)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("final_verdict") == "ready_to_propose" else 1)


if __name__ == "__main__":
    main()
