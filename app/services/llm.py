"""One place every Gemini call goes through, so each one is traced in Langfuse.

`generate()` returns the raw response text; `generate_json()` asks Gemini for
JSON output and parses it. Both raise on failure — callers decide the safe
fallback (for the conversation agent that means "change nothing").
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app import observability

logger = logging.getLogger(__name__)


def _describe_part(part: Any) -> Any:
    """Langfuse-safe view of a prompt part: never ship image bytes to the trace."""
    if isinstance(part, dict) and "data" in part:
        data = part.get("data") or ""
        return {"mime_type": part.get("mime_type"), "data": f"<{len(data)} base64 chars omitted>"}
    return part


def generate(
    name: str,
    parts: list[Any],
    *,
    model_name: str,
    timeout: float = 30.0,
    json_mode: bool = False,
    temperature: Optional[float] = None,
    metadata: Optional[dict] = None,
) -> str:
    # Imported lazily so tests can monkeypatch extraction._gemini_model.
    from app.services.extraction import _gemini_model

    gen_config: dict[str, Any] = {}
    if json_mode:
        gen_config["response_mime_type"] = "application/json"
    if temperature is not None:
        gen_config["temperature"] = temperature

    with observability.generation(
        name, model_name, [_describe_part(p) for p in parts], metadata
    ) as obs:
        model = _gemini_model(model_name)
        kwargs: dict[str, Any] = {"request_options": {"timeout": timeout}}
        if gen_config:
            kwargs["generation_config"] = gen_config
        response = model.generate_content(parts, **kwargs)
        text = response.text if hasattr(response, "text") else ""
        obs.update(output=text, usage_details=observability.usage_from_gemini(response))
        return text


def generate_json(name: str, parts: list[Any], *, model_name: str, timeout: float = 30.0,
                  temperature: Optional[float] = 0.1, metadata: Optional[dict] = None) -> dict:
    from app.services.extraction import _clean_json_response

    text = generate(name, parts, model_name=model_name, timeout=timeout, json_mode=True,
                    temperature=temperature, metadata=metadata)
    data = json.loads(_clean_json_response(text))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object from {name}, got {type(data).__name__}")
    return data
