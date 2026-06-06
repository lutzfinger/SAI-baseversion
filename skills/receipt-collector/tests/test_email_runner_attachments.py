"""cost_compiler image plumbing (2026-06-06): receipt photos arrive as image
attachments and were silently dropped (only text/plain was read), so the agent had no
amounts and asked for clarification. _extract_image_attachments downloads them as
Anthropic vision blocks (urlsafe gmail data -> standard base64), bounded + fail-soft."""
from __future__ import annotations

import base64
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.email_runner import _extract_image_attachments  # noqa: E402


class _FakeSvc:
    """Stands in for the Gmail API service: .users().messages().attachments().get().execute()."""

    def __init__(self, data_by_id):
        self._data = data_by_id
        self._cur = None

    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        return self

    def get(self, *, userId, messageId, id):  # noqa: A002 - mirrors the gmail api kwarg
        self._cur = id
        return self

    def execute(self):
        return {"data": self._data[self._cur]}


def _img_part(att_id, mime="image/jpeg"):
    return {"mimeType": mime, "filename": "receipt.jpg", "body": {"attachmentId": att_id}}


def test_extracts_image_as_vision_block():
    raw = b"\xff\xd8\xff\xe0 fake jpeg payload"
    svc = _FakeSvc({"att1": base64.urlsafe_b64encode(raw).decode()})
    msg = {
        "id": "m1",
        "payload": {
            "parts": [
                {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Cheers, Lutz").decode()}},
                _img_part("att1"),
            ]
        },
    }
    blocks = _extract_image_attachments(svc, msg)
    assert len(blocks) == 1
    assert blocks[0]["media_type"] == "image/jpeg"
    # decodes back to the original bytes (gmail urlsafe data -> standard b64 for Anthropic)
    assert base64.standard_b64decode(blocks[0]["data"]) == raw


def test_text_only_email_yields_no_images():
    svc = _FakeSvc({})
    msg = {
        "id": "m1",
        "payload": {
            "parts": [
                {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"hi").decode()}},
            ]
        },
    }
    assert _extract_image_attachments(svc, msg) == []


def test_non_image_attachment_skipped():
    svc = _FakeSvc({"a": base64.urlsafe_b64encode(b"ZIPDATA").decode()})
    msg = {
        "id": "m1",
        "payload": {"parts": [{"mimeType": "application/zip", "body": {"attachmentId": "a"}}]},
    }
    assert _extract_image_attachments(svc, msg) == []
