---
name: SAI-podcast-download
description: Atomic SAI skill. Download podcast episode audio from an RSS feed or a Spotify show URL (resolved to RSS via the iTunes Search API — Spotify itself blocks audio downloads). Resume-safe, retry-aware. Emits a manifest.json that downstream skills (local-transcribe, podcast-rag-index) consume. Use whenever you need to download a podcast, grab episodes of a show, pull audio for a Spotify show URL, or any workflow needs raw podcast MP3s as input.
---

# Trigger manifest — podcast-download (atomic, autonomous)

Code: `skills/podcast-download/`
Manifest: `skill.yaml` (4-tier cascade)
Entry point: `runner.py` → `run(inputs)`

## Cascade plan

```
1. resolve_feed       (rules) → Spotify URL → RSS via iTunes Search API
2. list_episodes      (rules) → parse RSS, filter by since_date / max_episodes
3. download_each      (rules) → fetch missing audio (retries, resume-safe)
4. write_manifest     (rules) → emit manifest.json; ready_to_propose
```

All tiers are deterministic Python. No LLM calls. No external auth required.

## Invocation

```bash
.venv/bin/python3.12 \
  skills/podcast-download/runner.py run \
  --inputs-json '{
    "spotify_show_url": "https://open.spotify.com/show/<spotify-show-id>",
    "output_dir":       "~/Media/podcasts/<show-slug>",
    "since_date":       "2020-01-01",
    "max_episodes":     0
  }'
```

Either `spotify_show_url` or `feed_url` is required. `max_episodes: 0` means all.
Invoke runners by absolute path (folder names use hyphens, so `python -m` won't import).

## Standard manifest shape

```json
{
  "workflow_id": "podcast-download",
  "feed_url":    "https://.../feed.xml",
  "episodes": [
    {
      "episode_id":  "<sha1-hex>",
      "title":       "...",
      "pub_date":    "YYYY-MM-DD",
      "audio_path":  "/abs/path/to/file.mp3",
      "audio_url":   "https://...",
      "show_title":  "..."
    }
  ],
  "download_results": {"downloaded": [...], "skipped": [...], "failed": [...]}
}
```

## Composes with

- `SAI-local-transcribe` — feeds `manifest.episodes[].audio_path` into Whisper
- `SAI-podcast-rag-index` — writes transcripts into the ChromaDB index
