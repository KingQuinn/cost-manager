from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response
from typing import Optional

from app.db import DataStore, get_store
from app.models import WhatsAppInboundMessage
from app.services.capture_pipeline import CapturePipeline, process_inbound_message
from app.services.whatsapp import WhatsAppClient, get_whatsapp

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

FALLBACK_REPLY = (
    "Hmmm, I don't think i got that. "
    "Could you try again in a moment."
)


_SEEN_MAX = 2000
_seen_ids: "OrderedDict[str, None]" = OrderedDict()
_seen_lock = threading.Lock()


def _claim_message(message_id: str) -> bool:
    """True the first time a message ID is seen, False for repeats."""
    if not message_id:
        return True  # can't dedupe without an ID; process it
    with _seen_lock:
        if message_id in _seen_ids:
            return False
        _seen_ids[message_id] = None
        if len(_seen_ids) > _SEEN_MAX:
            _seen_ids.popitem(last=False)
        return True


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
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(default=None),
    store: DataStore = Depends(get_store),
    whatsapp: WhatsAppClient = Depends(get_whatsapp),
):
    """Meta's POST webhook — receives both inbound messages and delivery-status
    updates (sent/delivered/read/failed) for messages we sent.

    Returns 200 to Meta immediately; inbound messages are processed afterwards
    in the background (see _handle_message). Meta treats a slow webhook as failed
    and redelivers it, so the slow work (Gemini, Supabase) must not happen here.

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

    queued = 0
    for msg in parsed.messages:
        if not _claim_message(msg.message_id):
            logger.info("Skipping duplicate delivery of message=%s", msg.message_id)
            continue
        background_tasks.add_task(_handle_message, store, whatsapp, msg)
        queued += 1

    return {
        "status": "ok",
        "messages_queued": queued,
        "status_updates_received": len(parsed.statuses),
    }


def _handle_message(
    store: DataStore, whatsapp: WhatsAppClient, msg: WhatsAppInboundMessage
) -> None:
    """Process one inbound message after Meta has its 200.

    A plain `def`, so FastAPI runs it in a worker thread: the blocking Gemini and
    Supabase calls inside the pipeline don't stall the server for other requests.
    """
    try:
        # "typing..." shows until the pipeline sends its reply (or finishes).
        with whatsapp.typing(msg.message_id, msg.from_phone):
            pipe = CapturePipeline(store, whatsapp)
            process_inbound_message(pipe, msg, whatsapp)
    except Exception:
        # Previously an error here became a 500 to Meta. Now Meta already has
        # its 200, so log it and tell the user rather than going silent.
        logger.exception("Failed to process WhatsApp message=%s", msg.message_id)
        try:
            whatsapp.send_text(msg.from_phone, FALLBACK_REPLY)
        except Exception:
            logger.exception("Could not send fallback reply for message=%s", msg.message_id)