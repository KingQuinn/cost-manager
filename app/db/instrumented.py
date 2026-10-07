"""DataStore wrapper: memory layer 1 (cache) + a Langfuse span per database call.

`get_store()` hands out `InstrumentedStore(real_store)`, so every caller gets
both without knowing about either:

  - Tracing: each call becomes a `db.<method>` span under the current message
    trace, with its arguments (ids only, phone numbers hashed), a row count,
    cache hit/miss, and latency. Off unless Langfuse keys are set.
  - Cache: the reads listed in CACHED come from app/services/cache.py when
    fresh; the writes listed in INVALIDATES drop the matching entries
    immediately, so this process never serves a value it knows is stale.
"""
from __future__ import annotations

import functools
import inspect
from datetime import date
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app import observability
from app.services.cache import TTLCache

CACHE_TTL_SECONDS = 300

# method -> cache key prefix. The key is (prefix, *args).
CACHED = {
    "get_member_by_phone": "member_by_phone",
    "list_members": "members",
    "list_categories": "categories",
    "list_member_memories": "memories",
}
# write method -> cache prefixes it makes stale
INVALIDATES = {
    "create_member": ("member_by_phone", "members"),
    "append_category_keywords": ("categories",),
    "add_member_memory": ("memories",),
    "delete_member_memory": ("memories",),
}


def _describe(value: Any) -> Any:
    """Small, PII-free description of an argument for the trace."""
    if isinstance(value, (UUID, date)):
        return str(value)
    if isinstance(value, BaseModel):
        return {"type": type(value).__name__, "id": str(getattr(value, "id", ""))}
    if isinstance(value, (list, tuple)):
        return [_describe(v) for v in value[:5]] + ([f"... {len(value)} total"] if len(value) > 5 else [])
    if isinstance(value, dict):
        return {"keys": sorted(map(str, value))[:10]}
    if isinstance(value, str):
        return value if len(value) <= 80 else value[:80] + "…"
    return value


def _result_size(result: Any) -> Any:
    if isinstance(result, list):
        return {"rows": len(result)}
    if result is None:
        return {"rows": 0}
    if isinstance(result, dict) and "total" in result:
        return {"total": str(result["total"]), "confirmed_receipts": result.get("confirmed_receipts")}
    return {"rows": 1}


class InstrumentedStore:
    def __init__(self, inner: Any, cache: TTLCache | None = None) -> None:
        self._inner = inner
        self.cache = cache or TTLCache(CACHE_TTL_SECONDS)

    @property
    def inner(self) -> Any:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name.startswith("_") or not inspect.ismethod(attr):
            return attr

        @functools.wraps(attr)
        def call(*args: Any, **kwargs: Any) -> Any:
            shown_args = [
                observability.user_hash(a) if name == "get_member_by_phone" else _describe(a)
                for a in args
            ]
            span_input = {"args": shown_args, **{k: _describe(v) for k, v in kwargs.items()}}
            with observability.span(f"db.{name}", input=span_input) as obs:
                cache_state = None
                if name in CACHED and not kwargs:
                    key = (CACHED[name], *args)
                    result, hit = self.cache.get_or_load(key, lambda: attr(*args))
                    cache_state = "hit" if hit else "miss"
                    if result is None:  # e.g. unknown phone: don't remember "not found"
                        self.cache.invalidate(key)
                else:
                    result = attr(*args, **kwargs)
                    for prefix in INVALIDATES.get(name, ()):
                        self.cache.invalidate(prefix=prefix)
                obs.update(output=_result_size(result),
                           metadata={"cache": cache_state} if cache_state else None)
                return result

        return call
