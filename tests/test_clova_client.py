from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from sp_pdf_judger.clova_client import request_structured_response
from sp_pdf_judger.clova_transport import ClovaServiceError


class DemoResponse(BaseModel):
    status: str
    reason: str


class FakeCompletions:
    def __init__(self) -> None:
        self.request: dict[str, object] = {}

    def create(self, **kwargs):
        self.request = kwargs
        content = json.dumps({"status": "pass", "reason": "matched"})
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


def test_clova_structured_request_uses_openai_compatible_chat_api() -> None:
    completions = FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    response = request_structured_response(
        client=client,
        model="HCX-007",
        prompt="judge this",
        response_model=DemoResponse,
        max_completion_tokens=1234,
    )

    assert response.status == "pass"
    assert completions.request["model"] == "HCX-007"
    assert completions.request["messages"] == [
        {"role": "user", "content": "judge this"}
    ]
    assert completions.request["max_completion_tokens"] == 1234
    assert completions.request["reasoning_effort"] == "none"
    assert completions.request["response_format"]["type"] == "json_schema"


def test_one_attempt_option_does_not_retry_provider_429(monkeypatch) -> None:
    class RateLimitError(RuntimeError):
        status_code = 429
        response = SimpleNamespace(headers={"retry-after": "0"})

    class AlwaysLimited:
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            raise RateLimitError("rate limited")

    completions = AlwaysLimited()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr("sp_pdf_judger.clova_transport._GATES", {})

    with pytest.raises(ClovaServiceError) as failed:
        request_structured_response(
            client=client,
            model="HCX-007",
            prompt="judge once",
            response_model=DemoResponse,
            max_provider_attempts=1,
        )

    assert completions.calls == 1
    assert failed.value.kind == "rate_limit"
