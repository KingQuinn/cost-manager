from __future__ import annotations

import logging
import re
from typing import Callable, Optional
from uuid import UUID

from app.models import Category, DEFAULT_CATEGORIES

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase + split into alphanumeric tokens. Good enough for keyword matching."""
    return _WORD_RE.findall((text or "").lower())


def build_default_categories() -> list[Category]:
    """Seed categories matching architecture.md §8.2 insert statement."""
    return [Category(name=name, keywords=list(keywords)) for name, keywords in DEFAULT_CATEGORIES]


def categorize_by_rules(
    item_name: str,
    categories: list[Category],
) -> Optional[Category]:
    """Rules-first categorization: substring match on keywords. First match wins.

    Lowercase both the item name and the keyword before comparing. Checks for
    substring match (not whole-word) so that e.g. a keyword of "shoprite"
    matches a line item "Shoprite Supermarket Ikeja".
    """
    name_lc = (item_name or "").lower()
    for category in categories:
        for kw in category.keywords:
            kw_lc = (kw or "").lower()
            if not kw_lc:
                continue
            if kw_lc in name_lc:
                return category
    return None


def _dedupe_append(existing: list[str], new: list[str]) -> list[str]:
    seen = set(w.lower() for w in existing)
    combined = list(existing)
    for w in new:
        if w.lower() not in seen:
            seen.add(w.lower())
            combined.append(w)
    return combined


def categorize_item(
    item_name: str,
    categories: list[Category],
    llm_fallback: Optional[Callable[[str], str]] = None,
    persist_keywords: Optional[Callable[[UUID, list[str]], None]] = None,
) -> tuple[Category, bool]:
    """Categorize a single line item.

    Returns (category, used_llm_fallback). Rules-first; LLM only on no match.

    If the LLM was used and `persist_keywords` is provided, the item's tokens
    are appended back into the assigned category's keyword list so future
    items with the same words resolve via rules instead of the LLM. This is
    the self-reinforcing design from architecture.md §11 / §7.4.
    """
    hit = categorize_by_rules(item_name, categories)
    if hit is not None:
        return hit, False

    if llm_fallback is None:
        # No LLM available — use Other as the final fallback.
        other = next((c for c in categories if c.name == "Other"), categories[-1])
        return other, False

    assigned_name = llm_fallback(item_name)
    assigned = next((c for c in categories if c.name == assigned_name), None)
    if assigned is None:
        # Shouldn't happen (categorize_with_llm guarantees allowed list), but be safe.
        assigned = next((c for c in categories if c.name == "Other"), categories[-1])

    tokens = _tokenize(item_name)
    if tokens and persist_keywords is not None:
        try:
            persist_keywords(assigned.id, tokens)
            assigned.keywords = _dedupe_append(assigned.keywords, tokens)
        except Exception as e:  # pragma: no cover - persistence failure is non-fatal
            logger.warning("Failed to persist self-reinforcing keywords: %s", e)

    return assigned, True
