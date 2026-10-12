"""Shared loader + checker for evals/intent_router_cases.json.

Used by validate_intents.py (live Gemini, optional Langfuse experiment) and
tests/test_evals.py (offline regressions), so both read the same cases.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

CASES_PATH = Path(__file__).with_name("intent_router_cases.json")


def load_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text())["cases"]


def build_household(case: dict):
    """In-memory store + member matching the case's `household`. Writes nothing real."""
    from app.db.instrumented import InstrumentedStore
    from app.db.memory import InMemoryStore
    from app.models import Clarification, LineItem, Receipt, ReceiptStatus
    from app.services.memory import ConversationMemory

    store = InstrumentedStore(InMemoryStore())
    fam = store.get_or_create_default_family()
    member = store.create_member(fam.id, "Aisha", "+2348000000000")
    cats = {c.name: c for c in store.list_categories()}
    r1 = store.create_receipt(Receipt(member_id=member.id, vendor="Shoprite",
                                      purchase_date=date.today(), status=ReceiptStatus.CONFIRMED))
    store.create_line_items([
        LineItem(receipt_id=r1.id, name="Rice 10kg", price=Decimal("30000"),
                 category_id=cats["Groceries"].id, position=1),
        LineItem(receipt_id=r1.id, name="Peak milk", price=Decimal("1500"),
                 category_id=cats["Groceries"].id, position=2),
    ])
    if case.get("household") == "open":
        r2 = store.create_receipt(Receipt(member_id=member.id, vendor="Spar",
                                          purchase_date=date.today(), status=ReceiptStatus.NEEDS_REVIEW))
        items = [LineItem(receipt_id=r2.id, name=n, price=Decimal(p), category_id=cats["Groceries"].id,
                          position=i, needs_review=i in (2, 5))
                 for i, (n, p) in enumerate([("Bread", 1200), ("Mlk", 900), ("Soap", 800),
                                             ("Tea", 700), ("Sugar", 1500)], start=1)]
        store.create_line_items(items)
        for li in items:
            if li.needs_review:
                store.create_clarification(Clarification(receipt_id=r2.id, line_item_id=li.id, question="?"))
    if case.get("awaiting_description"):
        empty = store.create_receipt(Receipt(member_id=member.id, status=ReceiptStatus.NEEDS_REVIEW))
        ConversationMemory(store).set_state(member.id, "awaiting_description", {"receipt_id": str(empty.id)})
    return store, member


def decision_summary(d: Any) -> dict:
    """The fields the checks look at, as plain JSON."""
    total = None
    if d.purchase and d.purchase.items and all(i.total_price is not None for i in d.purchase.items):
        total = float(sum(i.total_price for i in d.purchase.items))
    return {
        "intent": d.intent,
        "item_number": d.item_number,
        "item_name": d.item_name,
        "verdict": d.verdict,
        "price": float(d.patch.price) if d.patch and d.patch.price is not None else None,
        "category": (d.patch.category if d.patch and d.patch.category else None)
                    or (d.query.category if d.query else None),
        "scope": d.query.scope if d.query else None,
        "period": d.query.period if d.query else None,
        "item": d.query.item if d.query else None,
        "item_terms": [t.lower() for t in d.query.item_terms] if d.query else [],
        "purchase_total": total,
        "remembers": bool(d.remember),
        "confidence": d.confidence,
        "source": d.source,
    }


def check(expect: dict, got: dict) -> list[str]:
    """Mismatches between expected and actual fields (empty = pass)."""
    problems = []
    for key, want in expect.items():
        if key.endswith("_contains"):  # substring of a string field, case-insensitive
            have = got.get(key[: -len("_contains")]) or ""
            if want.lower() not in have.lower():
                problems.append(f"{key}: expected to contain {want!r}, got {have!r}")
            continue
        if key.endswith("_include"):  # some list entry contains it ("doughnut" in ["doughnut", "donut"])
            field = key[: -len("_include")]
            have = list(got.get(field) or [])
            if field == "item_terms" and got.get("item"):
                have.append(got["item"])  # the main item word counts as a search term too
            if not any(want.lower() in str(h).lower() for h in have):
                problems.append(f"{key}: expected an entry containing {want!r}, got {have!r}")
            continue
        have = got.get(key)
        if isinstance(want, str) and isinstance(have, str):
            ok = want.lower() == have.lower()
        elif isinstance(want, (int, float)) and not isinstance(want, bool) and have is not None:
            ok = float(have) == float(want)
        else:
            ok = have == want
        if not ok:
            problems.append(f"{key}: expected {want!r}, got {have!r}")
    return problems
