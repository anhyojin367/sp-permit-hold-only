"""Offline HOLD-only boundary contracts: real grounding, fake provider."""
from copy import deepcopy
import importlib
import json
from types import SimpleNamespace

import fitz
import pytest

from helpers.semantic_batch_fixtures import fake_judge
from sp_pdf_judger.config import FAIL_LABEL, HOLD_LABEL, PASS_LABEL
from sp_pdf_judger.judgement import JudgeEngine
from sp_pdf_judger.permit_catalog import PermitPolicy, ResolvedPermit
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.permit_semantic_settings import SemanticSettings
from sp_pdf_judger.policy_engine import RuleBook
from sp_pdf_judger.review_presentation import file_sha256, review_receipt
from sp_pdf_judger.schemas import Evaluation, ExtractedRecord, ProcessingResult, Summary


def review(*args, **kwargs):
    name = 'sp_pdf_judger.hold_only_review'
    assert importlib.util.find_spec(name) is not None, 'HOLD-only orchestration is not implemented'
    return importlib.import_module(name).apply_held_permit_review(*args, **kwargs)


@pytest.fixture
def setup(monkeypatch, tmp_path):
    pdf, permit = tmp_path / 'sp.pdf', tmp_path / 'permit.pdf'
    for path in (pdf, permit):
        with fitz.open() as doc:
            doc.new_page()
            doc.save(path)
    rules = tmp_path / 'rules'
    rules.mkdir()
    (rules / 'fixture.md').write_text('---\nrules:\n  - id: C.fixture\n    title: fixture\n    instruction: SP chronology\n    operation: page_continuity\n    params: {}\n---\n', encoding='utf-8')
    client, provider = fake_judge(monkeypatch, tmp_path, [])
    policy = PermitPolicy('hold-test', True, True, True, 'native', 'deterministic_first')
    store = PermitPdfStore.from_page_texts([(1, '1. CHO 마스터 세포주\n1.1. 확인시험\n음성이어야 한다.')],
                                          source_file=permit.name, policy=policy)
    store.permit_pdf_paths = [permit]
    store.pdf_sha256_by_path = {permit.resolve(): file_sha256(permit)}
    resolution = ResolvedPermit(policy, (permit,), 'fixture', (), True,
                                ((permit, file_sha256(permit)),))
    engine = JudgeEngine(None, client, store, policy, True)
    pipeline = SimpleNamespace(permit_store=store, judge_engine=engine, llm_client=client,
        permit_resolution=resolution, company='회사', product='제품', rule_dir=rules,
        semantic_settings=SemanticSettings('shared_evidence'),
        run=lambda *a, **kw: pytest.fail('Permit phase must not rerun Pipeline.run'))
    factory_calls, searched = [], []
    search = store.search_precise
    def search_spy(record):
        searched.append(deepcopy(record))
        return search(record)
    store.search_precise = search_spy
    def factory():
        factory_calls.append(True)
        return pipeline
    def respond(**kwargs):
        provider.requests.append(kwargs)
        prompt = kwargs['messages'][0]['content']
        assert kwargs['response_format']['json_schema']['schema']['title'] == 'GroundedPermitResponse'
        candidates = json.loads(prompt.split('허가서 후보:\n')[1])
        result_text = prompt.split('SP 결과: ')[1].splitlines()[0]
        data = {'permit_match_status': 'matched', 'candidate_id': 1, 'reason': '독립 관측 결과 비교',
            'clauses': [{'clause_id': c['clause_id'], 'kind': 'acceptance',
                         'status': 'FAIL' if result_text == '양성' else 'PASS', 'reason': '관측 근거 비교'}
                        for c in candidates[0]['clauses']]}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(data, ensure_ascii=False)))])
    provider.create = respond
    def baseline(statuses=(PASS_LABEL, FAIL_LABEL, HOLD_LABEL)):
        records = [ExtractedRecord(order_idx=7, section_number='2.1', section_title='CHO 마스터 세포주', test_name='확인시험',
            criteria='허가서 기준에 따름', result='양성' if i == 1 else '음성',
            page_start=1, page_end=1, raw_text=f'PRIVATE_SOURCE_{i}', batch_id=f'LOT-{i}')
                   for i in range(len(statuses))]
        evaluations = [Evaluation(order_idx=r.order_idx, section_number=r.section_number, section_title=r.section_title, test_name=r.test_name,
            criteria=r.criteria, result=r.result, raw_text=r.raw_text, page_start=1, page_end=1,
            final_status=status, source='gemini_second', reason=f'baseline-{i}', record_position=i,
            comparison_completed=status != HOLD_LABEL) for i, (r, status) in enumerate(zip(records, statuses))]
        evaluations.append(Evaluation(record_type='document', test_name='SP chronology',
            final_status=FAIL_LABEL, source='md_policy', reason='date ordering must remain unchanged'))
        return ProcessingResult(pdf, tmp_path / 'preview.png', records, evaluations, [], Summary(),
            {'permit_review_scope': 'hold_only', 'rule_fingerprint': RuleBook(rules).fingerprint,
             'review_receipt': review_receipt(pdf, [], RuleBook(rules).fingerprint, llm_enabled=True, model=client.model),
             'llm_call_count': 2, 'llm_success_count': 2, 'llm_last_error': '',
             'document_company': '회사', 'document_product': '제품',
             'policy_audit': [{'rule_id': 'C.date', 'status': 'FAIL'}]})
    return SimpleNamespace(pdf=pdf, permit=permit, rules=rules, client=client, provider=provider,
        store=store, engine=engine, pipeline=pipeline, factory=factory, factory_calls=factory_calls,
        searched=searched, baseline=baseline)


