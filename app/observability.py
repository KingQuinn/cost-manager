"""Langfuse tracing — optional, and a no-op until configured.

One trace per inbound WhatsApp message (`trace_message`). Inside it:
  - a generation for every Gemini call (`generation`, from app/services/llm.py)
  - a `route` span with the router's decision and a `act:<intent>` span with
    what the code did about it (app/services/agent.py)
  - a `db.<method>` span for every database call, marked cache hit/miss
    (app/db/instrumented.py)
Each observation has start/end times, so latency is visible per step. In
Langfuse that gives, per message: what the member said, which intent the
router picked and why, which Gemini calls and SQL queries ran, how long each
took, and what the bot replied.

Set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY (and LANGFUSE_BASE_URL if you
self-host or use the US region) to turn it on. Without them, or if the
`langfuse` package isn't installed, every helper here does nothing, so tests
and local dev never need a Langfuse account.

Privacy (architecture.md §13): phone numbers are never sent — the trace's
user_id is a short hash. Receipt image bytes are never sent — only their size.
"""
from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from datetime import date
from typing import Any, Iterator, Optional

from app.config import get_settings
from app.services.whatsapp import _is_set

logger = logging.getLogger(__name__)

_client: Any = None
_init_done = False


def get_langfuse() -> Any:
    """The Langfuse client, or None when tracing is off."""
    global _client, _init_done
    if _init_done:
        return _client
    _init_done = True
    s = get_settings()
    if not (_is_set(s.langfuse_public_key) and _is_set(s.langfuse_secret_key)):
        return None
    try:
        from langfuse import Langfuse
    except ImportError:
        logger.warning("LANGFUSE keys are set but the `langfuse` package isn't installed")
        return None
    try:
        _client = Langfuse(
            public_key=s.langfuse_public_key,
            secret_key=s.langfuse_secret_key,
            base_url=s.langfuse_base_url,
            environment=s.environment.strip().lower() or None,
        )
        logger.info("Langfuse tracing enabled (%s)", s.langfuse_base_url)
    except Exception as e:  # never let observability break the bot
        logger.warning("Langfuse init failed, tracing disabled: %s", e)
        _client = None
    return _client


def reset_for_tests() -> None:
    global _client, _init_done
    _client, _init_done = None, False


def user_hash(phone: str) -> str:
    """Stable, non-reversible id for a phone number (no PII in traces)."""
    return hashlib.sha256((phone or "").encode("utf-8")).hexdigest()[:16]


class _Obs:
    """Wraps a Langfuse observation (or nothing). Safe to call either way."""

    def __init__(self, inner: Any = None) -> None:
        self._inner = inner

    def update(self, **kwargs: Any) -> None:
        if self._inner is None:
            return
        try:
            self._inner.update(**kwargs)
        except Exception as e:
            logger.debug("Langfuse update failed: %s", e)


@contextmanager
def trace_message(phone: str, kind: str, input: Any) -> Iterator[_Obs]:
    """Root trace for one inbound message. `kind` is "text" / "image" / "other"."""
    lf = get_langfuse()
    if lf is None:
        yield _Obs()
        return
    try:
        from langfuse import propagate_attributes
    except ImportError:
        yield _Obs()
        return
    uid = user_hash(phone)
    try:
        span_cm = lf.start_as_current_observation(
            name="whatsapp-message", as_type="agent", input=input
        )
        attrs_cm = propagate_attributes(
            user_id=uid,
            # One Langfuse session per member per day: a day's chat reads as one thread.
            session_id=f"{uid}-{date.today().isoformat()}",
            tags=[f"message:{kind}"],
            trace_name="whatsapp-message",
        )
    except Exception as e:
        logger.debug("Langfuse trace start failed: %s", e)
        yield _Obs()
        return
    with span_cm as span, attrs_cm:
        obs = _Obs(span)
        try:
            yield obs
        except Exception as e:
            obs.update(level="ERROR", status_message=f"{e.__class__.__name__}: {e}")
            raise


@contextmanager
def span(
    name: str,
    input: Any = None,
    metadata: Optional[dict] = None,
    *,
    as_type: str = "span",
    **kwargs: Any,
) -> Iterator[_Obs]:
    """Child observation under the current trace (no-op when tracing is off).

    Langfuse records start/end times, so every span carries its latency. An
    exception inside marks the span as ERROR and propagates unchanged.
    """
    lf = get_langfuse()
    if lf is None:
        yield _Obs()
        return
    try:
        cm = lf.start_as_current_observation(
            name=name, as_type=as_type, input=input, metadata=metadata, **kwargs
        )
    except Exception as e:
        logger.debug("Langfuse %s start failed: %s", as_type, e)
        yield _Obs()
        return
    with cm as inner:
        obs = _Obs(inner)
        try:
            yield obs
        except Exception as e:
            obs.update(level="ERROR", status_message=f"{e.__class__.__name__}: {e}")
            raise


@contextmanager
def generation(name: str, model: str, input: Any, metadata: Optional[dict] = None) -> Iterator[_Obs]:
    """Child observation for one LLM call. Call .update(output=..., usage_details=...)."""
    with span(name, input, metadata, as_type="generation", model=model) as obs:
        yield obs


def usage_from_gemini(response: Any) -> Optional[dict]:
    """Token counts from a google-generativeai response, in Langfuse's shape."""
    um = getattr(response, "usage_metadata", None)
    if um is None:
        return None
    try:
        return {
            "input": int(getattr(um, "prompt_token_count", 0) or 0),
            "output": int(getattr(um, "candidates_token_count", 0) or 0),
        }
    except Exception:
        return None
