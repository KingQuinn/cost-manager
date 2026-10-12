"""Conversation agent: decides what a text message means, then acts on real data.

Every text from a known member goes through three steps:

  1. ROUTE  — Gemini reads the message with the member's 3-layer memory and
              live data (open clarifications, last receipt, what we're waiting
              on) and returns a structured decision: which intent, which item,
              what change. The LLM decides intent; it never writes to the DB.
  2. CHECK  — Code validates that decision against the data before anything
              changes: the item exists, the clarification is actually open,
              the category is real, the price is a number. Anything ambiguous
              becomes a question back to the member, never a guess. Low-confidence
              changes are restated for a yes/no before they're applied.
  3. ACT    — Apply the change (or compute the numbers) and reply. Replies that
              confirm a change are written by code, so they say exactly what
              changed. Answers to spending questions are phrased by Gemini from
              numbers this module computed, and rejected if they cite an amount
              that isn't in that data.

If Gemini is unavailable, a strict fallback parser handles only exact command
shapes ("ok 3", "item 2 price was 1500", "help", "how much have I spent") and
otherwise changes nothing.

This replaces the regex correction parser behind three silent-data-change bugs
(architecture.md changelog v1.5):
  1. "bought 2 crates of eggs for 4500" edited item 2 of the last receipt.
  2. Any message while a clarification was open was saved as its answer.
  3. With two open clarifications, a correction hit whichever the DB returned first.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app import observability
from app.config import get_settings
from app.db import DataStore
from app.models import (
    Category,
    ConversationTurn,
    LineItem,
    Member,
    Receipt,
    ReceiptStatus,
)
from app.services import llm
from app.db.base import squash
from app.services.categorize import _tokenize, categorize_by_rules, categorize_item, keyword_tokens
from app.services.memory import ConversationMemory
from app.services.spending import (
    amounts_in,
    format_ngn,
    format_snapshot,
    norm_amount,
    spending_snapshot,
)
from app.services.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

INTENTS = (
    "answer_clarification",  # replying to a "did I get item N right?" question
    "correct_item",          # changing an item on an already-logged receipt
    "log_purchase",          # describing a NEW purchase in words
    "spending_question",     # asking about spend — answered from the database
    "show_last_receipt",
    "expect_receipt",        # "I have a receipt" — ask for the photo
    "help",
    "confirm_pending",       # "yes" to a change we proposed
    "cancel_pending",        # "no" to a change we proposed
    "smalltalk",
    "unclear",
)
MUTATING = {"answer_clarification", "correct_item", "log_purchase"}

# Only used when Gemini didn't write its own reply for expect_receipt.
EXPECT_RECEIPT_TEXT = (
    "Sure, send it over: a photo or a PDF both work. "
    "Or just type what you bought, e.g. \"bought 2 crates of eggs for 4500 at Shoprite\"."
)

HELP_TEXT = (
    "Here's what I can do:\n"
    "- Send a photo of a receipt to log it\n"
    "- Tell me a purchase in words, e.g. \"bought 2 crates of eggs for 4500 at Shoprite\"\n"
    "- Ask about spending, e.g. \"how much did we spend on groceries this month?\"\n"
    "- \"show my last receipt\" — see what's on it\n"
    "- Fix an item, e.g. \"item 2 price was 2500\" or \"the milk was Groceries\"\n"
    "- \"help\" — see this again"
)


# ---------------------------------------------------------------------------
# Router output
# ---------------------------------------------------------------------------

def _to_decimal(v: Any) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float, Decimal)):
        d = Decimal(str(v))
    else:
        s = re.sub(r"(?i)(ngn|₦|naira|n(?=\d))", "", str(v)).replace(",", "").strip()
        s = re.sub(r"(?i)^(\d+(?:\.\d+)?)k$", lambda m: str(Decimal(m.group(1)) * 1000), s)
        try:
            d = Decimal(s)
        except InvalidOperation:
            return None
    return d.quantize(Decimal("0.01")) if d > 0 else None


class Patch(BaseModel):
    name: Optional[str] = None
    price: Optional[Decimal] = None
    category: Optional[str] = None

    @field_validator("price", mode="before")
    @classmethod
    def _price(cls, v):
        return _to_decimal(v)

    @field_validator("name", "category", mode="before")
    @classmethod
    def _blank(cls, v):
        return (str(v).strip() or None) if v is not None else None

    def is_empty(self) -> bool:
        return self.name is None and self.price is None and self.category is None


class PurchaseItem(BaseModel):
    name: str
    quantity: Decimal = Decimal("1")
    total_price: Optional[Decimal] = None

    @field_validator("total_price", mode="before")
    @classmethod
    def _price(cls, v):
        return _to_decimal(v)

    @field_validator("quantity", mode="before")
    @classmethod
    def _qty(cls, v):
        d = _to_decimal(v)
        return d if d is not None else Decimal("1")


class Purchase(BaseModel):
    vendor: Optional[str] = None
    date: Optional[str] = None
    items: list[PurchaseItem] = []


class SpendingQuery(BaseModel):
    scope: str = "me"
    period: str = "all_time"
    start: Optional[str] = None
    end: Optional[str] = None
    category: Optional[str] = None
    item: Optional[str] = None
    item_terms: list[str] = []

    @field_validator("item_terms", mode="before")
    @classmethod
    def _terms(cls, v):
        return [str(x) for x in v if str(x).strip()] if isinstance(v, list) else []


class RouterDecision(BaseModel):
    intent: str = "unclear"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    item_number: Optional[int] = None
    item_name: Optional[str] = None  # correct_item by name, when the number isn't known
    verdict: Optional[str] = None  # answer_clarification: "correct" | "fix"
    patch: Optional[Patch] = None
    purchase: Optional[Purchase] = None
    query: Optional[SpendingQuery] = None
    reply: Optional[str] = None
    remember: list[str] = []
    reasoning: Optional[str] = None
    source: str = "llm"  # "llm" | "fallback" | "pending"

    @field_validator("intent", mode="before")
    @classmethod
    def _intent(cls, v):
        v = str(v or "").strip().lower()
        return v if v in INTENTS else "unclear"

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v):
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    @field_validator("item_number", mode="before")
    @classmethod
    def _item(cls, v):
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @field_validator("remember", mode="before")
    @classmethod
    def _remember(cls, v):
        return [str(x) for x in v] if isinstance(v, list) else []


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

ROUTER_PROMPT = """\
You are the intent router for a family expense-tracking WhatsApp bot in Nigeria \
(amounts in Naira, ₦). Read the member's new message together with the CONTEXT \
and decide what they want. You only decide; code validates and acts.

