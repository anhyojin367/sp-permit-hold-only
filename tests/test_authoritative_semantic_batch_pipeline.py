from dataclasses import replace
import json
from types import SimpleNamespace

import fitz
import pytest

from helpers.semantic_batch_fixtures import fake_judge
from sp_pdf_judger.config import PASS_LABEL, FAIL_LABEL
from sp_pdf_judger.permit_catalog import resolve_submission_permits
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.permit_semantic_settings import SemanticSettings
from sp_pdf_judger.pipeline import DocumentJudgePipeline
from sp_pdf_judger.schemas import ExtractedRecord


STAGE = 'CHO 마스터 세포주'


def make_pipeline(monkeypatch, tmp_path, mode='shared_evidence', body='음성이어야 한다.', test_name='확인시험'):
    pdf, permit = tmp_path / 'sp.pdf', tmp_path / 'permit.pdf'
    for path in (pdf, permit):
        with fitz.open() as document:
            document.new_page()
            document.save(path)
    resolution = resolve_submission_permits([permit], '동국바이오사이언스', '스카이코비원멀티주')
    def store_factory(paths, policy, **kwargs):
        store = PermitPdfStore.from_page_texts([(1, f'1. {STAGE}\n1.1. {test_name}\n{body}')],
                                              policy=policy, source_file=permit.name)
        store.permit_pdf_paths = list(paths)
        return store
    monkeypatch.setattr('sp_pdf_judger.pipeline.PermitPdfStore', store_factory)
    judge, provider = fake_judge(monkeypatch, tmp_path, [])
    def create(**kwargs):
        provider.requests.append(kwargs)
        prompt = kwargs['messages'][0]['content']
        schema = kwargs['response_format']['json_schema']['schema']['title']
        def decision(candidates, result):
            return {'permit_match_status': 'matched', 'candidate_id': 1, 'reason': '허가서와 독립 결과 비교',
                    'clauses': [{'clause_id': c['clause_id'], 'kind': 'procedure' if '관찰한다.' in c['text'] else 'acceptance',
                                'status': 'N/A' if '관찰한다.' in c['text'] else ('FAIL' if result == '양성' else 'PASS'), 'reason': '관측 결과 비교'}
                               for c in candidates[0]['clauses']]}
        if schema == 'SemanticBatchResponse':
            payload = json.loads(prompt.split('\nPAYLOAD\n')[1])
            evidence = {e['evidence_set_id']: e for e in payload['evidence_sets']}
            answer = {'items': [{'record_id': r['record_id'], 'evidence_set_id': r['evidence_set_id'],
                                'decision': decision(evidence[r['evidence_set_id']]['candidates'], r['result'])}
                               for r in reversed(payload['records'])]}
        elif schema == 'GroundedPermitResponse':
            candidates = json.loads(prompt.split('허가서 후보:\n')[1])
            result = prompt.split('SP 결과: ')[1].splitlines()[0]
            answer = decision(candidates, result)
        else:
            answer = {'status': PASS_LABEL, 'reason': 'SP 기준에 적합하여 검수합격입니다.'}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer, ensure_ascii=False)))])
    provider.create = create
    monkeypatch.setattr('sp_pdf_judger.pipeline.ClovaJudgeClient', lambda **kwargs: judge)
    pipeline = DocumentJudgePipeline(company='동국바이오사이언스', product='스카이코비원멀티주', permit_resolution=resolution,
                                      semantic_settings=SemanticSettings(mode))
    return pipeline, pdf, provider


def records(count=8, *, explicit=True):
    return [ExtractedRecord(order_idx=7, section_title=STAGE, test_name='확인시험', criteria='음성',
                           result='양성' if i % 2 else '음성', page_start=1, page_end=1,
                           raw_text=f'독립 시험 관측 행 {i}',
                           batch_id=f'LOT-{i}' if explicit else None,
                           batch_evidence={'source_kind': 'explicit_table_column', 'label': '제조번호', 'quote': f'제조번호 LOT-{i}'} if explicit else None)
            for i in range(count)]


def count_schemas(provider):
    return [r['response_format']['json_schema']['schema']['title'] for r in provider.requests]


def test_singleton_keeps_all_thirteen_exact_candidates(monkeypatch, tmp_path):
    pipeline, pdf, provider = make_pipeline(monkeypatch, tmp_path)
    store = PermitPdfStore.from_page_texts([(1, f'1. {STAGE}\n' + '\n'.join(
        f'1.{i}. 확인시험\n음성이어야 한다. 고유 조건 {i}.' for i in range(1, 14)))],
        policy=pipeline.judge_engine.permit_policy, source_file='permit.pdf')
    pipeline.permit_store = pipeline.judge_engine.permit_store = store
    pipeline.run(pdf, extracted_records=records(1))
    sent = next(r for r in provider.requests if r['response_format']['json_schema']['schema']['title'] in
                {'SemanticBatchResponse', 'GroundedPermitResponse'})
    prompt = sent['messages'][0]['content']
    if '\nPAYLOAD\n' in prompt:
        candidates = json.loads(prompt.split('\nPAYLOAD\n')[1])['evidence_sets'][0]['candidates']
    else:
        candidates = json.loads(prompt.split('허가서 후보:\n')[1])
    assert len(candidates) == 13
    assert '고유 조건 13' in prompt


