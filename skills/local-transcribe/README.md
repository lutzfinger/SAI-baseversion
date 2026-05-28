# local-transcribe

Atomic SAI skill. Fully-local audio transcription via Whisper. No audio
leaves the machine; no API keys required.

## What it does

1. `pick_backend` — try mlx-whisper (Metal on Apple Silicon); fall back to
   faster-whisper (CPU). Both run the same Whisper models locally.
2. `collect_inputs` — accepts any of:
   - `manifest_path` (a podcast-download manifest.json — preferred; carries
     episode title/date/URL into the transcript frontmatter)
   - `audio_path` (single file)
   - `audio_dir` (recursive scan for .mp3/.m4a/.wav/etc.)
3. `transcribe_each` — Whisper-decode every input. Write
   `<YYYY-MM-DD>-<slug>.md` with YAML frontmatter into `output_dir`.
   Resume-safe (existing non-trivial transcripts are skipped).
4. `summarize` — counts and final paths.

## Output frontmatter (compatible with rag-index-updater)

```yaml
---
title: "..."
date: "YYYY-MM-DD"
url: "https://..."        # original audio URL
source: "Podcast"         # configurable via source_label
show_title: "..."
show_url: "..."
episode_id: "..."
language: "en"
transcribed_by: "mlx-whisper"
transcribed_at: "2026-05-25T19:00:00Z"
audio_source: "/abs/path/to/file.mp3"
---

<transcript body>
```

## CLI

Transcribe everything in a podcast-download manifest:

```bash
.venv/bin/python3.12 \
  skills/local-transcribe/runner.py run \
  --inputs-json '{
    "manifest_path": "~/Media/podcasts/<show-slug>/manifest.json",
    "output_dir":    "~/Media/transcripts/<show-slug>",
    "source_label":  "Podcast"
  }'
```

Transcribe one file:

```bash
.venv/bin/python3.12 \
  skills/local-transcribe/runner.py run \
  --inputs-json '{
    "audio_path": "~/Downloads/talk.mp3",
    "output_dir": "/tmp/transcripts"
  }'
```

## Why local?

- Privacy: Lutz's audio (interviews, drafts) never touches a remote API.
- Cost: zero per-minute fee; only electricity.
- Latency: Metal-accelerated on M-series; ~5–10× realtime for large-v3.
- Reliability: works offline, no rate limits.

## Composes with

- Upstream: `SAI-podcast-download` (manifest.json → audio_path list)
- Downstream: `SAI-podcast-rag-index` (transcripts → ChromaDB)
