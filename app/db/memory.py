from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Optional
from uuid import UUID

from app.db.base import DataStore, normalize_breakdown, squash
from app.models import (
    Category,
    Clarification,
    ConversationTurn,
    Family,
    LineItem,
    Member,
    MemberMemory,
    Receipt,
    WeeklySummary,
)
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
        self._turns: dict[UUID, list[ConversationTurn]] = defaultdict(list)
        self._member_state: dict[UUID, dict] = {}
        self._memories: dict[UUID, list[MemberMemory]] = defaultdict(list)
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

    def update_receipt(
        self,
        receipt_id: UUID,
        *,
        vendor: Optional[str] = None,
        purchase_date: Optional[date] = None,
        total: Optional[Decimal] = None,
        raw_extraction: Optional[dict] = None,
    ) -> None:
        r = self._receipts.get(receipt_id)
        if r is None:
            return
        if vendor is not None:
            r.vendor = vendor
        if purchase_date is not None:
            r.purchase_date = purchase_date
        if total is not None:
            r.total = total
        if raw_extraction is not None:
            r.raw_extraction = raw_extraction

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
        items = [self._line_items[i] for i in ids if i in self._line_items]
        return sorted(items, key=lambda li: li.position)

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

    def list_open_clarifications_for_member(self, member_id: UUID) -> list[Clarification]:
        mine = {r.id for r in self._receipts.values() if r.member_id == member_id}
        out = [c for c in self._clarifications.values() if not c.resolved and c.receipt_id in mine]
        out.sort(key=lambda c: c.created_at)
        return out

    def resolve_clarification(self, clarification_id: UUID, answer: str) -> Clarification:
        c = self._clarifications[clarification_id]
        c.resolved = True
        c.answer = answer
        return c

    # ---------- conversation memory ----------
    def add_conversation_turn(self, turn: ConversationTurn) -> ConversationTurn:
        self._turns[turn.member_id].append(turn)
        return turn

    def list_conversation_turns(
        self, member_id: UUID, limit: Optional[int] = None
    ) -> list[ConversationTurn]:
        turns = sorted(self._turns.get(member_id, []), key=lambda t: t.created_at)
        return turns[-limit:] if limit else turns

    def delete_conversation_turns(self, turn_ids: list[UUID]) -> None:
        drop = set(turn_ids)
        for mid in list(self._turns):
            self._turns[mid] = [t for t in self._turns[mid] if t.id not in drop]

    def get_member_state(self, member_id: UUID) -> dict:
        return dict(self._member_state.get(member_id, {}))

    def save_member_state(self, member_id: UUID, state: dict) -> None:
        self._member_state[member_id] = dict(state)

    def list_member_memories(self, member_id: UUID) -> list[MemberMemory]:
        return sorted(self._memories.get(member_id, []), key=lambda m: m.created_at)

    def add_member_memory(self, memory: MemberMemory) -> MemberMemory:
        self._memories[memory.member_id].append(memory)
        return memory

    def delete_member_memory(self, memory_id: UUID) -> None:
        for mid in list(self._memories):
            self._memories[mid] = [m for m in self._memories[mid] if m.id != memory_id]

    # ---------- lookups for the conversation agent ----------
    def latest_receipt_for_member(self, member_id: UUID) -> Optional[Receipt]:
        mine = [r for r in self._receipts.values() if r.member_id == member_id]
        return max(mine, key=lambda r: r.created_at) if mine else None

    def find_member_line_items(
        self, member_id: UUID, words: list[str], limit: int = 50
    ) -> list[tuple[LineItem, Receipt]]:
        want = [w for w in (squash(x) for x in words) if w]
        if not want:
            return []
        receipts = sorted((r for r in self._receipts.values() if r.member_id == member_id),
                          key=lambda r: r.created_at, reverse=True)
        out = []
        for r in receipts:
            for li in self.list_line_items(r.id):
                if all(w in squash(li.name) for w in want):
                    out.append((li, r))
        return out[:limit]

    def remove_category_keywords(self, category_id: UUID, keywords: list[str]) -> None:
        cat = self._categories.get(category_id)
        if cat is None:
            return
        drop = {k.lower() for k in keywords}
        cat.keywords = [k for k in cat.keywords if k.lower() not in drop]

    # ---------- spending questions ----------
    def spending_breakdown(
        self,
        family_id: UUID,
        *,
        member_id: Optional[UUID] = None,
        start: Optional[date] = None,
        end: Optional[date] = None,
        category_id: Optional[UUID] = None,
        item_terms: Optional[list[str]] = None,
    ) -> dict:
        """Python mirror of the `spending_breakdown` SQL function (migrations/0004)."""
        members = {m.id: m for m in self.list_members(family_id)}
        cats = {c.id: c for c in self._categories.values()}
        terms = [t for t in (squash(x) for x in item_terms or []) if t]
        in_range = [
            r for r in self.list_receipts(family_id=family_id, start=start, end=end)
            if member_id is None or r.member_id == member_id
        ]
        rows = []
        for r in in_range:
            if r.status != "confirmed":
                continue
            for li in self.list_line_items(r.id):
                if category_id is not None and li.category_id != category_id:
                    continue
                if terms and not any(t in squash(li.name) for t in terms):
                    continue
                cat = cats.get(li.category_id)
                who = members.get(r.member_id)
                rows.append({
                    "price": li.price, "item": li.name, "category": cat.name if cat else "Other",
                    "member": who.name if who else "Unknown", "vendor": r.vendor or "Unknown vendor",
                    "receipt_id": r.id, "date": (r.purchase_date or r.created_at.date()).isoformat(),
                })

        def group(key: str) -> list[dict]:
            sums: dict[str, Decimal] = defaultdict(Decimal)
            ids: dict[str, set] = defaultdict(set)
            for row in rows:
                sums[row[key]] += row["price"]
                ids[row[key]].add(row["receipt_id"])
            ordered = sorted(sums.items(), key=lambda kv: (-kv[1], kv[0]))
            return [{"name": k, "amount": v, "receipts": len(ids[k])} for k, v in ordered]

        pending = [r for r in in_range if r.status == "needs_review"]
        return normalize_breakdown({
            "total": sum((row["price"] for row in rows), Decimal("0")),
            "confirmed_receipts": len({row["receipt_id"] for row in rows}),
            "by_category": [{"name": g["name"], "amount": g["amount"]} for g in group("category")],
            "by_member": [{"name": g["name"], "amount": g["amount"]} for g in group("member")],
            "top_vendors": group("vendor")[:3],
            "top_items": [
                {"item": row["item"], "amount": row["price"], "category": row["category"],
                 "vendor": row["vendor"], "date": row["date"]}
                for row in sorted(rows, key=lambda row: (-row["price"], row["item"]))[:5]
            ],
            "first_date": min((row["date"] for row in rows), default=None),
            "last_date": max((row["date"] for row in rows), default=None),
            "pending_receipts": len(pending),
            "pending_amount": sum((r.total or Decimal("0") for r in pending), Decimal("0")),
        })

    # ---------- weekly summaries ----------
    def create_weekly_summary(self, summary: WeeklySummary) -> WeeklySummary:
        self._weekly_summaries[summary.id] = summary
        return summary

    def list_weekly_summaries(self, family_id: UUID) -> list[WeeklySummary]:
        return [s for s in self._weekly_summaries.values() if s.family_id == family_id]
