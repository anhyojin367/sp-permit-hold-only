import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from scripts import benchmark_authoritative_batch as bench
from sp_pdf_judger import clova_transport
from helpers.semantic_batch_fixtures import fake_judge


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    sp, permit = tmp_path/'sp.pdf', tmp_path/'permit.pdf'
    sp.write_bytes(b'sp'); permit.write_bytes(b'permit')
    def execute(**kwargs):
        root = kwargs['out']
        index, responses = root/'permit_index', root/'responses'
        observed = {'index': len(list(index.iterdir())), 'responses': len(list(responses.iterdir()))}
        (index/'fixture.json').write_text('index')
        (responses/'fixture.json').write_text('response')
        return {'observed': observed, 'page_counts': {'sp': 26, 'permit': 23}, 'records': []}
    monkeypatch.setattr(bench, '_execute_run', execute)
    return dict(sp=sp, permit=permit)


def test_cold_does_not_discover_any_existing_user_cache(inputs, tmp_path, monkeypatch):
    outside = tmp_path/'user-responses'; outside.mkdir()
    (outside/'keep').write_text('old')
    monkeypatch.setenv('CLOVA_RESPONSE_CACHE_DIR', str(outside))
    report = bench.run_benchmark(**inputs, out=tmp_path/'cold')
    assert report['observed'] == {'index': 0, 'responses': 0}
    assert report['execution_kind'] == 'offline-planning'
    assert report['scale_validation'] == 'unverified'
    assert (outside/'keep').read_text() == 'old'


@pytest.mark.parametrize('state,response_count', [('index-only', 0), ('responses', 1)])
def test_seed_cache_types_are_explicit_and_replay_is_labelled(inputs, tmp_path, state, response_count):
    seed = tmp_path/'seed'
    bench.run_benchmark(**inputs, out=seed)
    report = bench.run_benchmark(**inputs, out=tmp_path/'next', cache_state=state, seed_run=seed)
    assert report['observed'] == {'index': 1, 'responses': response_count}
    assert report['cached_replay'] is (state == 'responses')
    assert not (tmp_path/'next'/'judgement_artifacts.pkl').exists()


def test_existing_output_and_invalid_cache_combination_rejected(inputs, tmp_path):
    with pytest.raises(ValueError):
        bench.run_benchmark(**inputs, out=tmp_path)
    with pytest.raises(ValueError):
        bench.run_benchmark(**inputs, out=tmp_path/'missing-seed', cache_state='responses')
    with pytest.raises(ValueError):
        bench.run_benchmark(**inputs, out=tmp_path/'bad-cold', seed_run=tmp_path)


def test_changed_document_or_model_rejects_seed(inputs, tmp_path):
    seed = tmp_path/'seed'; bench.run_benchmark(**inputs, out=seed)
    with pytest.raises(ValueError):
        bench.run_benchmark(**inputs, out=tmp_path/'model', cache_state='index-only', seed_run=seed, model='other')
    inputs['permit'].write_bytes(b'new permit same name')
    with pytest.raises(ValueError):
        bench.run_benchmark(**inputs, out=tmp_path/'changed', cache_state='responses', seed_run=seed)


def test_offline_guard_runs_before_execute_or_provider_construction(inputs, tmp_path, monkeypatch):
    from sp_pdf_judger import llm
    def execute(**kwargs):
        with pytest.raises(RuntimeError, match='offline'):
            socket.create_connection(('example.invalid', 443))
        with pytest.raises(RuntimeError, match='offline'):
            llm.create_clova_client(api_key='never-send')
        return {'page_counts': {}, 'records': []}
    monkeypatch.setattr(bench, '_execute_run', execute)
    with pytest.raises(RuntimeError, match='offline'):
        bench.run_benchmark(**inputs, out=tmp_path/'guard')
    assert not (tmp_path/'guard'/'metrics.json').exists()


def test_seed_symlink_is_not_followed(inputs, tmp_path):
    seed = tmp_path/'seed'; bench.run_benchmark(**inputs, out=seed)
    link = seed/'permit_index'/'outside'
    try:
        link.symlink_to(tmp_path/'sp.pdf')
    except OSError:
        pytest.skip('symlink privilege unavailable')
    with pytest.raises(ValueError):
        bench.run_benchmark(**inputs, out=tmp_path/'linked', cache_state='index-only', seed_run=seed)


def test_record_deltas_are_by_position_not_aggregate():
    old = [{'position': 1, 'status': 'PASS', 'evidence_hash': 'a', 'reason_hash': 'x'},
           {'position': 2, 'status': 'FAIL', 'evidence_hash': 'b', 'reason_hash': 'y'}]
    new = [dict(old[0], status='FAIL'), dict(old[1], status='PASS', evidence_hash='c', reason_hash='z')]
    delta = bench.compare_records(old, new)
    assert delta == {'status_changed': [1, 2], 'evidence_changed': [2], 'reason_changed_needs_review': [2], 'missing_positions': [], 'added_positions': []}


def test_record_report_hashes_the_actual_comparison_basis():
    evaluation = SimpleNamespace(final_status='PASS', reason='same', record_position=5)
    def report(quote):
        return bench._record_report(SimpleNamespace(evaluations=[evaluation], metadata={
            'comparison_bases_by_evaluation': {0: [{'evidence': [{'quote': quote}]}]}}))
    delta = bench.compare_records(report('21일'), report('28일'))
    assert delta['evidence_changed'] == [0]


def test_http_attempt_ceiling_stops_before_extra_provider_call(monkeypatch, tmp_path):
    judge, provider = fake_judge(monkeypatch, tmp_path, [{'ok': True}, {'ok': True}])
    with clova_transport.clova_request_context(max_http_attempts=1):
        clova_transport.completion_with_backoff(judge.client, {'model': 'HCX-007'})
        with pytest.raises(clova_transport.ClovaServiceError):
            clova_transport.completion_with_backoff(judge.client, {'model': 'HCX-007'})
    assert len(provider.requests) == 1


def test_planner_uses_real_normalization_and_restores_disabled_client(monkeypatch, tmp_path):
    from test_authoritative_semantic_batch_pipeline import make_pipeline, records
    pipeline, _, provider = make_pipeline(monkeypatch, tmp_path)
    original = pipeline.judge_engine.llm_client
    original.enabled = False
    planned = bench._planning_report(pipeline, records(), 'HCX-007', 4096, 'shared_evidence')
    assert planned['batched_records'] == 8 and planned['initial_batch_requests'] == 2
    assert pipeline.judge_engine.llm_client is original and not original.enabled
    assert not provider.requests