def test_closed_tests_do_not_load_permit_and_clone_baseline(setup):
    before = setup.baseline((PASS_LABEL, FAIL_LABEL))
    saved = deepcopy(before)
    after = review(before, pipeline_factory=setup.factory)
    assert not setup.factory_calls and not setup.provider.requests
    assert after is not before and before == saved
    assert after.evaluations == before.evaluations
    after.evaluations[0].reason = 'separate artifact'
    assert before.evaluations[0].reason == 'baseline-0'
    assert after.metadata['hold_only_review']['eligible_count'] == 0


def test_only_held_position_changes_and_closed_and_document_verdicts_remain(setup):
    before = setup.baseline()
    before.evaluations[2].reason += '\nMD 단위 표기 대응: 원문 보존'
    saved = deepcopy(before)
    after = review(before, pipeline_factory=setup.factory)
    assert before == saved
    assert len(setup.factory_calls) == len(setup.searched) == len(setup.provider.requests) == 1
    assert setup.searched[0].batch_id == 'LOT-2'
    assert after.evaluations[:2] == before.evaluations[:2]
    assert after.evaluations[-1] == before.evaluations[-1]
    assert after.evaluations[2].final_status == PASS_LABEL
    assert after.evaluations[2].criteria == '허가서 기준에 따름'
    assert 'MD 단위 표기 대응: 원문 보존' in after.evaluations[2].reason
    assert (after.summary.passed, after.summary.failed, after.summary.held) == (2, 2, 0)
    assert after.tree
    assert after.metadata['llm_call_count'] == after.metadata['llm_success_count'] == 3
    assert set(after.metadata['comparison_bases_by_position']) == {2}
    assert after.runtime_permit_paths == (setup.permit,)
    audit = after.metadata['hold_only_review']
    assert (audit['eligible_count'], audit['reviewed_count'], audit['skipped_count']) == (1, 1, 0)
    assert audit['records'][0]['baseline_status'] == HOLD_LABEL
    assert 'PRIVATE_SOURCE' not in json.dumps(after.metadata['permit_llm_audit'], ensure_ascii=False)


@pytest.mark.parametrize('kind', ['extraction', 'ocr', 'missing_result', 'md_guard', 'table', 'requirement'])
def test_unsafe_holds_never_load_permit(setup, kind):
    before = setup.baseline((HOLD_LABEL,))
    record, ev = before.extracted_records[0], before.evaluations[0]
    if kind == 'extraction':
        ev.source = 'extraction_quality'
    elif kind == 'ocr':
        record.ocr_suspect = True
    elif kind == 'missing_result':
        record.result = ev.result = None
    elif kind == 'md_guard':
        ev.source = 'md_policy_guard'
    elif kind == 'table':
        record.result_table = {'rows': [{'result': '음성'}, {'result': '미확인'}]}
        ev.lot_judgements = [{'status': PASS_LABEL, 'result': '음성'}, {'status': HOLD_LABEL, 'result': '미확인'}]
    else:
        record.criteria = ev.criteria = record.result = ev.result = '음성이어야 한다.'
    after = review(before, pipeline_factory=setup.factory)
    assert not setup.factory_calls and not setup.provider.requests
    assert after.evaluations[0].final_status == HOLD_LABEL
    assert after.evaluations[0].lot_judgements == before.evaluations[0].lot_judgements
    audit = after.metadata['hold_only_review']
    assert audit['skipped_count'] == 1 and audit['eligible_count'] == 0
    assert audit['records'][0]['reason_code']
    assert after.evaluations[0].reason != before.evaluations[0].reason


