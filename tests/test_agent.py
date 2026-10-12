"""Conversation agent tests — one section per bug in architecture.md changelog v1.5,
plus memory, the confidence gate, grounding of answers, and typing.

Gemini is never called: `llm.generate` / `llm.generate_json` are replaced with a
scripted fake, so each test states exactly what the router "decided" and then
checks that the data only changed in the way the decision allows.
"""
import re
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pytest

from app.api.routes import webhooks
from app.db.instrumented import InstrumentedStore
from app.db.memory import InMemoryStore
from app.models import Clarification, LineItem, Receipt, ReceiptStatus, WhatsAppInboundMessage
from app.services import capture_pipeline, llm
from app.services.capture_pipeline import CapturePipeline, process_inbound_message

PHONE = "2348000000001"


class FakeWA:
    is_configured = False

    def __init__(self):
        self.sent = []
        self.events = []

    def send_text(self, to, text):
        self.sent.append(text)
        self.events.append(("send_text", text))

    @contextmanager
    def typing(self, message_id, to_phone):
        self.events.append(("typing_start", message_id))
        try:
            yield
        finally:
            self.events.append(("typing_end", message_id))


class FakeLLM:
    """Scripted Gemini. `router` is the JSON the intent router returns (or an
    exception to raise); `answer` is the phrased reply for spending questions."""

    def __init__(self, monkeypatch, wa=None):
        self.router = {"intent": "smalltalk", "confidence": 0.9, "reply": "hi"}
        self.routers = []  # optional queue, consumed first
        self.answer = "ok"
        self.summary = "They mostly ask about groceries."
        self.category = "Groceries"
        self.calls = []
        self.wa = wa

        def generate_json(name, parts, **kw):
            self._log(name)
            if name == "categorize-items":
                names = re.findall(r'^- "(.*)"$', parts[0], re.M)
                return {n: self.category for n in names}
            r = self.routers.pop(0) if self.routers else self.router
            if isinstance(r, Exception):
                raise r
            return dict(r)

        def generate(name, parts, **kw):
            self._log(name)
            if name == "answer-spending-question":
                if isinstance(self.answer, Exception):
                    raise self.answer
                return self.answer
            if name == "summarize-conversation":
                return self.summary
            if name == "categorize-item":
                return self.category
            raise AssertionError(f"unexpected LLM call {name}")

        monkeypatch.setattr(llm, "generate_json", generate_json)
        monkeypatch.setattr(llm, "generate", generate)

    def _log(self, name):
        self.calls.append(name)
        if self.wa is not None:
            self.wa.events.append(("llm", name))


@pytest.fixture(autouse=True)
def _reset_onboarding():
    capture_pipeline._PENDING_ONBOARD.clear()


@pytest.fixture
def env(monkeypatch):
    # Same wrapper get_store() uses in production: cache (memory layer 1) + DB spans.
    store = InstrumentedStore(InMemoryStore())
    wa = FakeWA()
    fake = FakeLLM(monkeypatch, wa)
    fam = store.get_or_create_default_family()
    member = store.create_member(fam.id, "Aisha", PHONE)
    pipe = CapturePipeline(store, wa)

    class Env:
        pass

    e = Env()
    e.store, e.wa, e.llm, e.member, e.pipe = store, wa, fake, member, pipe
    e.cats = {c.name: c for c in store.list_categories()}
    return e


def add_receipt(e, vendor, items, status=ReceiptStatus.CONFIRMED, unclear=()):
    """items: list of (name, price, category). `unclear`: positions with an open clarification."""
    r = e.store.create_receipt(Receipt(member_id=e.member.id, vendor=vendor, status=status,
                                       purchase_date=date.today()))
    lis = [
        LineItem(receipt_id=r.id, name=n, price=Decimal(p), category_id=e.cats[c].id,
                 position=i, needs_review=i in unclear, confidence=0.4 if i in unclear else 0.95)
        for i, (n, p, c) in enumerate(items, start=1)
    ]
    e.store.create_line_items(lis)
    for li in lis:
        if li.position in unclear:
            e.store.create_clarification(Clarification(
                receipt_id=r.id, line_item_id=li.id, question=f"item {li.position}?"))
    return r, lis


def say(e, text):
    return e.pipe.handle_text(PHONE, text)


def item(e, li):
    return e.store.get_line_item(li.id)


# ---------------------------------------------------------------------------
# Bug 1 — describing a purchase in words must never edit the last receipt
# ---------------------------------------------------------------------------

