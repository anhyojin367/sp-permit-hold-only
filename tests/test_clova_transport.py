"""No live API or real sleeps: exercise transport budgets and schema fallback."""
import json
from types import SimpleNamespace

import pytest

from sp_pdf_judger import clova_transport as transport
from sp_pdf_judger import clova_client, llm


class ProviderError(Exception):
    def __init__(self, status, headers=None, body=None):
        super().__init__("SECRET provider body / submitted document")
        self.status_code = status
        self.response = SimpleNamespace(headers=headers or {})
        self.body = body or {}


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=100.0, sleeps=[])
    def sleep(seconds):
        assert seconds >= 0
        state.sleeps.append(seconds)
        state.now += seconds
    monkeypatch.setattr(transport, "time", SimpleNamespace(monotonic=lambda: state.now, sleep=sleep))
    monkeypatch.setattr(transport, "_GATES", {})
    monkeypatch.delenv("CLOVA_RESPONSE_CACHE_DIR", raising=False)
    return state


def response(text=None, headers=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text or
        json.dumps({"status": "검수합격", "reason": "조건 충족"})))], headers=headers or {})


def client_with(clock, events):
    calls = []
    def create(**kwargs):
        calls.append((clock.now, kwargs))
        event = events.pop(0)
        if callable(event):
            return event(kwargs)
        if isinstance(event, Exception):
            raise event
        return event
    return SimpleNamespace(base_url="https://example.invalid", api_key="SECRET",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


def judge_with(client):
    obj = llm.ClovaJudgeClient.__new__(llm.ClovaJudgeClient)
    obj.client, obj.enabled, obj.model = client, True, "fixture"
    obj.call_count = obj.success_count = 0
    obj.max_completion_tokens = 1024
    obj.last_error, obj.domain_detail_context, obj.permit_audit = "", "", []
    return obj


def explain(judge):
    return judge.explain(test_name="성상", criteria="맑음", result="맑음", rag_contexts=[])


def batch_explain(judge):
    from sp_pdf_judger.permit_group_batching import PermitGroupBatchRequest
    return judge.explain_permit_group_batch_once(
        requests=[PermitGroupBatchRequest("g1", {"group_id": "g1"}, '{"group_id":"g1"}')]
    )


def test_group_batch_recovers_429_and_reuses_success_on_explicit_retry(clock, tmp_path):
    valid_batch = json.dumps({"items": [{"group_id": "g1", "selected_evidence_id": "e1",
        "decisions": [], "status": "검수보류", "reason": "관측 근거가 부족합니다."}]})
    client, calls = client_with(clock, [ProviderError(429, {"Retry-After": "12"}),
                                      response(valid_batch)])
    judge = judge_with(client)
    with transport.clova_request_context(cache_dir=tmp_path):
        assert batch_explain(judge).items[0].group_id == "g1"
        assert batch_explain(judge).items[0].group_id == "g1"
        assert transport.request_metrics() == {"http_attempts": 2, "retries": 1, "cache_hits": 1}
    assert len(calls) == 2
    assert calls[1][0] - calls[0][0] == 12
    assert all("response_format" in kwargs for _, kwargs in calls)
    assert transport.request_metrics() == {"http_attempts": 0, "retries": 0, "cache_hits": 0}


def test_group_batch_persistent_limit_is_bounded_operational_failure(clock):
    client, calls = client_with(clock, [ProviderError(429) for _ in range(3)])
    with pytest.raises(transport.ClovaServiceError, match="호출 한도"):
        batch_explain(judge_with(client))
    assert len(calls) == 3


def test_group_batch_authentication_never_retries(clock):
    client, calls = client_with(clock, [ProviderError(401)])
    with pytest.raises(transport.ClovaServiceError) as failed:
        batch_explain(judge_with(client))
    assert failed.value.kind == "authentication" and len(calls) == 1


def test_429_honors_provider_reset_and_recovers_without_format_fallback(clock):
    client, calls = client_with(clock, [ProviderError(429, {"x-ratelimit-reset-tokens": "23s"}), response()])
    progress = []
    with transport.clova_request_context(progress.append):
        judge = judge_with(client)
        assert explain(judge).status == "검수합격"
    assert calls[1][0] - calls[0][0] == 23
    assert all("response_format" in kw for _, kw in calls)
    assert judge.call_count == judge.success_count == 1
    assert any("23초" in line for line in progress)


def test_persistent_limit_stops_after_three_no_double_format_retries(clock, capsys):
    client, calls = client_with(clock, [ProviderError(429, body={"status": {"code": "42901"}}) for _ in range(3)])
    with pytest.raises(transport.ClovaServiceError) as failed:
        explain(judge_with(client))
    assert len(calls) == 3 and failed.value.provider_code == "42901"
    assert failed.value.kind == "rate_limit" and clock.now == 130
    assert "SECRET" not in str(failed.value) + capsys.readouterr().out


def test_huge_retry_after_does_not_sleep_or_request_past_budget(clock):
    client, calls = client_with(clock, [ProviderError(429, {"Retry-After": "3600"})])
    with pytest.raises(transport.ClovaServiceError) as failed:
        explain(judge_with(client))
    assert len(calls) == 1 and not clock.sleeps and failed.value.retry_after == 3600
    # Another document respects the shared cooldown and issues no request.
    with pytest.raises(transport.ClovaServiceError):
        explain(judge_with(client))
    assert len(calls) == 1


def test_timeout_budget_limits_request_time_and_propagates(clock):
    def timeout(kwargs):
        clock.now += kwargs["timeout"]
        raise TimeoutError("SECRET")
    client, calls = client_with(clock, [timeout, timeout, timeout])
    with pytest.raises(transport.ClovaServiceError) as failed:
        explain(judge_with(client))
    assert failed.value.kind == "timeout"
    assert len(calls) == 2 and clock.now == 230  # 60 + 10 + 60; next wait exceeds 150 budget.
    assert all("response_format" in kw for _, kw in calls)


@pytest.mark.parametrize("status,kind", [(401,"authentication"), (403,"authentication"),
                                         (400,"request"), (413,"request"), (422,"request")])
def test_nonrecoverable_http_never_retries_or_exposes_body(clock, status, kind):
    client, calls = client_with(clock, [ProviderError(status)])
    with pytest.raises(transport.ClovaServiceError) as failed:
        explain(judge_with(client))
    assert failed.value.kind == kind and len(calls) == 1
    assert "SECRET" not in str(failed.value)


@pytest.mark.parametrize("error", [ConnectionError("private"), ProviderError(503)])
def test_transient_connection_or_server_failure_can_recover(clock, error):
    client, calls = client_with(clock, [error, response()])
    assert explain(judge_with(client)).status == "검수합격"
    assert len(calls) == 2 and calls[1][0] - calls[0][0] == 10


@pytest.mark.parametrize("first", [ProviderError(400, body={"message": "Unsupported parameter: response_format"}),
                                    response("not json")])
def test_only_format_error_gets_one_schema_fallback_without_false_failure(clock, first):
    client, calls = client_with(clock, [first, response()])
    judge = judge_with(client)
    assert explain(judge).status == "검수합격"
    assert "response_format" in calls[0][1] and "response_format" not in calls[1][1]
    assert judge.call_count == judge.success_count == 1 and judge.last_error == ""


def test_invalid_format_twice_is_operational_failure_not_document_hold(clock):
    client, calls = client_with(clock, [response("bad"), response("bad")])
    with pytest.raises(transport.ClovaServiceError) as failed:
        explain(judge_with(client))
    assert failed.value.kind == "response" and len(calls) == 2


def test_success_headers_delay_next_request_and_raw_response_is_parsed(clock):
    calls = []
    raw = SimpleNamespace(headers={"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "1m2s"}, parse=response)
    def create(**kw):
        calls.append(clock.now)
        return raw
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        with_raw_response=SimpleNamespace(create=create))))
    transport.completion_with_backoff(client, {"model": "fixture"})
    transport.completion_with_backoff(client, {"model": "fixture"})
    assert calls == [100, 162]


