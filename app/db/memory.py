from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Optional
from uuid import UUID

from app.db.base import DataStore
from app.models import Category, Clarification, Family, LineItem, Member, Receipt, WeeklySummary
from app.services.categorize import build_default_categories


class InMemoryStore(DataStore):
    """Process-local store — used until USE_SUPABASE=true is set. Great for local dev/tests."""

    def __init__(self) -> None:
        self._families: dict[UUID, Family] = {}
        self._members: dict[UUID, Member] = {}
        self._members_by_phone: dict[str, UUID] = {}
        self._categories: dict[UUID, Category] = {}
        self._receipts: dict[UUID, Receipt] = {}
        self._line_items: dict[UUID, LineItem] = {}
        self._line_items_by_receipt: dict[UUID, list[UUID]] = defaultdict(list)
        self._clarifications: dict[UUID, Clarification] = {}
        self._clarifications_by_receipt: dict[UUID, list[UUID]] = defaultdict(list)
        self._weekly_summaries: dict[UUID, WeeklySummary] = {}
        # Seed categories on startup.
        for c in build_default_categories():
            self._categories[c.id] = c
        # Seed a default family and one test member (overridden once real onboarding runs).
        fam = Family(name="Family")
        self._families[fam.id] = fam
        self._default_family_id = fam.id

    # ---------- families & members ----------
    def get_or_create_default_family(self, name: str = "Family") -> Family:
        fam = next(iter(self._families.values())) if self._families else None
        if fam is None:
            fam = Family(name=name)
            self._families[fam.id] = fam
            self._default_family_id = fam.id
        return fam

    def get_member_by_phone(self, phone: str) -> Optional[Member]:
        mid = self._members_by_phone.get(phone)
        return self._members.get(mid) if mid else None

    def create_member(self, family_id: UUID, name: str, phone: str) -> Member:
        phone = phone.strip()
        m = Member(family_id=family_id, name=name.strip(), phone_number=phone)
        self._members[m.id] = m
        self._members_by_phone[phone] = m.id
        return m

    def list_members(self, family_id: UUID) -> list[Member]:
        return [m for m in self._members.values() if m.family_id == family_id]

    # ---------- categories ----------
    def list_categories(self) -> list[Category]:
        return list(self._categories.values())

    def append_category_keywords(self, category_id: UUID, new_keywords: list[str]) -> None:
        cat = self._categories.get(category_id)
        if cat is None:
            return
        seen = {k.lower() for k in cat.keywords}
        for kw in new_keywords:
            if kw.lower() not in seen:
                cat.keywords.append(kw)
                seen.add(kw.lower())

    # ---------- receipts ----------
    def create_receipt(self, receipt: Receipt) -> Receipt:
        self._receipts[receipt.id] = receipt
        return receipt

    def get_receipt(self, receipt_id: UUID) -> Optional[Receipt]:
        return self._receipts.get(receipt_id)

    def update_receipt_status(self, receipt_id: UUID, status: str) -> None:
        r = self._receipts.get(receipt_id)
        if r is not None:
            r.status = status

    def list_receipts(
        self,
        family_id: Optional[UUID] = None,
        status: Optional[str] = None,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> list[Receipt]:
        out = []
        member_ids = None
        if family_id is not None:
            member_ids = {m.id for m in self.list_members(family_id)}
        for r in self._receipts.values():
            if member_ids is not None and r.member_id not in member_ids:
                continue
            if status is not None and r.status != status:
                continue
            d = r.purchase_date or r.created_at.date()
            if start is not None and d < start:
                continue
            if end is not None and d > end:
                continue
            out.append(r)
        out.sort(key=lambda r: r.created_at, reverse=True)
        return out

    def create_line_items(self, items: list[LineItem]) -> list[LineItem]:
        for it in items:
            self._line_items[it.id] = it
            self._line_items_by_receipt[it.receipt_id].append(it.id)
        return list(items)

    def list_line_items(self, receipt_id: UUID) -> list[LineItem]:
        ids = self._line_items_by_receipt.get(receipt_id, [])
        return [self._line_items[i] for i in ids if i in self._line_items]

    def update_line_item(
        self,
        line_item_id: UUID,
        *,
        name: Optional[str] = None,
        price: Optional[Decimal] = None,
        category_id: Optional[UUID] = None,
        needs_review: Optional[bool] = None,
        confidence: Optional[float] = None,
    ) -> LineItem:
        li = self._line_items[line_item_id]
        if name is not None:
            li.name = name
        if price is not None:
            li.price = price
        if category_id is not None:
            li.category_id = category_id
        if needs_review is not None:
            li.needs_review = needs_review
        if confidence is not None:
            li.confidence = confidence
        return li

    def get_line_item(self, line_item_id: UUID) -> Optional[LineItem]:
        return self._line_items.get(line_item_id)

    # ---------- clarifications ----------
    def create_clarification(self, clarification: Clarification) -> Clarification:
        self._clarifications[clarification.id] = clarification
        self._clarifications_by_receipt[clarification.receipt_id].append(clarification.id)
        return clarification

    def get_open_clarification_for_member(self, member_id: UUID) -> Optional[Clarification]:
        """Return the oldest unresolved clarification for a member, so a reply can be matched to it.

        We find all receipts for the member, find unresolved clarifications,
        then take the most recent created. This mirrors what a real SQL query would
        do (open clarifications, ordered by created_at ASC) for the member's latest open question.
        """
        open_for = [
            c
            for c in self._clarifications.values()
            if not c.resolved
        ]
        receipts_by_id = {r.id: r for r in self._receipts.values() if r.member_id == member_id}
        candidates = [c for c in open_for if c.receipt_id in receipts_by_id]
        if not candidates:
            return None
        candidates.sort(key=lambda c: c.created_at)
        return candidates[-1]

    def resolve_clarification(self, clarification_id: UUID, answer: str) -> Clarification:
        c = self._clarifications[clarification_id]
        c.resolved = True
        c.answer = answer
        return c

    # ---------- weekly summaries ----------
    def create_weekly_summary(self, summary: WeeklySummary) -> WeeklySummary:
        self._weekly_summaries[summary.id] = summary
        return summary

    def list_weekly_summaries(self, family_id: UUID) -> list[WeeklySummary]:
        return [s for s in self._weekly_summaries.values() if s.family_id == family_id]
