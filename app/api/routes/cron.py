from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.db import DataStore, get_store
from app.services.weekly_report import generate_and_send
from app.services.whatsapp import WhatsAppClient, get_whatsapp

router = APIRouter(tags=["cron"])


class WeeklyRequest(BaseModel):
    week_start: Optional[str] = None
    week_end: Optional[str] = None


@router.post("/cron/weekly-summary")
def weekly_summary(
    body: WeeklyRequest = WeeklyRequest(),
    x_cron_secret: Optional[str] = Header(default=None),
    store: DataStore = Depends(get_store),
    whatsapp: WhatsAppClient = Depends(get_whatsapp),
):
    """Manual/scheduled trigger for the weekly reporting job — only needed if not using
    Supabase `pg_cron` (architecture.md §7.6 / §14)."""
    settings = get_settings()
    if settings.cron_shared_secret and x_cron_secret != settings.cron_shared_secret:
        raise HTTPException(status_code=403, detail="Missing or invalid X-Cron-Secret header")

    start = end = None
    if body.week_start and body.week_end:
        try:
            start = date.fromisoformat(body.week_start)
            end = date.fromisoformat(body.week_end)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid week_start/end YYYY-MM-DD")

    s = generate_and_send(store=store, whatsapp=whatsapp, week=(start, end) if start else None)
    return {
        "status": "sent",
        "id": str(s.id),
        "total": str(s.total_spend),
        "week": s.week_start.isoformat() + "/" + s.week_end.isoformat(),
    }