def test_bug1_purchase_in_words_logs_new_receipt_not_correction(env):
    r, lis = add_receipt(env, "Shoprite", [("Rice", 30000, "Groceries"), ("Milk", 1500, "Groceries")])
    env.llm.router = {
        "intent": "log_purchase", "confidence": 0.95,
        "purchase": {"vendor": None, "items": [{"name": "Eggs (crate)", "quantity": 2, "total_price": 4500}]},
    }
    reply = say(env, "bought 2 crates of eggs for 4500")

    assert item(env, lis[1]).price == Decimal("1500")  # item 2 untouched
    receipts = env.store.list_receipts(family_id=env.member.family_id)
    assert len(receipts) == 2
    new = next(x for x in receipts if x.id != r.id)
    new_items = env.store.list_line_items(new.id)
    assert [(i.name, i.price, i.quantity) for i in new_items] == [("Eggs (crate)", Decimal("4500.00"), Decimal("2.00"))]
    assert new.status == ReceiptStatus.CONFIRMED
    assert new.raw_extraction["source"] == "text"
    assert "Eggs (crate)" in reply and "₦4,500" in reply
    assert "Correction applied" not in reply


def test_bug1_description_after_unreadable_photo_fills_that_receipt(env, monkeypatch):
    from app.models import ExtractionResult
    monkeypatch.setattr(capture_pipeline, "extract_receipt",
                        lambda b: ExtractionResult(overall_confidence=0.0, extraction_notes="blurry"))
    add_receipt(env, "Shoprite", [("Rice", 30000, "Groceries"), ("Milk", 1500, "Groceries")])
    env.pipe.process_image(PHONE, b"\xff\xd8\xff blurry", "img1")
    assert "describe the purchase" in env.wa.sent[-1]
    blank = env.store.list_receipts(family_id=env.member.family_id)[0]
    assert env.pipe.agent.memory.state(env.member.id)["awaiting_description"] == {"receipt_id": str(blank.id)}
    assert blank.status == ReceiptStatus.NEEDS_REVIEW and not env.store.list_line_items(blank.id)

    env.llm.router = {
        "intent": "log_purchase", "confidence": 0.9,
        "purchase": {"vendor": "Mama Nkechi", "items": [{"name": "Eggs", "quantity": 2, "total_price": 4500}]},
    }
    say(env, "bought 2 crates of eggs for 4500 from mama nkechi")

    filled = env.store.get_receipt(blank.id)
    assert filled.status == ReceiptStatus.CONFIRMED
    assert filled.vendor == "Mama Nkechi"
    assert filled.raw_extraction["photo_extraction"]["extraction_notes"] == "blurry"
    assert [i.name for i in env.store.list_line_items(blank.id)] == ["Eggs"]
    assert len(env.store.list_receipts(family_id=env.member.family_id)) == 2  # no orphan row
    assert "awaiting_description" not in env.pipe.agent.memory.state(env.member.id)


def test_bug1_purchase_without_price_asks_instead_of_saving(env):
    env.llm.router = {"intent": "log_purchase", "confidence": 0.9,
                      "purchase": {"items": [{"name": "bread", "quantity": 1, "total_price": None}]}}
    reply = say(env, "bought bread")
    assert reply == "How much was the bread?"
    assert env.store.list_receipts(family_id=env.member.family_id) == []


def test_bug1_fallback_never_edits_on_free_text(env):
    """Gemini down: the strict parser must not read '2' as an item number."""
    r, lis = add_receipt(env, "Shoprite", [("Rice", 30000, "Groceries"), ("Milk", 1500, "Groceries")])
    env.llm.router = RuntimeError("gemini down")
    reply = say(env, "bought 2 crates of eggs for 4500")
    assert item(env, lis[1]).price == Decimal("1500")
    assert "haven't changed anything" in reply


# ---------------------------------------------------------------------------
# Bug 2 — a question while a clarification is open is answered, not saved as the answer
# ---------------------------------------------------------------------------

def test_bug2_question_during_clarification_is_answered_and_clarification_stays_open(env):
    add_receipt(env, "Shoprite", [("Rice", 30000, "Groceries")])  # confirmed
    r, lis = add_receipt(env, "Spar", [("Bread", 1200, "Groceries"), ("Soap", 800, "Household"),
                                       ("Biscuit", 500, "Dining")],
                         status=ReceiptStatus.NEEDS_REVIEW, unclear=(3,))
    env.llm.router = {"intent": "spending_question", "confidence": 0.95,
                      "query": {"scope": "family", "period": "all_time", "category": "Groceries"}}
    env.llm.answer = "You've spent ₦30,000 on Groceries so far."
    reply = say(env, "how much have we spent on groceries?")

    assert "₦30,000" in reply
    assert "still waiting on item 3 (Biscuit)" in reply
    open_now = env.store.list_open_clarifications_for_member(env.member.id)
    assert len(open_now) == 1 and open_now[0].answer is None
    assert item(env, lis[2]).category_id == env.cats["Dining"].id  # NOT changed to Groceries
    assert env.store.get_receipt(r.id).status == ReceiptStatus.NEEDS_REVIEW
    assert "confirmed" not in reply


def test_bug2_fallback_question_during_clarification(env):
    r, lis = add_receipt(env, "Spar", [("Bread", 1200, "Groceries"), ("Soap", 800, "Household"),
                                       ("Biscuit", 500, "Dining")],
                         status=ReceiptStatus.NEEDS_REVIEW, unclear=(3,))
    env.llm.router = RuntimeError("gemini down")
    reply = say(env, "how much have we spent on groceries?")
    assert "Spending for the whole family" in reply
    assert len(env.store.list_open_clarifications_for_member(env.member.id)) == 1
    assert item(env, lis[2]).category_id == env.cats["Dining"].id


