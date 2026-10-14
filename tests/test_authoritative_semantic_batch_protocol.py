import json
from copy import deepcopy

import pytest

from helpers.semantic_batch_fixtures import semantic_request, semantic_decision
from sp_pdf_judger.permit_semantic_batch import (
    SemanticBatchResponse, SemanticBatchWireResponse, SemanticProtocolError,
    build_semantic_batch_prompt, index_semantic_response,
)


def test_shared_evidence_serialized_once_but_results_stay_independent():
    requests = [semantic_request(0, result='음성'), semantic_request(1, result='양성', batch='BATCH-B')]
    prompt = build_semantic_batch_prompt(requests)
    payload = json.loads(prompt.split('\nPAYLOAD\n', 1)[1])
    assert len(payload['evidence_sets']) == 1
    assert len(payload['records']) == 2
    assert [r['result'] for r in payload['records']] == ['음성', '양성']
    assert 'BATCH-B' in prompt and 'BATCH-A' in prompt
    clauses = [clause['text'] for evidence in payload['evidence_sets'] for candidate in evidence['candidates'] for clause in candidate['clauses']]
    assert clauses.count(requests[0].bundle.candidates[0].clauses[-1]) == 1


def test_reversed_responses_map_by_id_not_position():
    requests = [semantic_request(0), semantic_request(1)]
    wire = SemanticBatchWireResponse(items=[semantic_decision(requests[1]), semantic_decision(requests[0], 'FAIL')])
    indexed = index_semantic_response(requests, wire)
    assert indexed['r0'].clauses[0].status == 'FAIL'
    assert indexed['r1'].clauses[0].status == 'PASS'


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'unknown', 'cross_set', 'wrong_type'])
def test_framing_never_cross_assigns_records_or_sets(damage):
    requests = [semantic_request(0), semantic_request(1)]
    items = [semantic_decision(r) for r in requests]
    if damage == 'missing': items.pop()
    elif damage == 'duplicate': items[1] = deepcopy(items[0])
    elif damage == 'unknown': items[1]['record_id'] = 'r999'
    elif damage == 'cross_set': items[1]['evidence_set_id'] = 'different-bundle'
    else: items[1]['record_id'] = 1
    with pytest.raises(SemanticProtocolError) as error:
        index_semantic_response(requests, SemanticBatchWireResponse(items=items))
    assert error.value.framing
    assert '허가서' not in str(error.value)  # safe error codes, no response text


def test_attributable_schema_error_preserves_healthy_item_for_repair():
    requests = [semantic_request(0), semantic_request(1)]
    items = [semantic_decision(r) for r in requests]
    items[1]['decision']['clauses'] = 'invalid-sensitive-text'
    with pytest.raises(SemanticProtocolError) as error:
        index_semantic_response(requests, SemanticBatchWireResponse(items=items))
    assert not error.value.framing
    assert set(error.value.item_errors) == {'r1'}
    assert error.value.response is not None
    assert 'invalid-sensitive-text' not in str(error.value)


def test_generation_schema_is_strict_and_not_wire_schema():
    schema = SemanticBatchResponse.model_json_schema()
    assert schema['additionalProperties'] is False
    assert schema['$defs']['SemanticBatchItem']['additionalProperties'] is False
    assert 'GroundedPermitResponse' in schema['$defs']
