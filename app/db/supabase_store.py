from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional
from uuid import UUID

from app.db.base import DataStore
from app.models import Category, Clarification, Family, LineItem, Member, Receipt, WeeklySummary


class SupabaseStore(DataStore):
    """Thin wrapper around the supabase-py client. Uses raw postgrest calls mirror the
    InMemoryStore method-for-method so callers stay identical.

    Note: schema must already exist in Supabase before this store is used — see the SQL
    in architecture.md §8.2; categories must be seeded.
    """

    def __init__(self, url: str, service_role_key: str):
        from supabase import create_client, Client
        self._client: Client = create_client(url, service_role_key)

    @property
    def client(self):
        return self._client

    # ---------- families & members ----------
    def get_or_create_default_family(self, name: str = "Family") -> Family:
        existing = self._client.table("families").select("*").limit(1).execute()
        if existing.data:
            return Family(**existing.data[0])
        r = self._client.table("families").insert({"name": name}).execute()
        return Family(**r.data[0])

    def get_member_by_phone(self, phone: str) -> Optional[Member]:
        r = self._client.table("members").select("*").eq("phone_number", phone).limit(1).execute()
        return Member(**r.data[0]) if r.data else None

    def create_member(self, family_id: UUID, name: str, phone: str) -> Member:
        r = (
            self._client.table("members")
            .insert({"family_id": str(family_id), "name": name, "phone_number": phone})
            .execute()
        )
        return Member(**r.data[0])

    def list_members(self, family_id: UUID) -> list[Member]:
        r = self._client.table("members").select("*").eq("family_id", str(family_id)).execute()
        return [Member(**row) for row in r.data]

    # ---------- categories ----------
    def list_categories(self) -> list[Category]:
        r = self._client.table("categories").select("*").execute()
        return [Category(**row) for row in r.data]

    def append_category_keywords(self, category_id: UUID, new_keywords: list[str]) -> None:
        # array_cat(keywords, new_keywords) with dedup on the DB side would be
        # nicer; for simplicity we do a read-modify-write (small table, low volume).
        cur = self._client.table("categories").select("*").eq("id", str(category_id)).single().execute()
        existing = list(cur.data.get("keywords") or [])
        seen = {k.lower() for k in existing}
        merged = list(existing)
        for kw in new_keywords:
            if kw.lower() not in seen:
                merged.append(kw)
                seen.add(kw.lower())
        self._client.table("categories").update({"keywords": merged}).eq("id", str(category_id)).execute()

    # ---------- receipts ----------
    def _receipt_to_row(self, r: Receipt) -> dict:
        d = r.model_dump(mode="json")
        return d

    def create_receipt(self, receipt: Receipt) -> Receipt:
        r = self._client.table("receipts").insert(self._receipt_to_row(receipt)).execute()
        return Receipt(**r.data[0])

    def get_receipt(self, receipt_id: UUID) -> Optional[Receipt]:
        r = self._client.table("receipts").select("*").eq("id", str(receipt_id)).limit(1).execute()
        return Receipt(**r.data[0]) if r.data else None

    def update_receipt_status(self, receipt_id: UUID, status: str) -> None:
        self._client.table("receipts").update({"status": status}).eq("id", str(receipt_id)).execute()

    def list_receipts(
        self,
        family_id: Optional[UUID] = None,
        status: Optional[str] = None,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> list[Receipt]:
        q = self._client.table("receipts").select("*")
        if family_id is not None:
            members = self.list_members(family_id)
            q = q.in_("member_id", [str(m.id) for m in members])
        if status is not None:
            q = q.eq("status", status)
        # Coerce date filter against purchase_date with fallback to created_at
        # handled client-side for simplicity (small volume).
        r = q.order("created_at", desc=True).execute()
        out = [Receipt(**row) for row in r.data]
        if start is not None or end is not None:
            filtered = []
            for rct in out:
                d = rct.purchase_date or rct.created_at.date()
                if start is not None and d < start:
                    continue
                if end is not None and d > end:
                    continue
                filtered.append(rct)
            out = filtered
        return out

    def create_line_items(self, items: list[LineItem]) -> list[LineItem]:
        if not items:
            return []
        rows = [li.model_dump(mode="json") for li in items]
        r = self._client.table("line_items").insert(rows).execute()
        return [LineItem(**row) for row in r.data]

    def list_line_items(self, receipt_id: UUID) -> list[LineItem]:
        r = self._client.table("line_items").select("*").eq("receipt_id", str(receipt_id)).execute()
        return [LineItem(**row) for row in r.data]

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
        payload: dict = {}
        if name is not None:
            payload["name"] = name
        if price is not None:
            payload["price"] = float(price)
        if category_id is not None:
            payload["category_id"] = str(category_id)
        if needs_review is not None:
            payload["needs_review"] = needs_review
        if confidence is not None:
            payload["confidence"] = confidence
        r = (
            self._client.table("line_items")
            .update(payload)
            .eq("id", str(line_item_id))
            .select("*")
            .execute()
        )
        return LineItem(**r.data[0])

    def get_line_item(self, line_item_id: UUID) -> Optional[LineItem]:
        r = self._client.table("line_items").select("*").eq("id", str(line_item_id)).limit(1).execute()
        return LineItem(**r.data[0]) if r.data else None

    # ---------- clarifications ----------
    def create_clarification(self, clarification: Clarification) -> Clarification:
        r = self._client.table("clarifications").insert(clarification.model_dump(mode="json")).execute()
        return Clarification(**r.data[0])

    def get_open_clarification_for_member(self, member_id: UUID) -> Optional[Clarification]:
        # Subquery-less approach: receipts for member -> their open clarifications, most recent first.
        r_receipts = self._client.table("receipts").select("id").eq("member_id", str(member_id)).execute()
        receipt_ids = [row["id"] for row in r_receipts.data]
        if not receipt_ids:
            return None
        r = (
            self._client.table("clarifications")
            .select("*")
            .in_("receipt_id", receipt_ids)
            .eq("resolved", False)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return Clarification(**r.data[0]) if r.data else None

    def resolve_clarification(self, clarification_id: UUID, answer: str) -> Clarification:
        r = (
            self._client.table("clarifications")
            .update({"resolved": True, "answer": answer})
            .eq("id", str(clarification_id))
            .select("*")
            .execute()
        )
        return Clarification(**r.data[0])

    # ---------- weekly summaries ----------
    def create_weekly_summary(self, summary: WeeklySummary) -> WeeklySummary:
        r = self._client.table("weekly_summaries").insert(summary.model_dump(mode="json")).execute()
        return WeeklySummary(**r.data[0])

    def list_weekly_summaries(self, family_id: UUID) -> list[WeeklySummary]:
        r = self._client.table("weekly_summaries").select("*").eq("family_id", str(family_id)).execute()
        return [WeeklySummary(**row) for row in r.data]
