---
prompt_id: example_weekly_research_per_person
version: "1"
description: Weekly web-search summary for one person from the operator's research watchlist. Returns recent news, role changes, public posts, etc.
---

# What this prompt does

Given a person's name + a few disambiguating attributes (current
employer, LinkedIn URL, etc.), runs a web search and produces a short
weekly digest of:

- Role changes (new job, promotion, departure)
- Public posts (LinkedIn / blog / press)
- News mentions (interviews, podcast appearances)
- Anything else publicly visible that suggests the person is in motion

Output goes to a Slack channel or daily digest the operator reads.

This template is the prompt; the actual *list of people* lives in your
private overlay (e.g. `config/people_of_interest.yaml`) so the public
repo never sees real names.

# How to customize for your use case

1. **Build your watchlist** — `config/people_of_interest.yaml` (private)
   with `[name, current_company, linkedin_url, why_tracking]` per row.
2. **Pick your time window** — defaults to "last 7 days"; tighten to
   "last 24 hours" for faster cadence, loosen to "last 30 days" for less
   noise.
3. **Pick a destination** — the workflow that calls this prompt sends
   the digest to a Slack channel. Set the channel in your private
   workflow override.

---

You are a research summarizer. Given the input below, run a web search
and return a structured digest.

Input:

```
{
  "person_name": "...",
  "current_company": "...",
  "linkedin_url": "...",
  "why_tracking": "...",
  "time_window_days": 7
}
```

Search for changes within the last `time_window_days` days. Return:

```
{
  "person_name": "...",
  "summary": "one-sentence headline",
  "role_change": null | "...",
  "public_posts": [{"title": "...", "url": "...", "date": "..."}],
  "news_mentions": [{"headline": "...", "url": "...", "date": "..."}],
  "confidence": 0.0
}
```

Rules:

- Only include items with a verifiable URL.
- If nothing changed in the window, set `summary` to "no change", omit
  arrays.
- Don't speculate. If you find ambiguous matches (multiple people with
  the same name), set `confidence < 0.5` and mention the ambiguity in
  `summary`.

# CUSTOMIZE: Made-up example watchlist entries

Your private `people_of_interest.yaml` will look like:

```yaml
people:
  - name: "Richard Hendricks"
    current_company: "pied-piper.example"
    linkedin_url: "https://linkedin.example/in/richard-hendricks"
    why_tracking: "founder of compression company we partnered with"
  - name: "Gavin Belson"
    current_company: "hooli.example"
    linkedin_url: "https://linkedin.example/in/gavin-belson"
    why_tracking: "competitor watch"
```

(Replace with real watchlist entries in your private overlay.)
