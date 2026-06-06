"""SAI shared library: fuzzy name matching against transcripts.

Atomic primitives used by any SAI skill that needs to count first-name mentions
in conversation transcripts. Generic — does NOT know about Granola, Google Sheets,
or any specific workflow. Just text → counts.

Public API
----------
- name_aliases(full_name)               → ['First', 'Nickname'] (parenthetical/quoted aliases)
- first_name(full_name)                 → 'First'
- tokenize(text)                        → ['lowercased', 'tokens']
- parse_granola_transcript_string(s)    → [{speaker, text}, ...]   (Granola format)
- identify_top_speaker(segments)        → 'Speaker A'              (most-talked)
- count_callouts(transcript, aliases, threshold=85, any_speaker=False, lutz_hints=...)
                                        → [count_per_student]
- load_transcripts(transcripts_dir)     → list of {meeting_id, title, start_time, transcript}

Speaker filter logic in count_callouts:
  - any_speaker=True  → every turn counted
  - any_speaker=False → only the "professor" turn counted, identified by
      (1) speaker label matching `lutz_hints` (default: lutz/finger/professor/instructor)
      (2) fallback: the speaker with the most spoken words (Granola anonymises labels)

Short-name protection:
  - alias of length ≤3 → exact match only (blocks "Sam"→"ham", "Ana"→"and")
  - alias of length 4-5 → fuzzy threshold raised to ≥90 (blocks "Samer"→"same")
  - alias of length ≥6 → uses caller-supplied threshold (default 85)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    from rapidfuzz import fuzz
except ImportError:
    sys.exit("ERROR: rapidfuzz not installed. Run: pip3 install rapidfuzz")


DEFAULT_LUTZ_HINTS = ("lutz", "finger", "professor", "instructor")


def name_aliases(full_name: str) -> list[str]:
    """Return name tokens to fuzzy-match for this person.

    Includes the first name, plus any parenthetical/quoted aliases:
      'Elizabeth (Liz) Chen'      → ['Elizabeth', 'Liz']
      'Michael "Mike" Davis'       → ['Michael', 'Mike']
      'Dr. Robert (Bob) Smith'     → ['Robert', 'Bob']
      'Johnson, Sarah'             → ['Sarah']
    """
    name = (full_name or "").strip()
    if not name:
        return []
    aliases = re.findall(r"[\(\"']([^\)\"']+)[\)\"']", name)
    base = re.sub(r"[\(\"'][^\)\"']+[\)\"']", " ", name).strip()
    if "," in base:
        parts = [p.strip() for p in base.split(",", 1)]
        if len(parts) == 2 and parts[1]:
            base = parts[1]
    tokens = base.split()
    titles = {"mr.", "ms.", "mrs.", "dr.", "prof.", "mr", "ms", "mrs", "dr", "prof"}
    while tokens and tokens[0].lower() in titles:
        tokens.pop(0)
    primary = tokens[0] if tokens else ""
    result: list[str] = []
    if primary:
        result.append(primary)
    for a in aliases:
        a = a.strip().split()[0] if a.strip() else ""
        if a and a.lower() != primary.lower():
            result.append(a)
    return result


def first_name(full_name: str) -> str:
    aliases = name_aliases(full_name)
    return aliases[0] if aliases else ""


def tokenize(text: str) -> list[str]:
    """Split into lowercased alphabetic tokens."""
    return [t.lower() for t in re.findall(r"[A-Za-z'\-]+", text or "")]


_SPEAKER_LABEL = (
    # Classroom anonymized: 'Speaker A', 'Speaker B', ...
    r"Speaker [A-Z]\w*"
    # 1-on-1 Granola: 'Me', 'Them'
    r"|Me|Them"
    # Generic role labels seen in podcasts/interviews
    r"|Host|Guest|Interviewer|Interviewee|Moderator"
)
_SPEAKER_SPLIT = re.compile(rf"(?:^|\s)({_SPEAKER_LABEL}):\s+")


def parse_granola_transcript_string(s: str) -> list[dict]:
    """Parse a Granola transcript string into [{speaker, text}, ...].

    Handles all observed Granola export formats:
      - Classroom (anonymized): 'assemblyai: Speaker A: ... Speaker B: ...'
      - 1-on-1: ' Me: ... Them: ... Me: ...'
      - Podcast-style: ' Host: ... Guest: ...'
    """
    if not s:
        return []
    s = re.sub(r"^\s*assemblyai:\s*", "", s.strip())
    parts = _SPEAKER_SPLIT.split(s)
    # parts[0] = preamble before first speaker label (usually empty)
    # parts[1], parts[3], ... = speaker labels
    # parts[2], parts[4], ... = the text following each label
    out: list[dict] = []
    for i in range(1, len(parts), 2):
        speaker = parts[i].strip()
        text = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if text:
            out.append({"speaker": speaker, "text": text})
    return out


def _speaker_label(speaker) -> str:
    """Coerce any Granola speaker representation to a single string.

    Granola Personal API:
      {"speaker": {"source": "microphone", "diarization_label": "Speaker A"}, ...}
    Granola string format (legacy MCP):
      "Speaker A"
    """
    if speaker is None:
        return ""
    if isinstance(speaker, str):
        return speaker
    if isinstance(speaker, dict):
        return str(
            speaker.get("diarization_label")
            or speaker.get("name")
            or speaker.get("label")
            or ""
        )
    return str(speaker)


def is_named_speaker(speaker, hints: tuple[str, ...] = DEFAULT_LUTZ_HINTS) -> bool:
    """Does the speaker label contain one of the hint substrings (case-insensitive)?
    Accepts string or dict speaker shapes."""
    s = _speaker_label(speaker).lower()
    return any(h in s for h in hints)


def identify_top_speaker(segments: list[dict]) -> str | None:
    """Return the speaker label with the most spoken words (or None if empty)."""
    words: dict[str, int] = {}
    for seg in segments:
        sp = _speaker_label(seg.get("speaker", ""))
        words[sp] = words.get(sp, 0) + len(seg.get("text", "").split())
    if not words:
        return None
    return max(words.items(), key=lambda kv: kv[1])[0]


def load_transcripts(transcripts_dir: Path) -> list[dict]:
    """Load all `*.json` transcript files in a dir, sorted by `start_time`.

    Accepts two shapes for the inner `transcript` field:
      (a) structured  [{"speaker": ..., "text": ...}, ...]
      (b) Granola string  "assemblyai: Speaker A: ..."
    Files with null/empty transcripts are skipped.
    """
    if not transcripts_dir.is_dir():
        raise FileNotFoundError(f"transcripts dir not found: {transcripts_dir}")
    files = sorted(transcripts_dir.glob("*.json"))
    if not files:
        raise FileNotFoundError(f"no .json transcript files in {transcripts_dir}")
    out: list[dict] = []
    for f in files:
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            print(f"WARNING: malformed transcript {f.name}: {e}", file=sys.stderr)
            continue
        data.setdefault("meeting_id", f.stem)
        data.setdefault("title", f.stem)
        data.setdefault("start_time", "")
        ts = data.get("transcript")
        if ts is None or ts == "":
            print(f"WARNING: {f.name} has no transcript — skipping", file=sys.stderr)
            continue
        if isinstance(ts, str):
            data["transcript"] = parse_granola_transcript_string(ts)
        if not data["transcript"]:
            print(f"WARNING: {f.name} parsed to zero segments — skipping", file=sys.stderr)
            continue
        out.append(data)
    out.sort(key=lambda t: t.get("start_time") or "")
    return out


def effective_threshold(alias_length: int, base_threshold: int) -> int:
    """Length-aware threshold: short names need exact/near-exact to avoid false positives."""
    if alias_length <= 3:
        return 101  # exact only (no fuzzy will fire)
    if alias_length <= 5:
        return max(base_threshold, 90)
    return base_threshold


def count_callouts(
    transcript: dict,
    person_aliases: list[list[str]],
    threshold: int = 85,
    any_speaker: bool = False,
    lutz_hints: tuple[str, ...] = DEFAULT_LUTZ_HINTS,
) -> list[int]:
    """Count fuzzy first-name mentions in one transcript.

    Args:
      transcript: dict with `transcript` = list of {speaker, text} segments.
      person_aliases: list of alias-lists, one per person. e.g. [["Sarah"], ["Elizabeth","Liz"], ...]
      threshold: rapidfuzz similarity threshold (0-100) for names of length ≥6.
      any_speaker: if True, count every speaker's turn; else only the "professor" turn.
      lutz_hints: substrings used to identify the professor by speaker label.

    Returns:
      List of counts, one per person, in the same order as `person_aliases`.
    """
    counts = [0] * len(person_aliases)
    segments = transcript.get("transcript") or []
    if not person_aliases or not segments:
        return counts

    target_label: str | None = None
    if not any_speaker:
        named = [_speaker_label(s.get("speaker", "")) for s in segments
                 if is_named_speaker(s.get("speaker", ""), lutz_hints)]
        target_label = named[0] if named else identify_top_speaker(segments)

    pairs: list[tuple[int, str, int]] = []
    for i, aliases in enumerate(person_aliases):
        for a in aliases:
            al = a.lower()
            pairs.append((i, al, effective_threshold(len(al), threshold)))

    for turn in segments:
        if not any_speaker:
            if target_label is None or _speaker_label(turn.get("speaker", "")) != target_label:
                continue
        for tok in tokenize(turn.get("text", "")):
            for i, alias_l, eff in pairs:
                if tok == alias_l:
                    counts[i] += 1
                elif len(tok) >= 3 and eff <= 100 and fuzz.ratio(tok, alias_l) >= eff:
                    counts[i] += 1
    return counts