def test_duplicate_order_idx_keeps_authoritative_basis_for_downstream_numeric_rule(monkeypatch, tmp_path):
    from sp_pdf_judger.policy_engine import Rule, numeric_compare
    pipeline, pdf, provider = make_pipeline(monkeypatch, tmp_path, body='10 ng/mL 이하이어야 한다.')
    create = provider.create
    def answer(**kwargs):
        response = create(**kwargs)
        if kwargs['response_format']['json_schema']['schema']['title'] == 'SemanticBatchResponse':
            data = json.loads(response.choices[0].message.content)
            for item in data['items']:
                if item['record_id'] == 'r1':
                    item['decision'] = {'permit_match_status': 'not_found', 'candidate_id': None,
                                        'clauses': [], 'reason': '대응 기준 없음'}
            response.choices[0].message.content = json.dumps(data)
        return response
    provider.create = answer
    findings = []
    def evaluate(ctx, book):
        rule = Rule(id='T', title='수치', instruction='허가서 기준', operation='numeric_compare', criterion_source='permit_first')
        found = numeric_compare(rule, ctx)
        findings.append(found)
        return []
    monkeypatch.setattr('sp_pdf_judger.pipeline.evaluate_policies', evaluate)
    rows = [replace(r, criteria='5 ng/mL 이하', result='8 ng/mL' if i == 0 else '4 ng/mL')
            for i, r in enumerate(records(2))]
    pipeline.run(pdf, extracted_records=rows)
    assert findings[0].status == 'PASS'
    assert [b['source'] for b in findings[0].details['comparison_bases']] == ['permit', 'sp']


def test_shared_fallback_audit_does_not_persist_raw_text(monkeypatch, tmp_path):
    pipeline, pdf, _ = make_pipeline(monkeypatch, tmp_path)
    row = replace(records(1, explicit=False)[0], raw_text='PRIVATE_SOURCE_TEXT')
    result = pipeline.run(pdf, extracted_records=[row])
    audit = json.dumps(result.metadata['permit_llm_audit'], ensure_ascii=False)
    assert 'PRIVATE_SOURCE_TEXT' not in audit
    assert 'record_context' not in audit and 'response' not in audit


def test_shared_unit_notes_are_attached_to_only_their_record(monkeypatch, tmp_path):
    pipeline, pdf, _ = make_pipeline(monkeypatch, tmp_path, body='10 EU/mL 이하이어야 한다.', test_name='엔도톡신시험')
    rows = [replace(r, test_name='엔도톡신시험', criteria='10 IU/mL 이하', result='8 IU/mL') for r in records(2)]
    result = pipeline.run(pdf, extracted_records=rows)
    notes = result.metadata['unit_normalizations']
    assert notes, 'fixture must exercise a real MD unit alias'
    assert {n.get('record_position') for n in notes} == {0, 1}
    assert all('MD 단위' in e.reason for e in result.evaluations if e.record_type == 'test')


def test_eight_independent_rows_with_duplicate_order_idx_use_two_permit_requests(monkeypatch, tmp_path):
    pipeline, pdf, provider = make_pipeline(monkeypatch, tmp_path)
    original = records()
    result = pipeline.run(pdf, extracted_records=original)
    evaluations = [e for e in result.evaluations if e.record_type == 'test']
    assert [e.final_status for e in evaluations] == [PASS_LABEL, FAIL_LABEL] * 4
    assert [e.result for e in evaluations] == [r.result for r in original]
    assert [e.record_position for e in evaluations] == list(range(8))
    schemas = count_schemas(provider)
    assert schemas.count('SemanticBatchResponse') == 2
    assert schemas.count('GroundedPermitResponse') == 0
    assert not any('Group' in name for name in schemas)
    stats = result.metadata['semantic_batch_diagnostics']
    assert stats['eligible_records'] == stats['batched_records'] == 8
    assert stats['initial_batch_requests'] == 2 and stats['singleton_records'] == 0
    assert stats['batch_prompt_chars'] < stats['equivalent_single_prompt_chars']
    assert stats['candidate_count'] == 8 and stats['shared_evidence_sets'] == 1
    assert 'LOT-' not in json.dumps(stats) and str(tmp_path) not in json.dumps(stats)


def test_explicit_stage_inventory_is_reused_without_mutating_source_records(monkeypatch, tmp_path):
    pipeline, pdf, provider = make_pipeline(monkeypatch, tmp_path)
    original = records(4, explicit=False)
    info = ExtractedRecord(record_type='info', section_title=STAGE, content='제조번호: STAGE-BATCH-1')
    pipeline.run(pdf, extracted_records=[info, *original])
    assert count_schemas(provider).count('SemanticBatchResponse') == 1
    assert all(r.batch_id is None and r.batch_evidence is None for r in original)