Return ONLY a JSON object:
{
  "intent": one of %(intents)s,
  "confidence": 0.0-1.0,            // how sure you are of the intent AND the details
  "item_number": integer | null,    // the "item N" the message refers to
  "item_name": str | null,          // correct_item: the item's name as they wrote it
  "verdict": "correct" | "fix" | null,   // answer_clarification only
  "patch": {"name": str|null, "price": number|null, "category": str|null} | null,
  "purchase": {"vendor": str|null, "date": "YYYY-MM-DD"|null,
               "items": [{"name": str, "quantity": number, "total_price": number|null}]} | null,
  "query": {"scope": "me"|"family", "period": %(periods)s,
            "start": "YYYY-MM-DD"|null, "end": "YYYY-MM-DD"|null,
            "category": str|null, "item": str|null, "item_terms": [str]} | null,
  "reply": str | null,              // smalltalk / unclear / expect_receipt: a short natural reply
  "remember": [str],                // durable facts worth keeping (see below), usually []
  "reasoning": str                  // one short sentence, for debugging
}

How to decide:
- answer_clarification: ONLY when the message clearly responds to one of the \
OPEN CLARIFICATIONS (e.g. "ok 3", "yes that's right", "item 5 was 1800", "no it \
was 1500"). verdict "correct" = the item is right as shown; verdict "fix" = they \
gave a corrected value (put it in patch). Set item_number to the item they mean. \
If several clarifications are open and the message doesn't say which item, \
leave item_number null — do NOT guess.
- correct_item: changing an item on an EXISTING logged receipt — "item 2 price \
was 2500", "the milk should be Groceries", "move Schweppes from Personal to \
Groceries". If they give a number, set item_number. If they name the item, ALWAYS \
set item_name to the name as they wrote it; also set item_number only if that item \
is on LAST RECEIPT. The item may be on an older receipt — that's fine, code finds \
it by name. patch holds only the fields they changed.
- expect_receipt: they mention a receipt or purchase to log but haven't sent \
it or said what they bought ("I have a receipt", "I'll send the invoice later", \
"can I upload a PDF?", "I have 3 receipts from the market"). Write reply: a short, \
natural answer to what they actually said. The bot accepts receipt photos, PDF \
receipts (sent as a document), or a purchase typed in words; mention what fits \
their message rather than listing everything. Never log_purchase without items.
- log_purchase: describing a NEW purchase in words ("bought 2 crates of eggs for \
4500", "paid 1500 for keke"). Numbers here are quantities and prices — never item \
numbers, and this is never a correction. total_price is the line total \
(2 crates for 4500 → quantity 2, total_price 4500). If a price is missing, use \
intent "unclear" and ask for it in reply. If STATE says awaiting_description, a \
purchase description is almost certainly log_purchase.
- spending_question: any question about spending/totals/categories/budgets, \
EVEN WHILE a clarification is open. A question is never a clarification answer. \
scope "family" for we/us/our/family/everyone, otherwise "me". Pick the period \
from the words (default all_time). category must be one of CATEGORIES if it is \
a category; for a specific product use item, and put spellings to search \
receipt lines for in item_terms: the singular, common variants and spacing \
("doughnuts" -> ["doughnut", "donut", "dough nut"]; "coke" -> ["coke", \
"coca cola"]). Receipt names are often abbreviated, so keep terms short.
- show_last_receipt: wants to see their last receipt / logged items.
- help: asks what the bot can do.
- confirm_pending / cancel_pending: yes / no to STATE.pending_action. Only use \
these when a pending_action exists.
- smalltalk: greetings, thanks, chit-chat. Write a short warm reply.
- unclear: you can't tell, or required details are missing. Write a short \
question in reply asking for exactly what's missing.

Categories in patch/query must be spelled exactly as in CATEGORIES.
remember: only stable facts about the member/household that would help later \
(budgets, what a shop or nickname means, household size, preferences). Never \
store one-off purchases or amounts already in receipts. Phrase as a full sentence.
Use RECENT CONVERSATION to resolve short replies ("yes", "the second one", "1500").

CONTEXT:
%(context)s

NEW MESSAGE from %(name)s:
%(message)s
"""

ANSWER_PROMPT = """\
You are a friendly family budgeting assistant replying on WhatsApp. Answer the \
member's question using ONLY the numbers in DATA — do not calculate new totals, \
estimate, or invent figures. Copy amounts exactly as written in DATA (₦ format). \
If DATA has no receipts, say so plainly and suggest sending receipt photos. If \
excluded_pending_review has receipts, mention briefly that they aren't counted \
yet. You may use LONG-TERM MEMORY (e.g. a stated budget) for context. Plain \
text, no markdown headers, at most 6 short lines.

LONG-TERM MEMORY:
%(memory)s

QUESTION from %(name)s: %(question)s

DATA:
%(data)s
"""

SUMMARY_PROMPT = """\
Summarize this WhatsApp conversation between a family member and their \
expense-tracking bot into a short paragraph (max 120 words) that will help the \
bot later: recurring topics, preferences, things they asked about, unresolved \
issues. Do not list individual receipt amounts — those are stored separately. \
Merge with the previous summary if there is one.

PREVIOUS SUMMARY:
%(previous)s

