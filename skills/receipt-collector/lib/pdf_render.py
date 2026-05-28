"""
Receipt → PDF rendering.

Two render paths:
  * Primary: weasyprint (HTML+CSS+images, ~3s per receipt). Produces a
    high-fidelity PDF that matches what the original Gmail email looks
    like — vendor logos, brand colors, tables, etc.
  * Fallback: fpdf2 text-only (~0.2s, no GTK deps). Used if weasyprint
    can't import or the source thread has no HTML body.

weasyprint needs Homebrew's GTK natives (pango, glib, fontconfig).
Apple's `ctypes.util.find_library` doesn't look in /opt/homebrew/lib by
default, so we monkey-patch it once at import time. The patch is a no-op
if Homebrew isn't present (the fallback path catches the ImportError).
"""
from __future__ import annotations

import ctypes.util
import os
import re
import textwrap
from pathlib import Path

from fpdf import FPDF


# --- WeasyPrint native-lib finder patch (macOS / Homebrew) ---
_HOMEBREW_PREFIXES = ("/opt/homebrew/lib", "/usr/local/lib")
_orig_find_library = ctypes.util.find_library


def _hb_find_library(name: str):
    for base in _HOMEBREW_PREFIXES:
        for cand in (f"{base}/lib{name}.dylib",
                     f"{base}/lib{name}-0.dylib",
                     f"{base}/lib{name}-1.dylib",
                     f"{base}/lib{name}.1.dylib",
                     f"{base}/lib{name}.0.dylib"):
            if os.path.exists(cand):
                return cand
    return _orig_find_library(name)


ctypes.util.find_library = _hb_find_library

try:
    import weasyprint  # noqa: F401
    _WEASYPRINT_OK = True
except Exception as _weasy_err:  # pragma: no cover - macOS / missing brew
    weasyprint = None
    _WEASYPRINT_OK = False


# --- Latin-1 substitution for the fpdf2 fallback path ---
_LATIN1_SUBS = {
    "–": "-", "—": "-", "―": "-",
    "‘": "'", "’": "'", "‚": ",",
    "“": '"', "”": '"', "„": '"',
    "…": "...", "•": "*", "·": "-",
    "→": "->", "←": "<-",
    "↑": "^",  "↓": "v",
    " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", " ": " ", "　": " ",
    "​": "",  "‌": "",  "‍": "",  "﻿": "",
}


def _latin1(s: str) -> str:
    for k, v in _LATIN1_SUBS.items():
        s = s.replace(k, v)
    return s.encode("latin-1", errors="replace").decode("latin-1")


# ------------------------------------------------------------------
# Banner — prepended to the email HTML so the rendered PDF carries the
# Purchase context the operator needs to bind it back to QB.
# ------------------------------------------------------------------
_BANNER_TEMPLATE = """\
<style>
.sai-banner {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  border: 1px solid #d0d0d0;
  border-radius: 6px;
  padding: 10px 14px;
  margin-bottom: 14px;
  background: #fafafa;
  color: #222;
}}
.sai-banner .title {{ font-size: 14pt; font-weight: 700; margin: 0 0 4px 0; }}
.sai-banner .meta  {{ font-size: 9pt;  color: #666;       margin: 0; }}
.sai-banner .subj  {{ font-size: 9pt;  color: #444; margin: 4px 0 0 0; }}
</style>
<div class="sai-banner">
  <p class="title">{vendor} &mdash; {amount} {currency}</p>
  <p class="meta">QB Purchase Id={purchase_id} &middot; {date_iso}{customer_bit}</p>
  {subject_html}
</div>
"""


def _build_banner(vendor: str, amount: str, currency: str, purchase_id: str,
                  date_iso: str, subject: str, customer: str = "") -> str:
    import html as _h
    customer_bit = f" &middot; billed to {_h.escape(customer)}" if customer else ""
    subject_html = (f'<p class="subj">{_h.escape(subject)}</p>'
                    if subject else "")
    return _BANNER_TEMPLATE.format(
        vendor=_h.escape(vendor or "?"),
        amount=_h.escape(amount or ""),
        currency=_h.escape(currency or ""),
        purchase_id=_h.escape(str(purchase_id)),
        date_iso=_h.escape(date_iso or ""),
        customer_bit=customer_bit,
        subject_html=subject_html,
    )


