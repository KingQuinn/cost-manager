from __future__ import annotations

import json
import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from app.config import get_settings
from app.models import DEFAULT_CATEGORIES, ExtractedLineItem, ExtractionResult

logger = logging.getLogger(__name__)

EXTRACTION_SYSTEM_PROMPT = """\
You are a receipt data extraction system. Given a photo of a receipt, extract \
structured data. Respond ONLY with valid JSON matching the schema below — \
no markdown fences, no commentary, no explanation.

Schema:
{
  "vendor": string | null,
  "date": string | null,        // YYYY-MM-DD
  "currency": "NGN",
  "total": number | null,
  "line_items": [
    {
      "name": string,
      "quantity": number,
      "unit_price": number | null,
      "total_price": number,
      "confidence": number       // 0.0-1.0
    }
  ],
  "overall_confidence": number,  // 0.0-1.0
  "extraction_notes": string | null
}

Rules:
- Amounts are numeric only, no currency symbols. Naira may appear as ₦, N, or \
NGN; normalize all of these away.
- If handwriting or print is unclear, still give your best guess, but lower \
that item's confidence score rather than omitting it.
- Never invent a line item that isn't visibly on the receipt.
- If the summed line items don't match the printed total, report both and \
note the mismatch in extraction_notes rather than silently correcting.
- If the image isn't a receipt at all, return overall_confidence: 0 and \
explain in extraction_notes.
"""

_CATEGORY_PROMPT_TEMPLATE = """\
You are a categorizer for a family expense tracker. Given the name of a single \
purchase line item, assign it to exactly one category from this list: \
{category_list}. Respond with ONLY the category name, nothing else.

Line item name: "{item_name}"
"""


_PLACEHOLDER_PREFIXES = ("your_", "optional_test_", "pick_a_random")


def _is_set(v) -> bool:
    if not v:
        return False
    s = str(v).strip().lower()
    if not s:
        return False
    for p in _PLACEHOLDER_PREFIXES:
        if s.startswith(p):
            return False
    return True


def _gemini_model(model_name: str = "gemini-3.5-flash-lite"):
    """Lazy import + configure Gemini once. Uses gemini-3.5-flash-lite default.

    The caller is responsible for ensuring GEMINI_API_KEY is set before the
    first call. If the SDK or key is missing, we raise a clear error rather
    than silently faking output.

    `model_name` is overridable so the same code path can benchmark against
    gemini-2.5-pro (architecture.md §7.3) without a second implementation.
    """
    try:
        import google.generativeai as genai
    except ImportError as e:  # pragma: no cover - deps missing
        raise RuntimeError(
            "google-generativeai is not installed. Run `pip install -r requirements.txt`."
        ) from e

    api_key = get_settings().gemini_api_key
    if not _is_set(api_key):
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add a real value to .env or the environment "
            "before calling Gemini. (Starts-with-placeholder values like "
            "'your_gemini_api_key_here' count as not-set.)"
        )

    genai.configure(api_key=api_key)
    return genai.GenerativeModel(model_name)


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _clean_json_response(text: str) -> str:
    """Strip markdown fences, trailing prose, BOM — anything that breaks json.loads."""
    if not text:
        return "{}"
    text = text.strip().lstrip("﻿")
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    # If the model added commentary after the JSON, find the first '{' and last '}'.
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        text = text[first : last + 1]
    return text


