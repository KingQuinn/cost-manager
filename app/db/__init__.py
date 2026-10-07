from __future__ import annotations

from typing import Optional

from app.config import get_settings
from app.db.base import DataStore
from app.db.instrumented import InstrumentedStore
from app.db.memory import InMemoryStore
from app.db.supabase_store import SupabaseStore
from app.services.categorize import build_default_categories

__all__ = ["DataStore", "InMemoryStore", "InstrumentedStore", "SupabaseStore", "get_store"]

_store: Optional[DataStore] = None


def get_store() -> DataStore:
    """Return the singleton DataStore for the process.

    Set USE_SUPABASE=true + SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY in env -> SupabaseStore.
    Otherwise -> InMemoryStore.
    Either way it's wrapped in InstrumentedStore: memory layer 1 (cache) and a
    Langfuse span per database call.
    """
    global _store
    if _store is not None:
        return _store

    settings = get_settings()
    if settings.use_supabase:
        if not settings.supabase_url or not settings.supabase_service_role_key:
            raise RuntimeError(
                "USE_SUPABASE=true but SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are missing from env."
            )
        real = SupabaseStore(url=settings.supabase_url, service_role_key=settings.supabase_service_role_key)
        # Ensure default categories if the user hasn't run the seed yet.
        existing = real.list_categories()
        if not existing:
            for c in build_default_categories():
                real.client.table("categories").insert(c.model_dump(mode="json")).execute()
    else:
        real = InMemoryStore()
    _store = InstrumentedStore(real)
    return _store
