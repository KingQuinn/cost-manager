from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from app import observability
from app.config import get_settings
from app.db import DataStore
from app.models import Category, Clarification, LineItem, Receipt, ReceiptStatus, WhatsAppInboundMessage
from app.services.agent import ConversationAgent, format_receipt
from app.services.categorize import categorize_item
from app.services.extraction import categorize_with_llm, extract_receipt
from app.services.memory import ConversationMemory
from app.services.spending import format_ngn
from app.services.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

# Small in-process cache for "sender is mid-onboarding (texts their name)".
# Resets on restart — fine for local dev; in production we'd persist this in the DB.
_PENDING_ONBOARD: dict[str, bool] = {}


def is_pending_onboard(phone: str) -> bool:
    return _PENDING_ONBOARD.get(phone, False)


def mark_pending_onboard(phone: str) -> None:
    _PENDING_ONBOARD[phone] = True


_format_ngn = format_ngn  # kept for older imports


class CapturePipeline:
    """Orchestrates the Path-1 capture flow for one receipt:

    inbound image → extract → categorize → write → reply (confirm or question).

    Split out of the FastAPI route handlers so it's unit-testable and framework-agnostic.
    """

    def __init__(self, store: DataStore, whatsapp: WhatsAppClient) -> None:
        self.store = store
        self.whatsapp = whatsapp
        self.memory = ConversationMemory(store)
        self.agent = ConversationAgent(store, whatsapp)
        settings = get_settings()
        self.overall_conf_threshold = settings.overall_confidence_threshold
        self.per_item_conf_threshold = settings.per_item_confidence_threshold

    # ---- onboarding -------------------------------------------------
    def handle_known_sender(self, phone: str) -> tuple[bool, Optional[str]]:
        """Return (member exists, question). If no member, reply-prompt for a name.
        second return is a follow-up"""
        self.store.get_or_create_default_family()
        if not self.store.get_member_by_phone(phone):
            return False, "Hi! Before I can log receipts, I need to know who you are. Reply with just your first name (e.g. 'Aisha')."
        return True, None

    def complete_onboarding(self, phone: str, name: str) -> None:
        fam = self.store.get_or_create_default_family()
        self.store.create_member(fam.id, name.strip(), phone.strip())
        _PENDING_ONBOARD.pop(phone, None)

    # ---- image receipt processing ----------------------------------------
    def process_image(self, from_phone: str, image_bytes: bytes, image_id: str) -> str:
        """Core capture: extraction → categorize → persist → reply. Returns a summary string for testing."""
        member = self.store.get_member_by_phone(from_phone)
        if member is None:
            return "[process_image called on unknown phone"

        extraction = extract_receipt(image_bytes)
        raw_extraction = extraction.model_dump(mode="json")

        if extraction.overall_confidence == 0:
            # architecture.md edge case: non-receipt image (FR-3 acceptance criteria).
            # Still write a receipt with empty line items.
            receipt = Receipt(
                member_id=member.id,
                vendor=extraction.vendor,
                purchase_date=extraction.date,
                total=extraction.total,
                status=ReceiptStatus.NEEDS_REVIEW,
                raw_extraction=raw_extraction,
            )
            receipt = self.store.create_receipt(receipt)
            msg = (
                "Hmm, I couldn't tell that was a receipt (the words were too unclear or it isn't one). "
                "Could you try another photo, or describe the purchase in words "
                "(e.g. 'bought 2 crates of eggs for 4500 at Shoprite')?"
            )
            # Layer 2 memory: the next text is probably that description. The agent
            # logs it as a purchase onto this receipt — never as an edit to another one.
            self.memory.clear_state(member.id, "pending_action")
            self.memory.set_state(member.id, "awaiting_description", {"receipt_id": str(receipt.id)})
            self._send_and_remember(member.id, from_phone, msg)
            return msg

        categories = self.store.list_categories()

        receipt = Receipt(
            member_id=member.id,
            vendor=extraction.vendor,
            purchase_date=extraction.date,
            total=extraction.total,
            status=ReceiptStatus.PENDING,  # updated after confidence check
            raw_extraction=raw_extraction,
        )
        receipt = self.store.create_receipt(receipt)
        # A readable receipt arrived: we're no longer waiting on a typed description,
        # and any proposed edit (which pointed at the previous "last receipt") is void.
        self.memory.clear_state(member.id, "awaiting_description", "pending_action")
        self.memory.record(member.id, "user", f"[sent a receipt photo from {extraction.vendor or 'unknown vendor'}]")

        # ---- build line items + categorize each ------------------------------------
        line_items: list[LineItem] = []
        low_conf_extracted_items_for_question: list[tuple[int, LineItem, str]] = []
        # index (1-based) used in chat UX

        for idx, ext in enumerate(extraction.line_items, start=1):
            cat, _used_llm = categorize_item(
                ext.name, categories,
                llm_fallback=categorize_with_llm,
                persist_keywords=self.store.append_category_keywords,
            )
            li = LineItem(
                receipt_id=receipt.id,
                name=ext.name,
                quantity=ext.quantity,
                price=ext.total_price,
                category_id=cat.id,
                confidence=ext.confidence,
                needs_review=False,
                position=idx,
            )
            item_conf_ok = ext.confidence >= self.per_item_conf_threshold
            if not item_conf_ok:
                li.needs_review = True
                low_conf_extracted_items_for_question.append((idx, li, extraction.vendor or "the vendor"))

            line_items.append(li)

        saved_items = self.store.create_line_items(line_items)

        # ---- confidence routing (architecture.md §11) -------------------------
        all_items_clear = not any(li.needs_review for li in saved_items)
        overall_clear = extraction.overall_confidence >= self.overall_conf_threshold

        if overall_clear and all_items_clear:
            self.store.update_receipt_status(receipt.id, ReceiptStatus.CONFIRMED)
            receipt.status = ReceiptStatus.CONFIRMED
            msg = format_receipt(saved_items, categories, receipt)
            self._send_and_remember(member.id, from_phone, msg)
            return msg

        # Otherwise: needs review. One targeted clarification for each unclear item
        self.store.update_receipt_status(receipt.id, ReceiptStatus.NEEDS_REVIEW)
        questions: list[str] = []
        for idx, li, vendor in low_conf_extracted_items_for_question:
            vendor_text = f"from {vendor}" if vendor else "from the receipt"
            q_text = f"Quick check — item {idx} {vendor_text}: I saw '{li.name}' for {_format_ngn(li.price)}. Did I get that right? If yes reply 'ok {idx}', if not correct me (e.g. 'item {idx} price was 1500 and name was indomie')."
            cl = Clarification(
                receipt_id=receipt.id,
                line_item_id=li.id,
                question=q_text,
            )
            self.store.create_clarification(cl)
            questions.append(q_text)
        # If overall confidence was low but no individual item was flagged (rare),
        # create a catch-all so the receipt doesn't sit in needs_review forever.
        if not questions:
            q_text = f"A couple things on this receipt were a little fuzzy — could you quickly confirm: vendor {extraction.vendor or '(unknown)'}, total {_format_ngn(extraction.total or Decimal(0))}? Reply 'ok' if correct, or describe what's off."
            cl = Clarification(receipt_id=receipt.id, line_item_id=None, question=q_text)
            self.store.create_clarification(cl)
            questions.append(q_text)

        combined = "\n\n".join(questions)
        if len(questions) > 1:
            combined += "\n\nPlease start each answer with its item number (e.g. 'ok 2')."
        self._send_and_remember(member.id, from_phone, combined)
        return combined

    def _send_and_remember(self, member_id, phone: str, text: str) -> None:
        self.whatsapp.send_text(phone, text)
        self.memory.record(member_id, "assistant", text)

    def _format_confirmation(self, items: list[LineItem], categories: list[Category], receipt: Receipt) -> str:
        return format_receipt(items, categories, receipt)

    # ---- text messages: onboarding here, everything else -> conversation agent ----
    def handle_text(self, from_phone: str, text: str) -> str:
        # 1. Onboarding in progress? Text is a name.
        if is_pending_onboard(from_phone):
            self.complete_onboarding(from_phone, text)
            reply = f"Thanks {text.strip().split()[0]}! Got you down as '{text.strip()}' 🙂. Send a receipt photo anytime to log it."
            self.whatsapp.send_text(from_phone, reply)
            return reply

        member = self.store.get_member_by_phone(from_phone)
        if member is None:
            # Unknown sender sending text before sending an image: treat as onboarding request.
            mark_pending_onboard(from_phone)
            prompt = "Hi! Before I can log receipts, I need to know who you are. Reply with just your first name (e.g. 'Aisha')."
            self.whatsapp.send_text(from_phone, prompt)
            return prompt

        # 2. Everything else: the agent decides the intent from the message plus
        # memory and live data, validates it, then acts (app/services/agent.py).
        return self.agent.handle(member, text)