def test_bug2_real_answer_resolves_and_confirms(env):
    r, lis = add_receipt(env, "Spar", [("Bread", 1200, "Groceries"), ("Biscuit", 500, "Dining")],
                         status=ReceiptStatus.NEEDS_REVIEW, unclear=(2,))
    env.llm.router = {"intent": "answer_clarification", "confidence": 0.95, "item_number": 2, "verdict": "correct"}
    reply = say(env, "yes that's right")
    assert env.store.list_open_clarifications_for_member(env.member.id) == []
    assert env.store.get_receipt(r.id).status == ReceiptStatus.CONFIRMED
    assert "item 2 (Biscuit) confirmed" in reply and "now confirmed" in reply


# ---------------------------------------------------------------------------
# Bug 3 — with two open clarifications, a correction hits the item it names
# ---------------------------------------------------------------------------

def _two_open(e):
    return add_receipt(e, "Spar", [("Bread", 1200, "Groceries"), ("Mlk", 900, "Groceries"),
                                   ("Soap", 800, "Household"), ("Tea", 700, "Groceries"),
                                   ("Sugar", 1500, "Groceries")],
                       status=ReceiptStatus.NEEDS_REVIEW, unclear=(2, 5))


def test_bug3_correction_targets_named_item(env):
    r, lis = _two_open(env)
    env.llm.router = {"intent": "answer_clarification", "confidence": 0.95, "item_number": 5,
                      "verdict": "fix", "patch": {"price": 1800}}
    reply = say(env, "item 5 price was 1800")
    assert item(env, lis[4]).price == Decimal("1800.00")
    assert item(env, lis[1]).price == Decimal("900")  # item 2 untouched
    open_now = env.store.list_open_clarifications_for_member(env.member.id)
    assert [env.store.get_line_item(c.line_item_id).position for c in open_now] == [2]
    assert env.store.get_receipt(r.id).status == ReceiptStatus.NEEDS_REVIEW
    assert "₦1,500 → ₦1,800" in reply and "item 2 (Mlk" in reply


def test_bug3_router_says_correct_item_but_item_is_open_clarification(env):
    r, lis = _two_open(env)
    env.llm.router = {"intent": "correct_item", "confidence": 0.95, "item_number": 5, "patch": {"price": "1,800"}}
    say(env, "item 5 was 1800")
    assert item(env, lis[4]).price == Decimal("1800.00")
    assert item(env, lis[1]).price == Decimal("900")
    assert len(env.store.list_open_clarifications_for_member(env.member.id)) == 1


def test_bug3_unnumbered_answer_with_two_open_asks_which(env):
    r, lis = _two_open(env)
    env.llm.router = {"intent": "answer_clarification", "confidence": 0.9, "verdict": "fix",
                      "patch": {"price": 1800}}
    reply = say(env, "the price was 1800")
    assert reply.startswith("Which item is that for?")
    assert item(env, lis[1]).price == Decimal("900") and item(env, lis[4]).price == Decimal("1500")
    assert len(env.store.list_open_clarifications_for_member(env.member.id)) == 2


def test_bug3_fallback_routes_by_item_number(env):
    r, lis = _two_open(env)
    env.llm.router = RuntimeError("gemini down")
    say(env, "item 5 price was 1800")
    assert item(env, lis[4]).price == Decimal("1800.00")
    assert item(env, lis[1]).price == Decimal("900")
    say(env, "ok 2")
    assert env.store.list_open_clarifications_for_member(env.member.id) == []
    assert env.store.get_receipt(r.id).status == ReceiptStatus.CONFIRMED


# ---------------------------------------------------------------------------
# Validation + confidence gate
# ---------------------------------------------------------------------------

def test_correct_item_out_of_range_changes_nothing(env):
    r, lis = add_receipt(env, "Shoprite", [("Rice", 30000, "Groceries")])
    env.llm.router = {"intent": "correct_item", "confidence": 0.95, "item_number": 4, "patch": {"price": 500}}
    reply = say(env, "item 4 was 500")
    assert "no item 4" in reply
    assert item(env, lis[0]).price == Decimal("30000")


def test_unknown_category_asks(env):
    r, lis = add_receipt(env, "Shoprite", [("Rice", 30000, "Groceries")])
    env.llm.router = {"intent": "correct_item", "confidence": 0.95, "item_number": 1, "patch": {"category": "Food"}}
    reply = say(env, "item 1 is food")
    assert "don't have a 'Food' category" in reply
    assert item(env, lis[0]).category_id == env.cats["Groceries"].id