def _to_decimal(v) -> Optional[Decimal]:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _coerce_result(raw: dict) -> ExtractionResult:
    """Convert a raw dict (possibly with wrong types) into a validated ExtractionResult."""
    raw_items = raw.get("line_items") or []
    items: list[ExtractedLineItem] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        try:
            items.append(
                ExtractedLineItem(
                    name=str(it.get("name") or "Unnamed item").strip(),
                    quantity=Decimal(str(it.get("quantity") or 1)),
                    unit_price=_to_decimal(it.get("unit_price")),
                    total_price=_to_decimal(it.get("total_price")) or Decimal("0"),
                    confidence=max(0.0, min(1.0, float(it.get("confidence") or 0.5))),
                )
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Dropping malformed line item %r: %s", it, e)

    raw_date = raw.get("date")
    parsed_date: Optional[date] = None
    if isinstance(raw_date, str) and raw_date:
        try:
            parsed_date = date.fromisoformat(raw_date[:10])
        except ValueError:
            parsed_date = None

    return ExtractionResult(
        vendor=(raw.get("vendor") or None),
        date=parsed_date,
        currency=str(raw.get("currency") or "NGN"),
        total=_to_decimal(raw.get("total")),
        line_items=items,
        overall_confidence=max(0.0, min(1.0, float(raw.get("overall_confidence") or 0.0))),
        extraction_notes=(raw.get("extraction_notes") or None),
    )


def extract_receipt(
    image_bytes: bytes, mime_hint: Optional[str] = None, model_name: str = "gemini-3.5-flash-lite"
) -> ExtractionResult:
    """Run Gemini vision extraction on a receipt image.

    All of the "talk to Gemini for extraction" logic lives in this one
    function. If we ever swap providers or change the prompt, this is the
    only place that needs to change.

    Parameters
    ----------
    image_bytes:
        Raw bytes of the image.
    mime_hint:
        Optional MIME type (e.g. "image/jpeg"). If omitted, we'll guess from
        the bytes / magic numbers.
    """
    if mime_hint is None:
        # Tiny magic-number fallback for JPEG/PNG — enough for WhatsApp images
        # which are almost always JPEG.  mimetypes.guess_type on bytes isn't
        # a real API, so do this manually.
        if image_bytes.startswith(b"\xff\xd8\xff"):
            mime_hint = "image/jpeg"
        elif image_bytes.startswith(b"\x89PNG"):
            mime_hint = "image/png"
        elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
            mime_hint = "image/webp"
        else:
            mime_hint = "image/jpeg"

    model = _gemini_model(model_name)

    import base64

    prompt = [
        EXTRACTION_SYSTEM_PROMPT,
        {"mime_type": mime_hint, "data": base64.b64encode(image_bytes).decode("ascii")},
    ]

    logger.info("Calling Gemini extract_receipt (%d bytes, %s)", len(image_bytes), mime_hint)
    response = model.generate_content(prompt, request_options={"timeout": 60})
    text = response.text if hasattr(response, "text") else ""
    logger.debug("Gemini raw extraction response: %s", text)

    cleaned = _clean_json_response(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning("Gemini returned non-JSON, coercing to needs-review: %s", e)
        return ExtractionResult(
            overall_confidence=0.0,
            extraction_notes=f"Failed to parse extraction JSON: {e}. Raw: {text[:300]}",
        )

    return _coerce_result(data)


def categorize_with_llm(item_name: str) -> str:
    """Return one category name from DEFAULT_CATEGORIES for an unmatched item.

    Only used as the fallback in categorize.py when the rules-based keyword
    table has no match. Output is *just* the category name string, validated
    against the allowed list — if the model hallucinates, we fall back to
    "Other" rather than inventing a new category.
    """
    allowed = [name for name, _ in DEFAULT_CATEGORIES]
    allowed_lc = {name.lower(): name for name in allowed}

    prompt = _CATEGORY_PROMPT_TEMPLATE.format(
        category_list=", ".join(allowed),
        item_name=item_name,
    )

    model = _gemini_model()
    logger.info("Calling Gemini categorize_with_llm for %r", item_name)
    response = model.generate_content([prompt], request_options={"timeout": 30})
    raw = (response.text if hasattr(response, "text") else "").strip().strip('"').strip("'")
    raw_lc = raw.lower()
    if raw_lc in allowed_lc:
        return allowed_lc[raw_lc]
    # Sometimes the model adds punctuation or a short sentence. Try last
    # ditch: scan the response for any occurrence of an allowed name.
    for name in allowed:
        if name.lower() in raw_lc:
            return name
    logger.warning("LLM categorizer returned %r for %r; using Other", raw, item_name)
    return "Other"
