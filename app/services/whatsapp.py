from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime
from typing import NamedTuple, Optional

import httpx

from app.config import get_settings
from app.models import WhatsAppInboundMessage, WhatsAppStatusUpdate

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"


_PLACEHOLDER_PREFIXES = ("your_", "optional_test_", "pick_a_random")


def _is_set(v: Optional[str]) -> bool:
    if not v:
        return False
    v_stripped = v.strip().lower()
    if not v_stripped:
        return False
    for p in _PLACEHOLDER_PREFIXES:
        if v_stripped.startswith(p):
            return False
    return True


class ParsedWebhookPayload(NamedTuple):
    """Meta's webhook body carries two distinct event kinds under one `field: "messages"`
    change — inbound messages (`value.messages`) and delivery-status events for messages
    we sent (`value.statuses`). Kept separate here so callers route each appropriately."""

    messages: list[WhatsAppInboundMessage]
    statuses: list[WhatsAppStatusUpdate]


class WhatsAppClient:
    """Send + receive helpers for WhatsApp Business Cloud API.

    Behavior:
    - If WHATSAPP_ACCESS_TOKEN + WHATSAPP_PHONE_NUMBER_ID are set -> real REST calls to Meta.
    - Otherwise -> stub mode: logs every "sent" message to stdout at INFO level so the
      capture/reporting flows can be exercised locally end-to-end with no Meta credentials.
    """

    def __init__(
        self,
        access_token: Optional[str] = None,
        phone_number_id: Optional[str] = None,
        verify_token: Optional[str] = None,
        app_secret: Optional[str] = None,
    ) -> None:
        settings = get_settings()
        self.access_token = access_token or settings.whatsapp_access_token
        self.phone_number_id = phone_number_id or settings.whatsapp_phone_number_id
        self.verify_token = verify_token or settings.whatsapp_verify_token
        self.app_secret = app_secret or settings.whatsapp_app_secret

    @property
    def is_configured(self) -> bool:
        return _is_set(self.access_token) and _is_set(self.phone_number_id)

    # ------------------------------------------------------------------
    # Inbound — verify Meta's request signature
    # ------------------------------------------------------------------
    def verify_signature(self, raw_body: bytes, signature_header: Optional[str]) -> bool:
        """Validate the `X-Hub-Signature-256` header Meta signs every webhook POST with.

        Returns True (skip verification) when WHATSAPP_APP_SECRET isn't configured, so
        local/dev setups that haven't wired up an App Secret yet keep working — but this
        means signature checking is a no-op until that env var is set. Set it before
        exposing the webhook publicly.
        """
        if not _is_set(self.app_secret):
            return True
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        expected = hmac.new(self.app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        provided = signature_header.split("=", 1)[1]
        return hmac.compare_digest(expected, provided)

    # ------------------------------------------------------------------
    # Outbound — send a text message
    # ------------------------------------------------------------------
    def send_text(self, to_phone: str, text: str) -> dict:
        """Send a plain text message to a single recipient (E.164 phone number)."""
        if not self.is_configured:
            logger.info(
                "[WHATSAPP STUB] send_text to=%s:\n%s", to_phone, text
            )
            return {"stub": True, "to": to_phone, "text": text}

        url = (
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
            f"{self.phone_number_id}/messages"
        )
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        try:
            r = httpx.post(url, headers=headers, json=payload, timeout=30.0)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            logger.error("WhatsApp send_text failed for %s: %s", to_phone, e)
            raise

    # ------------------------------------------------------------------
    # Outbound — download an image (by media ID) sent to the bot
    # ------------------------------------------------------------------
    def download_image(self, media_id: str) -> bytes:
        """Given a WhatsApp media ID (from an inbound image message), fetch the raw bytes."""
        if not self.is_configured:
            raise RuntimeError(
                "Cannot download WhatsApp image without WHATSAPP_ACCESS_TOKEN + PHONE_NUMBER_ID."
            )
        headers = {"Authorization": f"Bearer {self.access_token}"}
        # Step 1: resolve media ID -> media URL
        meta_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{media_id}"
        meta = httpx.get(meta_url, headers=headers, timeout=30)
        meta.raise_for_status()
        media_url = meta.json()["url"]
        # Step 2: GET the actual bytes
        img = httpx.get(media_url, headers=headers, timeout=60)
        img.raise_for_status()
        return img.content

    # ------------------------------------------------------------------
    # Inbound — parse Meta's raw webhook payload into our DTOs
    # ------------------------------------------------------------------
    def parse_webhook_payload(self, payload: dict) -> ParsedWebhookPayload:
        """Convert a Meta webhook JSON body into normalized messages + status updates.

        A single POST can carry either (or both): new inbound messages under
        `value.messages`, and delivery-status events for messages we previously sent
        under `value.statuses` (sent/delivered/read/failed). Returns empty lists if the
        payload has neither.
        """
        messages: list[WhatsAppInboundMessage] = []
        statuses: list[WhatsAppStatusUpdate] = []
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                if change.get("field") != "messages":
                    continue
                value = change.get("value") or {}

                for msg in value.get("messages", []) or []:
                    from_phone = msg.get("from")
                    if not from_phone:
                        continue
                    msg_type = msg.get("type")
                    item = WhatsAppInboundMessage(
                        from_phone=from_phone,
                        message_id=msg.get("id", ""),
                    )
                    ts = msg.get("timestamp")
                    if ts:
                        try:
                            item.timestamp = datetime.utcfromtimestamp(int(ts))
                        except (ValueError, TypeError):
                            pass
                    if msg_type == "text":
                        item.text = (msg.get("text") or {}).get("body")
                        messages.append(item)
                    elif msg_type == "image":
                        item.image_id = (msg.get("image") or {}).get("id")
                        # Caller is responsible for calling download_image if
                        # the actual bytes are needed.
                        messages.append(item)
                    else:
                        # Audio, video, sticker, location, contact, etc. We ignore
                        # these for v1 (FR-1: only image + text responses matter),
                        # but we still surface them as text=None so the handler can
                        # reply with a friendly "I don't support that yet".
                        messages.append(item)

                for st in value.get("statuses", []) or []:
                    recipient = st.get("recipient_id")
                    if not recipient:
                        continue
                    su = WhatsAppStatusUpdate(
                        message_id=st.get("id", ""),
                        recipient_phone=recipient,
                        status=st.get("status", "unknown"),
                    )
                    ts = st.get("timestamp")
                    if ts:
                        try:
                            su.timestamp = datetime.utcfromtimestamp(int(ts))
                        except (ValueError, TypeError):
                            pass
                    errors = st.get("errors") or []
                    if errors:
                        su.error = "; ".join(
                            f"{e.get('code')}: {e.get('title')}" for e in errors
                        )
                    statuses.append(su)

        return ParsedWebhookPayload(messages=messages, statuses=statuses)


# Singleton-like default instance for the app.
_client: Optional[WhatsAppClient] = None


def get_whatsapp() -> WhatsAppClient:
    global _client
    if _client is None:
        _client = WhatsAppClient()
    return _client