def test_correction_reply_says_exactly_what_changed_and_teaches_keywords(env):
    r, lis = add_receipt(env, "Shoprite", [("Rice", 30000, "Groceries"), ("Zobo drink", 600, "Groceries")])
    env.llm.router = {"intent": "correct_item", "confidence": 0.95, "item_number": 2, "patch": {"category": "Dining"}}
    reply = say(env, "the zobo was dining")
    assert reply == "Updated item 2 on your Shoprite receipt: category Groceries → Dining ✅"
    assert "zobo" in env.store.list_categories()[[c.name for c in env.store.list_categories()].index("Dining")].keywords


def test_low_confidence_change_asks_first_then_applies_on_yes(env):
    r, lis = add_receipt(env, "Shoprite", [("Rice", 30000, "Groceries"), ("Milk", 1500, "Groceries")])
    env.llm.router = {"intent": "correct_item", "confidence": 0.5, "item_number": 2, "patch": {"price": 2000}}
    reply = say(env, "milk na 2k")
    assert reply.startswith("Just to be sure — update item 2 (Milk)")
    assert item(env, lis[1]).price == Decimal("1500")

    env.llm.router = {"intent": "confirm_pending", "confidence": 0.95}
    reply = say(env, "yes")
    assert item(env, lis[1]).price == Decimal("2000.00")
    assert "₦1,500 → ₦2,000" in reply
    assert "pending_action" not in env.pipe.agent.memory.state(env.member.id)


def test_low_confidence_change_cancelled(env):
    r, lis = add_receipt(env, "Shoprite", [("Milk", 1500, "Groceries")])
    env.llm.router = {"intent": "correct_item", "confidence": 0.4, "item_number": 1, "patch": {"price": 2000}}
    say(env, "milk 2k?")
    env.llm.router = {"intent": "cancel_pending", "confidence": 0.95}
    assert say(env, "no") == "Okay, I haven't changed anything."
    assert item(env, lis[0]).price == Decimal("1500")


# ---------------------------------------------------------------------------
# Answers are grounded in data
# ---------------------------------------------------------------------------

def test_answer_with_invented_amount_is_replaced_by_computed_summary(env):
    add_receipt(env, "Shoprite", [("Rice", 30000, "Groceries")])
    env.llm.router = {"intent": "spending_question", "confidence": 0.9, "query": {"scope": "me"}}
    env.llm.answer = "You've spent about ₦45,000."  # not in the data
    reply = say(env, "how much have I spent?")
    assert "₦45,000" not in reply
    assert "Total: ₦30,000" in reply


def test_needs_review_receipts_excluded_from_answers(env):
    add_receipt(env, "Shoprite", [("Rice", 30000, "Groceries")])
    add_receipt(env, "Spar", [("Bread", 1200, "Groceries")], status=ReceiptStatus.NEEDS_REVIEW, unclear=(1,))
    env.llm.router = RuntimeError("down")
    reply = say(env, "how much have I spent")
    assert "Total: ₦30,000" in reply and "aren't counted yet" in reply


# ---------------------------------------------------------------------------
# 3-layer memory: cache (layer 1), conversation state (layer 2), long-term (layer 3)
# ---------------------------------------------------------------------------

def test_cache_serves_hot_reads_and_drops_them_on_write(env):
    cache = env.store.cache
    cache.clear()
    env.llm.router = {"intent": "help", "confidence": 0.99}
    say(env, "help")
    misses = cache.misses
    say(env, "help")
    assert cache.misses == misses and cache.hits > 0  # categories/members/memories from cache

    # A write through the store makes the cached copy stale -> dropped at once.
    groceries = env.cats["Groceries"]
    env.store.append_category_keywords(groceries.id, ["ugwu"])
    cats = {c.name: c for c in env.store.list_categories()}
    assert "ugwu" in cats["Groceries"].keywords


def test_cache_never_remembers_unknown_sender(env):
    stranger = "2348099999999"
    assert env.store.get_member_by_phone(stranger) is None
    fam = env.store.get_or_create_default_family()
    env.store.create_member(fam.id, "Tunde", stranger)
    assert env.store.get_member_by_phone(stranger).name == "Tunde"


def test_cache_returns_copies(env):
    cats = env.store.list_categories()
    cats[0].keywords.append("mutated-by-caller")
    assert "mutated-by-caller" not in env.store.list_categories()[0].keywords


def test_cache_does_not_hold_state_or_receipts(env):
    """Receipts, clarifications, turns and state slots are always read fresh."""
    from app.db.instrumented import CACHED
    assert set(CACHED) == {"get_or_create_default_family", "get_member_by_phone", "list_members",
                           "list_categories", "list_member_memories"}


def test_longterm_memory_visible_right_after_it_is_written(env):
    assert env.store.list_member_memories(env.member.id) == []  # now cached as empty
    env.llm.router = {"intent": "smalltalk", "confidence": 0.9, "reply": "Noted!",
                      "remember": ["We shop at Shoprite on Saturdays."]}
    say(env, "we always shop at shoprite on saturdays")
    assert [m.content for m in env.store.list_member_memories(env.member.id)] == [
        "We shop at Shoprite on Saturdays."]


