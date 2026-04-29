from __future__ import annotations

from app.tools.browser_unsubscribe import (
    _is_allowed_control_label,
    _request_url_allowed,
)


def test_request_url_allowed_requires_exact_origin() -> None:
    allowed_origin = "https://example.com/"

    assert _request_url_allowed(
        url="https://example.com/unsubscribe?id=123",
        allowed_origin_prefix=allowed_origin,
    )
    assert _request_url_allowed(
        url="about:blank",
        allowed_origin_prefix=allowed_origin,
    )
    assert not _request_url_allowed(
        url="https://cdn.example.com/script.js",
        allowed_origin_prefix=allowed_origin,
    )
    assert not _request_url_allowed(
        url="https://example.com.evil.invalid/unsubscribe",
        allowed_origin_prefix=allowed_origin,
    )


def test_allowed_control_label_only_accepts_unsubscribe_actions() -> None:
    assert _is_allowed_control_label("Unsubscribe")
    assert _is_allowed_control_label("Stop emails")
    assert _is_allowed_control_label("Opt out of future messages")
    assert not _is_allowed_control_label("Manage preferences")
    assert not _is_allowed_control_label("Update preferences")
    assert not _is_allowed_control_label("Subscribe")
