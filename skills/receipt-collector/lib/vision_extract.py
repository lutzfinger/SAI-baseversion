"""
Receipt OCR + structured extraction via Anthropic's vision model.

Tried Tesseract first (free, local, fast) but it mangled the rotated
phone photos and lost amount columns. Claude's vision model handles
rotated images, handwritten amounts, and mixed languages cleanly.

LLM-cost policy: stays opt-in. The runner only invokes this when the
operator explicitly asks (e.g., `extract-receipt-amounts` subcommand
or a `--vision` flag on `attach-onsite-photos`). The result is treated
as advisory — the operator confirms before any QB amount is written.

API key: pulled from 1Password at runtime via the OP service-account
token; never stored on disk in this skill. The specific 1Password item
+ vault names come from the operator overlay
(`overlay['secrets']['anthropic']`) — the base skill itself does not
know any operator's 1Password naming.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Cheapest Claude vision-capable model. The receipt-amount task is
# token-light — typical receipt extraction is < 2K total tokens.
DEFAULT_MODEL = "claude-haiku-4-5"

# Default 1Password field for OP CLI lookups. The item + vault names are
# operator-specific and must be supplied by the caller (overlay).
DEFAULT_OP_FIELD = "password"


@dataclass
class ReceiptExtraction:
    total: Optional[float] = None
    currency: Optional[str] = None
    date_iso: Optional[str] = None
    vendor: Optional[str] = None
    line_items: list[dict] = field(default_factory=list)
    confidence: Optional[str] = None      # "high" | "medium" | "low"
    notes: str = ""
    raw_response: str = ""
    # cost tracking
    input_tokens: int = 0
    output_tokens: int = 0
    usd_cost: float = 0.0


def _get_api_key(secret_ref: dict | None = None) -> str:
    """Read the Anthropic API key.

    Resolution order:
      1. ANTHROPIC_API_KEY env var (always wins — handy for CI / tests)
      2. 1Password lookup using `secret_ref` from the overlay:
           {"op_item": "<name>", "op_vault": "<vault>", "field": "password"}

    If neither is set, raises RuntimeError. The base skill carries no
    operator-specific 1Password naming.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    if not secret_ref:
        raise RuntimeError(
            "No ANTHROPIC_API_KEY env var and no secret_ref provided. "
            "Pass overlay['secrets']['anthropic'] to extract_receipt()."
        )
    op_item = secret_ref.get("op_item")
    op_vault = secret_ref.get("op_vault")
    field = secret_ref.get("field", DEFAULT_OP_FIELD)
    if not op_item or not op_vault:
        raise RuntimeError(
            f"secret_ref missing op_item/op_vault: {secret_ref!r}"
        )
    # Ensure service-account auth before invoking `op` (SAI #7a).
    from lib import op_env
    op_env.ensure_sa_token()
    r = subprocess.run(
        ["op", "item", "get", op_item, "--vault", op_vault,
         "--reveal", "--fields", f"label={field}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Couldn't read Anthropic API key from 1Password: {r.stderr.strip()}")
    return r.stdout.strip()


def _haiku_price_per_mtok() -> tuple[float, float]:
    """Per-1M-token prices for Claude Haiku 4.5 in USD (input, output).
    Public list-price as of writing; replace with a live lookup if the
    operator wants exact billing.
    """
    return (1.00, 5.00)  # USD per 1M tokens — Haiku 4.5 list


_EXTRACTION_PROMPT = """\
You are receipt-OCR for an accounting pipeline. Read the receipt in the
image and return a STRICT JSON object with these fields:

  total        number (the final total the customer paid, after taxes)
  currency     3-letter ISO code ("EUR", "USD", "GBP", ...)
  date_iso     YYYY-MM-DD of the transaction
  vendor       short vendor name as printed on the receipt
  line_items   array of {description, amount} — top 3-5 most important lines
  confidence   "high" | "medium" | "low"
  notes        anything ambiguous (e.g., handwritten correction, smudge)

Rules:
- Output ONLY the JSON, no prose around it, no code fences.
- If a value can't be read reliably, set it to null and lower confidence.
- If multiple receipts visible, prefer the one with the highest visible total.
- Amounts use a decimal point, not a comma.
"""


def extract_receipt(
    image_path: Path, *,
    model: str = DEFAULT_MODEL,
    secret_ref: dict | None = None,
    overlay: dict | None = None,
    skill_name: str = "receipt-collector",
    step_name: str = "extract_receipt_amounts",
) -> ReceiptExtraction:
    """Run vision-based receipt extraction on a single image. Returns
    a structured ReceiptExtraction with cost tracking.

    `secret_ref` should be `overlay['secrets']['anthropic']` so we can
    look up the API key without baking the operator's 1Password naming
    into the base skill.

    Daily budget cap (SAI #28): if `overlay['policy']['daily_llm_cap_usd']`
    is set, this call raises `llm_costs.BudgetExceeded` BEFORE invoking
    the model when today's spend + this call's estimate would exceed it.
    Per #6 fail-closed, the caller MUST NOT silently swallow that.
    """
    # Daily budget cap check (fail closed before the API call).
    # Estimated cost is conservative: ~2K input tokens + ~500 output =
    # $0.0045 at Haiku 4.5 list prices; round to $0.01 for safety.
    from lib import llm_costs as _lc
    _lc.enforce_daily_cap(
        skill=skill_name,
        step=step_name,
        upcoming_usd_cost=0.01,
        overlay=overlay,
    )

    try:
        import anthropic
    except ImportError as e:
        raise ImportError(
            "anthropic SDK required. Install:  python3 -m pip install --user anthropic"
        ) from e

    img_path = Path(image_path)
    img_bytes = img_path.read_bytes()
    # Anthropic vision rejects images whose BASE64-encoded size exceeds
    # 5 MiB; that means raw bytes > ~3.75 MiB will fail. Auto-downsize
    # via Pillow if needed (cap longest edge 1800px).
    # Always pre-process via Pillow when available: (1) honor EXIF rotation
    # so the model sees the receipt upright, (2) downsize if base64 size
    # would exceed Anthropic's 5 MiB limit (raw > ~3.75 MiB triggers it).
    try:
        from PIL import Image, ImageOps
        import io
        im = Image.open(io.BytesIO(img_bytes))
        im = ImageOps.exif_transpose(im)  # apply EXIF orientation
        if len(img_bytes) > 3_700_000 or max(im.size) > 2200:
            im.thumbnail((1800, 1800))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=88, optimize=True)
        img_bytes = buf.getvalue()
        mime = "image/jpeg"
    except ImportError:
        ext = img_path.suffix.lower()
        mime = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp",
        }.get(ext, "image/jpeg")
        if len(img_bytes) > 3_700_000:
            raise RuntimeError(
                f"Image {img_path.name} is too large and Pillow isn't installed."
            )
    img_b64 = base64.standard_b64encode(img_bytes).decode()

    client = anthropic.Anthropic(api_key=_get_api_key(secret_ref))
    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": img_b64}},
                {"type": "text", "text": _EXTRACTION_PROMPT},
            ],
        }],
    )

    raw = "".join(block.text for block in resp.content if block.type == "text").strip()
    # Strip code fences if the model ignored "no fences" instruction
    raw_clean = re.sub(r"^```(?:json)?\s*", "", raw)
    raw_clean = re.sub(r"\s*```$", "", raw_clean).strip()

    try:
        data = json.loads(raw_clean)
    except json.JSONDecodeError:
        data = {}

    # Cost
    in_tok = resp.usage.input_tokens
    out_tok = resp.usage.output_tokens
    in_p, out_p = _haiku_price_per_mtok()
    cost = in_tok * in_p / 1e6 + out_tok * out_p / 1e6

    return ReceiptExtraction(
        total=_to_float(data.get("total")),
        currency=(data.get("currency") or "").upper() or None,
        date_iso=data.get("date_iso"),
        vendor=data.get("vendor"),
        line_items=data.get("line_items") or [],
        confidence=data.get("confidence"),
        notes=data.get("notes") or "",
        raw_response=raw,
        input_tokens=in_tok,
        output_tokens=out_tok,
        usd_cost=cost,
    )