CONVERSATION:
%(turns)s
"""


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------

class ConversationAgent:
    def __init__(self, store: DataStore, whatsapp: WhatsAppClient) -> None:
        self.store = store
        self.whatsapp = whatsapp
        self.memory = ConversationMemory(store)
        s = get_settings()
        self.model = s.gemini_chat_model
        self.min_conf = s.intent_min_confidence

    # ---------------- entry point ----------------
    def handle(self, member: Member, text: str) -> str:
        ctx = self._build_context(member)
        with observability.span("route", input=text, metadata={
            "open_clarifications": [e["item_number"] for e in ctx["open"]],
            "state": sorted(ctx["state"]),
        }) as obs:
            decision = self._route(member, text, ctx)
            obs.update(output=decision.model_dump(mode="json", exclude_none=True),
                       metadata={"source": decision.source})
        if "pending_action" in ctx["state"] and decision.intent not in ("confirm_pending", "cancel_pending"):
            # A proposed change only applies to the very next reply. Anything else
            # drops it, so a later "yes" can't land on a receipt that has since changed.
            self.memory.clear_state(member.id, "pending_action")
            ctx["state"].pop("pending_action", None)
        logger.info(
            "Intent %s (conf %.2f, %s) item=%s: %s",
            decision.intent, decision.confidence, decision.source,
            decision.item_number, decision.reasoning or "",
        )
        with observability.span(f"act:{decision.intent}", input={
            "intent": decision.intent, "item_number": decision.item_number,
            "confidence": decision.confidence, "source": decision.source,
        }) as obs:
            reply = self._execute(member, text, decision, ctx)
            obs.update(output=reply)

        self.memory.record(member.id, "user", text, intent=decision.intent)
        self.whatsapp.send_text(member.phone_number, reply)
        self.memory.record(member.id, "assistant", reply, intent=decision.intent)

        # After the reply (typing already stopped): durable facts + compaction.
        if decision.remember and decision.source == "llm":
            self.memory.remember(member.id, decision.remember)
        self.memory.compact_if_needed(member.id, self._summarize)
        return reply

    # ---------------- context ----------------
    def _build_context(self, member: Member) -> dict:
        """Everything the router and the checks need, fetched in parallel.

        Each read is a separate Supabase round trip (~0.35s from Lagos), so
        running them side by side instead of one after another is most of the
        time saved before the bot can start "thinking" (changelog v1.7).
        """
        def open_entries() -> list[dict]:
            cls = self.store.list_open_clarifications_for_member(member.id)
            if not cls:
                return []

            def entry(cl) -> dict:
                receipt = self.store.get_receipt(cl.receipt_id)
                li = self.store.get_line_item(cl.line_item_id) if cl.line_item_id else None
                return {
                    "clarification": cl,
                    "receipt": receipt,
                    "line_item": li,
                    "item_number": li.position if li else None,
                    "vendor": (receipt.vendor if receipt else None) or "the receipt",
                }
            return run_parallel({i: (lambda cl=cl: entry(cl)) for i, cl in enumerate(cls)}).values()

        def last_receipt():
            r = self.store.latest_receipt_for_member(member.id)
            return r, (self.store.list_line_items(r.id) if r else [])

        got = run_parallel({
            "categories": self.store.list_categories,
            "open": lambda: list(open_entries()),
            "last": last_receipt,
            "state": lambda: self.memory.state(member.id),
            "recent": lambda: self.memory.recent(member.id),
            "long_term": lambda: self.memory.long_term(member.id),
        })
        categories = got["categories"]
        last_receipt_row, last_items = got["last"]
        return {
            "categories": categories,
            "cat_by_id": {c.id: c for c in categories},
            "open": got["open"],
            "last_receipt": last_receipt_row,
            "last_items": last_items,
            "state": got["state"],
            "recent": got["recent"],
            "long_term": got["long_term"],
        }

    def _context_for_prompt(self, ctx: dict) -> str:
        cat_by_id = ctx["cat_by_id"]

        def item_line(li: LineItem) -> str:
            cat = cat_by_id.get(li.category_id)
            flag = " [awaiting check]" if li.needs_review else ""
            return f"  item {li.position}: {li.name} · {format_ngn(li.price)} · {cat.name if cat else 'Other'}{flag}"

        parts = [f"TODAY: {date.today().isoformat()} ({date.today():%A})"]
        parts.append("CATEGORIES: " + ", ".join(c.name for c in ctx["categories"]))

        if ctx["open"]:
            lines = ["OPEN CLARIFICATIONS (questions the bot asked and is waiting on):"]
            for e in ctx["open"]:
                li = e["line_item"]
                if li is not None:
                    lines.append(
                        f"  item {li.position} from {e['vendor']}: '{li.name}' {format_ngn(li.price)}"
                        f" — asked: did I get this right?"
                    )
                else:
                    lines.append(f"  whole receipt from {e['vendor']}: vendor/total check")
            parts.append("\n".join(lines))
        else:
            parts.append("OPEN CLARIFICATIONS: none")

        r = ctx["last_receipt"]
        if r is not None:
            when = (r.purchase_date or r.created_at.date()).isoformat()
            lines = [f"LAST RECEIPT: {r.vendor or 'unknown vendor'}, {when}, status {r.status}"]
            lines += [item_line(li) for li in ctx["last_items"][:40]] or ["  (no items)"]
            parts.append("\n".join(lines))
        else:
            parts.append("LAST RECEIPT: none yet")

        state = ctx["state"]
        st_lines = []
        if "awaiting_description" in state:
            st_lines.append("  awaiting_description: we asked them to type out a purchase "
                            "because their photo was unreadable")
        if "pending_action" in state:
            st_lines.append(f"  pending_action: we asked them to confirm — "
                            f"{(state['pending_action'] or {}).get('summary', '')}")
        parts.append("STATE:\n" + ("\n".join(st_lines) if st_lines else "  nothing pending"))

        facts = [m for m in ctx["long_term"] if m.kind == "fact"]
        summary = next((m for m in reversed(ctx["long_term"]) if m.kind == "summary"), None)
        mem_lines = [f"  - {m.content}" for m in facts]
        if summary:
            mem_lines.append(f"  earlier conversation: {summary.content}")
        parts.append("LONG-TERM MEMORY:\n" + ("\n".join(mem_lines) if mem_lines else "  (none)"))

        parts.append("RECENT CONVERSATION:\n" + (self._turns_text(ctx["recent"]) or "  (none)"))
        return "\n\n".join(parts)

    @staticmethod
    def _turns_text(turns: list[ConversationTurn]) -> str:
        return "\n".join(
            f"  {'Member' if t.role == 'user' else 'Bot'}: {t.content}" for t in turns
        )

    # ---------------- step 1: route ----------------
    def _route(self, member: Member, text: str, ctx: dict) -> RouterDecision:
        prompt = ROUTER_PROMPT % {
            "intents": " | ".join(f'"{i}"' for i in INTENTS),
            "periods": " | ".join(f'"{p}"' for p in (
                "today", "yesterday", "this_week", "last_week", "last_7_days", "this_month",
                "last_month", "last_30_days", "this_year", "all_time", "custom")),
            "context": ctx.setdefault("prompt_context", self._context_for_prompt(ctx)),
            "name": member.name,
            "message": text.strip(),
        }
        try:
            raw = llm.generate_json(
                "route-intent", [prompt], model_name=self.model, timeout=20,
                metadata={"open_clarifications": len(ctx["open"])},
            )
            decision = RouterDecision.model_validate(raw)
            decision.source = "llm"
            return decision
        except Exception as e:
            logger.warning("Intent router unavailable, using strict fallback: %s", e)
            return fallback_parse(text, ctx)

    # ---------------- steps 2+3: check and act ----------------
    def _execute(self, member: Member, text: str, d: RouterDecision, ctx: dict) -> str:
        handlers = {
            "answer_clarification": self._do_answer_clarification,
            "correct_item": self._do_correct_item,
            "log_purchase": self._do_log_purchase,
            "spending_question": self._do_spending_question,
            "show_last_receipt": self._do_show_last_receipt,
            "help": lambda m, t, d, c: HELP_TEXT,
            "confirm_pending": self._do_confirm_pending,
            "cancel_pending": self._do_cancel_pending,
        }
        handler = handlers.get(d.intent)
        if handler is None:  # smalltalk / unclear / expect_receipt: Gemini's own words
            generic = EXPECT_RECEIPT_TEXT if d.intent == "expect_receipt" else (
                "Sorry, I didn't quite get that. You can send a receipt photo, tell me a "
                "purchase in words, or ask how much you've spent. Type 'help' for more."
            )
            reply = (d.reply or "").strip() or generic
            # Free-text replies may not quote money the bot can't back up.
            if d.source == "llm" and amounts_in(reply) - amounts_in(ctx.get("prompt_context", "") + " " + text):
                logger.warning("Free-text reply cited unknown amounts; using generic reply")
                reply = generic
        else:
            reply = handler(member, text, d, ctx)

        if d.intent not in MUTATING and d.intent not in ("confirm_pending", "cancel_pending"):
            # Non-changing intents leave clarifications as they were, so the
            # context we already loaded is current: no extra round trip.
            reply = self._with_open_reminder(ctx["open"], reply)
        return reply

    # -- confidence gate ------------------------------------------------
    def _gate(self, member: Member, d: RouterDecision, summary: str) -> Optional[str]:
        """If the router isn't sure, save the decision and ask before changing data."""
        if d.source != "llm" or d.confidence >= self.min_conf:
            return None
        pending = d.model_dump(mode="json")
        pending["summary"] = summary
        self.memory.set_state(member.id, "pending_action", pending)
        return f"Just to be sure — {summary}? Reply yes or no."

    def _do_confirm_pending(self, member: Member, text: str, d: RouterDecision, ctx: dict) -> str:
        pending = ctx["state"].get("pending_action")
        if not pending:
            return "Nothing is waiting on a yes from you right now 🙂"
        self.memory.clear_state(member.id, "pending_action")
        decision = RouterDecision.model_validate({**pending, "confidence": 1.0})
        decision.source = "pending"
        # Re-validates against current data — it may have changed since we asked.
        return self._execute(member, text, decision, self._build_context(member))

    def _do_cancel_pending(self, member: Member, text: str, d: RouterDecision, ctx: dict) -> str:
        if ctx["state"].get("pending_action"):
            self.memory.clear_state(member.id, "pending_action")
            return "Okay, I haven't changed anything."
        return "Okay 🙂"

    # -- answer a clarification (bugs 2 + 3) ---------------------------
    def _do_answer_clarification(self, member: Member, text: str, d: RouterDecision, ctx: dict) -> str:
        open_entries = ctx["open"]
        if not open_entries:
            if d.item_number is not None and d.patch and not d.patch.is_empty():
                return self._do_correct_item(member, text, d, ctx)
            return "There's nothing waiting on an answer from you right now 🙂"

        if d.item_number is not None:
            matches = [e for e in open_entries if e["item_number"] == d.item_number]
            if not matches:
                return f"I'm not waiting on item {d.item_number}. {self._open_list_sentence(open_entries)}"
            if len(matches) > 1:
                return (f"Item {d.item_number} is open on more than one receipt "
                        f"({', '.join(m['vendor'] for m in matches)}). Which receipt do you mean?")
            entry = matches[0]
        elif len(open_entries) == 1:
            entry = open_entries[0]
        else:
            return f"Which item is that for? {self._open_list_sentence(open_entries)}"

        verdict = (d.verdict or "").lower()
        has_patch = d.patch is not None and not d.patch.is_empty()
        if verdict not in ("correct", "fix"):
            verdict = "fix" if has_patch else ""
        if verdict == "fix" and not has_patch:
            return f"What should {self._entry_label(entry)} be? e.g. 'item {entry['item_number'] or 'N'} price was 1500'."
        if not verdict:
            return f"Is {self._entry_label(entry)} right? Reply 'ok' if yes, or tell me the correct price/name/category."

        li: Optional[LineItem] = entry["line_item"]
        if li is None:  # whole-receipt check (no single item flagged)
            if verdict == "fix":
                return "Thanks — which item was off? e.g. 'item 2 price was 1500'."
            if (gated := self._gate(member, d, f"confirm your {entry['vendor']} receipt as correct")):
                return gated
            self.store.resolve_clarification(entry["clarification"].id, text.strip())
            return self._finish_receipt(entry["receipt"].id, "Thanks!")

        changes: list[str] = []
        if verdict == "fix":
            err = self._validate_patch(d.patch, ctx["categories"])
            if err:
                return err
            changes = self._describe_patch(li, d.patch, ctx)
            if not changes:
                verdict = "correct"  # they restated the same values
        summary = (f"update item {li.position} ({li.name}) on your {entry['vendor']} receipt: "
                   + "; ".join(changes)) if changes else f"confirm item {li.position} ({li.name}) as correct"
        if (gated := self._gate(member, d, summary)):
            return gated

        if changes:
            self._apply_patch(li, d.patch, ctx["categories"])
        self.store.update_line_item(li.id, needs_review=False)
        self.store.resolve_clarification(entry["clarification"].id, text.strip())
        head = (f"Updated item {li.position} ({li.name}): " + "; ".join(changes) + " ✅") if changes \
            else f"Thanks, item {li.position} ({li.name}) confirmed ✅"
        return self._finish_receipt(entry["receipt"].id, head)

    # -- correct an item on an existing receipt (bug 1 + 3) -------------
    def _do_correct_item(self, member: Member, text: str, d: RouterDecision, ctx: dict) -> str:
        if d.item_number is None and not d.item_name:
            return "Which item should I change? e.g. 'item 2 price was 2500' or 'the milk should be Groceries'."
        if d.patch is None or d.patch.is_empty():
            what = f"item {d.item_number}" if d.item_number is not None else f"'{d.item_name}'"
            return f"What should {what} be? e.g. 'item 2 price was 2500' or 'the milk should be Groceries'."
        if d.item_number is None or not self._names_item_on_last_receipt(d, ctx):
            # Named an item that isn't the numbered one on the last receipt (often on an
            # older receipt, where every item may even be "item 1"): find it by name.
            return self._correct_item_by_name(member, d, ctx)

        # Bug 3: if this item is one we're asking about, it's that clarification's answer.
        open_matches = [e for e in ctx["open"] if e["item_number"] == d.item_number]
        if len(open_matches) == 1:
            return self._do_answer_clarification(
                member, text, d.model_copy(update={"intent": "answer_clarification", "verdict": "fix"}), ctx
            )

        receipt: Optional[Receipt] = ctx["last_receipt"]
        items: list[LineItem] = ctx["last_items"]
        if receipt is None:
            return "You haven't logged a receipt yet, so there's nothing to change. Send a photo to start 🙂"
        li = next((i for i in items if i.position == d.item_number), None)
        if li is None:
            return (f"Your last receipt ({receipt.vendor or 'no vendor'}) has {len(items)} item(s), "
                    f"so there's no item {d.item_number}. Type 'show my last receipt' to see them.")
        err = self._validate_patch(d.patch, ctx["categories"])
        if err:
            return err
        changes = self._describe_patch(li, d.patch, ctx)
        if not changes:
            return f"Item {li.position} ({li.name}) already looks like that — nothing to change."
        if (gated := self._gate(member, d, f"update item {li.position} ({li.name}) on your "
                                f"{receipt.vendor or 'last'} receipt: " + "; ".join(changes))):
            return gated

        self._apply_patch(li, d.patch, ctx["categories"])
        head = f"Updated item {li.position} on your {receipt.vendor or 'last'} receipt: " + "; ".join(changes) + " ✅"
        if receipt.status == ReceiptStatus.NEEDS_REVIEW:
            return self._finish_receipt(receipt.id, head)
        return head

    def _names_item_on_last_receipt(self, d: RouterDecision, ctx: dict) -> bool:
        li = next((i for i in ctx["last_items"] if i.position == d.item_number), None)
        if li is None:
            return not d.item_name  # a bare "item 7" that doesn't exist -> number path says so
        words = item_words(d.item_name)
        return not words or all(w in squash(li.name) for w in words)

    def _correct_item_by_name(self, member: Member, d: RouterDecision, ctx: dict) -> str:
        """Edit an item found by name on any of the member's receipts (changelog v1.7).

        A category change applies to every item with that name, since it's the
        same product bought on several receipts. A price or name change must
        point at exactly one item, otherwise the bot lists them and asks.
        """
        words = item_words(d.item_name)
        if not words:
            return "Which item should I change? Tell me its name or its number on the receipt."
        err = self._validate_patch(d.patch, ctx["categories"])
        if err:
            return err
        matches = self.store.find_member_line_items(member.id, words)
        if not matches:
            return (f"I couldn't find an item called '{d.item_name}' on your receipts. "
                    "Check the spelling, or type 'show my last receipt' to see item names.")

        def where(li: LineItem, r: Receipt) -> str:
            when = r.purchase_date or r.created_at.date()
            return f"{li.name} {format_ngn(li.price)} · {r.vendor or 'receipt'} · {when:%d %b %Y}"

        names = {squash(li.name): li.name for li, _ in matches}
        if len(names) > 1:
            listed = "; ".join(sorted(names.values())[:6])
            return f"I found more than one item matching '{d.item_name}': {listed}. Which one do you mean?"
        name = next(iter(names.values()))
        patch = d.patch

        if patch.price is not None or patch.name:
            if len(matches) > 1:
                listed = "\n".join(f"- {where(li, r)}" for li, r in matches[:6])
                return (f"'{name}' is on {len(matches)} receipt lines:\n{listed}\n"
                        "Which one should change? Tell me the vendor or date.")
            (li, receipt), = matches
            changes = self._describe_patch(li, patch, ctx)
            if not changes:
                return f"{li.name} already looks like that, so there's nothing to change."
            if (gated := self._gate(member, d, f"update {li.name} on your {receipt.vendor or ''} "
                                    f"receipt: " + "; ".join(changes))):
                return gated
            self._apply_patch(li, patch, ctx["categories"])
            head = f"Updated {li.name} on your {receipt.vendor or 'receipt'} receipt: " + "; ".join(changes) + " ✅"
            if receipt.status == ReceiptStatus.NEEDS_REVIEW:
                return self._finish_receipt(receipt.id, head)
            return head

        # Category only: move every line of this product.
        cat = self._find_category(patch.category, ctx["categories"])
        to_move = [(li, r) for li, r in matches if li.category_id != cat.id]
        if not to_move:
            return f"{name} is already in {cat.name}, so there's nothing to change."
        olds = sorted({(ctx["cat_by_id"].get(li.category_id).name if ctx["cat_by_id"].get(li.category_id)
                        else "Other") for li, _ in to_move})
        lines = "line" if len(to_move) == 1 else "lines"
        if (gated := self._gate(member, d, f"move {name} ({len(to_move)} receipt {lines}) "
                                f"from {', '.join(olds)} to {cat.name}")):
            return gated
        for li, _ in to_move:
            self._apply_patch(li, Patch(category=cat.name), ctx["categories"])
        receipts = sorted({f"{r.vendor or 'receipt'} ({(r.purchase_date or r.created_at.date()):%d %b})"
                           for _, r in to_move})
        return (f"Moved {name} from {', '.join(olds)} to {cat.name} on {len(to_move)} receipt {lines} "
                f"({', '.join(receipts)}) ✅\nI'll put it in {cat.name} automatically from now on.")

    # -- log a purchase described in words (bug 1) ----------------------
    def _do_log_purchase(self, member: Member, text: str, d: RouterDecision, ctx: dict) -> str:
        p = d.purchase
        if p is None or not p.items:
            return "What did you buy and how much was it? e.g. 'bought 2 crates of eggs for 4500 at Shoprite'."
        missing = [i.name for i in p.items if i.total_price is None]
        if missing:
            return f"How much was the {missing[0]}?"

        total = sum((i.total_price for i in p.items), Decimal("0"))
        lines = ", ".join(f"{i.name} {format_ngn(i.total_price)}" for i in p.items)
        if (gated := self._gate(member, d, f"log {lines}" + (f" from {p.vendor}" if p.vendor else ""))):
            return gated

        purchase_date: Optional[date] = None
        if p.date:
            try:
                purchase_date = date.fromisoformat(p.date[:10])
            except ValueError:
                purchase_date = None
        purchase_date = purchase_date or date.today()
        raw = {"source": "text", "message": text, "parsed": p.model_dump(mode="json")}

        # After an unreadable photo we already saved an empty needs_review receipt.
        # Fill that one in rather than leaving it orphaned next to a new row.
        receipt: Optional[Receipt] = None
        awaiting = ctx["state"].get("awaiting_description") or {}
        if awaiting.get("receipt_id"):
            try:
                existing = self.store.get_receipt(UUID(awaiting["receipt_id"]))
            except (ValueError, TypeError):
                existing = None
            if existing is not None and existing.member_id == member.id \
                    and not self.store.list_line_items(existing.id):
                prev = existing.raw_extraction if isinstance(existing.raw_extraction, dict) else {}
                self.store.update_receipt(
                    existing.id, vendor=p.vendor or existing.vendor, purchase_date=purchase_date,
                    total=total, raw_extraction={**raw, "photo_extraction": prev},
                )
                receipt = self.store.get_receipt(existing.id)
        if receipt is None:
            receipt = self.store.create_receipt(Receipt(
                member_id=member.id, vendor=p.vendor, purchase_date=purchase_date, total=total,
                status=ReceiptStatus.PENDING, raw_extraction=raw,
            ))

        categories = ctx["categories"]
        from app.services.capture_pipeline import _llm_categories  # lazy: capture_pipeline imports this module
        llm_answers = _llm_categories([i.name for i in p.items if categorize_by_rules(i.name, categories) is None])
        new_items = []
        for pos, it in enumerate(p.items, start=1):
            cat, _ = categorize_item(
                it.name, categories, llm_fallback=lambda name: llm_answers.get(name, "Other"),
                persist_keywords=self.store.append_category_keywords,
            )
            new_items.append(LineItem(
                receipt_id=receipt.id, name=it.name, quantity=it.quantity, price=it.total_price,
                category_id=cat.id, confidence=1.0, needs_review=False, position=pos,
            ))
        saved = self.store.create_line_items(new_items)
        # Typed by the member themselves, so it's confirmed — they can still correct it.
        self.store.update_receipt_status(receipt.id, ReceiptStatus.CONFIRMED)
        self.memory.clear_state(member.id, "awaiting_description")
        receipt.status = ReceiptStatus.CONFIRMED
        return format_receipt(saved, self.store.list_categories(), receipt)

    # -- questions, answered from data ----------------------------------
    def _do_spending_question(self, member: Member, text: str, d: RouterDecision, ctx: dict) -> str:
        q = d.query or SpendingQuery()
        data = spending_snapshot(
            self.store, member, scope=q.scope, period=q.period, start=q.start, end=q.end,
            category=q.category, item_query=q.item, item_terms=q.item_terms,
        )
        fallback = format_snapshot(data)
        if d.source != "llm":
            return fallback
        memory_text = "\n".join(f"- {m.content}" for m in ctx["long_term"]) or "(none)"
        try:
            answer = llm.generate(
                "answer-spending-question",
                [ANSWER_PROMPT % {
                    "memory": memory_text, "name": member.name, "question": text.strip(),
                    "data": json.dumps(data, ensure_ascii=False, indent=1),
                }],
                model_name=self.model, timeout=20, temperature=0.3,
            ).strip()
        except Exception as e:
            logger.warning("Answer phrasing failed, sending computed summary: %s", e)
            return fallback
        # Grounding check: every ₦ amount must come from the data (or memory/question).
        allowed = amounts_in(json.dumps(data, ensure_ascii=False)) | amounts_in(memory_text) | amounts_in(text)
        allowed |= {norm_amount(Decimal(n.replace(",", "")))
                    for n in re.findall(r"\d[\d,]*(?:\.\d+)?", memory_text + " " + text)}
        unknown = amounts_in(answer) - allowed
        if not answer or unknown:
            logger.warning("Answer cited amounts not in data %s; sending computed summary", unknown)
            return fallback
        return answer

    def _do_show_last_receipt(self, member: Member, text: str, d: RouterDecision, ctx: dict) -> str:
        if ctx["last_receipt"] is None:
            return "You haven't logged any receipts yet — send a photo to get started 🙂."
        return format_receipt(ctx["last_items"], ctx["categories"], ctx["last_receipt"])

    # ---------------- helpers ----------------
    def _finish_receipt(self, receipt_id: UUID, head: str) -> str:
        """Confirm the receipt if nothing on it is still waiting; say what's left otherwise."""
        items = self.store.list_line_items(receipt_id)
        still_open = [
            c for c in self.store.list_open_clarifications_for_member(self._receipt_owner(receipt_id))
            if c.receipt_id == receipt_id
        ]
        if not still_open and not any(li.needs_review for li in items):
            self.store.update_receipt_status(receipt_id, ReceiptStatus.CONFIRMED)
            return f"{head}\nThe receipt is now confirmed."
        remaining = []
        by_id = {li.id: li for li in items}
        for c in still_open:
            li = by_id.get(c.line_item_id)
            remaining.append(f"item {li.position} ({li.name}, {format_ngn(li.price)})" if li else "the vendor/total")
        return f"{head}\nStill need a quick check on {', '.join(remaining)} — reply 'ok N' if right, or correct it."

    def _receipt_owner(self, receipt_id: UUID) -> UUID:
        r = self.store.get_receipt(receipt_id)
        return r.member_id if r else receipt_id

    def _with_open_reminder(self, open_entries: list[dict], reply: str) -> str:
        """Bug 2: answering something else must not swallow the open question — remind instead."""
        if not open_entries:
            return reply
        labels = []
        for e in open_entries[:3]:
            li = e["line_item"]
            labels.append(f"item {li.position} ({li.name})" if li else "a receipt total")
        return f"{reply}\n\n(I'm still waiting on {', '.join(labels)} — reply 'ok N' if it's right, or correct it.)"

    @staticmethod
    def _entry_label(entry: dict) -> str:
        li = entry["line_item"]
        if li is None:
            return f"the {entry['vendor']} receipt"
        return f"item {li.position} ('{li.name}', {format_ngn(li.price)})"

    def _open_list_sentence(self, entries: list[dict]) -> str:
        labels = [self._entry_label(e) + f" from {e['vendor']}" for e in entries]
        return "I'm waiting on " + "; ".join(labels) + ". Start your reply with the item number, e.g. 'ok 3' or 'item 3 price was 1500'."

    @staticmethod
    def _find_category(name: Optional[str], categories: list[Category]) -> Optional[Category]:
        if not name:
            return None
        return next((c for c in categories if c.name.lower() == name.strip().lower()), None)

    def _validate_patch(self, patch: Patch, categories: list[Category]) -> Optional[str]:
        if patch.category and self._find_category(patch.category, categories) is None:
            return (f"I don't have a '{patch.category}' category. Pick one of: "
                    + ", ".join(c.name for c in categories) + ".")
        return None

    def _describe_patch(self, li: LineItem, patch: Patch, ctx: dict) -> list[str]:
        changes = []
        if patch.price is not None and patch.price != li.price:
            changes.append(f"price {format_ngn(li.price)} → {format_ngn(patch.price)}")
        if patch.name and patch.name != li.name:
            changes.append(f"name '{li.name}' → '{patch.name}'")
        cat = self._find_category(patch.category, ctx["categories"])
        if cat is not None and cat.id != li.category_id:
            old = ctx["cat_by_id"].get(li.category_id)
            changes.append(f"category {old.name if old else 'Other'} → {cat.name}")
        return changes

    def _apply_patch(self, li: LineItem, patch: Patch, categories: list[Category]) -> None:
        cat = self._find_category(patch.category, categories)
        old_category_id = li.category_id  # read first: the update may change `li` in place
        self.store.update_line_item(
            li.id,
            price=patch.price,
            name=patch.name,
            category_id=cat.id if cat else None,
        )
        if cat is not None and cat.id != old_category_id:
            # A human said what this item is: teach the rules table (architecture.md §7.4).
            # Also take these words out of the category it was wrongly put in, or the
            # rules would keep matching them there first and repeat the mistake.
            tokens = keyword_tokens(patch.name or li.name)
            if tokens:
                try:
                    if old_category_id is not None:
                        self.store.remove_category_keywords(old_category_id, tokens)
                    self.store.append_category_keywords(cat.id, tokens)
                except Exception as e:
                    logger.warning("Could not reinforce keywords from correction: %s", e)

    def _summarize(self, previous: Optional[str], turns: list[ConversationTurn]) -> str:
        return llm.generate(
            "summarize-conversation",
            [SUMMARY_PROMPT % {"previous": previous or "(none)", "turns": self._turns_text(turns)}],
            model_name=self.model, timeout=30, temperature=0.2,
        )


