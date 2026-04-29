from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from app.workers.email_models import EmailClassification, EmailMessage

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "calendar_link_positive_meeting_requests.json"

def test_schema_accepts_live_meeting_request_dataset() -> None:
    records = _load_records_or_skip()
    for record in records:
        EmailMessage.model_validate(
            {
                "message_id": record["message_id"],
                "thread_id": record.get("thread_id"),
                "from_email": record["from_email"],
                "from_name": record.get("from_name"),
                "to": record["to"],
                "cc": record.get("cc", []),
                "subject": record["subject"],
                "snippet": record["snippet"],
                "body_excerpt": record.get("body_excerpt", ""),
                "received_at": record.get("received_at"),
            }
        )
        EmailClassification.model_validate(
            {
                "message_id": record["message_id"],
                "level1_classification": record["expected_level1_classification"],
                "level2_intent": record["expected_level2_intent"],
                "confidence": 0.9,
                "reason": "fixture expectation",
            }
        )


def _load_records_or_skip() -> list[dict[str, object]]:
    if not FIXTURE_PATH.exists():
        pytest.skip("Build the live meeting-request fixture with `make build-meeting-fixture`.")
    records = cast(
        list[dict[str, object]],
        json.loads(FIXTURE_PATH.read_text(encoding="utf-8")),
    )
    if not records:
        pytest.skip("Live meeting-request fixture is empty; rebuild it from Gmail.")
    return records
