
import time
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import webhooks
from app.db import get_store
from app.models import WhatsAppInboundMessage
from app.services.whatsapp import ParsedWebhookPayload, get_whatsapp


class FakeWhatsApp:
    verify_token = "VERIFY"

    def __init__(self, messages):
        self._messages = messages
        self.events = []

    def verify_signature(self, raw_body, header):
        return True

    def parse_webhook_payload(self, payload):
        return ParsedWebhookPayload(messages=self._messages, statuses=[])

    @contextmanager
    def typing(self, message_id, to_phone):
        self.events.append(("typing_start", message_id))
        try:
            yield
        finally:
            self.events.append(("typing_end", message_id))

    def send_text(self, to_phone, text):
        self.events.append(("send_text", text))


def msg(mid="wamid.1"):
    return WhatsAppInboundMessage(from_phone="2348000000000", message_id=mid)


@pytest.fixture(autouse=True)
def clear_seen_ids():
    webhooks._seen_ids.clear()


def make_client(fake_wa, monkeypatch, pipeline):
    monkeypatch.setattr(webhooks, "CapturePipeline", lambda store, wa: object())
    monkeypatch.setattr(webhooks, "process_inbound_message", pipeline)
    app = FastAPI()
    app.include_router(webhooks.router)
    app.dependency_overrides[get_store] = lambda: object()
    app.dependency_overrides[get_whatsapp] = lambda: fake_wa
    return TestClient(app)


def test_processing_runs_inside_typing(monkeypatch):
    wa = FakeWhatsApp([msg()])

    def pipeline(pipe, m, whatsapp):
        whatsapp.events.append(("pipeline", m.message_id))

    r = make_client(wa, monkeypatch, pipeline).post("/webhook", json={})
    assert r.status_code == 200
    assert r.json()["messages_queued"] == 1
    assert wa.events == [
        ("typing_start", "wamid.1"),
        ("pipeline", "wamid.1"),
        ("typing_end", "wamid.1"),
    ]


def test_duplicate_delivery_processed_once(monkeypatch):
    wa = FakeWhatsApp([msg("wamid.dup")])
    seen = []
    client = make_client(wa, monkeypatch, lambda p, m, w: seen.append(m.message_id))
    client.post("/webhook", json={})
    r = client.post("/webhook", json={})  # Meta redelivers the same message
    assert r.json()["messages_queued"] == 0
    assert seen == ["wamid.dup"]


def test_pipeline_crash_sends_fallback_reply(monkeypatch):
    wa = FakeWhatsApp([msg()])

    def pipeline(pipe, m, whatsapp):
        raise RuntimeError("gemini blew up")

    r = make_client(wa, monkeypatch, pipeline).post("/webhook", json={})
    assert r.status_code == 200
    assert ("send_text", webhooks.FALLBACK_REPLY) in wa.events
    assert ("typing_end", "wamid.1") in wa.events


def test_response_does_not_wait_for_processing(monkeypatch):
    """Meta's 200 is decided before the slow work runs."""
    wa = FakeWhatsApp([msg()])
    route = webhooks.meta_webhook
    tasks = []

    class Recorder:
        def add_task(self, fn, *args):
            tasks.append((fn, args))

    import asyncio

    class FakeRequest:
        async def body(self):
            return b"{}"

        async def json(self):
            return {}

    start = time.monotonic()
    result = asyncio.run(route(FakeRequest(), Recorder(), None, object(), wa))
    assert time.monotonic() - start < 0.5
    assert result["messages_queued"] == 1
    assert tasks and tasks[0][0] is webhooks._handle_message
    assert wa.events == []  # nothing processed yet