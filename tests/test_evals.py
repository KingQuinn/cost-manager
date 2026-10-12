"""Offline regressions from the shared eval set (evals/intent_router_cases.json).

validate_intents.py runs these same cases against live Gemini. Here Gemini is
"down" (the router raises), so every case goes through the strict fallback
parser — and the three bugs must still be impossible: a purchase in words
never edits a receipt, a question never answers a clarification, and a
numbered correction only touches the item it names.
"""
import pytest

from app.models import WhatsAppInboundMessage
from app.services import llm
from app.services.capture_pipeline import CapturePipeline, process_inbound_message
from evals.intent_cases import build_household, check, decision_summary, load_cases

CASES = load_cases()
BUG_CASES = [c for c in CASES if {"bug1", "bug2", "bug3"} & set(c.get("tags", []))]


class SilentWA:
    is_configured = False

    def __init__(self):
        self.sent = []

    def send_text(self, to, text):
        self.sent.append(text)


def snapshot(store, member):
    items = {}
    for r in store.list_receipts(family_id=member.family_id):
        for li in store.list_line_items(r.id):
            items[li.id] = (li.name, li.price, li.category_id, li.needs_review)
    open_ids = {c.id for c in store.list_open_clarifications_for_member(member.id)}
    return items, open_ids


def test_eval_file_covers_all_three_bugs():
    tags = {t for c in CASES for t in c.get("tags", [])}
    assert {"bug1", "bug2", "bug3"} <= tags
    assert len({c["id"] for c in CASES}) == len(CASES)


@pytest.mark.parametrize("case", BUG_CASES, ids=lambda c: c["id"])
def test_bug_cases_never_change_the_wrong_data_when_gemini_is_down(case, monkeypatch):
    def down(*a, **k):
        raise RuntimeError("Gemini unavailable")

    monkeypatch.setattr(llm, "generate_json", down)
    monkeypatch.setattr(llm, "generate", down)
    store, member = build_household(case)
    before_items, before_open = snapshot(store, member)

    wa = SilentWA()
    process_inbound_message(CapturePipeline(store, wa),
                            WhatsAppInboundMessage(from_phone=member.phone_number, message_id=case["id"],
                                                   text=case["message"]), wa)
    after_items, after_open = snapshot(store, member)
    assert len(wa.sent) == 1

    changed = {i for i in before_items if before_items[i] != after_items.get(i)}
    named = case["expect"].get("item_number") if case["expect"].get("intent") == "answer_clarification" else None
    if named and case["expect"].get("price") is not None:
        # Bug 3: exactly the named item changed, and only its clarification closed.
        (only,) = changed
        assert store.get_line_item(only).position == named
        assert float(store.get_line_item(only).price) == case["expect"]["price"]
        assert len(before_open - after_open) == 1
    elif named:
        # "ok 2": item 2 confirmed as-is — only its needs_review flag clears.
        (only,) = changed
        assert store.get_line_item(only).position == named
        assert before_items[only][:3] == after_items[only][:3]
        assert len(before_open - after_open) == 1
    else:
        # Bugs 1 + 2 (and an unnumbered answer with two open): nothing edited, nothing closed.
        assert not changed
        assert after_open == before_open


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_checker_passes_a_router_that_returns_exactly_the_expected_decision(case):
    """Guards the eval harness itself: a perfect decision scores as a pass."""
    from app.services.agent import RouterDecision

    e = case["expect"]
    raw = {"intent": e.get("intent", "smalltalk"), "confidence": 0.95, "item_number": e.get("item_number"),
           "item_name": e.get("item_name_contains"),
           "verdict": e.get("verdict"), "remember": ["x"] if e.get("remembers") else []}
    if "price" in e or (e.get("category") and e.get("intent") != "spending_question"):
        raw["patch"] = {"price": e.get("price"), "category": e.get("category")}
    if e.get("intent") == "spending_question":
        raw["query"] = {k: e[k] for k in ("scope", "period", "category") if k in e}
        if "item_terms_include" in e:
            raw["query"]["item_terms"] = [e["item_terms_include"]]
    if "purchase_total" in e:
        raw["purchase"] = {"items": [{"name": "x", "total_price": e["purchase_total"]}]}
    assert check(e, decision_summary(RouterDecision.model_validate(raw))) == []
