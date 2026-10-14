import json
from types import SimpleNamespace

from helpers.semantic_batch_fixtures import fake_judge, semantic_request, semantic_decision
from sp_pdf_judger import clova_transport


def test_usage_is_actual_only_and_cache_hit_does_not_recount_tokens(monkeypatch, tmp_path):
    requests = [semantic_request(0), semantic_request(1)]
    judge, provider = fake_judge(monkeypatch, tmp_path, [{'items': [semantic_decision(r) for r in requests]}])
    original = provider.create
    def create(**kwargs):
        response = original(**kwargs)
        response.usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120)
        return response
    provider.create = create
    with clova_transport.clova_request_context():
        judge.explain_authoritative_semantic_batch(requests=requests)
        measured = clova_transport.request_diagnostics()
        assert measured['actual_input_tokens'] == 100
        assert measured['actual_output_tokens'] == 20
        assert measured['usage_reported_responses'] == 1
        assert measured['http_attempts'] == 1
        assert measured['repairs'] == 0
    with clova_transport.clova_request_context():
        judge.explain_authoritative_semantic_batch(requests=requests)
        cached = clova_transport.request_diagnostics()
        assert cached['actual_input_tokens'] is None
        assert cached['usage_reported_responses'] == 0 and cached['cache_hits'] == 1
        assert cached['http_attempts'] == 0
        assert 'BATCH-A' not in json.dumps(cached)
        assert 'provider.invalid' not in json.dumps(cached)


def test_missing_usage_is_unavailable_not_zero(monkeypatch, tmp_path):
    requests = [semantic_request(0), semantic_request(1)]
    judge, _ = fake_judge(monkeypatch, tmp_path, [{'items': [semantic_decision(r) for r in requests]}])
    with clova_transport.clova_request_context():
        judge.explain_authoritative_semantic_batch(requests=requests)
        stats = clova_transport.request_diagnostics()
    assert stats['actual_input_tokens'] is None and stats['actual_output_tokens'] is None
    assert stats['usage_missing_responses'] == 1


def test_gate_time_and_response_wait_are_separate(monkeypatch, tmp_path):
    judge, provider = fake_judge(monkeypatch, tmp_path, [{'status': 'ok'}, {'status': 'ok'}])
    with clova_transport.clova_request_context():
        for _ in range(2):
            clova_transport.completion_with_backoff(judge.client, {'model': 'HCX-007'})
        stats = clova_transport.request_diagnostics()
    assert stats['http_attempts'] == 2
    assert stats['gate_wait_seconds'] == 2
    assert stats['response_wait_seconds'] == 0
    assert set(clova_transport.request_metrics()) == {'http_attempts', 'retries', 'cache_hits'}
