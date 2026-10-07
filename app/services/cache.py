"""Memory layer 1 — an in-process cache for hot, slow-changing reads.

Every WhatsApp message used to re-read the same rows from Supabase: the
sender's member row, the category list (with keywords), the family's members
and the sender's long-term memories. Those change rarely and only through
this app, so they're cached here for a short TTL and dropped the moment the
app writes to them (write-through invalidation in CachedStore).

Deliberately NOT cached, because a stale value would change data or answers:
receipts, line items, clarifications, conversation turns, member state, and
spending totals. Those are always read fresh.

Process-local by design (one Render/Fly instance). With several instances a
write on one only expires the others' copies after the TTL, which for these
tables means at most a few minutes' lag on a new keyword or memory.
"""
from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable, Hashable, Optional


class TTLCache:
    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl_seconds
        self._clock = clock
        self._data: dict[Hashable, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get_or_load(self, key: Hashable, loader: Callable[[], Any]) -> tuple[Any, bool]:
        """(value, was_hit). Values are deep-copied out so callers can't mutate the cache."""
        now = self._clock()
        with self._lock:
            entry = self._data.get(key)
            if entry is not None and entry[0] > now:
                self.hits += 1
                return copy.deepcopy(entry[1]), True
        value = loader()
        with self._lock:
            self._data[key] = (now + self.ttl, copy.deepcopy(value))
            self.misses += 1
        return value, False

    def invalidate(self, key: Optional[Hashable] = None, *, prefix: Optional[str] = None) -> None:
        with self._lock:
            if key is not None:
                self._data.pop(key, None)
            if prefix is not None:
                for k in [k for k in self._data if isinstance(k, tuple) and k and k[0] == prefix]:
                    self._data.pop(k, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
