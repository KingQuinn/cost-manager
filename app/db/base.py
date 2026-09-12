from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from typing import Optional
from uuid import UUID

from app.models import (
    Category,
    Clarification,
    Family,
    LineItem,
    Member,
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
    def resolve_clarification(self, clarification_id: UUID, answer: str) -> Clarification: ...

    # ---- weekly summaries -------------------------------------------------
    @abstractmethod
    def create_weekly_summary(self, summary: WeeklySummary) -> WeeklySummary: ...

    @abstractmethod
    def list_weekly_summaries(self, family_id: UUID) -> list[WeeklySummary]: ...

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