@pytest.mark.parametrize('damage', ['missing_position', 'duplicate_position', 'wrong_record'])
def test_bad_position_identity_aborts_without_permit_loading(setup, damage):
    before = setup.baseline()
    if damage == 'missing_position':
        before.evaluations[2].record_position = None
    elif damage == 'duplicate_position':
        before.evaluations[2].record_position = 0
    else:
        before.evaluations[2].result = 'different observation'
    with pytest.raises(ValueError, match='identity|position'):
        review(before, pipeline_factory=setup.factory)
    assert not setup.factory_calls


def test_all_thirteen_complete_candidates_reach_grounded_provider(setup):
    store = PermitPdfStore.from_page_texts([(1, '1. CHO 마스터 세포주\n' + '\n'.join(
        f'1.{i}. 확인시험\n음성이어야 한다. 고유 조건 {i}.' for i in range(1, 14)))],
        source_file=setup.permit.name, policy=setup.store.policy)
    store.permit_pdf_paths = [setup.permit]
    setup.pipeline.permit_store = setup.engine.permit_store = store
    after = review(setup.baseline((HOLD_LABEL,)), pipeline_factory=setup.factory)
    assert after.evaluations[0].final_status == PASS_LABEL
    prompt = setup.provider.requests[0]['messages'][0]['content']
    assert len(json.loads(prompt.split('허가서 후보:\n')[1])) == 13
    assert '고유 조건 13' in prompt


def test_incomplete_retrieval_keeps_hold_without_provider_call(setup):
    before = setup.baseline((HOLD_LABEL,))
    before.extracted_records[0].test_name = before.evaluations[0].test_name = 'unmapped test'
    after = review(before, pipeline_factory=setup.factory)
    assert not setup.provider.requests
    assert after.evaluations[0].final_status == HOLD_LABEL
    assert after.metadata['hold_only_review']['records'][0]['reason_code'] == 'retrieval_incomplete'


def test_oversized_evidence_is_not_truncated_or_sent(setup):
    setup.pipeline.semantic_settings = SemanticSettings('shared_evidence', max_prompt_chars=1)
    after = review(setup.baseline((HOLD_LABEL,)), pipeline_factory=setup.factory)
    assert not setup.provider.requests
    assert after.evaluations[0].final_status == HOLD_LABEL
    assert after.metadata['hold_only_review']['records'][0]['reason_code'] == 'evidence_oversized'


def test_independent_held_batches_keep_their_own_results(setup):
    after = review(setup.baseline((HOLD_LABEL, HOLD_LABEL)), pipeline_factory=setup.factory)
    assert [e.final_status for e in after.evaluations[:2]] == [PASS_LABEL, FAIL_LABEL]
    assert len(setup.provider.requests) == 2
    assert set(after.metadata['comparison_bases_by_position']) == {0, 1}


def test_provider_failure_aborts_without_mutating_baseline(setup):
    from sp_pdf_judger.clova_transport import ClovaServiceError
    before = setup.baseline((HOLD_LABEL,))
    saved = deepcopy(before)
    def fail(**kwargs):
        raise ClovaServiceError('service')
    setup.provider.create = fail
    with pytest.raises(ClovaServiceError):
        review(before, pipeline_factory=setup.factory)
    assert before == saved


@pytest.mark.parametrize('target', ['sp', 'permit', 'rules', 'engine'])
def test_changed_inputs_during_provider_call_abort_publication(setup, monkeypatch, target):
    before = setup.baseline((HOLD_LABEL,))
    respond = setup.provider.create
    def changed(**kwargs):
        response = respond(**kwargs)
        if target == 'sp':
            setup.pdf.write_bytes(b'changed SP')
        elif target == 'permit':
            setup.permit.write_bytes(b'changed permit')
        elif target == 'rules':
            rule_path = setup.rules / 'fixture.md'
            rule_path.write_text(rule_path.read_text(encoding='utf-8') + '\nchanged\n', encoding='utf-8')
        else:
            monkeypatch.setattr('sp_pdf_judger.review_presentation.source_fingerprint', lambda: 'changed-engine')
        return response
    setup.provider.create = changed
    with pytest.raises((RuntimeError, ValueError), match='changed|변경|fingerprint|규칙'):
        review(before, pipeline_factory=setup.factory)
    assert before.evaluations[0].final_status == HOLD_LABEL


def test_changed_sp_after_baseline_aborts_before_permit_loading(setup):
    before = setup.baseline((HOLD_LABEL,))
    setup.pdf.write_bytes(b'changed SP')
    with pytest.raises(RuntimeError, match='changed|변경'):
        review(before, pipeline_factory=setup.factory)
    assert not setup.factory_calls
