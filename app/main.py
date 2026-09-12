from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api.routes import api_router
from app.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("app")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Family Expense Tracker", version="0.1.0")

    app.include_router(api_router)

    if settings.is_development:
        from app.api.routes import testing

        app.include_router(testing.router)
        logger.info("ENVIRONMENT=%s — /test/message mounted", settings.environment)

    return app


app = create_app()
