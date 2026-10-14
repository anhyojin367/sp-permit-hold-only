from copy import deepcopy
import json

import pytest

from helpers.semantic_batch_fixtures import fake_judge, semantic_request, semantic_decision
from sp_pdf_judger.clova_transport import ClovaServiceError, clova_request_context, request_metrics
from sp_pdf_judger.config import PASS_LABEL, FAIL_LABEL, HOLD_LABEL
from sp_pdf_judger.llm import ClovaJudgeClient


def test_real_boundary_returns_mixed_results_and_source_spans(monkeypatch, tmp_path):
    requests = [semantic_request(i) for i in range(3)]
    answer = {'items': [semantic_decision(r, status) for r, status in zip(requests, ['PASS', 'FAIL', 'HOLD'])]}
    judge, provider = fake_judge(monkeypatch, tmp_path, [answer])
    with clova_request_context():
        results = judge.explain_authoritative_semantic_batch(requests=requests)
        assert request_metrics()['http_attempts'] == 1
        assert request_metrics()['cache_hits'] == 0
    assert [results[r.record_id].status for r in requests] == [PASS_LABEL, FAIL_LABEL, HOLD_LABEL]
    assert results['r0']._permit_evidence
    assert results['r0']._permit_evidence[0]['span_ids']
    schema = provider.requests[0]['response_format']['json_schema']['schema']
    assert schema['title'] == 'SemanticBatchResponse'
    assert schema['$defs']['SemanticBatchItem']['additionalProperties'] is False


def test_disabled_client_has_no_provider_call():
    judge = ClovaJudgeClient(execution_settings=('', 'HCX-007', 'https://provider.invalid'))
    assert judge.explain_authoritative_semantic_batch(requests=[semantic_request(0)]) is None
    assert judge.call_count == 0


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'unknown'])
def test_successful_envelope_repair_is_completed_and_counted_once(monkeypatch, tmp_path, damage):
    from sp_pdf_judger.clova_transport import request_diagnostics
    requests = [semantic_request(0), semantic_request(1)]
    good = {'items': [semantic_decision(r) for r in requests]}
    broken = deepcopy(good)
    if damage == 'missing': broken['items'].pop()
    elif damage == 'duplicate': broken['items'][1] = deepcopy(broken['items'][0])
    else: broken['items'][1]['record_id'] = 'unknown'
    judge, _ = fake_judge(monkeypatch, tmp_path, [broken, good])
    with clova_request_context():
        answers = judge.explain_authoritative_semantic_batch(requests=requests)
        assert len(answers) == 2
        assert judge.call_count == judge.success_count == 2
        assert request_diagnostics()['repairs'] == 1


def test_semantic_client_audit_contains_no_document_or_provider_text(monkeypatch, tmp_path):
    requests = [semantic_request(0)]
    answer = {'items': [semantic_decision(requests[0])]}
    answer['items'][0]['decision']['reason'] = 'PRIVATE_PROVIDER_REASON'
    judge, _ = fake_judge(monkeypatch, tmp_path, [answer])
    results = judge.explain_authoritative_semantic_batch(requests=requests)
    audit = json.dumps(judge.permit_audit, ensure_ascii=False)
    assert 'PRIVATE_PROVIDER_REASON' not in audit
    assert 'BATCH-A' not in audit
    assert 'record_context' not in audit
    assert results['r0']._permit_evidence  # intentional source evidence is retained


def test_outbound_output_limit_matches_scheduled_model_budget(monkeypatch, tmp_path):
    requests = [semantic_request(0), semantic_request(1)]
    judge, provider = fake_judge(monkeypatch, tmp_path, [{'items': [semantic_decision(r) for r in requests]}])
    judge.max_completion_tokens = 40000
    judge.explain_authoritative_semantic_batch(requests=requests)
    assert provider.requests[0]['max_completion_tokens'] == 32768


