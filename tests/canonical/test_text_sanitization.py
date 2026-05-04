"""Tests for text sanitization."""

from app.canonical import text_sanitization as ts


def test_strips_control_chars():
    out = ts.sanitize("hello\x00world\x01!")
    assert out.text == "helloworld!"
    assert out.too_long is False


def test_preserves_newlines_and_tabs():
    out = ts.sanitize("line1\nline2\tindented")
    assert "\n" in out.text
    assert "\t" in out.text


def test_replaces_urls():
    out = ts.sanitize("see https://example.com/foo and http://other.org")
    assert "https://example.com" not in out.text
    assert out.url_count == 2
    assert "[URL]" in out.text


def test_too_long_flag():
    big = "x" * 5000
    out = ts.sanitize(big, max_len=4096)
    assert out.too_long is True
    assert out.original_length == 5000


def test_short_input_not_too_long():
    out = ts.sanitize("brief", max_len=4096)
    assert out.too_long is False


def test_handles_none():
    out = ts.sanitize(None)
    assert out.text == ""
    assert out.too_long is False
    assert out.original_length == 0


def test_handles_empty():
    out = ts.sanitize("")
    assert out.text == ""
    assert out.original_length == 0
