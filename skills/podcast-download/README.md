# podcast-download

Atomic SAI skill. Download podcast episode audio from an RSS feed (or a
Spotify show URL — resolved to a real RSS feed via the iTunes Search API,
since Spotify itself blocks direct downloads).

## What it does

Given `(feed_url | spotify_show_url, output_dir [, since_date, max_episodes])`:

1. `resolve_feed` — if a Spotify URL was supplied, query iTunes Search by
   the show's public title and pick the matching `feedUrl`.
2. `list_episodes` — parse the RSS, extract every `<enclosure>` audio URL,
   filter by `since_date`, cap by `max_episodes` (most recent first).
3. `download_each` — fetch each missing file with retries; resume-safe
   (existing non-zero files are skipped). Files are named
   `<YYYY-MM-DD>-<slug>-<episode_id>.mp3`.
4. `write_manifest` — drop `manifest.json` into `output_dir` capturing
   feed_url, every episode's metadata, and download results.

Output JSON shape every downstream skill can rely on:

```json
{
  "episode_id":   "<sha1-hex>",
  "title":        "...",
  "pub_date":     "YYYY-MM-DD",
  "audio_path":   "/abs/path/to/file.mp3",
  "audio_url":    "https://...",
  "description":  "...",
  "show_title":   "...",
  "show_url":     "..."
}
```

## CLI

```bash
.venv/bin/python3.12 \
  skills/podcast-download/runner.py run \
  --inputs-json '{
    "spotify_show_url": "https://open.spotify.com/show/<spotify-show-id>",
    "output_dir":       "~/Media/podcasts/<show-slug>",
    "max_episodes":     0
  }'
```

## Composed workflows

- `podcast → local-transcribe → podcast-rag-index` — full ingest pipeline
  that adds podcasts to Lutz's RAG knowledge base.

## Why no Spotify API?

Spotify's API doesn't expose podcast audio (DRM-protected on their CDN).
Every public podcast on Spotify is also on Apple Podcasts via the show's
real RSS feed. iTunes Search is a stable, key-free way to resolve the
Spotify show ID → that RSS feed.
