from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional
from uuid import UUID

from app.db import DataStore, get_store
from app.models import Member, WeeklySummary
from app.services.whatsapp import WhatsAppClient, get_whatsapp


def week_range(ref: Optional[date] = None) -> tuple[date, date]:
    """Return Monday–Sunday for the week containing `ref`. Defaults to last completed week.

    Reasoning: a weekly summary is usually sent on Monday *after* the week closes,
    so defaulting to "previous completed week" means the report is always actionable
    when the cron job fires on a Monday morning.
    """
    if ref is None:
        ref = date.today()
    # Monday=0.  Compute the Monday of the current week, then step back 7 days
    # to get the *previous* Monday–Sunday window (the completed week).
    monday_this_week = ref - timedelta(days=ref.weekday())
    start = monday_this_week - timedelta(days=7)
    end = start + timedelta(days=6)
    return start, end


def _fmt(n: Decimal) -> str:
    """NGN format with thousand separators, no decimals if .00."""
    q = n.quantize(Decimal("0.01"))
    if q == q.to_integral():
        s = f"{q:,.0f}"
    else:
        s = f"{q:,.2f}"
    return f"₦{s}"


def build_summary(
    store: DataStore,
    family_id: UUID,
    members: list[Member],
    start: date,
    end: date,
) -> tuple[str, Decimal]:
    """Return (formatted_summary_text, total_spend).

    Layout (rough):
      Week: Mon DD — Sun DD, YYYY
      Total: ₦X
      By category (descending):
        Category A: ₦X (N%)
        ...
      By member:
        Member 1: ₦X
        ...
    """
    rows = store.all_confirmed_line_items(family_id, start, end)
    by_cat: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    by_member: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    total = Decimal("0")
    for li, cat, member in rows:
        by_cat[cat.name] += li.price
        by_member[member.name] += li.price
        total += li.price

    lines = []
    lines.append(
        f"Weekly spending summary · {start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"
    )
    lines.append("")
    if not rows:
        lines.append("No confirmed receipts this week — did everyone send photos?")
        return "\n".join(lines), Decimal("0")

    lines.append(f"Total: {_fmt(total)}")
    lines.append("")
    lines.append("By category:")
    for name, amt in sorted(by_cat.items(), key=lambda kv: -kv[1]):
        pct = int((amt / total * 100).to_integral()) if total else 0
        lines.append(f"  {name}: {_fmt(amt)} ({pct}%)")

    lines.append("")
    lines.append("By member:")
    for m in members:
        amt = by_member.get(m.name, Decimal("0"))
        lines.append(f"  {m.name}: {_fmt(amt)}")

    return "\n".join(lines), total


def generate_and_send(
    store: Optional[DataStore] = None,
    whatsapp: Optional[WhatsAppClient] = None,
    week: Optional[tuple[date, date]] = None,
) -> WeeklySummary:
    """Aggregate, save a WeeklySummary row, send to every family member via WhatsApp.

    This is the function called by the cron endpoint (or pg_cron via a wrapped HTTP call).
    """
    store = store or get_store()
    whatsapp = whatsapp or get_whatsapp()
    family = store.get_or_create_default_family()
    members = store.list_members(family.id)

    start, end = week or week_range()
    text, total = build_summary(store, family.id, members, start, end)

    summary = WeeklySummary(
        family_id=family.id,
        week_start=start,
        week_end=end,
        summary_text=text,
        total_spend=total,
    )
    store.create_weekly_summary(summary)

    for m in members:
        whatsapp.send_text(m.phone_number, text)

    return summary
