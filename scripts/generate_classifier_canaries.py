"""Generate classifier canaries: one synthetic test per rule.

Per the two-tier regression principle (PRINCIPLES.md §11a / §33,
2026-05-01): the regression eval set is split into

  1. Classifier canaries — one synthetic example per rule. Catches
     accidental rule deletions, regressions in the classifier
     mechanism, or threshold drift. Deterministic; any miss is a
     hard fail.

  2. LLM edge cases — operator-curated examples where the cascade
     reaches the LLM tier. Probabilistic; tracked as P/R/F1.

This script handles (1). It walks the classifier config in the
keyword-classify prompt frontmatter and emits one canary per rule
entry to eval/classifier_canaries.jsonl. Synthetic emails are
deterministic (sender from rule definition, placeholder subject /
snippet) so the file is reproducible across runs.

Usage:
    python -m scripts.generate_classifier_canaries
    python -m scripts.generate_classifier_canaries --dry-run
    python -m scripts.generate_classifier_canaries --output /tmp/canaries.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.shared.config import get_settings


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    from app.shared.runtime_env import load_runtime_env_best_effort
    load_runtime_env_best_effort()

    settings = get_settings()
    prompt_path = settings.prompts_dir / "email" / "keyword-classify.md"
    if not prompt_path.exists():
        print(f"error: prompt not found: {prompt_path}", file=sys.stderr)
        return 2

    config = _load_classifier_config(prompt_path)
    canaries = list(_build_canaries(config))

    if not canaries:
        print("warning: no rules found in classifier config; nothing to write",
              file=sys.stderr)
        return 1

    print(f"generated {len(canaries)} canaries from "
          f"{prompt_path.relative_to(settings.root_dir)}")

    # Distribution by rule_kind + category + action
    from collections import Counter
    by_kind = Counter(c["rule_kind"] for c in canaries)
    by_cat = Counter(c["expected_level1_classification"] for c in canaries)
    by_action = Counter(c["expected_action"] for c in canaries)
    for kind, n in by_kind.most_common():
        print(f"  by kind: {kind:36s} {n:4d}")
    for cat, n in by_cat.most_common():
        print(f"  by category: {cat:18s} {n:4d}")
    for action, n in by_action.most_common():
        print(f"  by action: {action:20s} {n:4d}")

    if args.dry_run:
        print("\n--dry-run: not writing")
        for c in canaries[:5]:
            print(f"  {c['rule_id']:50s} → L1={c['expected_level1_classification']}")
        if len(canaries) > 5:
            print(f"  ... and {len(canaries) - 5} more")
        return 0

    out_path = (
        Path(args.output).expanduser() if args.output
        else settings.root_dir / "eval" / "canaries.jsonl"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for canary in canaries:
            fh.write(json.dumps(canary, sort_keys=True) + "\n")
    print(f"\nwrote {len(canaries)} canaries → {out_path}")
    return 0


def _load_classifier_config(prompt_path: Path) -> dict[str, Any]:
    raw = prompt_path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        return {}
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}
    frontmatter = yaml.safe_load(parts[1]) or {}
    return frontmatter.get("classifier", {}) or {}


def _build_canaries(config: dict[str, Any]):
    """One canary per rule entry. Each canary is a synthetic minimal
    email + the L1 the rule should produce.
    """

    now = datetime.now(UTC).isoformat()

    # Display-name substring rules (v16 night, 2026-05-04): synthetic
    # email from a generic address but the matching name is in
    # `from_name` → category. One canary per substring per category.
    name_subs = config.get("level1_sender_name_substring_matches") or {}
    for category, subs in name_subs.items():
        for raw_sub in subs or []:
            s = str(raw_sub).strip()
            if not s:
                continue
            yield _make_canary(
                rule_kind="sender_name_substring",
                rule_value=s,
                expected_l1=category,
                synthetic_from="canary-name-substring@example.org",
                synthetic_subject="[canary] sender_name_substring fixture",
                directly_addressed=False,
                is_thread_start=True,
                generated_at=now,
                from_name=f"Canary {s} Sender",
            )

    # Subject-prefix rules (v16, 2026-05-04): synthetic email from a
    # random sender with the prefix in the subject → category. One
    # canary per prefix per category.
    for category, prefixes in (config.get("level1_subject_prefix_matches") or {}).items():
        for prefix in prefixes or []:
            p = str(prefix).strip()
            if not p:
                continue
            yield _make_canary(
                rule_kind="subject_prefix",
                rule_value=p,
                expected_l1=category,
                synthetic_from="canary-subject-prefix@example.org",
                synthetic_subject=f"{p} canary fixture",
                directly_addressed=False,
                is_thread_start=True,
                generated_at=now,
            )

    # Sender-email rules: exact email → category.
    for category, emails in (config.get("level1_sender_email_matches") or {}).items():
        for email in emails or []:
            email_norm = str(email).strip().lower()
            if not email_norm:
                continue
            yield _make_canary(
                rule_kind="sender_email",
                rule_value=email_norm,
                expected_l1=category,
                synthetic_from=email_norm,
                synthetic_subject=f"[canary] sender_email rule for {category}",
                directly_addressed=False,
                is_thread_start=True,
                generated_at=now,
            )

    # Sender-domain rules: domain → category, no direct-address required.
    for category, domains in (config.get("level1_sender_domain_matches") or {}).items():
        for domain in domains or []:
            d = str(domain).strip().lower()
            if not d:
                continue
            yield _make_canary(
                rule_kind="sender_domain",
                rule_value=d,
                expected_l1=category,
                synthetic_from=f"canary@{d.lstrip('@')}",
                synthetic_subject=f"[canary] sender_domain rule for {category}",
                directly_addressed=False,
                is_thread_start=True,
                generated_at=now,
            )

    # Direct-address domain rules: domain → category, requires direct address.
    direct = config.get("level1_sender_domain_matches_require_direct_address") or {}
    for category, domains in direct.items():
        for domain in domains or []:
            d = str(domain).strip().lower()
            if not d:
                continue
            yield _make_canary(
                rule_kind="sender_domain_direct_address",
                rule_value=d,
                expected_l1=category,
                synthetic_from=f"canary@{d.lstrip('@')}",
                synthetic_subject=f"[canary] direct_address rule for {category}",
                directly_addressed=True,
                is_thread_start=True,
                generated_at=now,
            )

    # First-email skip rules: sender/domain on a thread start → fallback
    # bucket. Per N1 (2026-05-02), the fallback bucket name is `no_label`
    # and the L1/no_label Gmail label IS applied (operator can see the
    # email was processed without a clear category).
    skip = config.get("level1_skip_first_email_from") or {}
    fallback = (config.get("level1_fallback") or "no_label").lower()
    for sender in skip.get("senders", []) or []:
        s = str(sender).strip().lower()
        if not s:
            continue
        yield _make_canary(
            rule_kind="skip_first_email_sender",
            rule_value=s,
            expected_l1=fallback,
            synthetic_from=s,
            synthetic_subject="[canary] first-email skip (sender)",
            directly_addressed=False,
            is_thread_start=True,
            generated_at=now,
        )
    for domain in skip.get("domains", []) or []:
        d = str(domain).strip().lower()
        if not d:
            continue
        yield _make_canary(
            rule_kind="skip_first_email_domain",
            rule_value=d,
            expected_l1=fallback,
            synthetic_from=f"canary@{d.lstrip('@')}",
            synthetic_subject="[canary] first-email skip (domain)",
            directly_addressed=False,
            is_thread_start=True,
            generated_at=now,
        )


SKIP_RULE_KINDS = {"skip_first_email_sender", "skip_first_email_domain"}
# Retained for reference + downstream tooling that may inspect rule
# kinds. Per N1 (2026-05-02), these rules now produce `no_label` +
# `apply_l1_label` (the L1/no_label Gmail label IS applied). The
# constant no longer gates expected_action selection.


def _make_canary(
    *,
    rule_kind: str,
    rule_value: str,
    expected_l1: str,
    synthetic_from: str,
    synthetic_subject: str,
    directly_addressed: bool,
    is_thread_start: bool,
    generated_at: str,
    from_name: str = "Canary Sender",
) -> dict[str, Any]:
    rule_id = f"{rule_kind}::{rule_value}::{expected_l1}"
    # Direct-address detection scans the opening text for one of the
    # aliases (e.g., "lutz", "finger", "professor"). For canaries that
    # need to fire a direct-address rule, embed an alias in the
    # body/snippet so detection succeeds.
    if directly_addressed:
        snippet = f"Hi Lutz, canary input for rule {rule_kind}/{rule_value}."
    else:
        snippet = f"Canary input for rule {rule_kind}/{rule_value}."
    # Per N1 (2026-05-02): every rule produces a real Gmail label,
    # including the no_label fallback. The old `skip_l1_tagging` action
    # is retained in the schema for back-compat with historical canaries
    # but new canaries always use `apply_l1_label`.
    expected_action = "apply_l1_label"
    return {
        "rule_id": rule_id,
        "rule_kind": rule_kind,
        "rule_value": rule_value,
        "expected_level1_classification": expected_l1,
        "expected_action": expected_action,
        "min_confidence": 0.85,
        "generated_at": generated_at,
        "synthetic_email": {
            "message_id": f"canary-{abs(hash(rule_id)) % (10**12):012d}",
            "thread_id": f"canary-thread-{abs(hash(rule_id)) % (10**12):012d}",
            "from_email": synthetic_from,
            "from_name": from_name,
            "to": (["operator@example.com"] if directly_addressed else []),
            "cc": [],
            "subject": synthetic_subject,
            "snippet": snippet,
            "body_excerpt": snippet,
            "list_unsubscribe": [],
            "list_unsubscribe_post": None,
            "unsubscribe_links": [],
            "received_at": None,
            "directly_addressed": directly_addressed,
            "is_thread_start": is_thread_start,
        },
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="generate_classifier_canaries", description=__doc__
    )
    p.add_argument("--output", default=None,
                   help="output JSONL path (default: <repo>/eval/classifier_canaries.jsonl)")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be written; don't write")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
