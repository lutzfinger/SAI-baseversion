"""Quarantine synthetic learning rows and rebuild the active training corpus."""

from __future__ import annotations

import json

from app.learning.corpus_hygiene import rebuild_learning_corpus
from app.shared.config import get_settings


def main() -> int:
    summary = rebuild_learning_corpus(settings=get_settings())
    print(json.dumps(summary.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