_STOP_WORDS = {"the", "a", "an", "my", "our", "item", "items", "of", "on", "from", "to", "category", "price"}


def item_words(name: Optional[str]) -> list[str]:
    """Words that identify an item by name ("the Schweppes chapman" -> schweppes, chapman)."""
    return [w for w in _tokenize(name or "") if w not in _STOP_WORDS and len(w) > 1]


def run_parallel(tasks: dict) -> dict:
    """Run zero-arg callables side by side; {key: result}. Each runs in a copy of
    the caller's context so its Langfuse spans stay inside the message's trace."""
    if not tasks:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as pool:
        futures = {k: pool.submit(contextvars.copy_context().run, fn) for k, fn in tasks.items()}
        return {k: f.result() for k, f in futures.items()}


# ---------------------------------------------------------------------------
# Strict fallback parser — used only when Gemini is unavailable.
# Recognizes exact command shapes; anything else changes nothing.
# ---------------------------------------------------------------------------

_YES = r"(?:ok|okay|yes|yeah|yep|correct|right|confirmed?|👍)"
_NO = r"(?:no|nope|cancel|don'?t)"


def fallback_parse(text: str, ctx: dict) -> RouterDecision:
    t = (text or "").strip()
    tl = t.lower()
    has_pending = bool(ctx.get("state", {}).get("pending_action"))
    open_entries = ctx.get("open", [])
    open_items = {e["item_number"] for e in open_entries if e["item_number"] is not None}

    def d(**kw) -> RouterDecision:
        return RouterDecision(source="fallback", confidence=1.0, **kw)

    m = re.fullmatch(rf"{_YES}[\s,.!]*(?:item\s*)?(\d+)[.!]*", tl)
    if m:
        return d(intent="answer_clarification", item_number=int(m.group(1)), verdict="correct")
    if re.fullmatch(rf"{_YES}[\s.!]*", tl):
        if has_pending:
            return d(intent="confirm_pending")
        if len(open_entries) == 1:
            return d(intent="answer_clarification", verdict="correct")
        return d(intent="smalltalk", reply="👍")
    if re.fullmatch(rf"{_NO}[\s.!]*", tl) and has_pending:
        return d(intent="cancel_pending")

    m = re.match(r"^item\s*(\d+)\b(.*)$", tl, re.DOTALL)
    if m:
        n, rest = int(m.group(1)), m.group(2)
        patch = Patch()
        pm = re.search(r"(?:price|cost|was|is|=)\s*(?:₦|ngn|n)?\s*([0-9][0-9,]*(?:\.\d+)?)", rest)
        if pm:
            patch.price = _to_decimal(pm.group(1))
        for name in (c.name for c in ctx.get("categories", [])):
            if re.search(rf"\b{re.escape(name.lower())}\b", rest):
                patch.category = name
                break
        nm = re.search(r"name\s+(?:was|is|should be)\s+(.+?)(?:\s+and\b|[.,]|$)", t[m.start(2):], re.IGNORECASE)
        if nm:
            patch.name = nm.group(1).strip()
        intent = "answer_clarification" if n in open_items else "correct_item"
        return d(intent=intent, item_number=n, verdict="fix" if not patch.is_empty() else None, patch=patch)

    if re.fullmatch(r"(?:i\s+)?(?:have|got|want to (?:log|send))\s+(?:a|my|the|another)?\s*receipts?[.!]*", tl):
        return d(intent="expect_receipt")
    if re.search(r"\bhelp\b", tl) or tl in ("?", "commands"):
        return d(intent="help")
    if re.search(r"\b(show|list|see)\b", tl) and re.search(r"\b(items?|receipts?|logs?)\b", tl):
        return d(intent="show_last_receipt")
    if re.search(r"\b(spent|spend|spending|how much)\b", tl):
        scope = "family" if re.search(r"\b(we|us|our|family|everyone)\b", tl) else "me"
        period = "all_time"
        for phrase, p in (("last week", "last_week"), ("this week", "this_week"),
                          ("last month", "last_month"), ("this month", "this_month"),
                          ("today", "today"), ("yesterday", "yesterday")):
            if phrase in tl:
                period = p
                break
        category = next((c.name for c in ctx.get("categories", [])
                         if re.search(rf"\b{re.escape(c.name.lower())}\b", tl)), None)
        return d(intent="spending_question", query=SpendingQuery(scope=scope, period=period, category=category))

    return d(
        intent="unclear",
        reply=("I'm having trouble understanding messages right now, so I haven't changed anything. "
               "You can still send a receipt photo, or use exact commands like 'item 2 price was 2500', "
               "'ok 3', or 'how much have I spent'."),
    )


