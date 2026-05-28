---
name: SAI-local-transcribe
description: Atomic SAI skill. Fully-local audio transcription via Whisper (mlx-whisper on Apple Silicon, faster-whisper fallback elsewhere). No audio leaves the machine; no API keys. Accepts a single audio file, a directory, or a podcast-download manifest.json. Writes one .md per input with YAML frontmatter compatible with a rag-index-updater pipeline. Use whenever you need to transcribe audio, run whisper on a file, transcribe all episodes in a folder, or any workflow needs text from audio.
---

# Trigger manifest — local-transcribe (atomic, autonomous)

Code: `skills/local-transcribe/`
Manifest: `skill.yaml` (4-tier cascade)
Entry point: `runner.py` → `run(inputs)`

## Cascade plan

```
1. pick_backend     (rules) → mlx-whisper (preferred) or faster-whisper
2. collect_inputs   (rules) → manifest_path | audio_path | audio_dir
3. transcribe_each  (rules) → Whisper → .md with YAML frontmatter
4. summarize        (rules) → counts; ready_to_propose
```

All tiers deterministic. No LLM calls. No network (after the model is cached).

## Invocation — chained off podcast-download

```bash
.venv/bin/python3.12 \
  skills/local-transcribe/runner.py run \
  --inputs-json '{
    "manifest_path": "~/Media/podcasts/<show-slug>/manifest.json",
    "output_dir":    "~/Media/transcripts/<show-slug>",
    "source_label":  "Podcast"
  }'
```

## Invocation — single file

```bash
.venv/bin/python3.12 \
  skills/local-transcribe/runner.py run \
  --inputs-json '{
    "audio_path":  "~/Downloads/interview.mp3",
    "output_dir":  "/tmp/transcripts"
  }'
```

Invoke runners by absolute path (folder names use hyphens, so `python -m` won't import).

## Frontmatter shape (rag-ready)

```yaml
title:            "Episode Title"
date:             "YYYY-MM-DD"
url:              "https://..."
source:           "Podcast"        # configurable via source_label
show_title:       "..."
episode_id:       "..."
language:         "en"
transcribed_by:   "mlx-whisper"
transcribed_at:   "..."
audio_source:     "/abs/path"
```

## Composes with

- Upstream: `SAI-podcast-download`
- Downstream: `SAI-podcast-rag-index`