# ------------------------------------------------------------------
# weasyprint primary path
# ------------------------------------------------------------------
def render_html_pdf(
    pdf_path: Path,
    html_body: str,
    *,
    vendor: str = "",
    purchase_id: str = "",
    amount: str = "",
    currency: str = "",
    subject: str = "",
    date_iso: str = "",
    customer: str = "",
    base_url: str = "https://mail.google.com/",
) -> None:
    """Render the original HTML (with images, CSS, brand colors) to PDF
    via weasyprint. Prepends a SAI banner with the QB context.

    Raises RuntimeError if weasyprint can't be loaded — caller should
    fall back to render_text_pdf().
    """
    if not _WEASYPRINT_OK:
        raise RuntimeError("weasyprint unavailable (install pango+glib via brew)")

    banner = _build_banner(vendor, amount, currency, purchase_id, date_iso, subject, customer)
    # Inject the banner right after <body> if there's one; otherwise prepend.
    full_html = _inject_banner(html_body, banner)
    weasyprint.HTML(string=full_html, base_url=base_url).write_pdf(str(pdf_path))


_BODY_RE = re.compile(r"<body[^>]*>", re.IGNORECASE)


def _inject_banner(html_body: str, banner: str) -> str:
    if "<body" in html_body.lower():
        return _BODY_RE.sub(lambda m: m.group(0) + banner, html_body, count=1)
    return f"<html><body>{banner}{html_body}</body></html>"


# ------------------------------------------------------------------
# fpdf2 fallback path (text-only)
# ------------------------------------------------------------------
_PAGE_W_MM = 210
_MARGIN_MM = 14
_LINE_H = 4.0
_BODY_CHARS = 92


def _collapse_blanks(lines: list[str]) -> list[str]:
    out: list[str] = []
    blanks = 0
    for line in lines:
        if line.strip() == "":
            blanks += 1
            if blanks <= 1:
                out.append("")
        else:
            blanks = 0
            out.append(line.rstrip())
    while out and out[0] == "": out.pop(0)
    while out and out[-1] == "": out.pop()
    return out


def _wrap(lines: list[str], width: int) -> list[str]:
    out: list[str] = []
    for ln in lines:
        if not ln:
            out.append("")
            continue
        leading = re.match(r"^[ \t]*", ln).group(0)
        wrapped = textwrap.wrap(ln, width=width, break_long_words=True,
                                break_on_hyphens=False, subsequent_indent=leading) or [""]
        out.extend(wrapped)
    return out


def render_text_pdf(
    pdf_path: Path,
    body_text: str,
    *,
    vendor: str = "",
    purchase_id: str = "",
    amount: str = "",
    currency: str = "",
    subject: str = "",
    date_iso: str = "",
    customer: str = "",
) -> None:
    """Text-only fpdf2 fallback. Used when weasyprint isn't available or
    the source has no HTML body."""
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=_MARGIN_MM)
    pdf.set_margins(_MARGIN_MM, _MARGIN_MM, _MARGIN_MM)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(15, 15, 15)
    title = _latin1(f"{vendor or '?'}   {amount} {currency}")
    pdf.cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(80, 80, 80)
    meta = []
    if purchase_id: meta.append(f"QB Purchase Id={purchase_id}")
    if date_iso:    meta.append(date_iso)
    if customer:    meta.append(f"billed to {customer}")
    pdf.cell(0, 5, _latin1(" · ".join(meta)), new_x="LMARGIN", new_y="NEXT")
    if subject:
        for chunk in textwrap.wrap(_latin1(subject), width=110) or [""]:
            pdf.cell(0, 5, chunk, new_x="LMARGIN", new_y="NEXT")

    pdf.set_draw_color(180, 180, 180)
    y = pdf.get_y() + 1
    pdf.line(_MARGIN_MM, y, _PAGE_W_MM - _MARGIN_MM, y)
    pdf.set_y(y + 3)

    pdf.set_font("Courier", "", 9)
    pdf.set_text_color(20, 20, 20)
    lines = _collapse_blanks(_latin1(body_text).splitlines())
    lines = _wrap(lines, _BODY_CHARS)
    for line in lines:
        pdf.cell(0, _LINE_H, line, new_x="LMARGIN", new_y="NEXT")

    pdf.output(str(pdf_path))


