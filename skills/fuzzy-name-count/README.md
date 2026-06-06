# fuzzy-name-count

Atomic SAI skill. Count fuzzy first-name mentions of a person list inside a directory of transcripts.

## Input contract

- **Transcripts directory** — one `*.json` file per meeting in the SAI-standard shape (see `granola-fetch`). The `transcript` field may be a Granola string (any supported format) or a list of `{speaker, text}` segments.
- **Names file** — text file with one full name per line. Parenthetical or quoted alias notation supported: `Elizabeth (Liz) Chen`, `Michael "Mike" Davis`, `Dr. Robert (Bob) Smith`, `Johnson, Sarah`.

## Output contract

CSV at the requested path:

```
Name, <start_date_1>, <start_date_2>, …, Total
Sarah Smith, 7, 0, …, 23
```

## Algorithm — speaker filtering

By default, count only the **target speaker** (typically the professor / host / interviewer). Identified by:

1. Speaker label matching one of `[lutz, finger, professor, instructor]` (or the `lutz_hints` arg).
2. Fallback: the speaker with the most words spoken (Granola anonymizes classroom transcripts as `Speaker A/B/C`).

Pass `--any-speaker` to count every mention regardless of who said it.

## Algorithm — length-aware threshold

Fuzzy matching with [`rapidfuzz`](https://github.com/maxbachmann/RapidFuzz). Threshold adapts to the alias length, to block common-word collisions:

| Alias length | Effective threshold | Example outcome |
|---|---|---|
| ≤ 3 chars | exact only (101) | `Ana` will not match `Anna` or `and`. |
| 4–5 chars | `max(threshold, 90)` | `Samer` (5) will not match `same` (ratio 89). |
| ≥ 6 chars | base `threshold` (default 85) | `Saraa` (typo) matches `Sarah` (ratio 90). |

## Invocation

```bash
python3 -m skills.fuzzy-name-count.runner count \
    --transcripts <dir>  \
    --names-file <txt>  \
    --output-csv <out.csv> \
    [--threshold 85] [--any-speaker]
```

For composed workflows, import the underlying library directly:

```python
from app.shared.fuzzy_match import load_transcripts, name_aliases, count_callouts
```
