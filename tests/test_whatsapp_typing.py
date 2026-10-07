
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from app.services import whatsapp as wa_module

FAKE_SETTINGS = SimpleNamespace(
    whatsapp_access_token="TOKEN",
    whatsapp_phone_number_id="PNID",
    whatsapp_verify_token="VERIFY",
    whatsapp_app_secret=None,
)


@pytest.fixture
def calls(monkeypatch):
    """Record every httpx.post. Set calls.status / calls.raise_exc to simulate failures."""
    class Recorder(list):
        pass

    lock = threading.Lock()
    recorded = Recorder()
    recorded.status = 200
    recorded.raise_exc = None

    def fake_post(url, headers=None, json=None, timeout=None):
        with lock:
            recorded.append({"url": url, "headers": headers, "json": json, "t": time.monotonic()})
        if recorded.raise_exc:
            raise recorded.raise_exc
        return httpx.Response(recorded.status, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(wa_module, "get_settings", lambda: FAKE_SETTINGS)
    monkeypatch.setattr(wa_module.httpx, "post", fake_post)
    return recorded


def make_client(refresh=0.05, max_seconds=60.0):
    client = wa_module.WhatsAppClient()
    client.TYPING_REFRESH_SECONDS = refresh
    client.TYPING_MAX_SECONDS = max_seconds
    return client


def test_sends_correct_payload(calls):
    assert make_client().send_typing_indicator("wamid.IN") is True
    assert len(calls) == 1
    call = calls[0]
    assert call["url"].endswith("/PNID/messages")
    assert call["headers"]["Authorization"] == "Bearer TOKEN"
    assert call["json"] == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.IN",
        "typing_indicator": {"type": "text"},
    }


def test_quick_work_sends_once(calls):
    with make_client(refresh=1.0).typing("wamid.IN", "2348000000000"):
        time.sleep(0.01)
    assert len(calls) == 1


def test_refreshes_during_long_work_and_respects_cap(calls):
    with make_client(refresh=0.05, max_seconds=0.22).typing("wamid.IN", "2348000000000"):
        time.sleep(0.5)  # work outlives the cap
    # initial send + refreshes at ~0.05, 0.10, 0.15, 0.20, then capped
    assert 4 <= len(calls) <= 5


def test_nothing_sent_after_block_exits(calls):
    """A late refresh must never land after the reply."""
    with make_client(refresh=0.05).typing("wamid.IN", "2348000000000"):
        time.sleep(0.12)
    exited_at = time.monotonic()
    time.sleep(0.2)  # give any stray refresh a chance to fire
    assert all(c["t"] <= exited_at for c in calls)


def test_meta_error_does_not_break_processing(calls):
    calls.status = 400
    with make_client(refresh=0.05).typing("wamid.IN", "2348000000000"):
        time.sleep(0.2)
        result = "reply"
    assert result == "reply"
    assert len(calls) == 1  # first failure stops the loop: no hammering Meta


def test_network_error_does_not_break_processing(calls):
    calls.raise_exc = httpx.ConnectError("down")
    with make_client().typing("wamid.IN", "2348000000000"):
        result = "reply"
    assert result == "reply"


def test_exception_in_work_still_stops_indicator(calls):
    with pytest.raises(RuntimeError):
        with make_client(refresh=0.05).typing("wamid.IN", "2348000000000"):
            time.sleep(0.01)
            raise RuntimeError("gemini blew up")
    n = len(calls)
    time.sleep(0.2)
    assert len(calls) == n


def test_stub_mode_makes_no_http_calls(calls, monkeypatch):
    monkeypatch.setattr(
        wa_module, "get_settings",
        lambda: SimpleNamespace(**{**vars(FAKE_SETTINGS), "whatsapp_access_token": "your_token"}),
    )
    with wa_module.WhatsAppClient().typing("wamid.IN", "2348000000000"):
        pass
    assert calls == []


def test_empty_message_id_is_skipped(calls):
    assert make_client().send_typing_indicator("") is False
    assert calls == []


def test_send_text_stops_typing_inside_block(calls):
    """The pipeline sends its reply from inside the block; typing must stop there."""
    client = make_client(refresh=0.05)
    with client.typing("wamid.IN", "2348000000000"):
        time.sleep(0.01)
        client.send_text("2348000000000", "Logged: Shoprite, N12,500")
        time.sleep(0.2)  # post-reply work (DB writes etc.) longer than a refresh
    kinds = ["typing" if "typing_indicator" in c["json"] else "reply" for c in calls]
    assert kinds == ["typing", "reply"]


def test_send_text_to_other_phone_does_not_stop_typing(calls):
    client = make_client(refresh=0.05)
    with client.typing("wamid.IN", "2348000000000"):
        client.send_text("2349999999999", "hello")
        time.sleep(0.12)
    typing_calls = [c for c in calls if "typing_indicator" in c["json"]]
    assert len(typing_calls) >= 2  # kept refreshing for the original user