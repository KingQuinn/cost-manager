from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from typing import Optional
from uuid import UUID

from app.models import (
    Category,
    Clarification,
    ConversationTurn,
    Family,
    LineItem,
    Member,
    MemberMemory,
    Receipt,
    ReceiptStatus,
    WeeklySummary,
)


class DataStore(ABC):
    """Abstract interface. Swap implementations: InMemoryStore vs SupabaseStore. Both speak the
    same method signatures, so all callers are backend-agnostic."""

    # ---- families / members ------------------------------------------------
    @abstractmethod
    def get_or_create_default_family(self, name: str = "Family") -> Family: ...

    @abstractmethod
    def get_member_by_phone(self, phone: str) -> Optional[Member]: ...

    @abstractmethod
    def create_member(self, family_id: UUID, name: str, phone: str) -> Member: ...

    @abstractmethod
    def list_members(self, family_id: UUID) -> list[Member]: ...

    # ---- categories -------------------------------------------------------
    @abstractmethod
    def list_categories(self) -> list[Category]: ...

    @abstractmethod
    def append_category_keywords(self, category_id: UUID, new_keywords: list[str]) -> None: ...

    # ---- receipts / line items ------------------------------------------
    @abstractmethod
    def create_receipt(self, receipt: Receipt) -> Receipt: ...

    @abstractmethod
    def get_receipt(self, receipt_id: UUID) -> Optional[Receipt]: ...

    @abstractmethod
    def update_receipt_status(self, receipt_id: UUID, status: str) -> None: ...

    @abstractmethod
    def update_receipt(
        self,
        receipt_id: UUID,
        *,
        vendor: Optional[str] = None,
        purchase_date: Optional[date] = None,
        total: Optional[Decimal] = None,
        raw_extraction: Optional[dict] = None,
    ) -> None:
        """Patch receipt fields (used when a member describes an unreadable receipt in words)."""

    @abstractmethod
    def list_receipts(
        self,
        family_id: Optional[UUID] = None,
        status: Optional[str] = None,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> list[Receipt]: ...

    @abstractmethod
    def create_line_items(self, items: list[LineItem]) -> list[LineItem]: ...

    @abstractmethod
    def list_line_items(self, receipt_id: UUID) -> list[LineItem]: ...

    @abstractmethod
    def update_line_item(
        self,
        line_item_id: UUID,
        *,
        name: Optional[str] = None,
        price: Optional[Decimal] = None,
        category_id: Optional[UUID] = None,
        needs_review: Optional[bool] = None,
        confidence: Optional[float] = None,
    ) -> LineItem: ...

    @abstractmethod
    def get_line_item(self, line_item_id: UUID) -> Optional[LineItem]: ...

    # ---- clarifications ---------------------------------------------------
    @abstractmethod
    def create_clarification(self, clarification: Clarification) -> Clarification: ...

    @abstractmethod
    def get_open_clarification_for_member(
        self, member_id: UUID
    ) -> Optional[Clarification]: ...

    @abstractmethod
    def list_open_clarifications_for_member(self, member_id: UUID) -> list[Clarification]:
        """Every unresolved clarification on this member's receipts, oldest first.

        The conversation agent matches a reply to one of these by item number —
        never by "whichever row the database returns first" (architecture.md v1.5).
        """

    @abstractmethod
    def resolve_clarification(self, clarification_id: UUID, answer: str) -> Clarification: ...

    # ---- conversation memory (architecture.md §7.7) -------------------------
    # Layer 2 — conversation state: recent chat turns.
    @abstractmethod
    def add_conversation_turn(self, turn: ConversationTurn) -> ConversationTurn: ...

    @abstractmethod
    def list_conversation_turns(
        self, member_id: UUID, limit: Optional[int] = None
    ) -> list[ConversationTurn]:
        """The member's most recent `limit` turns (all if None), oldest first."""

    @abstractmethod
    def delete_conversation_turns(self, turn_ids: list[UUID]) -> None: ...

    # Layer 2 — conversation state: small JSON blob per member.
    @abstractmethod
    def get_member_state(self, member_id: UUID) -> dict: ...

    @abstractmethod
    def save_member_state(self, member_id: UUID, state: dict) -> None: ...

    # Layer 3 — long-term memory: durable facts + rolling conversation summary.
    @abstractmethod
    def list_member_memories(self, member_id: UUID) -> list[MemberMemory]:
        """All of a member's long-term memories, oldest first."""

    @abstractmethod
    def add_member_memory(self, memory: MemberMemory) -> MemberMemory: ...

    @abstractmethod
    def delete_member_memory(self, memory_id: UUID) -> None: ...

    # ---- weekly summaries -------------------------------------------------
    @abstractmethod
    def create_weekly_summary(self, summary: WeeklySummary) -> WeeklySummary: ...

    @abstractmethod
    def list_weekly_summaries(self, family_id: UUID) -> list[WeeklySummary]: ...

    # ---- spending questions (architecture.md §7.6) ---------------------------
    @abstractmethod
    def spending_breakdown(
        self,
        family_id: UUID,
        *,
        member_id: Optional[UUID] = None,
        start: Optional[date] = None,
        end: Optional[date] = None,
        category_id: Optional[UUID] = None,
        item_query: Optional[str] = None,
    ) -> dict:
        """Totals for a spending question, computed by the database.

        Supabase runs the `spending_breakdown` SQL function
        (supabase/migrations/0003); the in-memory store mirrors it. Shape, with
        every amount a Decimal:
          {"total", "confirmed_receipts",
           "by_category": [{"name", "amount"}], "by_member": [{"name", "amount"}],
           "top_vendors": [{"name", "amount", "receipts"}],
           "top_items": [{"item", "amount", "category", "vendor", "date"}],
           "pending_receipts", "pending_amount"}
        Only confirmed receipts count toward totals; needs_review ones are
        reported separately (architecture.md §9).
        """

    # ---- aggregate helpers -------------------------------------------------
    def all_confirmed_line_items(
        self, family_id: UUID, start: date, end: date
    ) -> list[tuple[LineItem, Category, Member]]:
        """Return (line_item, category, member) for confirmed receipts in [start, end].
        Default impl delegates to list methods. Most callers just need this join."""
        receipts = self.list_receipts(family_id=family_id, status=ReceiptStatus.CONFIRMED, start=start, end=end)
        members_by_id = {m.id: m for m in self.list_members(family_id)}
        cats_by_id = {c.id: c for c in self.list_categories()}
        out: list[tuple[LineItem, Category, Member]] = []
        for r in receipts:
            member = members_by_id.get(r.member_id)
            if member is None:
                continue
            for li in self.list_line_items(r.id):
                cat = cats_by_id.get(li.category_id) if li.category_id else None
                if cat is None:
                    continue
                out.append((li, cat, member))
        return out


def normalize_breakdown(raw: dict) -> dict:
    """Coerce a spending_breakdown result (e.g. JSON numbers from PostgREST) to Decimals."""
    def dec(v) -> Decimal:
        return Decimal(str(v if v is not None else 0)).quantize(Decimal("0.01"))

    def rows(key: str) -> list[dict]:
        return [{**r, "amount": dec(r.get("amount"))} for r in (raw.get(key) or [])]

    return {
        "total": dec(raw.get("total")),
        "confirmed_receipts": int(raw.get("confirmed_receipts") or 0),
        "by_category": rows("by_category"),
        "by_member": rows("by_member"),
        "top_vendors": [{**r, "receipts": int(r.get("receipts") or 0)} for r in rows("top_vendors")],
        "top_items": [{**r, "date": str(r.get("date"))} for r in rows("top_items")],
        "pending_receipts": int(raw.get("pending_receipts") or 0),
        "pending_amount": dec(raw.get("pending_amount")),
    }
