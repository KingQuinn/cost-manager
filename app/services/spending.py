"""Spending queries: turn a structured question into numbers from the database.

The conversation agent never lets the LLM do arithmetic or remember totals.
The router turns "how much did we spend on groceries last week?" into a
query spec (scope/period/category); the database sums confirmed receipts
(the `spending_breakdown` SQL function); the LLM only phrases it. Every
amount is pre-formatted here, and the agent checks that each ₦ figure in the
reply came from this data before sending it.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from app.db import DataStore
from app.models import Member

PERIODS = (
    "today", "yesterday", "this_week", "last_week", "last_7_days", "this_month",
    "last_month", "last_30_days", "this_year", "all_time", "custom",
)


def format_ngn(n: Decimal) -> str:
    q = Decimal(n).quantize(Decimal("0.01"))
    s = f"{q:,.2f}" if q != q.to_integral() else f"{q:,.0f}"
    return f"₦{s}"


def resolve_period(
    period: Optional[str], start: Optional[str], end: Optional[str], today: date
) -> tuple[Optional[date], Optional[date], str]:
    """(start, end, human label). Weeks run Monday–Sunday."""
    p = (period or "all_time").lower()
    monday = today - timedelta(days=today.weekday())
    first_of_month = today.replace(day=1)
    if p == "today":
        return today, today, "today"
    if p == "yesterday":
        y = today - timedelta(days=1)
        return y, y, "yesterday"
    if p == "this_week":
        return monday, today, "this week"
    if p == "last_week":
        return monday - timedelta(days=7), monday - timedelta(days=1), "last week"
    if p == "last_7_days":
        return today - timedelta(days=6), today, "the last 7 days"
    if p == "this_month":
        return first_of_month, today, "this month"
    if p == "last_month":
        last_end = first_of_month - timedelta(days=1)
        return last_end.replace(day=1), last_end, "last month"
    if p == "last_30_days":
        return today - timedelta(days=29), today, "the last 30 days"
    if p == "this_year":
        return today.replace(month=1, day=1), today, "this year"
    if p == "custom":
        try:
            s = date.fromisoformat(start) if start else None
            e = date.fromisoformat(end) if end else None
        except ValueError:
            s = e = None
        if s or e:
            label = f"{s.isoformat() if s else 'the start'} to {e.isoformat() if e else 'today'}"
            return s, e, label
    return None, None, "all time"


def spending_snapshot(
    store: DataStore,
    member: Member,
    *,
    scope: str = "me",
    period: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    category: Optional[str] = None,
    item_query: Optional[str] = None,
    today: Optional[date] = None,
) -> dict:
    """Answer data for a spending question. Totals come from the database
    (`store.spending_breakdown`, a SQL function on Supabase); this function
    only picks the filters and formats the amounts."""
    today = today or date.today()
    d_start, d_end, label = resolve_period(period, start, end, today)
    family_scope = (scope or "me").lower() in ("family", "everyone", "all", "we", "us")

    cat_filter = None
    if category:
        cat_filter = next(
            (c for c in store.list_categories() if c.name.lower() == category.strip().lower()), None
        )
        if cat_filter is None and not item_query:
            item_query = category  # "how much on eggs" — not a category, so match item names

    b = store.spending_breakdown(
        member.family_id,
        member_id=None if family_scope else member.id,
        start=d_start,
        end=d_end,
        category_id=cat_filter.id if cat_filter else None,
        item_query=(item_query or "").strip() or None,
    )
    total = b["total"]

    def share(amt: Decimal) -> str:
        return f"{int((amt / total * 100).to_integral())}%" if total else "0%"

    data: dict = {
        "who": "the whole family" if family_scope else f"{member.name} only",
        "period": label,
        "date_range": {
            "start": d_start.isoformat() if d_start else None,
            "end": d_end.isoformat() if d_end else None,
        },
        "filter": cat_filter.name if cat_filter else (item_query or None),
        "confirmed_receipts": b["confirmed_receipts"],
        "total": format_ngn(total),
        "by_category": [
            {"category": r["name"], "amount": format_ngn(r["amount"]), "share": share(r["amount"])}
            for r in b["by_category"]
        ],
        "top_items": [{**r, "amount": format_ngn(r["amount"])} for r in b["top_items"]],
        "top_vendors": [
            {"vendor": r["name"], "amount": format_ngn(r["amount"]), "receipts": r["receipts"]}
            for r in b["top_vendors"]
        ],
        "excluded_pending_review": {
            "receipts": b["pending_receipts"],
            "amount": format_ngn(b["pending_amount"]),
        },
    }
    if family_scope:
        data["by_member"] = [
            {"member": r["name"], "amount": format_ngn(r["amount"]), "share": share(r["amount"])}
            for r in b["by_member"]
        ]
    return data


def norm_amount(d: Decimal) -> str:
    """Canonical string for comparing amounts: 12500.00 -> "12500", 3000.50 -> "3000.5"."""
    return format(Decimal(d).normalize(), "f")


_AMOUNT_RE = re.compile(r"₦\s?([0-9][0-9,]*(?:\.[0-9]+)?)")


def amounts_in(text: str) -> set[str]:
    """Normalized ₦ figures in a text, e.g. {"12500", "3000.5"}."""
    out = set()
    for m in _AMOUNT_RE.finditer(text or ""):
        raw = m.group(1).replace(",", "").rstrip(".")
        try:
            out.add(norm_amount(Decimal(raw)))
        except Exception:
            continue
    return out


def format_snapshot(data: dict) -> str:
    """Deterministic plain-text answer, used when the LLM is unavailable or
    its phrasing mentions an amount that isn't in the data."""
    head = f"Spending for {data['who']}, {data['period']}"
    if data.get("filter"):
        head += f" ({data['filter']})"
    lines = [head + ":", ""]
    if not data["confirmed_receipts"]:
        lines.append("No confirmed receipts match that yet.")
    else:
        lines.append(f"Total: {data['total']} across {data['confirmed_receipts']} receipt(s)")
        if len(data["by_category"]) > 1:
            lines.append("")
            lines.append("By category:")
            for c in data["by_category"]:
                lines.append(f"  {c['category']}: {c['amount']} ({c['share']})")
        if data.get("by_member") and len(data["by_member"]) > 1:
            lines.append("")
            lines.append("By member:")
            for m in data["by_member"]:
                lines.append(f"  {m['member']}: {m['amount']}")
    pend = data["excluded_pending_review"]
    if pend["receipts"]:
        lines.append("")
        lines.append(f"({pend['receipts']} receipt(s) still waiting on a quick check aren't counted yet.)")
    return "\n".join(lines)