@pytest.mark.parametrize('damage', ['bad_schema', 'missing_clause', 'extra_clause', 'masked_acceptance'])
def test_only_damaged_item_is_repaired_once_and_bad_batch_not_cached(monkeypatch, tmp_path, damage):
    requests = [semantic_request(0), semantic_request(1, result='양성')]
    good = semantic_decision(requests[0])
    bad = semantic_decision(requests[1], 'FAIL')
    if damage == 'bad_schema': bad['decision']['clauses'] = 'broken'
    elif damage == 'missing_clause': bad['decision']['clauses'].pop()
    elif damage == 'extra_clause': bad['decision']['clauses'].append({'clause_id': 999, 'kind': 'acceptance', 'status': 'FAIL', 'reason': 'bad'})
    else: bad['decision']['clauses'][0].update(kind='procedure', status='N/A')
    repaired = semantic_decision(requests[1], 'FAIL')['decision']
    judge, provider = fake_judge(monkeypatch, tmp_path, [{'items': [good, bad]}, repaired])
    results = judge.explain_authoritative_semantic_batch(requests=requests)
    assert [results[r.record_id].status for r in requests] == [PASS_LABEL, FAIL_LABEL]
    assert len(provider.requests) == 2 and judge.call_count == 2
    assert judge.success_count == judge.call_count  # resolved repair is not an operational failure
    assert 'BATCH-A' in provider.requests[1]['messages'][0]['content']
    assert provider.requests[1]['response_format']['json_schema']['schema']['title'] == 'GroundedPermitResponse'
    caches = list((tmp_path / 'responses').glob('*.json'))
    assert len(caches) == 1
    assert 'items' not in json.loads(caches[0].read_text(encoding='utf-8'))


@pytest.mark.parametrize('response', ['malformed-json', {'items': []}, {'items': [{'record_id': 'unknown'}]}])
def test_framing_has_one_correction_then_raises_not_hold(monkeypatch, tmp_path, response):
    judge, provider = fake_judge(monkeypatch, tmp_path, [response, response])
    with pytest.raises(ClovaServiceError) as error:
        judge.explain_authoritative_semantic_batch(requests=[semantic_request(0), semantic_request(1)])
    assert error.value.kind == 'response'
    assert len(provider.requests) == 2
    assert not list((tmp_path / 'responses').glob('*.json'))


def test_envelope_correction_does_not_receive_additional_item_repair(monkeypatch, tmp_path):
    requests = [semantic_request(0), semantic_request(1)]
    items = [semantic_decision(r) for r in requests]
    items[1]['decision']['clauses'] = []
    judge, provider = fake_judge(monkeypatch, tmp_path, ['malformed', {'items': items}])
    with pytest.raises(ClovaServiceError):
        judge.explain_authoritative_semantic_batch(requests=requests)
    assert len(provider.requests) == 2


def test_exhausted_item_repair_raises_service_error(monkeypatch, tmp_path):
    requests = [semantic_request(0), semantic_request(1)]
    items = [semantic_decision(r) for r in requests]
    items[1]['decision']['clauses'] = []
    judge, provider = fake_judge(monkeypatch, tmp_path, [{'items': items}, items[1]['decision']])
    with pytest.raises(ClovaServiceError) as error:
        judge.explain_authoritative_semantic_batch(requests=requests)
    assert error.value.kind == 'response' and len(provider.requests) == 2


def test_service_errors_are_not_format_repairs(monkeypatch, tmp_path):
    judge, provider = fake_judge(monkeypatch, tmp_path, [ClovaServiceError('rate_limit', 429, '42901', retry_after=200)])
    with pytest.raises(ClovaServiceError) as error:
        judge.explain_authoritative_semantic_batch(requests=[semantic_request(0), semantic_request(1)])
    assert error.value.kind == 'rate_limit'
    assert len(provider.requests) == 1


def test_valid_cached_batch_is_revalidated_and_no_http_is_needed(monkeypatch, tmp_path):
    requests = [semantic_request(0), semantic_request(1)]
    answer = {'items': [semantic_decision(r) for r in requests]}
    judge, provider = fake_judge(monkeypatch, tmp_path, [answer])
    judge.explain_authoritative_semantic_batch(requests=requests)
    with clova_request_context():
        result = judge.explain_authoritative_semantic_batch(requests=requests)
        assert request_metrics() == {'http_attempts': 0, 'retries': 0, 'cache_hits': 1}
    assert result['r0'].status == PASS_LABEL
    assert len(provider.requests) == 1
