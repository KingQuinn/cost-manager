from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import cron, health, webhooks

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(webhooks.router)
api_router.include_router(cron.router)