def test_adaptive_interval_retained_after_successful_rate_retry(clock):
    client, calls = client_with(clock, [ProviderError(429), response(), response()])
    explain(judge_with(client)); explain(judge_with(client))
    assert calls[2][0] - calls[1][0] == 5


@pytest.mark.parametrize("value,expected", [("23s",23), ("1m2s",62), ("500ms",.5),
    ("NaN",0), ("inf",0), ("-10",0), ("garbage",0), ("0.5",.5)])
def test_reset_duration_parser(value, expected):
    assert transport._duration(value) == expected


def test_retry_after_http_date():
    assert transport.retry_delay({"Retry-After": "Thu, 01 Jan 1970 00:00:00 GMT"}) == 0
    assert transport.retry_delay({"Retry-After": "bad"}) == 0
    assert transport.retry_delay({"Retry-After": "15", "x-ratelimit-reset-tokens": "23s"}, limited=True) == 23


def test_success_response_cache_reused_only_for_same_request_and_context(clock, tmp_path):
    client, calls = client_with(clock, [response(), response(), response()])
    def request(prompt):
        return clova_client.request_structured_response(client=client, model="fixture", prompt=prompt,
                                                        response_model=llm.JudgeResponse)
    with transport.clova_request_context(cache_dir=tmp_path):
        request("first")
        request("first")
        request("changed")
    assert len(calls) == 2
    assert transport.response_cache_dir() is None
    request("first")  # No context = no implicit cross-workflow caching.
    assert len(calls) == 3


