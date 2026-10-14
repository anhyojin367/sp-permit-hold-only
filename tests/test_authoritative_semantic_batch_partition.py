from dataclasses import replace

from helpers.semantic_batch_fixtures import semantic_request
from sp_pdf_judger.permit_semantic_settings import SemanticSettings, model_budget
from sp_pdf_judger.permit_semantic_batch import partition_semantic_requests


SETTINGS = SemanticSettings('shared_evidence')
BUDGET = model_budget('HCX-007', 4096)


def partition(requests, budget=BUDGET):
    return partition_semantic_requests(requests, SETTINGS, budget)


def test_eight_records_become_two_requests_without_deduping_results():
    requests = [semantic_request(i, result=f'{i}', batch=f'B-{i}') for i in range(8)]
    result = partition(requests)
    assert [len(b) for b in result.batches] == [4, 4]
    assert not result.singles and not result.oversized_ids
    assert [r.record_id for b in result.batches for r in b] == [f'r{i}' for i in range(8)]


def test_unknown_model_unknown_batch_and_incomplete_bundle_are_singletons():
    requests = [semantic_request(0), semantic_request(1)]
    assert partition(requests, None).singles == tuple(requests)
    assert not partition(requests, None).batches
    unknown = semantic_request(2, batch='')
    incomplete = replace(requests[1], bundle=replace(requests[1].bundle, complete=False))
    assert partition([requests[0], unknown, incomplete]).singles == (requests[0], unknown, incomplete)


def test_different_evidence_or_method_is_not_shared():
    requests = [semantic_request(0), semantic_request(1, body='양성이어야 한다.'), semantic_request(2, method='방법 B')]
    assert partition(requests).singles == tuple(requests)


def test_oversized_single_is_explicit_not_truncated():
    huge = semantic_request(0, body=('조건은 충족되어야 한다. ' * 9000))
    result = partition([huge])
    assert result.oversized_ids == ('r0',)
    assert not result.batches and not result.singles


def test_output_clause_budget_splits_before_input_limit():
    body = ' '.join(f'조건{i}는 충족되어야 한다.' for i in range(10))
    requests = [semantic_request(i, body=body) for i in range(4)]
    result = partition(requests, model_budget('HCX-007', 4096))
    assert [len(b) for b in result.batches] == [2, 2]


def test_legacy_mode_never_batches():
    requests = [semantic_request(0), semantic_request(1)]
    result = partition_semantic_requests(requests, SemanticSettings('legacy'), BUDGET)
    assert result.singles == tuple(requests) and not result.batches
