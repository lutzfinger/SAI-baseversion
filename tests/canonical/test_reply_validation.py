"""Tests for ReplyDraft validators (#6 fail-closed safety layer)."""

import pytest

from app.canonical.reply_validation import ReplyDraft


def _good_body(extra: str = "") -> str:
    body = (
        "Hi there,\n\nI am SAI, the AI assistant for this course. I noticed "
        "your message about an extension. The current late-work policy allows "
        "you to coordinate with the teaching assistants on a workable plan. "
        "I have copied them on this message. Best, SAI"
    )
    return body + extra + (" " * max(0, 200 - len(body) - len(extra)))


def _good_kwargs(**overrides):
    base = {
        "classification": "no_exception",
        "to": "student@example.edu",
        "cc": ["ta1@example.edu"],
        "subject": "Re: extension request",
        "body": _good_body(),
    }
    base.update(overrides)
    return base


def test_well_formed_draft_accepted():
    d = ReplyDraft(**_good_kwargs())
    assert d.classification == "no_exception"


def test_body_must_self_identify_as_ai():
    bad_body = ("Hi there, your request has been received and we'll process "
                "it shortly with care and attention to all relevant details. "
                "Please coordinate with the teaching assistants on a workable "
                "plan. We've copied them on this thread for ease of follow-up.")
    bad_body += " " * max(0, 200 - len(bad_body))
    with pytest.raises(Exception, match="self-identify"):
        ReplyDraft(**_good_kwargs(body=bad_body))


def test_body_rejects_promise_language():
    bad = _good_body(extra=" I will give you an extension on this.")
    with pytest.raises(Exception, match="promise-language"):
        ReplyDraft(**_good_kwargs(body=bad))


def test_body_rejects_guarantee():
    bad = _good_body(extra=" We guarantee an extension here.")
    with pytest.raises(Exception, match="promise-language"):
        ReplyDraft(**_good_kwargs(body=bad))


def test_cc_must_be_non_empty():
    with pytest.raises(Exception, match="cc list"):
        ReplyDraft(**_good_kwargs(cc=[]))


def test_cc_entries_must_be_emails():
    with pytest.raises(Exception, match="cc entry"):
        ReplyDraft(**_good_kwargs(cc=["not-an-email"]))


def test_to_must_be_email():
    with pytest.raises(Exception, match="to must look like an email"):
        ReplyDraft(**_good_kwargs(to="aaaaaaaa"))


def test_body_too_short_rejected():
    with pytest.raises(Exception):
        ReplyDraft(**_good_kwargs(body="short. SAI assistant. "))


def test_body_too_long_rejected():
    huge = _good_body() + " filler" * 1000
    with pytest.raises(Exception):
        ReplyDraft(**_good_kwargs(body=huge))


def test_other_student_names_block_pii_leak():
    body_with_other = _good_body(extra=" I also asked Alex Other about this.")
    with pytest.raises(Exception, match="another student"):
        ReplyDraft(**_good_kwargs(
            body=body_with_other,
            other_student_names=["Alex Other"],
        ))


def test_empathy_allowed_on_no_exception():
    """Operator decision 2026-05-04 (see
    docs/design_reply_validator_loosen.md): warm acknowledgement
    is the right tone for ALL student-facing replies, even routine
    no_exception cases. The previous _tone_appropriate validator
    that banned 'sorry to hear' on no_exception was REMOVED.
    """
    body = _good_body(extra=" Sorry to hear about it.")
    d = ReplyDraft(**_good_kwargs(body=body, classification="no_exception"))
    assert d.classification == "no_exception"


def test_empathy_allowed_on_exception():
    """Empathy allowed on any classification — explicit assertion
    so a future re-introduction of the validator can't silently
    re-add the restriction."""
    body = _good_body(extra=" I'm sorry to hear this is a difficult time.")
    d = ReplyDraft(**_good_kwargs(body=body, classification="exception"))
    assert d.classification == "exception"
