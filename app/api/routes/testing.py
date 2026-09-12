from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request

from app.db import DataStore, get_store
from app.models import WhatsAppInboundMessage
from app.services.capture_pipeline import CapturePipeline, process_inbound_message
from app.services.whatsapp import WhatsAppClient, get_whatsapp

router = APIRouter(tags=["testing"])


@router.post("/test/message")
async def test_message_local(
    req: Request,
    store: DataStore = Depends(get_store),
    whatsapp: WhatsAppClient = Depends(get_whatsapp),
):
    """Dev-only endpoint that bypasses Meta's webhook format entirely — lets the
    capture flow be exercised end-to-end with no Meta app. Mounted only when
    ENVIRONMENT=development (see app/main.py).

    Accept JSON body either:
      {"from": "+234...", "text": "Aisha"}      -> text, or
      {"from": "+234...", "image_path": "/path/to/receipt.jpg"} -> image path (reads local file as if it was a received WhatsApp image).
    """
    data = await req.json()
    from_phone = data.get("from") or "+2348000000000"
    pipe = CapturePipeline(store, whatsapp)
    img_path = data.get("image_path")
    img_bytes: bytes | None = None
    if img_path:
        img_bytes = Path(img_path).read_bytes()
    msg = WhatsAppInboundMessage(
        from_phone=from_phone,
        message_id=data.get("message_id") or "test-msg",
        text=data.get("text"),
        image_bytes=img_bytes,
        image_id=data.get("image_id") or ("test-img" if img_bytes else None),
    )
    out = process_inbound_message(pipe, msg, whatsapp)
    return {"reply": out}