def _to_float(v):
    if v is None: return None
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return None


# --- Local-LLM vision tier (Phase D.1) ------------------------------------

DEFAULT_LOCAL_MODEL = "llava:7b"


def _have_ollama() -> bool:
    try:
        subprocess.run(["ollama", "--version"], capture_output=True, timeout=3)
        return True
    except Exception:
        return False


def _have_ollama_model(model: str) -> bool:
    try:
        r = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    if r.returncode != 0:
        return False
    return any(line.startswith(model.split(":")[0]) and (model in line)
               for line in r.stdout.splitlines())


def extract_receipt_local(
    image_path: Path,
    *,
    model: str = DEFAULT_LOCAL_MODEL,
    skill_name: str = "receipt-collector",
    step_name: str = "extract_receipt_amounts_local",
) -> ReceiptExtraction:
    """Run vision-OCR via a local Llava model through Ollama. Free.

    Returns a `ReceiptExtraction` with the same shape as `extract_receipt`
    but `usd_cost=0.0`. If Ollama or the model is missing, returns a
    ReceiptExtraction with confidence="low" and notes="local LLM unavailable"
    so the caller (cmd_extract_amounts) can decide to escalate to Haiku.

    Per SAI #1 (local-first) + #12 (cascade upward only) this is the
    cheaper tier; the runner only falls back to Haiku when the local
    tier abstains.
    """
    if not _have_ollama():
        return ReceiptExtraction(confidence="low",
                                 notes="ollama not installed")
    if not _have_ollama_model(model):
        return ReceiptExtraction(confidence="low",
                                 notes=f"ollama model {model} not pulled")
    try:
        import ollama
    except ImportError:
        return ReceiptExtraction(confidence="low",
                                 notes="ollama python lib not installed")

    img_path = Path(image_path)
    img_bytes = img_path.read_bytes()
    # Always rotate per EXIF + downsize aggressively for local — small
    # models do better with smaller, upright images.
    try:
        from PIL import Image, ImageOps
        import io
        im = Image.open(io.BytesIO(img_bytes))
        im = ImageOps.exif_transpose(im)
        im.thumbnail((1024, 1024))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
        img_bytes = buf.getvalue()
    except ImportError:
        pass

    try:
        resp = ollama.chat(
            model=model,
            messages=[{
                "role": "user",
                "content": _EXTRACTION_PROMPT,
                "images": [img_bytes],
            }],
            options={"temperature": 0.0, "num_predict": 512},
        )
    except Exception as e:
        return ReceiptExtraction(
            confidence="low",
            notes=f"ollama call failed: {type(e).__name__}: {e}",
        )

    raw = ((resp.get("message") or {}).get("content") or "").strip()
    # Strip code fences just like the cloud path
    raw_clean = re.sub(r"^```(?:json)?\s*", "", raw)
    raw_clean = re.sub(r"\s*```$", "", raw_clean).strip()
    try:
        data = json.loads(raw_clean)
    except json.JSONDecodeError:
        # Local models sometimes emit prose around the JSON; try grep.
        m = re.search(r"\{.*\}", raw_clean, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                data = {}
        else:
            data = {}

    return ReceiptExtraction(
        total=_to_float(data.get("total")),
        currency=(data.get("currency") or "").upper() or None,
        date_iso=data.get("date_iso"),
        vendor=data.get("vendor"),
        line_items=data.get("line_items") or [],
        confidence=data.get("confidence") or "low",
        notes=data.get("notes") or "",
        raw_response=raw,
        input_tokens=0,
        output_tokens=0,
        usd_cost=0.0,
    )


def extract_receipt_cascaded(
    image_path: Path,
    *,
    cloud_model: str = DEFAULT_MODEL,
    local_model: str = DEFAULT_LOCAL_MODEL,
    secret_ref: dict | None = None,
    overlay: dict | None = None,
    skill_name: str = "receipt-collector",
    step_name: str = "extract_receipt_amounts",
    local_first: bool = True,
) -> tuple[ReceiptExtraction, str]:
    """Cascade: try local Llava first; escalate to Haiku on low confidence.

    Returns (extraction, tier_used) where tier_used is one of:
      "local"        — Llava handled it (confidence high/medium)
      "cloud"        — Llava abstained or low-confidence; Haiku ran
      "cloud-only"   — local_first=False; only Haiku ran
      "failed"       — both tiers failed (rare; returns last attempt)

    Per #12 cascade upward only, the local tier ALWAYS runs first when
    `local_first=True`. The cloud tier is the long tail.
    """
    if local_first:
        local = extract_receipt_local(image_path, model=local_model,
                                      skill_name=skill_name, step_name=step_name)
        # Treat anything with high/medium confidence AND a parseable
        # total as resolved. Otherwise escalate to cloud.
        if local.total is not None and local.confidence in ("high", "medium"):
            return local, "local"
        # Escalate
        cloud = extract_receipt(
            image_path, model=cloud_model, secret_ref=secret_ref,
            overlay=overlay, skill_name=skill_name, step_name=step_name,
        )
        return cloud, "cloud"
    # No local pass; cloud only.
    cloud = extract_receipt(
        image_path, model=cloud_model, secret_ref=secret_ref,
        overlay=overlay, skill_name=skill_name, step_name=step_name,
    )
    return cloud, "cloud-only"