# ---------------------------------------------------------------------------
# Shared formatting
# ---------------------------------------------------------------------------

def format_receipt(items: list[LineItem], categories: list[Category], receipt: Receipt) -> str:
    cat_by_id = {c.id: c for c in categories}
    verb = "Logged" if receipt.status == ReceiptStatus.CONFIRMED else "Saved"
    when = receipt.purchase_date
    dated = f", dated {when:%d %b %Y}" if when else ""
    lines = [f"✅ {verb} {len(items)} item(s) from {receipt.vendor or 'this receipt'}{dated}.\n"]
    total = Decimal("0")
    by_category: dict[str, Decimal] = {}
    for li in items:
        cat = cat_by_id.get(li.category_id)
        cat_name = cat.name if cat else "Other"
        flag = " (checking)" if li.needs_review else ""
        lines.append(f"{li.position}. {li.name} · {format_ngn(li.price)} · {cat_name}{flag}")
        total += li.price
        by_category[cat_name] = by_category.get(cat_name, Decimal("0")) + li.price
    lines.append("")
    lines.append(f"Total: {format_ngn(total)}")
    if len(by_category) > 1:
        lines.append("")
        lines.append("By category:")
        for cat_name, amt in sorted(by_category.items(), key=lambda kv: -kv[1]):
            pct = int((amt / total * 100).to_integral()) if total else 0
            lines.append(f"  {cat_name}: {format_ngn(amt)} ({pct}%)")
    lines.append("")
    lines.append("To fix an item, reply e.g. 'item 2 was 2500' or 'item 3 is Dining'.")
    return "\n".join(lines)