def test_state_recent_turns_recorded_and_fed_to_router(env, monkeypatch):
    seen_prompts = []
    orig = llm.generate_json

    def spy(name, parts, **kw):
        seen_prompts.append(parts[0])
        return orig(name, parts, **kw)

    monkeypatch.setattr(llm, "generate_json", spy)
    env.llm.router = {"intent": "smalltalk", "confidence": 0.9, "reply": "Hello Aisha!"}
    say(env, "hello")
    say(env, "how are you")
    turns = env.store.list_conversation_turns(env.member.id)
    assert [(t.role, t.content) for t in turns][:2] == [("user", "hello"), ("assistant", "Hello Aisha!")]
    assert "Member: hello" in seen_prompts[1] and "Bot: Hello Aisha!" in seen_prompts[1]


def test_longterm_facts_remembered_deduped_and_in_prompt(env, monkeypatch):
    env.llm.router = {"intent": "smalltalk", "confidence": 0.9, "reply": "Noted!",
                      "remember": ["Our grocery budget is ₦60,000 a week."]}
    say(env, "our grocery budget is 60k a week")
    say(env, "remember that")
    facts = [m.content for m in env.store.list_member_memories(env.member.id) if m.kind == "fact"]
    assert facts == ["Our grocery budget is ₦60,000 a week."]
    ctx = env.pipe.agent._build_context(env.member)
    assert "grocery budget is ₦60,000" in env.pipe.agent._context_for_prompt(ctx)


def test_longterm_budget_amount_allowed_in_answer(env):
    add_receipt(env, "Shoprite", [("Rice", 30000, "Groceries")])
    env.llm.router = {"intent": "smalltalk", "confidence": 0.9, "reply": "Noted!",
                      "remember": ["Grocery budget is ₦60,000 a month."]}
    say(env, "grocery budget is 60k a month")
    env.llm.router = {"intent": "spending_question", "confidence": 0.9,
                      "query": {"scope": "me", "category": "Groceries"}}
    env.llm.answer = "₦30,000 on Groceries — half of your ₦60,000 budget."
    assert say(env, "am I within budget?").startswith("₦30,000 on Groceries")


def test_state_turns_compact_into_longterm_summary(env, monkeypatch):
    agent = env.pipe.agent
    agent.memory.compact_after = 6
    agent.memory.recent_turns = 2
    env.llm.router = {"intent": "smalltalk", "confidence": 0.9, "reply": "hey"}
    for i in range(4):
        say(env, f"msg {i}")  # 8 turns -> compaction
    turns = env.store.list_conversation_turns(env.member.id)
    assert len(turns) <= 6
    summaries = [m for m in env.store.list_member_memories(env.member.id) if m.kind == "summary"]
    assert len(summaries) == 1 and summaries[0].content == env.llm.summary


def test_state_slot_expires(env):
    mem = env.pipe.agent.memory
    mem.set_state(env.member.id, "awaiting_description", {"receipt_id": "x"})
    assert "awaiting_description" in mem.state(env.member.id)
    raw = env.store.get_member_state(env.member.id)
    raw["awaiting_description"]["set_at"] = "2020-01-01T00:00:00"
    env.store.save_member_state(env.member.id, raw)
    assert mem.state(env.member.id) == {}


# ---------------------------------------------------------------------------
# Typing shows for the whole time the agent is thinking
# ---------------------------------------------------------------------------

def test_typing_covers_llm_calls_until_reply(env):
    add_receipt(env, "Shoprite", [("Rice", 30000, "Groceries")])
    env.llm.router = {"intent": "spending_question", "confidence": 0.9, "query": {"scope": "me"}}
    env.llm.answer = "₦30,000 so far."
    msg = WhatsAppInboundMessage(from_phone=PHONE, message_id="wamid.Q", text="how much?")
    webhooks._handle_message(env.store, env.wa, msg)
    kinds = [e[0] if e[0] != "llm" else f"llm:{e[1]}" for e in env.wa.events]
    assert kinds == ["typing_start", "llm:route-intent", "llm:answer-spending-question",
                     "send_text", "typing_end"]


def test_one_reply_per_message(env):
    env.llm.router = {"intent": "help", "confidence": 0.99}
    process_inbound_message(env.pipe, WhatsAppInboundMessage(from_phone=PHONE, message_id="m", text="help"), env.wa)
    assert len(env.wa.sent) == 1


# ---------------------------------------------------------------------------
# Langfuse: one trace per message, a generation per Gemini call, no PII
# ---------------------------------------------------------------------------

