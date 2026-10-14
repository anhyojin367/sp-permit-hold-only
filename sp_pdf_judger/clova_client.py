from __future__ import annotations

import json
import re
import os
import hashlib
import uuid
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel
from .clova_transport import completion_with_backoff, response_cache_dir, report_response_cache_hit

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional until runtime dependencies are installed
    OpenAI = None


ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)


def _completion_with_backoff(client, request, *, max_attempts: int | None = None):
    return completion_with_backoff(client, request, max_attempts=max_attempts)


def create_clova_client(api_key: str, base_url: str) -> Any | None:
    if not api_key or OpenAI is None:
        return None
    return OpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=60.0)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(getattr(item, "text", None), str):
                parts.append(item.text)
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _parse_model_json(text: str, response_model: type[ResponseModelT]) -> ResponseModelT:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return response_model.model_validate_json(cleaned)
    except Exception:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise ValueError("CLOVA response did not contain a JSON object")
        return response_model.model_validate(json.loads(match.group(0)))


def request_structured_response(
    *,
    client: Any,
    model: str,
    prompt: str,
    response_model: type[ResponseModelT],
    max_completion_tokens: int = 4096,
    use_schema: bool = True,
    max_provider_attempts: int = 5,
    response_schema_model: type[BaseModel] | None = None,
    response_validator: Callable[[ResponseModelT], None] | None = None,
) -> ResponseModelT:
    if max_provider_attempts < 1:
        raise ValueError("max_provider_attempts must be at least 1")
    request: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "reasoning_effort": "none",
        "max_completion_tokens": max_completion_tokens,
    }
    schema_model = response_schema_model or response_model
    if use_schema:
        request["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": response_model.__name__,
                "strict": True,
                "schema": schema_model.model_json_schema(),
            },
        }
    cache_path = None
    cache_root = os.getenv("CLOVA_RESPONSE_CACHE_DIR") or response_cache_dir()
    if cache_root:
        from .policy_engine import policy_fingerprint
        signature = json.dumps({"request": request, "schema": schema_model.model_json_schema(),
            "endpoint": str(getattr(client, "base_url", "")), "rules": policy_fingerprint()}, ensure_ascii=False, sort_keys=True)
        cache_path = Path(cache_root) / (hashlib.sha256(signature.encode()).hexdigest() + ".json")
        if cache_path.is_file():
            try:
                cached = response_model.model_validate_json(cache_path.read_text(encoding="utf-8"))
                if response_validator is not None:
                    response_validator(cached)
                report_response_cache_hit()
                return cached
            except (OSError, ValueError):
                pass
    response = _completion_with_backoff(
        client,
        request,
        max_attempts=max_provider_attempts,
    )
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise ValueError("CLOVA response did not contain any choices")
    text = _message_text(getattr(choices[0].message, "content", ""))
    if not text:
        raise ValueError("CLOVA response content was empty")
    parsed = _parse_model_json(text, response_model)
    if response_validator is not None:
        response_validator(parsed)
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(parsed.model_dump_json(), encoding="utf-8")
            os.replace(temporary, cache_path)
        finally:
            temporary.unlink(missing_ok=True)
    return parsed