def process_inbound_message(
    pipe: CapturePipeline, msg: WhatsAppInboundMessage, whatsapp: WhatsAppClient
) -> str:
    """Route one normalized inbound message through onboarding → image/text handling.

    Shared by the real webhook route and the local `/test/message` route so both
    exercise the exact same dispatch logic. Each call is one Langfuse trace.
    """
    kind = "image" if (msg.image_id or msg.image_bytes is not None) else ("text" if msg.text is not None else "other")
    with observability.trace_message(
        msg.from_phone, kind, input=msg.text if kind == "text" else f"[{kind} message]"
    ) as trace:
        result = _dispatch(pipe, msg, whatsapp)
        trace.update(output=result)
        return result


def _dispatch(pipe: CapturePipeline, msg: WhatsAppInboundMessage, whatsapp: WhatsAppClient) -> str:
    member_exists, question = pipe.handle_known_sender(msg.from_phone)
    if not member_exists:
        if is_pending_onboard(msg.from_phone) and msg.text is not None:
            # This is the reply to the onboarding prompt (their name) — let
            # handle_text run complete_onboarding instead of re-prompting forever.
            return pipe.handle_text(msg.from_phone, msg.text)
        mark_pending_onboard(msg.from_phone)
        whatsapp.send_text(msg.from_phone, question)
        return "onboarding-prompted"

    if msg.image_id and msg.image_bytes is None:
        try:
            msg.image_bytes = whatsapp.download_image(msg.image_id)
        except Exception as e:
            logger.warning("Couldn't download WhatsApp image %s: %s", msg.image_id, e)
            whatsapp.send_text(msg.from_phone, "I couldn't load that image — could you try sending it again?")
            return "image-download-failed"

    if msg.image_bytes is not None:
        return pipe.process_image(msg.from_phone, msg.image_bytes, msg.image_id or "")
    elif msg.text is not None:
        return pipe.handle_text(msg.from_phone, msg.text)
    else:
        whatsapp.send_text(msg.from_phone, "I only understand photos of receipts or correction messages right now 🙂")
        return "ignored"