def test_langfuse_trace_structure(env, monkeypatch):
    pytest.importorskip("langfuse")
    from langfuse import Langfuse
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from app import observability

    exporter = InMemorySpanExporter()
    client = Langfuse(public_key="pk-lf-test", secret_key="sk-lf-test", base_url="http://localhost:1",
                      span_exporter=exporter, flush_at=1)
    monkeypatch.setattr(observability, "get_langfuse", lambda: client)

    # Real llm.generate path (with a fake Gemini model) so the generation span is the real one.
    monkeypatch.undo()
    monkeypatch.setattr(observability, "get_langfuse", lambda: client)

    class Resp:
        text = '{"intent": "help", "confidence": 0.99, "reasoning": "asked for help"}'
        usage_metadata = type("U", (), {"prompt_token_count": 120, "candidates_token_count": 9})()

    class Model:
        def generate_content(self, parts, **kw):
            return Resp()

    from app.services import extraction
    monkeypatch.setattr(extraction, "_gemini_model", lambda name="x": Model())
    process_inbound_message(env.pipe, WhatsAppInboundMessage(from_phone=PHONE, message_id="m", text="help"), env.wa)
    client.flush()

    finished = exporter.get_finished_spans()
    spans = {s.name: s for s in finished}
    assert {"whatsapp-message", "route", "route-intent", "act:help"} <= set(spans)
    root, route, gen, act = (spans[n] for n in ("whatsapp-message", "route", "route-intent", "act:help"))
    # message -> route (decision) -> Gemini call; message -> act (what code did)
    assert route.parent.span_id == root.context.span_id
    assert gen.parent.span_id == route.context.span_id
    assert act.parent.span_id == root.context.span_id
    assert '"intent": "help"' in route.attributes["langfuse.observation.output"]
    # Every database call is a span under the same trace, with latency (start/end).
    db = [s for s in finished if s.name.startswith("db.")]
    assert {"db.get_member_by_phone", "db.list_categories", "db.list_open_clarifications_for_member"} <= {
        s.name for s in db}
    assert all(s.context.trace_id == root.context.trace_id and s.end_time > s.start_time for s in db)
    flat = " ".join(f"{k}={v}" for s in finished for k, v in s.attributes.items())
    assert PHONE not in flat  # phone numbers never leave the server
    assert observability.user_hash(PHONE) in flat
    assert "gemini-3.5-flash-lite" in flat


def test_pending_change_dropped_if_next_message_is_something_else(env):
    r, lis = add_receipt(env, "Shoprite", [("Rice", 30000, "Groceries"), ("Milk", 1500, "Groceries")])
    env.llm.router = {"intent": "correct_item", "confidence": 0.5, "item_number": 2, "patch": {"price": 2000}}
    say(env, "milk na 2k")
    env.llm.router = {"intent": "spending_question", "confidence": 0.9, "query": {"scope": "me"}}
    env.llm.answer = "₦31,500 so far."
    say(env, "how much have I spent")
    env.llm.router = {"intent": "confirm_pending", "confidence": 0.9}
    assert say(env, "yes") == "Nothing is waiting on a yes from you right now 🙂"
    assert item(env, lis[1]).price == Decimal("1500")


def test_smalltalk_reply_with_made_up_amount_is_replaced(env):
    env.llm.router = {"intent": "smalltalk", "confidence": 0.9, "reply": "You've spent ₦12,000 this week!"}
    reply = say(env, "hey")
    assert "₦12,000" not in reply and "didn't quite get that" in reply


# ---------------------------------------------------------------------------
# Totals come from the database (spending_breakdown), never from the LLM
# ---------------------------------------------------------------------------

def test_spending_totals_come_only_from_store_breakdown(env, monkeypatch):
    add_receipt(env, "Shoprite", [("Rice", 30000, "Groceries")])
    calls = []
    real = env.store.spending_breakdown

    def spy(*a, **kw):
        calls.append(kw)
        # Whatever the database says is what the member is told — nothing re-sums it.
        return {**real(*a, **kw), "total": Decimal("99999")}

    monkeypatch.setattr(env.store, "spending_breakdown", spy)
    env.llm.router = {"intent": "spending_question", "confidence": 0.9,
                      "query": {"scope": "family", "period": "this_month", "category": "Groceries"}}
    env.llm.answer = "The family spent ₦99,999 on Groceries this month."
    assert say(env, "how much have we spent on groceries this month?") == env.llm.answer
    env.llm.router = RuntimeError("Gemini down")  # deterministic fallback reads the same numbers
    assert "Total: ₦99,999" in say(env, "how much have we spent on groceries this month?")
    assert calls[0]["member_id"] is None and calls[0]["category_id"] == env.cats["Groceries"].id
    assert calls[0]["start"] == date.today().replace(day=1)


