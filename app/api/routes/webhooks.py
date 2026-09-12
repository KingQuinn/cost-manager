from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from typing import Optional

from app.db import DataStore, get_store
from app.services.capture_pipeline import CapturePipeline, process_inbound_message
from app.services.whatsapp import WhatsAppClient, get_whatsapp

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


@router.get("/webhook")
async def meta_webhook_verify(
    request: Request,
    whatsapp: WhatsAppClient = Depends(get_whatsapp),
):
    """Meta's GET webhook handshake: echo hub.challenge iff verify token matches.

    Run once when registering the webhook URL in the Meta developer dashboard.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == whatsapp.verify_token:
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Bad verify token")


@router.post("/webhook")
async def meta_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(default=None),
    store: DataStore = Depends(get_store),
    whatsapp: WhatsAppClient = Depends(get_whatsapp),
):
    """Meta's POST webhook — receives both inbound messages and delivery-status
    updates (sent/delivered/read/failed) for messages we sent.

    Signature verification runs whenever WHATSAPP_APP_SECRET is configured (see
    WhatsAppClient.verify_signature); without it, the check is skipped so local
    dev keeps working without a Meta App Secret.
    """
    raw_body = await request.body()
    if not whatsapp.verify_signature(raw_body, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Invalid X-Hub-Signature-256")

    payload = await request.json()
    parsed = whatsapp.parse_webhook_payload(payload)

    for status in parsed.statuses:
        if status.status == "failed":
            logger.warning(
                "WhatsApp delivery failed: message=%s recipient=%s error=%s",
                status.message_id, status.recipient_phone, status.error,
            )
        else:
            logger.info(
                "WhatsApp status update: message=%s recipient=%s status=%s",
                status.message_id, status.recipient_phone, status.status,
            )

    pipe = CapturePipeline(store, whatsapp)
    results = [process_inbound_message(pipe, msg, whatsapp) for msg in parsed.messages]

    return {
        "status": "ok",
        "messages_processed": len(results),
        "status_updates_received": len(parsed.statuses),
    }
