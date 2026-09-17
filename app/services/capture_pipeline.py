from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Optional

from app.config import get_settings
from app.db import DataStore
from app.models import Category, Clarification, LineItem, Receipt, ReceiptStatus, WhatsAppInboundMessage
from app.services.categorize import categorize_item
from app.services.extraction import categorize_with_llm, extract_receipt
from app.services.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

# Small in-process cache for "sender is mid-onboarding (texts their name)".
# Resets on restart — fine for local dev; in production we'd persist this in the DB.
_PENDING_ONBOARD: dict[str, bool] = {}


def is_pending_onboard(phone: str) -> bool:
    return _PENDING_ONBOARD.get(phone, False)


def mark_pending_onboard(phone: str) -> None:
    _PENDING_ONBOARD[phone] = True


def _format_ngn(n: Decimal) -> str:
    q = n.quantize(Decimal("0.01"))
    s = f"{q:,.2f}" if q != q.to_integral() else f"{q:,.0f}"
    return f"₦{s}"


class CapturePipeline:
    """Orchestrates the Path-1 capture flow for one receipt:

    inbound image → extract → categorize → write → reply (confirm or question).

    Split out of the FastAPI route handlers so it's unit-testable and framework-agnostic.
    """

    def __init__(self, store: DataStore, whatsapp: WhatsAppClient) -> None:
        self.store = store
        self.whatsapp = whatsapp
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
            self.store.create_receipt(receipt)
            msg = (
                "Hmm, I couldn't tell that was a receipt (the words were too unclear or it isn't one). "
                "Could you try another photo, or describe the purchase in words?"
            )
            self.whatsapp.send_text(from_phone, msg)
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
        self.store.create_receipt(receipt)

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
            msg = self._format_confirmation(saved_items, categories, receipt)
            self.whatsapp.send_text(from_phone, msg)
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
        self.whatsapp.send_text(from_phone, combined)
        return combined

    def _format_confirmation(self, items: list[LineItem], categories: list[Category], receipt: Receipt) -> str:
        cat_by_id = {c.id: c for c in categories}
        lines = [f"✅ Logged {len(items)} item(s) from {receipt.vendor or 'this receipt'}.\n"]
        total = Decimal("0")
        by_category: dict[str, Decimal] = {}
        for idx, li in enumerate(items, 1):
            cat = cat_by_id.get(li.category_id).name if cat_by_id.get(li.category_id) else "Other"
            lines.append(f"{idx}. {li.name} · {_format_ngn(li.price)} · {cat}")
            total += li.price
            by_category[cat] = by_category.get(cat, Decimal("0")) + li.price
        lines.append("")
        lines.append(f"Total: {_format_ngn(total)}")

        if len(by_category) > 1:
            lines.append("")
            lines.append("By category:")
            for cat_name, amt in sorted(by_category.items(), key=lambda kv: -kv[1]):
                pct = int((amt / total * 100).to_integral()) if total else 0
                lines.append(f"  {cat_name}: {_format_ngn(amt)} ({pct}%)")

        lines.append("")
        lines.append("To correct any item, reply e.g. 'item 2 was 2500 and category was Dining'.")
        return "\n".join(lines)

    # ---- text reply handling (corrections + clarifications + onboarding ------------
    def handle_text(self, from_phone: str, text: str) -> str:
        # 1. Onboarding in progress? Text is a name.
        if is_pending_onboard(from_phone):
            self.complete_onboarding(from_phone, text)
            reply = f"Thanks {text.strip().split()[0]}! Got you down as '{text.strip()} 🙂. Send a receipt photo anytime to log it."
            self.whatsapp.send_text(from_phone, reply)
            return reply

        member = self.store.get_member_by_phone(from_phone)
        if member is None:
            # Unknown sender sending text before sending an image: treat as onboarding request.
            mark_pending_onboard(from_phone)
            prompt = "Hi! Before I can log receipts, I need to know who you are. Reply with just your first name (e.g. 'Aisha')."
            self.whatsapp.send_text(from_phone, prompt)
            return prompt

        # 2. Open clarification to resolve?
        cl = self.store.get_open_clarification_for_member(member.id)
        if cl is not None:
            return self._resolve_clarification(member, cl, text, from_phone)

        # 3. A known keyword intent (help / recap / running total)? Checked before
        # the correction parser since none of these contain an item number.
        intent_reply = self._handle_known_intent(member, text, from_phone)
        if intent_reply is not None:
            return intent_reply

        # 4. Correction against most recent confirmed receipt? e.g. "item 2 price was 1500 category groceries"
        return self._apply_freeform_correction(member, text, from_phone)

    _HELP_TEXT = (
        "Here's what I can do:\n"
        "- Send a photo of a receipt to log it\n"
        "- \"show my items\" — see what's on your last receipt\n"
        "- \"how much have I spent\" — your running total by category\n"
        "- \"item 2 price was 2500\" — correct a logged item\n"
        "- \"help\" — see this again"
    )

    def _handle_known_intent(self, member, text: str, from_phone: str) -> Optional[str]:
        """Simple keyword matching — no LLM call, kept intentionally cheap. Checked
        before the correction parser so plain-English asks don't get mistaken for a
        failed correction attempt."""
        t = text.strip().lower()

        if re.search(r"\bhelp\b", t) or t in ("?", "commands"):
            self.whatsapp.send_text(from_phone, self._HELP_TEXT)
            return self._HELP_TEXT

        if ("show" in t or "list" in t or "see" in t) and ("item" in t or "log" in t):
            reply = self._format_last_receipt_recap(member)
            self.whatsapp.send_text(from_phone, reply)
            return reply

        if "spent" in t or "spending" in t or "how much" in t:
            reply = self._format_member_spend(member)
            self.whatsapp.send_text(from_phone, reply)
            return reply

        return None

    def _format_last_receipt_recap(self, member) -> str:
        recent = self.store.list_receipts(family_id=member.family_id)
        my_recent = [r for r in recent if r.member_id == member.id]
        if not my_recent:
            return "You haven't logged any receipts yet — send a photo to get started 🙂."
        receipt = my_recent[0]
        items = self.store.list_line_items(receipt.id)
        categories = self.store.list_categories()
        return self._format_confirmation(items, categories, receipt)

    def _format_member_spend(self, member) -> str:
        receipts = [
            r for r in self.store.list_receipts(family_id=member.family_id, status=ReceiptStatus.CONFIRMED)
            if r.member_id == member.id
        ]
        if not receipts:
            return "You don't have any confirmed receipts yet — send a photo to log one 🙂."

        categories = self.store.list_categories()
        cat_by_id = {c.id: c for c in categories}
        by_category: dict[str, Decimal] = {}
        total = Decimal("0")
        for r in receipts:
            for li in self.store.list_line_items(r.id):
                cat_name = cat_by_id.get(li.category_id).name if cat_by_id.get(li.category_id) else "Other"
                by_category[cat_name] = by_category.get(cat_name, Decimal("0")) + li.price
                total += li.price

        lines = [f"Your spending so far ({len(receipts)} confirmed receipt(s)):", "", f"Total: {_format_ngn(total)}"]
        if len(by_category) > 1:
            lines.append("")
            lines.append("By category:")
            for name, amt in sorted(by_category.items(), key=lambda kv: -kv[1]):
                pct = int((amt / total * 100).to_integral()) if total else 0
                lines.append(f"  {name}: {_format_ngn(amt)} ({pct}%)")
        return "\n".join(lines)

    def _resolve_clarification(self, member, cl: Clarification, text: str, from_phone: str) -> str:
        answer = text.strip()
        self.store.resolve_clarification(cl.id, answer)

        # Best-effort apply the answer to the linked line item, if any.
        if cl.line_item_id is not None:
            li = self.store.get_line_item(cl.line_item_id)
            if li is not None:
                categories = self.store.list_categories()
                cat_by_name = {c.name.lower(): c for c in categories}
                self._apply_text_patch_to_line_item(li, answer, cat_by_name)
                # Also clear needs_review now that the clarification is resolved.
                self.store.update_line_item(li.id, needs_review=False)

        # If no more open clarifications for this receipt -> flip to confirmed.
        receipt_id = cl.receipt_id
        receipt = self.store.get_receipt(receipt_id)
        items = self.store.list_line_items(receipt_id) if receipt else []
        any_needs_review = any(li.needs_review for li in items)
        if not any_needs_review and receipt is not None:
            self.store.update_receipt_status(receipt_id, ReceiptStatus.CONFIRMED)
            self.whatsapp.send_text(from_phone, "Got it, thanks! The receipt is now confirmed ✅")
            return "resolved and receipt confirmed"
        self.whatsapp.send_text(from_phone, "Got it, thanks!")
        return "resolved"

    def _apply_freeform_correction(self, member, text: str, from_phone: str) -> str:
        # No digit at all -> this isn't an attempted correction (e.g. "hey", "thanks"),
        # so don't imply we tried and failed to parse one.
        if not re.search(r"\d", text):
            reply = "Hey! Send a photo of a receipt to log a purchase, or reply to a recent one with a correction like 'item 2 price was 2500'."
            self.whatsapp.send_text(from_phone, reply)
            return reply

        # Find latest receipt (any status).
        recent = self.store.list_receipts(family_id=member.family_id)
        my_recent = [r for r in recent if r.member_id == member.id]
        if not my_recent:
            reply = "Nothing to correct yet — send a receipt photo first 🙂."
            self.whatsapp.send_text(from_phone, reply)
            return reply
        receipt = my_recent[0]
        items = self.store.list_line_items(receipt.id)
        categories = self.store.list_categories()
        cat_by_name = {c.name.lower(): c for c in categories}

        patched_any = False
        for m in re.finditer(r"(?:item\s*)?(\d+)\b", text, re.IGNORECASE):
            try:
                idx1 = int(m.group(1))
            except ValueError:
                continue
            if 1 <= idx1 <= len(items):
                li = items[idx1 - 1]
                self._apply_text_patch_to_line_item(li, text, cat_by_name)
                patched_any = True

        if patched_any:
            # Re-check if all line items are now clean -> promote to confirmed
            fresh = self.store.list_line_items(receipt.id)
            if not any(li.needs_review for li in fresh):
                self.store.update_receipt_status(receipt.id, ReceiptStatus.CONFIRMED)
            reply = "Correction applied ✅"
            self.whatsapp.send_text(from_phone, reply)
            return reply

        reply = (
            "I didn't spot a correction pattern I understand. Try: 'item 2 price was 2500', "
            "'item 3 was Groceries', or 'item 1 name was Indomie & price 1500'."
        )
        self.whatsapp.send_text(from_phone, reply)
        return reply

    def _apply_text_patch_to_line_item(self, li: LineItem, text: str, cat_by_name: dict[str, Category]) -> None:
        """Parse a freeform correction and patch price/category/name on a line item.
        Intentionally simple regex-based heuristic, not an LLM call — corrections are cheap and short.

        Patterns supported:
          - "price was X" / "price X" / "was X" (X digits) -> price
          - "category was X" / X ∈ Groceries/Transport/... (case-insensitive)
          - "name was X" or any long string afterwards -> name
        """
        t = text.lower()
        # Price: any digit sequence with optional decimals after a price/was/for.
        price_match = re.search(r"(?:price|was|for)\s*[₦n]?\s*([0-9][0-9,]*(?:\.\d+)?)", t)
        if not price_match:
            # Bare number near a price-ish indicator: second try: any number after 'item N '
            after_idx = re.search(r"item\s+\d+\s+(?:.*?)([0-9][0-9,]*(?:\.\d+)?)$", t)
            if after_idx:
                price_match = after_idx
        if price_match:
            raw = price_match.group(1).replace(",", "")
            try:
                new_price = Decimal(raw)
                self.store.update_line_item(li.id, price=new_price.quantize(Decimal("0.01")))
            except Exception:
                pass

        # Category: any exact category name in text.
        for name_lc, cat in cat_by_name.items():
            if re.search(rf"\b{re.escape(name_lc)}\b", t):
                self.store.update_line_item(li.id, category_id=cat.id)
                break

        # Name: "name was <something>" up to end or next dot/period delimiter.
        name_m = re.search(r"name\s+was\s+(.+?)(?:\.|$)", text, re.IGNORECASE)
        if name_m:
            new_name = name_m.group(1).strip()
            if new_name:
                self.store.update_line_item(li.id, name=new_name)


def process_inbound_message(
    pipe: CapturePipeline, msg: WhatsAppInboundMessage, whatsapp: WhatsAppClient
) -> str:
    """Route one normalized inbound message through onboarding → image/text handling.

    Shared by the real webhook route and the local `/test/message` route so both
    exercise the exact same dispatch logic.
    """
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