def test_breakdown_filters_and_excludes_needs_review(env):
    fam = env.store.get_or_create_default_family()
    other = env.store.create_member(fam.id, "Bayo", "2348000000002")
    add_receipt(env, "Shoprite", [("Rice", 30000, "Groceries"), ("Taxi", 2000, "Transport")])
    add_receipt(env, "Spar", [("Eggs crate", 4500, "Groceries")], status=ReceiptStatus.NEEDS_REVIEW, unclear=(1,))
    r = env.store.create_receipt(Receipt(member_id=other.id, vendor="Market", status=ReceiptStatus.CONFIRMED,
                                         purchase_date=date.today()))
    env.store.create_line_items([LineItem(receipt_id=r.id, name="Eggs", price=Decimal("3000"),
                                          category_id=env.cats["Groceries"].id, position=1)])

    fam_b = env.store.spending_breakdown(fam.id)
    assert fam_b["total"] == Decimal("35000") and fam_b["confirmed_receipts"] == 2
    assert [(m["name"], m["amount"]) for m in fam_b["by_member"]] == [
        ("Aisha", Decimal("32000")), ("Bayo", Decimal("3000"))]
    assert fam_b["pending_receipts"] == 1

    mine = env.store.spending_breakdown(fam.id, member_id=env.member.id, category_id=env.cats["Groceries"].id)
    assert mine["total"] == Decimal("30000")
    eggs = env.store.spending_breakdown(fam.id, item_terms=["EGGS"])
    assert eggs["total"] == Decimal("3000")  # the needs_review eggs aren't counted


def test_supabase_breakdown_calls_sql_function_and_parses_numbers():
    from uuid import uuid4
    from app.db.supabase_store import SupabaseStore

    class RPC:
        def __init__(self, sink, name, params):
            sink.append((name, params))

        def execute(self):
            return type("R", (), {"data": {
                "total": 31500.5, "confirmed_receipts": 2,
                "by_category": [{"name": "Groceries", "amount": 31500.5}], "by_member": [],
                "top_vendors": [{"name": "Shoprite", "amount": 31500.5, "receipts": 2}],
                "top_items": [{"item": "Rice", "amount": 30000, "category": "Groceries",
                               "vendor": "Shoprite", "date": "2026-10-01"}],
                "pending_receipts": 0, "pending_amount": 0}})()

    sink = []
    store = SupabaseStore.__new__(SupabaseStore)
    store._client = type("C", (), {"rpc": lambda self, name, params: RPC(sink, name, params)})()
    fam = uuid4()
    out = store.spending_breakdown(fam, start=date(2026, 10, 1), item_terms=["Rice 10kg", "rice"])
    assert sink == [("spending_breakdown", {
        "p_family_id": str(fam), "p_member_id": None, "p_start": "2026-10-01", "p_end": None,
        "p_category_id": None, "p_items": ["rice10kg", "rice"]})]
    assert out["total"] == Decimal("31500.50") and out["top_items"][0]["amount"] == Decimal("30000.00")


# ---------------------------------------------------------------------------
# v1.7 — feedback from live testing
# ---------------------------------------------------------------------------

def add_dated_receipt(e, vendor, items, when, created_days_ago=30):
    """A confirmed receipt with a real purchase date, logged in the past (every item at position 1,
    like receipts saved before migration 0001)."""
    from datetime import datetime, timedelta
    r = e.store.create_receipt(Receipt(member_id=e.member.id, vendor=vendor, status=ReceiptStatus.CONFIRMED,
                                       purchase_date=when,
                                       created_at=datetime.utcnow() - timedelta(days=created_days_ago)))
    lis = [LineItem(receipt_id=r.id, name=n, price=Decimal(p), category_id=e.cats[c].id, position=1)
           for n, p, c in items]
    e.store.create_line_items(lis)
    return r, lis


def test_category_edit_by_name_reaches_older_receipts(env):
    """Live bug: 'Move Schweppes chapman from personal to groceries' always got 'Which item should I change?'
    because it was on an older receipt, not the last one."""
    old = date(2026, 8, 16)
    _, a = add_dated_receipt(env, "PRINCEEBEANO", [("SCHWEPPES CHAPMAN", 12338.40, "Personal")], old, 25)
    _, b = add_dated_receipt(env, "PRINCEEBEANO", [("SCHWEPPES CHAPMAN", 12338.40, "Personal")], old, 24)
    env.store.append_category_keywords(env.cats["Personal"].id, ["schweppes", "chapman"])
    _, last = add_receipt(env, "COLD STONE", [("Wallet Transfer", 14800, "Personal")])
    # The router even guesses item 1 — but item 1 on the last receipt is the Wallet Transfer.
    env.llm.router = {"intent": "correct_item", "confidence": 0.95, "item_number": 1,
                      "item_name": "Schweppes chapman", "patch": {"category": "Groceries"}}
    reply = say(env, "Move Schweppes chapman from personal to groceries")
    assert reply.startswith("Moved SCHWEPPES CHAPMAN from Personal to Groceries on 2 receipt lines")
    assert item(env, a[0]).category_id == item(env, b[0]).category_id == env.cats["Groceries"].id
    assert item(env, last[0]).category_id == env.cats["Personal"].id  # untouched
    cats = {c.name: c for c in env.store.list_categories()}
    assert "schweppes" in cats["Groceries"].keywords and "schweppes" not in cats["Personal"].keywords


