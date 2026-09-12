"""Local dev entrypoint: `python run.py`. In production, run the ASGI app
directly instead: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
"""
from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        reload=True,
    )
