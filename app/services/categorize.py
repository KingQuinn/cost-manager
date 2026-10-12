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


MIN_KEYWORD_LEN = 3

# Words that say nothing about what an item is. Learned keywords used to include
# "k", "g", "per", "pack", "500ml" and similar, which matched almost anything.
_NOISE_WORDS = {
    "the", "and", "for", "with", "per", "pack", "piece", "pcs", "pkt", "kilo", "kilogram",
    "var", "variety", "varieties", "assorted", "original", "special", "real", "fresh",
    "big", "small", "mini", "medium", "large", "plus", "new", "red", "green", "blue",
    "black", "white", "pet", "can", "tin", "bottle", "sachet", "carton", "each",
}
_UNIT_RE = re.compile(r"^\d+([.,]\d+)?(ml|l|cl|g|kg|mg|oz|pcs|pc|x)?$|^#?\d+$|^\d+[a-z]{0,3}$")


def keyword_tokens(item_name: str) -> list[str]:
    """Words from an item name worth learning as category keywords: at least
    3 characters, not a size/unit ("500ml", "45cl"), not filler ("per", "pack")."""
    return [
        t for t in _tokenize(item_name)
        if len(t) >= MIN_KEYWORD_LEN and t not in _NOISE_WORDS and not _UNIT_RE.match(t)
    ]


def _keyword_matches(kw: str, name_lc: str) -> bool:
    # Keyword must start a word: "cola" matches "american cola" but not "chocolate";
    # truncated receipt words still match ("mayonnais" in "mayonnaise").
    return re.search(r"(?<![a-z0-9])" + re.escape(kw), name_lc) is not None


def categorize_by_rules(
    item_name: str,
    categories: list[Category],
) -> Optional[Category]:
    """Rules-first categorization against each category's keywords.

    A keyword matches when it starts a word in the item name (case-insensitive),
    e.g. "shoprite" matches "Shoprite Supermarket Ikeja". If keywords from
    several categories match, the longest keyword wins ("ice tea" beats "tea"),
    so the answer doesn't depend on the order categories come back from the DB.
    Keywords shorter than 3 characters are ignored.
    """
    name_lc = (item_name or "").lower()
    best: Optional[Category] = None
    best_len = 0
    for category in categories:
        for kw in category.keywords:
            kw_lc = (kw or "").strip().lower()
            if len(kw_lc) < MIN_KEYWORD_LEN or len(kw_lc) <= best_len:
                continue
            if _keyword_matches(kw_lc, name_lc):
                best, best_len = category, len(kw_lc)
    return best


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

    tokens = keyword_tokens(item_name)
    if tokens and persist_keywords is not None:
        try:
            persist_keywords(assigned.id, tokens)
            assigned.keywords = _dedupe_append(assigned.keywords, tokens)
        except Exception as e:  # pragma: no cover - persistence failure is non-fatal
            logger.warning("Failed to persist self-reinforcing keywords: %s", e)

    return assigned, True