def test_price_edit_by_name_with_several_lines_asks_which(env):
    old = date(2026, 8, 16)
    _, lis = add_dated_receipt(env, "PRINCEEBEANO", [("DE PRINCE DOUGHNUT", 650, "Groceries"),
                                                     ("DE PRINCE DOUGHNUT", 550, "Groceries")], old)
    env.llm.router = {"intent": "correct_item", "confidence": 0.95, "item_name": "doughnut", "patch": {"price": 600}}
    reply = say(env, "the doughnut was 600")
    assert "is on 2 receipt lines" in reply and "Which one should change?" in reply
    assert [item(env, li).price for li in lis] == [Decimal("650"), Decimal("550")]


def test_edit_by_name_not_found_changes_nothing(env):
    add_receipt(env, "Shoprite", [("Rice", 30000, "Groceries")])
    env.llm.router = {"intent": "correct_item", "confidence": 0.95, "item_name": "chapman", "patch": {"category": "Dining"}}
    assert "couldn't find an item called 'chapman'" in say(env, "chapman is dining")


def test_i_have_a_receipt_asks_for_the_photo(env):
    env.llm.router = {"intent": "expect_receipt", "confidence": 0.95}
    reply = say(env, "i have a receipt")
    assert reply.startswith("Great, send me a photo of the receipt")
    assert env.store.list_receipts(family_id=env.member.family_id) == []
    env.llm.router = RuntimeError("Gemini down")  # the offline parser knows it too
    assert say(env, "I have a receipt").startswith("Great, send me a photo")


def test_item_question_matches_spellings_and_says_when_last_bought(env):
    """Live bug: 'doughnuts last week' found nothing — the receipt says 'DE PRINCE DOUGH NUT' and was from August."""
    add_dated_receipt(env, "PRINCEEBEANO", [("DE PRINCE DOUGH NUT", 650, "Groceries"),
                                            ("DE PRINCE DOUGHNUT", 550, "Groceries"),
                                            ("MENTOS", 1990, "Groceries")], date(2026, 8, 16))
    env.llm.router = {"intent": "spending_question", "confidence": 0.95,
                      "query": {"scope": "me", "period": "last_week", "item": "doughnuts",
                                "item_terms": ["doughnut", "donut", "dough nut"]}}
    env.llm.answer = RuntimeError("phrasing down")  # templated answer shows the data itself
    reply = say(env, "How much did I spend on doughnuts last week?")
    assert "No confirmed receipts match that yet." in reply
    assert "Last bought on 2026-08-16 (₦1,200 all time)." in reply

    env.llm.router["query"]["period"] = "all_time"
    assert "Total: ₦1,200 across 1 receipt(s)" in say(env, "how much on doughnuts ever?")


def test_item_search_terms_add_singular():
    from app.services.spending import item_search_terms
    assert item_search_terms("doughnuts", ["donut"]) == ["doughnuts", "doughnut", "donut"]
    assert item_search_terms("berries") == ["berries", "berry"]
    assert item_search_terms("glass") == ["glass"]


def test_receipt_confirmation_shows_purchase_date(env):
    r, lis = add_dated_receipt(env, "COLD STONE", [("Ice cream", 14800, "Dining")], date(2026, 9, 17))
    env.llm.router = {"intent": "show_last_receipt", "confidence": 0.95}
    assert "from COLD STONE, dated 17 Sep 2026." in say(env, "show my last receipt")


def test_receipt_items_the_rules_miss_go_to_gemini_in_one_call(env, monkeypatch):
    """Live trace: a 35-item receipt made 14 categorize calls one after another (~18s,
    and over the free tier's 15 requests/minute). Now it's one call."""
    from app.models import ExtractedLineItem, ExtractionResult

    names = ["QWALPHA", "QWBETA", "QWGAMMA", "Rice 5kg"]  # Rice is caught by the keyword rules
    monkeypatch.setattr(capture_pipeline, "extract_receipt", lambda b: ExtractionResult(
        vendor="Spar", date=date(2026, 10, 1), total=Decimal("400"), overall_confidence=0.95,
        line_items=[ExtractedLineItem(name=n, quantity=1, total_price=Decimal("100"), confidence=0.95)
                    for n in names]))
    env.llm.category = "Household"
    reply = env.pipe.process_image(PHONE, b"img", "img-1")
    assert env.llm.calls.count("categorize-items") == 1 and "categorize-item" not in env.llm.calls
    assert "QWALPHA · ₦100 · Household" in reply and "Rice 5kg · ₦100 · Groceries" in reply


def test_categorization_failure_files_items_under_other(env, monkeypatch):
    from app.models import ExtractedLineItem, ExtractionResult

    monkeypatch.setattr(capture_pipeline, "extract_receipt", lambda b: ExtractionResult(
        vendor="Spar", total=Decimal("100"), overall_confidence=0.95,
        line_items=[ExtractedLineItem(name="QWDELTA", quantity=1, total_price=Decimal("100"), confidence=0.95)]))
    monkeypatch.setattr(capture_pipeline, "categorize_many_with_llm",
                        lambda names: (_ for _ in ()).throw(RuntimeError("429 quota")))
    assert "QWDELTA · ₦100 · Other" in env.pipe.process_image(PHONE, b"img", "img-2")