@pytest.mark.parametrize('mode,explicit', [('legacy', True), ('shared_evidence', False)])
def test_legacy_and_unknown_batch_keep_single_semantics(monkeypatch, tmp_path, mode, explicit):
    pipeline, pdf, provider = make_pipeline(monkeypatch, tmp_path, mode=mode)
    result = pipeline.run(pdf, extracted_records=records(2, explicit=explicit))
    assert count_schemas(provider).count('SemanticBatchResponse') == 0
    assert count_schemas(provider).count('GroundedPermitResponse') == 2
    assert [e.final_status for e in result.evaluations if e.record_type == 'test'] == [PASS_LABEL, FAIL_LABEL]


def test_preparation_excludes_requirement_text_and_unexpanded_tables(monkeypatch, tmp_path):
    pipeline, _, provider = make_pipeline(monkeypatch, tmp_path)
    baseline = records(1)[0]
    for changed in [replace(baseline, result='음성이어야 한다.', criteria='음성이어야 한다.'),
                    replace(baseline, result_table={'rows': [{'시험결과': '음성'}]}),
                    replace(baseline, tables=[{'rows': []}])]:
        assert pipeline.judge_engine.prepare_authoritative_request(changed, position=0) is None
    assert not provider.requests


def test_prepared_response_cannot_be_applied_to_changed_record(monkeypatch, tmp_path):
    from sp_pdf_judger.permit_semantic_batch import PreparedAuthoritativeResponse
    pipeline, _, _ = make_pipeline(monkeypatch, tmp_path)
    row = records(1)[0]
    request = pipeline.judge_engine.prepare_authoritative_request(row, position=0)
    response = pipeline.llm_client.explain_authoritative_semantic_batch(requests=[request])['r0']
    prepared = PreparedAuthoritativeResponse(request.request_fingerprint, 'r0', response, request)
    with pytest.raises(ValueError, match='fingerprint'):
        pipeline.judge_engine.judge_record(replace(row, result='양성'), prepared_authoritative=prepared)


def test_submitted_decimal_spelling_is_retained(monkeypatch, tmp_path):
    pipeline, pdf, _ = make_pipeline(monkeypatch, tmp_path, body='1.00 이상이어야 한다.')
    original = [replace(r, criteria='1.00 이상', result='2.0') for r in records(2)]
    result = pipeline.run(pdf, extracted_records=original)
    assert [(e.criteria, e.result) for e in result.evaluations if e.record_type == 'test'] == [('1.00 이상', '2.0')] * 2


def test_model_without_verified_budget_does_not_batch(monkeypatch, tmp_path):
    pipeline, pdf, provider = make_pipeline(monkeypatch, tmp_path)
    pipeline.llm_client.model = 'unknown-model'
    pipeline.run(pdf, extracted_records=records(2))
    assert count_schemas(provider).count('SemanticBatchResponse') == 0


def test_conflicting_stage_inventory_does_not_guess_nearest_batch(monkeypatch, tmp_path):
    pipeline, pdf, provider = make_pipeline(monkeypatch, tmp_path)
    info = [ExtractedRecord(record_type='info', section_title=STAGE, content=f'제조번호: LOT-{i}') for i in range(2)]
    pipeline.run(pdf, extracted_records=[*info, *records(2, explicit=False)])
    assert count_schemas(provider).count('SemanticBatchResponse') == 0


@pytest.mark.parametrize('title,body,result,period', [
    ('외래성인자부정시험(in vitro)', '14일 간 배양하고 추가로 14일간 배양하면서 세포병변을 관찰한다. 검사결과가 모두 음성이어야 한다.',
     '음성', '2024.10.29 ~ 2024.11.26'),
    ('불용성이물시험', '대한민국약전의 불용성이물시험법 제1법에 따르며 이에 적합하여야 한다.', '이물이 없음', '2025.07.18'),
])
def test_shared_path_preserves_composite_period_and_foreign_matter_semantics(monkeypatch, tmp_path, title, body, result, period):
    pipeline, pdf, provider = make_pipeline(monkeypatch, tmp_path, body=body, test_name=title)
    rows = [replace(r, test_name=title, criteria='허가서에 적합하여야 한다.', result=result, test_period=period) for r in records(2)]
    reviewed = pipeline.run(pdf, extracted_records=rows)
    assert [e.final_status for e in reviewed.evaluations if e.record_type == 'test'] == [PASS_LABEL, PASS_LABEL]
    sent = next(r for r in provider.requests if r['response_format']['json_schema']['schema']['title'] == 'SemanticBatchResponse')
    payload = json.loads(sent['messages'][0]['content'].split('\nPAYLOAD\n')[1])
    assert period in payload['records'][0]['record_context']
    clauses = payload['evidence_sets'][0]['candidates'][0]['clauses']
    assert all(sentence.strip() in '\n'.join(c['text'] for c in clauses) for sentence in body.split('다. ') if sentence.strip())