# ------------------------------------------------------------------
# Image (JPEG/PNG) → PDF — wraps the photo in a one-page PDF with banner.
# Used for on-site receipts the operator forwarded as phone-camera photos.
# ------------------------------------------------------------------
def image_to_pdf(
    pdf_path: Path,
    image_paths: list[Path],
    *,
    vendor: str = "",
    purchase_id: str = "",
    amount: str = "",
    currency: str = "",
    subject: str = "",
    date_iso: str = "",
    customer: str = "",
) -> None:
    """Wrap one or more JPEG/PNG photos in a PDF — one page per image, with
    a small SAI banner at the top of the first page (vendor / Purchase id /
    customer / subject)."""
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.set_margins(_MARGIN_MM, _MARGIN_MM, _MARGIN_MM)

    for i, img in enumerate(image_paths):
        pdf.add_page()
        if i == 0:
            pdf.set_font("Helvetica", "B", 13)
            pdf.set_text_color(15, 15, 15)
            pdf.cell(0, 7, _latin1(f"{vendor or '?'}   {amount} {currency}"),
                     new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(80, 80, 80)
            meta = []
            if purchase_id: meta.append(f"QB Purchase Id={purchase_id}")
            if date_iso:    meta.append(date_iso)
            if customer:    meta.append(f"billed to {customer}")
            pdf.cell(0, 5, _latin1(" · ".join(meta)), new_x="LMARGIN", new_y="NEXT")
            if subject:
                pdf.cell(0, 5, _latin1(subject[:110]), new_x="LMARGIN", new_y="NEXT")
            pdf.set_draw_color(180, 180, 180)
            y_line = pdf.get_y() + 1
            pdf.line(_MARGIN_MM, y_line, _PAGE_W_MM - _MARGIN_MM, y_line)
            pdf.set_y(y_line + 4)
        # Fit image into the remaining page width while respecting aspect ratio.
        # fpdf2 auto-scales when w is given but h is omitted.
        max_w = _PAGE_W_MM - 2 * _MARGIN_MM
        max_h = 297 - pdf.get_y() - _MARGIN_MM  # remaining vertical space
        pdf.image(str(img), x=_MARGIN_MM, y=pdf.get_y(), w=max_w, h=max_h,
                  keep_aspect_ratio=True)

    pdf.output(str(pdf_path))


# ------------------------------------------------------------------
# Public entry: try HTML first, fall back to text.
# ------------------------------------------------------------------
def render_pdf(
    pdf_path: Path,
    body_text: str = "",
    *,
    html_body: str = "",
    vendor: str = "",
    purchase_id: str = "",
    amount: str = "",
    currency: str = "",
    subject: str = "",
    date_iso: str = "",
    customer: str = "",
) -> str:
    """Render a receipt to PDF.

    If `html_body` is non-empty AND weasyprint is available, render HTML
    with images/CSS/brand colors. Otherwise fall back to text-only fpdf2.

    Returns the path string used: "html" (weasyprint) or "text" (fpdf2).
    """
    pdf_path = Path(pdf_path)
    if html_body and _WEASYPRINT_OK:
        try:
            render_html_pdf(
                pdf_path, html_body,
                vendor=vendor, purchase_id=purchase_id,
                amount=amount, currency=currency,
                subject=subject, date_iso=date_iso, customer=customer,
            )
            return "html"
        except Exception:
            # Fall through to text path if HTML render breaks (rare)
            pass
    render_text_pdf(
        pdf_path, body_text,
        vendor=vendor, purchase_id=purchase_id,
        amount=amount, currency=currency,
        subject=subject, date_iso=date_iso, customer=customer,
    )
    return "text"