def test_failed_provider_response_is_not_cached(clock, tmp_path):
    client, calls = client_with(clock, [ProviderError(401)])
    with transport.clova_request_context(cache_dir=tmp_path), pytest.raises(transport.ClovaServiceError):
        explain(judge_with(client))
    assert not list(tmp_path.iterdir())


def test_auth_identity_has_independent_pacing(clock):
    client, calls = client_with(clock, [ProviderError(429, {"Retry-After": "3600"}), response()])
    with pytest.raises(transport.ClovaServiceError):
        explain(judge_with(client))
    client.api_key = "OTHER_SECRET"
    assert explain(judge_with(client)).status == "검수합격"
    assert len(calls) == 2 and calls[1][0] == 100


def test_sdk_disables_nested_retries(monkeypatch):
    options = {}
    monkeypatch.setattr(clova_client, "OpenAI", lambda **kw: options.update(kw) or object())
    clova_client.create_clova_client("fake", "https://example.invalid")
    assert options["max_retries"] == 0 and options["timeout"] == 60


def test_installed_sdk_raw_headers_and_429_contract_without_network(clock):
    import httpx2
    from openai import OpenAI
    received = []
    def handler(request):
        received.append((clock.now, json.loads(request.content)))
        if len(received) == 1:
            return httpx2.Response(429, json={"status": {"code": "42901", "message": "rate exceeded"}},
                headers={"x-ratelimit-reset-tokens": "17s"})
        return httpx2.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 0,
            "model": "fixture", "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({"status": "검수합격", "reason": "조건 충족"})}}]},
            headers={"x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "23s"})
    with OpenAI(api_key="synthetic-not-a-key", base_url="https://example.invalid/v1", max_retries=0,
                http_client=httpx2.Client(transport=httpx2.MockTransport(handler))) as client:
        judge = judge_with(client)
        assert explain(judge).status == "검수합격"
        assert explain(judge).status == "검수합격"
    assert [when for when, _ in received] == [100,117,140]
    assert all("response_format" in body and "timeout" not in body for _, body in received)


def test_repair_transport_failure_is_not_silently_turned_into_hold(monkeypatch):
    from test_permit_llm_protocol import response as grounded_response, CANDIDATE
    client = SimpleNamespace(permit_audit=[], call_count=0, success_count=0, client=None,
                             model="fixture", max_completion_tokens=1024)
    incomplete = grounded_response([(1, "acceptance", "PASS")])
    def fail(**kw):
        raise transport.ClovaServiceError("timeout")
    monkeypatch.setattr(llm, "request_structured_response", fail)
    with pytest.raises(transport.ClovaServiceError):
        llm.ClovaJudgeClient._ground_permit_result(client, incomplete, [CANDIDATE], "record", "prompt")
    assert client.call_count == 1 and client.success_count == 0
