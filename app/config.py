from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Single source of truth for env-driven configuration.

    Values fall back to the same defaults the app has always used; nothing
    here changes behavior, it just centralizes what used to be scattered
    `os.environ.get(...)` calls across main.py/whatsapp.py/db.py.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"

    # Gemini
    gemini_api_key: Optional[str] = None

    # WhatsApp Business Cloud API
    whatsapp_access_token: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_verify_token: str = "local-dev-verify"
    whatsapp_app_secret: Optional[str] = None  # HMAC key for X-Hub-Signature-256

    # Supabase
    use_supabase: bool = False
    supabase_url: Optional[str] = None
    supabase_service_role_key: Optional[str] = None

    # Cron
    cron_shared_secret: str = ""

    # Confidence routing thresholds — architecture.md §11. Starting points from
    # the design phase, not yet validated against real distributions.
    overall_confidence_threshold: float = 0.75
    per_item_confidence_threshold: float = 0.6

    @property
    def is_development(self) -> bool:
        return self.environment.strip().lower() in ("development", "dev", "local")


@lru_cache
def get_settings() -> Settings:
    return Settings()
