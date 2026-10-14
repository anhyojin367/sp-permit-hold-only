"""Real request/parse/cache boundary, with only the external API replaced."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from sp_pdf_judger.clova_transport import ClovaServiceError, clova_request_context
from sp_pdf_judger.llm import ClovaJudgeClient
from sp_pdf_judger.permit_group_batching import PermitGroupBatchRequest
from sp_pdf_judger.permit_llm_protocol import build_group_payload


@pytest.mark.parametrize("kind,reason,semantic", [
    ("unparsed", "unparsed_condition", True),
    ("qualitative", "qualifier_missing", True),
    ("qualitative", "fact_missing", True),
    ("compare", "fact_missing", False),
    ("compare", "unit_incompatible", False),
])
def test_payload_distinguishes_semantic_pending_from_structured_blockers(kind, reason, semantic):
    quote = "원문은 6.00 \nmOsm/kg이다."
    condition = SimpleNamespace(condition_id="c1", evidence_id="e1", kind=kind, quote=quote)
    execution = SimpleNamespace(condition_id="c1", status="hold", fact_ids=(), reason_code=reason)
    payload = build_group_payload(group=SimpleNamespace(facts=()), candidates=(),
        conditions=(condition,), executions=(execution,))
    assert payload["deterministic_executions"][0]["semantic_review_required"] is semantic
    assert payload["deterministic_executions"][0]["status"] == "hold"
    assert payload["conditions"][0]["quote"] == "원문은 6.00 mOsm/kg이다."
    assert condition.quote == quote  # Original authoritative evidence is untouched.


def valid_item():
    return {
        "group_id": "g1", "selected_evidence_id": "e1",
        "decisions": [{"condition_id": "c1", "status": "pass",
                       "fact_ids": ["f1"], "quote": "음성이어야 한다."}],
        "status": "검수합격", "reason": "허가서의 음성 기준과 결과가 일치한다.",
    }


def test_single_line_source_spacing_is_not_rewritten_in_wire_quote():
    quote = "삼투압은 6.00  mOsm/kg이다."
    condition = SimpleNamespace(condition_id="c1", evidence_id="e1", kind="unparsed", quote=quote)
    payload = build_group_payload(group=SimpleNamespace(facts=()), candidates=(),
        conditions=(condition,), executions=())
    assert payload["conditions"][0]["quote"] == quote


class Provider:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(self.response)),
            finish_reason="stop",
        )])


def client_for(provider):
    return SimpleNamespace(
        enabled=True, client=SimpleNamespace(base_url="https://example.invalid",
            chat=SimpleNamespace(completions=provider)),
        model="test", max_completion_tokens=4096, call_count=0,
        success_count=0, last_error="", permit_audit=[],
    )


def invoke(client):
    request = PermitGroupBatchRequest(
        group_id="g1", payload={"group": {"group_id": "g1"}},
        serialized_payload='{"group":{"group_id":"g1"}}',
    )
    return ClovaJudgeClient.explain_permit_group_batch_once(client, requests=(request,))


@pytest.fixture(autouse=True)
def isolated_transport(monkeypatch):
    monkeypatch.setattr("sp_pdf_judger.clova_transport._GATES", {})
    monkeypatch.delenv("CLOVA_RESPONSE_CACHE_DIR", raising=False)


def test_provider_receives_the_same_required_fields_and_enums_as_final_validation():
    provider = Provider({"items": [valid_item()]})
    result = invoke(client_for(provider))
    assert result.items[0].reason
    schema = provider.requests[0]["response_format"]["json_schema"]["schema"]
    item = schema["$defs"][schema["properties"]["items"]["items"]["$ref"].split("/")[-1]]
    assert set(item["required"]) == {
        "group_id", "selected_evidence_id", "decisions", "status", "reason",
    }
    assert set(item["properties"]["status"]["enum"]) == {"검수합격", "검수불합격", "검수보류"}
    assert item["properties"]["reason"]["type"] == "string"
    assert item["properties"]["reason"]["minLength"] == 1
    decision = schema["$defs"][item["properties"]["decisions"]["items"]["$ref"].split("/")[-1]]
    assert set(decision["required"]) == {"condition_id", "status", "fact_ids", "quote"}
    assert set(decision["properties"]["status"]["enum"]) == {"pass", "fail", "hold"}


@pytest.mark.parametrize("fault", ["observed", "reason_null", "condition_null", "duplicate", "missing"])
def test_invalid_batch_is_not_success_cached_or_a_document_verdict(tmp_path, fault):
    item = valid_item()
    if fault in {"observed", "reason_null"}:
        item["reason"] = None
    if fault in {"observed", "condition_null"}:
        item["decisions"][0]["status"] = None
    if fault == "observed":
        item["status"] = "pass"
    response = {"items": [] if fault == "missing" else [item]}
    if fault == "duplicate":
        response["items"].append(copy.deepcopy(item))
    provider = Provider(response)
    client = client_for(provider)
    with clova_request_context(cache_dir=tmp_path):
        with pytest.raises(ClovaServiceError) as error:
            invoke(client)
    assert error.value.kind == "response"
    assert client.call_count == 1
    assert client.success_count == 0
    assert client.last_error
    assert len(provider.requests) == 1  # no unbounded format retries
    assert not list(tmp_path.glob("*.json"))


def test_good_batch_response_is_reused_without_another_http_request(tmp_path):
    provider = Provider({"items": [valid_item()]})
    client = client_for(provider)
    with clova_request_context(cache_dir=tmp_path):
        assert invoke(client).items[0].status == "검수합격"
        assert invoke(client).items[0].status == "검수합격"
    assert len(provider.requests) == 1
    assert client.success_count == 2
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_outer_parseable_but_invalid_cached_batch_is_not_reused(tmp_path):
    provider = Provider({"items": [valid_item()]})
    client = client_for(provider)
    with clova_request_context(cache_dir=tmp_path):
        invoke(client)
        cache_path, = tmp_path.glob("*.json")
        invalid = valid_item()
        invalid["reason"] = None
        cache_path.write_text(json.dumps({"items": [invalid]}), encoding="utf-8")
        result = invoke(client)
    assert len(provider.requests) == 2
    assert result.items[0].reason == "허가서의 음성 기준과 결과가 일치한다."
    assert json.loads(cache_path.read_text(encoding="utf-8"))["items"][0]["reason"]
