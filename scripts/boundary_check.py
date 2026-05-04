"""Boundary linter — keep personal data out of the public SAI repo.

Walks every tracked file in the repo (or every file under --root if outside
git) and fails if any matches a deny-list of personal-data patterns:

  - Email addresses with domains other than example.com / example.org /
    localhost / test (configurable allowlist of placeholder domains).
  - Strings: lutzfinger, lfinger, Lutz_Dev (case-insensitive).
  - Absolute paths beginning with /Users/.
  - Slack channel names other than #general / #example / #test-channel
    (configurable).
  - Phone numbers in common formats.
  - Secret-reference scheme strings: `op://`, `keychain://` (these belong in
    private SAI-overlay templates, not in the public repo).

Files listed in `boundary_check_allowlist.txt` (one path per line, # for
comments) are exempt — every entry SHOULD be followed by a comment giving
the reason.

Exit codes: 0 clean, 1 violations found, 2 bad input.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_FILENAME = "boundary_check_allowlist.txt"

# Operator-specific terms (private contact names, private bucket names,
# operator's own domains beyond what `personal-string` already covers)
# go in this file. It's NEVER committed (it's in .gitignore by default).
# When present, the scanner treats every line as a regex pattern AND
# violations are reported even in allowlisted files. This catches the
# kind of narrative leaks that crept into PRINCIPLES.md / MIGRATION-
# PRINCIPLES.md before this layer existed.
PRIVATE_TERMS_FILENAME = "boundary_check_private_terms.txt"

PLACEHOLDER_EMAIL_DOMAINS = frozenset({
    "example.com", "example.org", "example.net", "example.edu",
    "localhost", "test",
})
ALLOWED_SLACK_CHANNELS = frozenset({
    "#general", "#example", "#test-channel",
    # Canonical SAI channel names (documented in PRINCIPLES.md
    # operating defaults + #16i channel registry). These are the
    # framework's default channel names for stranger installs;
    # operator can override via private overlay.
    "#sai-eval", "#sai-cost", "#sai-metrics", "#sai-dashboard",
    "#sai-status", "#sai-errors", "#sai-denied", "#sai-feedback",
    "#sai-tracing-feedback", "#sai-rag",
})

# Binary / build artifact extensions that shouldn't be scanned.
BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".tar", ".gz",
    ".sqlite", ".sqlite3", ".db", ".pyc", ".pyo", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
})
# Directories never to scan (also covered by .gitignore for tracked files,
# but defensive when scanning by --root outside git).
SKIP_DIRS = frozenset({
    ".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", ".egg-info",
})

# --- pattern definitions -----------------------------------------------------

# Match an email-shaped token, capture the domain part.
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")

# Personal-name strings (case-insensitive). Bounded to whole-word-ish to
# avoid false positives in paths like "lutz" appearing in fixtures.
PERSONAL_STRINGS_RE = re.compile(
    r"\b(lutzfinger|lfinger|Lutz_Dev)\b",
    re.IGNORECASE,
)

# /Users/ paths. We allow /Users/example/ as a placeholder.
USERS_PATH_RE = re.compile(r"/Users/(?!example/)[A-Za-z0-9._-]+")

# Slack channels: #channelname not in the allowlist.
SLACK_CHANNEL_RE = re.compile(r"(?<![A-Za-z0-9_/=])#([a-z0-9][a-z0-9._-]{1,79})\b")

# Phone numbers (US-ish; loose). Accepts +1 555-555-5555, (555) 555-5555,
# 555.555.5555, etc. Excludes obvious placeholder 5555555555 / 1234567890.
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)(?!\.\d)"
)
PHONE_PLACEHOLDERS = frozenset({"5555555555", "1234567890", "0000000000"})

# Secret-reference scheme strings.
SECRET_SCHEME_RE = re.compile(r"\b(op://|keychain://)\S*")


@dataclass(frozen=True)
class Violation:
    relpath: str
    line_number: int
    rule: str
    snippet: str

    def __str__(self) -> str:
        return f"{self.relpath}:{self.line_number}: [{self.rule}] {self.snippet}"


def _is_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    # Read the first chunk and look for a NUL byte.
    try:
        with path.open("rb") as f:
            head = f.read(8192)
    except OSError:
        return True
    return b"\x00" in head


def _list_files_via_git(root: Path) -> list[Path] | None:
    """Return tracked files via `git ls-files`, or None if not a git repo."""

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True, capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    raw = result.stdout.decode("utf-8")
    return [root / rel for rel in raw.split("\0") if rel]


def _list_files_via_walk(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            files.append(path)
    return sorted(files)


def _load_allowlist(root: Path) -> set[str]:
    allow_path = root / ALLOWLIST_FILENAME
    if not allow_path.exists():
        return set()
    entries: set[str] = set()
    for line in allow_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entries.add(line)
    return entries


def _load_private_terms(root: Path) -> list[re.Pattern[str]]:
    """Load operator-specific regex patterns from the private terms file.

    The file is gitignored by convention. Each non-blank, non-comment
    line is compiled as a case-insensitive regex. Patterns matched
    here are reported even in allowlisted files — they're operator
    data and should never appear in public.
    """

    path = root / PRIVATE_TERMS_FILENAME
    if not path.exists():
        return []
    patterns: list[re.Pattern[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line, re.IGNORECASE))
        except re.error as exc:
            print(
                f"warning: invalid regex in {PRIVATE_TERMS_FILENAME!r}: "
                f"{line!r} ({exc})", file=sys.stderr,
            )
    return patterns


def _scan_private_terms(
    relpath: str, line_no: int, line: str,
    private_terms: list[re.Pattern[str]],
) -> list[Violation]:
    out: list[Violation] = []
    for pattern in private_terms:
        for match in pattern.finditer(line):
            out.append(Violation(
                relpath, line_no, "private-term",
                f"matched {pattern.pattern!r}: {match.group(0)}",
            ))
    return out


def scan_line(relpath: str, line_no: int, line: str) -> list[Violation]:
    violations: list[Violation] = []

    for match in EMAIL_RE.finditer(line):
        domain = match.group(1).lower()
        if domain in PLACEHOLDER_EMAIL_DOMAINS:
            continue
        # IANA-reserved .example TLD (RFC 2606) — any *.example domain is
        # a documentation placeholder. Lets templates use distinctive made-up
        # company names like pied-piper.example, hooli.example, etc.
        if domain.endswith(".example"):
            continue
        # Subdomains of placeholder domains: grad.example.edu, mail.example.com,
        # etc. — also placeholders by construction.
        if any(domain.endswith("." + p) for p in PLACEHOLDER_EMAIL_DOMAINS):
            continue
        violations.append(Violation(relpath, line_no, "email-non-placeholder",
                                    match.group(0)))

    for match in PERSONAL_STRINGS_RE.finditer(line):
        violations.append(Violation(relpath, line_no, "personal-string",
                                    match.group(0)))

    for match in USERS_PATH_RE.finditer(line):
        violations.append(Violation(relpath, line_no, "users-path",
                                    match.group(0)))

    for match in SLACK_CHANNEL_RE.finditer(line):
        channel = "#" + match.group(1)
        if channel in ALLOWED_SLACK_CHANNELS:
            continue
        # CSS hex color exclusion: pure-hex tokens of length 3, 4, 6, or 8
        # are colors (#RGB, #RGBA, #RRGGBB, #RRGGBBAA), not Slack channels.
        body = match.group(1)
        if len(body) in (3, 4, 6, 8) and re.fullmatch(r"[0-9a-f]+", body):
            continue
        # Principle-reference exclusion: tokens like `#6a`, `#16i`, `#33b`,
        # `#3` are PRINCIPLES.md cross-references, not Slack channels.
        # Shape: digits optionally followed by 1-2 lowercase letters.
        if re.fullmatch(r"\d+[a-z]{0,2}", body):
            continue
        # Test-fixture channels: `#test-*`, `#mock-*`, `#fake-*` are
        # synthetic test placeholders, not real channels.
        if body.startswith(("test-", "mock-", "fake-", "sample-")):
            continue
        violations.append(Violation(relpath, line_no, "slack-channel-non-placeholder",
                                    channel))

    for match in PHONE_RE.finditer(line):
        normalized = re.sub(r"[^\d]", "", match.group(0))
        if normalized in PHONE_PLACEHOLDERS:
            continue
        # Allow strings of repeating digits (often appearing in API
        # response examples like "1234567890123" — too restrictive otherwise)
        if len(set(normalized)) <= 2:
            continue
        violations.append(Violation(relpath, line_no, "phone-number",
                                    match.group(0)))

    for match in SECRET_SCHEME_RE.finditer(line):
        violations.append(Violation(relpath, line_no, "secret-scheme-reference",
                                    match.group(0)))

    return violations


def scan_file(
    path: Path, *, root: Path,
    private_terms: list[re.Pattern[str]] | None = None,
    skip_standard_rules: bool = False,
) -> list[Violation]:
    rel = path.relative_to(root).as_posix()
    if _is_binary(path):
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as exc:
        return [Violation(rel, 0, "io-error", str(exc))]
    violations: list[Violation] = []
    for line_no, line in enumerate(lines, start=1):
        clean = line.rstrip("\n")
        if not skip_standard_rules:
            violations.extend(scan_line(rel, line_no, clean))
        if private_terms:
            violations.extend(_scan_private_terms(rel, line_no, clean, private_terms))
    return violations


def scan(
    root: Path, files: Iterable[Path], allowlist: set[str],
    private_terms: list[re.Pattern[str]] | None = None,
) -> list[Violation]:
    violations: list[Violation] = []
    private_terms = private_terms or []
    for path in files:
        rel = path.relative_to(root).as_posix()
        if rel == ALLOWLIST_FILENAME:
            continue
        if rel == PRIVATE_TERMS_FILENAME:
            continue
        if rel == "scripts/boundary_check.py":
            continue
        if rel in allowlist:
            # Allowlisted files skip the standard rules but STILL get
            # scanned for operator-specific private terms — those are
            # narrative leaks the allowlist isn't meant to permit.
            if private_terms:
                violations.extend(scan_file(
                    path, root=root,
                    private_terms=private_terms,
                    skip_standard_rules=True,
                ))
            continue
        violations.extend(scan_file(
            path, root=root, private_terms=private_terms,
        ))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT,
                        help="repo root to scan (default: this script's parent)")
    parser.add_argument("--paths", nargs="*", type=Path, default=None,
                        help="specific files to scan (relative to --root). Useful as a pre-commit hook.")
    parser.add_argument("--list-rules", action="store_true",
                        help="list active rules and their placeholder allowlists, then exit")
    args = parser.parse_args(argv)

    if args.list_rules:
        print("rules:")
        print(f"  email-non-placeholder      placeholders: {sorted(PLACEHOLDER_EMAIL_DOMAINS)}")
        print(f"  personal-string            (lutzfinger | lfinger | Lutz_Dev)")
        print(f"  users-path                 (placeholder allowed: /Users/example/)")
        print(f"  slack-channel-non-placeholder  placeholders: {sorted(ALLOWED_SLACK_CHANNELS)}")
        print(f"  phone-number")
        print(f"  secret-scheme-reference    (op:// | keychain://)")
        return 0

    root: Path = args.root.resolve()
    if not root.exists():
        print(f"root does not exist: {root}", file=sys.stderr)
        return 2

    allowlist = _load_allowlist(root)
    private_terms = _load_private_terms(root)

    if args.paths:
        files: list[Path] = []
        for p in args.paths:
            absolute = (root / p).resolve() if not p.is_absolute() else p
            if absolute.exists() and absolute.is_file():
                files.append(absolute)
    else:
        from_git = _list_files_via_git(root)
        files = from_git if from_git is not None else _list_files_via_walk(root)

    violations = scan(root, files, allowlist, private_terms=private_terms)

    if not violations:
        suffix = ""
        if private_terms:
            suffix = f"; {len(private_terms)} private-term pattern(s) loaded"
        print(f"boundary check ok: {len(files)} files scanned, 0 violations "
              f"(allowlist entries: {len(allowlist)}{suffix})")
        return 0

    print(f"boundary check FAILED: {len(violations)} violation(s) "
          f"in {len({v.relpath for v in violations})} file(s)", file=sys.stderr)
    for v in violations:
        print(f"  {v}", file=sys.stderr)
    print(file=sys.stderr)
    print("To exempt a file with reviewed justification, add its path to "
          f"{ALLOWLIST_FILENAME} (with a comment).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
