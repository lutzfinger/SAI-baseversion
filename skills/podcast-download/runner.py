"""Runner for podcast-download — atomic SAI skill.

Takes a podcast feed URL (RSS/Atom) **or** a Spotify show URL, resolves it to
an iTunes/RSS feed (Spotify itself doesn't expose audio downloads), enumerates
episodes, and downloads any not yet on disk. Outputs a manifest JSON describing
every episode (downloaded + skipped).

Public API
----------
- resolve_feed_handler         → if input is a Spotify URL, find RSS via iTunes
- list_episodes_handler        → parse feed → episode list (filtered by date/cap)
- download_each_handler        → fetch missing audio files (resume-safe)
- write_manifest_handler       → write manifest.json with full episode metadata
- run(inputs)                  → CLI / cascade entry
- main()                       → CLI

Cascade inputs
--------------
  feed_url:         str (optional if spotify_show_url given). RSS/Atom URL.
  spotify_show_url: str (optional). Resolved via iTunes Search API.
  output_dir:       str (required). Where audio files + manifest land.
  since_date:       str (optional). "YYYY-MM-DD"; episodes before are skipped.
  max_episodes:     int (optional). 0 = unlimited.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import feedparser
import requests


WORKFLOW_ID = "podcast-download"
USER_AGENT = "SAI-podcast-download/0.1.0"


# ─── helpers ──────────────────────────────────────────────────────────

def _slugify(text: str, max_len: int = 80) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:max_len].strip("-") or "episode"


def _extract_show_id(spotify_url: str) -> Optional[str]:
    m = re.search(r"spotify\.com/show/([A-Za-z0-9]+)", spotify_url)
    return m.group(1) if m else None


def _itunes_lookup_by_spotify(spotify_url: str) -> Optional[str]:
    """Spotify doesn't expose RSS. We query iTunes Search by show name —
    iTunes returns the canonical podcast RSS feed URL (the same one Apple
    Podcasts uses). Two-step: scrape the public Spotify page title, then
    iTunes search by that title."""
    headers = {"User-Agent": USER_AGENT}
    page = requests.get(spotify_url, headers=headers, timeout=15)
    page.raise_for_status()
    m = re.search(r'<title>([^<|]+?)\s*\|', page.text)
    if not m:
        m = re.search(r'<meta property="og:title" content="([^"]+)"', page.text)
    if not m:
        return None
    name = m.group(1).strip()
    r = requests.get(
        "https://itunes.apple.com/search",
        params={"term": name, "entity": "podcast", "limit": 10},
        headers=headers, timeout=15,
    )
    r.raise_for_status()
    for result in r.json().get("results", []):
        if result.get("collectionName", "").strip().lower() == name.lower():
            return result.get("feedUrl")
    results = r.json().get("results", [])
    return results[0].get("feedUrl") if results else None


def _parse_pub_date(entry: Any) -> Optional[str]:
    raw = entry.get("published") or entry.get("updated")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        try:
            return datetime.fromisoformat(raw).strftime("%Y-%m-%d")
        except Exception:
            return None


def _enclosure_url(entry: Any) -> Optional[str]:
    for enc in entry.get("enclosures", []) or []:
        if enc.get("href"):
            return enc["href"]
    links = entry.get("links", []) or []
    for link in links:
        if link.get("rel") == "enclosure" and link.get("href"):
            return link["href"]
    return None


def _ext_from_url(url: str) -> str:
    path = urlparse(url).path
    for ext in (".mp3", ".m4a", ".mp4", ".wav", ".ogg", ".aac"):
        if path.lower().endswith(ext):
            return ext
    return ".mp3"


def _episode_id(entry: Any, fallback: str) -> str:
    raw = entry.get("id") or entry.get("guid") or fallback
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _download(url: str, dest: Path, max_retries: int = 3) -> None:
    headers = {"User-Agent": USER_AGENT}
    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            with requests.get(url, headers=headers, stream=True, timeout=60) as r:
                r.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            f.write(chunk)
                tmp.replace(dest)
            return
        except Exception as exc:
            last_err = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"download_failed_after_{max_retries}_retries: {last_err}")


# ─── handlers (each returns a small status dict) ──────────────────────

def resolve_feed_handler(state: dict) -> dict:
    feed_url = (state.get("feed_url") or "").strip()
    spotify_url = (state.get("spotify_show_url") or "").strip()
    if feed_url:
        return {"feed_url": feed_url, "resolution": "passthrough"}
    if not spotify_url:
        raise ValueError("missing_input: feed_url or spotify_show_url required")
    resolved = _itunes_lookup_by_spotify(spotify_url)
    if not resolved:
        raise RuntimeError(f"could_not_resolve_feed_from_spotify: {spotify_url}")
    state["feed_url"] = resolved
    return {"feed_url": resolved, "resolution": "spotify_to_itunes"}


def list_episodes_handler(state: dict) -> dict:
    feed_url = state["feed_url"]
    parsed = feedparser.parse(feed_url, agent=USER_AGENT)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"feed_parse_failed: {parsed.bozo_exception}")
    since = state.get("since_date")
    max_n = int(state.get("max_episodes") or 0)
    episodes = []
    for entry in parsed.entries:
        audio = _enclosure_url(entry)
        if not audio:
            continue
        pub = _parse_pub_date(entry)
        if since and pub and pub < since:
            continue
        title = (entry.get("title") or "untitled").strip()
        episodes.append({
            "episode_id": _episode_id(entry, audio),
            "title": title,
            "pub_date": pub or "",
            "audio_url": audio,
            "description": (entry.get("summary") or "").strip(),
            "source_entry_id": entry.get("id") or entry.get("guid") or "",
            "show_title": parsed.feed.get("title", ""),
            "show_url": parsed.feed.get("link", ""),
        })
    episodes.sort(key=lambda e: e["pub_date"] or "0", reverse=True)
    if max_n:
        episodes = episodes[:max_n]
    state["episodes"] = episodes
    return {"episode_count": len(episodes), "show_title": parsed.feed.get("title", "")}


def download_each_handler(state: dict) -> dict:
    out_dir = Path(os.path.expanduser(state["output_dir"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    downloaded, skipped, failed = [], [], []
    for ep in state["episodes"]:
        ext = _ext_from_url(ep["audio_url"])
        date_prefix = (ep["pub_date"] or "0000-00-00")
        fname = f"{date_prefix}-{_slugify(ep['title'])}-{ep['episode_id']}{ext}"
        dest = out_dir / fname
        ep["audio_path"] = str(dest)
        if dest.exists() and dest.stat().st_size > 0:
            skipped.append(ep["episode_id"])
            continue
        try:
            _download(ep["audio_url"], dest)
            downloaded.append(ep["episode_id"])
        except Exception as exc:
            failed.append({"episode_id": ep["episode_id"], "error": str(exc)})
            ep["audio_path"] = None
    state["download_results"] = {
        "downloaded": downloaded, "skipped": skipped, "failed": failed,
    }
    return state["download_results"]


def write_manifest_handler(state: dict) -> dict:
    out_dir = Path(os.path.expanduser(state["output_dir"]))
    manifest_path = out_dir / "manifest.json"
    payload = {
        "workflow_id": WORKFLOW_ID,
        "version": "0.1.0",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "feed_url": state.get("feed_url"),
        "spotify_show_url": state.get("spotify_show_url"),
        "episodes": state.get("episodes", []),
        "download_results": state.get("download_results", {}),
    }
    manifest_path.write_text(json.dumps(payload, indent=2))
    return {"manifest_path": str(manifest_path), "episodes": len(payload["episodes"])}


# ─── orchestrator ─────────────────────────────────────────────────────

def run(inputs: dict) -> dict:
    state = dict(inputs)
    audit: list[dict] = []
    for name, fn in [
        ("resolve_feed", resolve_feed_handler),
        ("list_episodes", list_episodes_handler),
        ("download_each", download_each_handler),
        ("write_manifest", write_manifest_handler),
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
        "manifest_path": str(Path(os.path.expanduser(state["output_dir"])) / "manifest.json"),
        "episodes": state["episodes"],
    }


def main() -> None:
    p = argparse.ArgumentParser(prog="podcast-download")
    p.add_argument("subcmd", choices=["run"])
    p.add_argument("--inputs-json", required=True)
    args = p.parse_args()
    inputs = json.loads(args.inputs_json)
    result = run(inputs)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("final_verdict") == "ready_to_propose" else 1)


if __name__ == "__main__":
    main()
