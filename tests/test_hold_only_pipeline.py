"""The app's HOLD-only baseline must never read a permit or skip SP semantics."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import os

import fitz
import pytest

from helpers.semantic_batch_fixtures import fake_judge
from sp_pdf_judger.config import PASS_LABEL, FAIL_LABEL
from sp_pdf_judger.pipeline import DocumentJudgePipeline
from sp_pdf_judger.schemas import ExtractedRecord


def baseline(monkeypatch, tmp_path, responses=()):
    pdf = tmp_path / 'sp.pdf'
    with fitz.open() as doc:
        doc.new_page()
        doc.save(pdf)
    judge, provider = fake_judge(monkeypatch, tmp_path, responses)
    monkeypatch.setattr('sp_pdf_judger.pipeline.ClovaJudgeClient', lambda **kw: judge)
    pipe = DocumentJudgePipeline(company='시험회사', product='시험제품', permit_enabled=False,
                                 permit_review_scope='hold_only')
    return pipe, pdf, provider


def test_hold_baseline_runs_sp_semantic_llm_and_keeps_duplicate_positions(monkeypatch, tmp_path):
    pipe, pdf, provider = baseline(monkeypatch, tmp_path, [
        {'status': PASS_LABEL, 'reason': 'SP에 기재된 세포 경계와 배열 조건을 충족했습니다.'}])
    rows = [ExtractedRecord(order_idx=7, section_title='CHO 마스터 세포주', test_name='확인시험',
                            criteria='세포는 명확한 경계와 일정한 배열을 보여야 함',
                            result='세포 경계가 명확하고 배열이 일정함'),
            ExtractedRecord(order_idx=7, test_name='함량', criteria='10 이하', result='15'),
            ExtractedRecord(order_idx=7, test_name='발열성물질시험', criteria='일반시험법', result='0.7°C')]
    result = pipe.run(pdf, extracted_records=rows)
    tests = [e for e in result.evaluations if e.record_type == 'test']
    assert [e.final_status for e in tests] == [PASS_LABEL, FAIL_LABEL, PASS_LABEL]
    assert [e.record_position for e in tests] == [0, 1, 2]
    assert len(provider.requests) == 1
    prompt = provider.requests[0]['messages'][0]['content']
    assert '허가서 내용을 추측하거나 참고하지' in prompt
    assert result.metadata['permit_review_scope'] == 'hold_only'
    assert result.metadata['permit_pdf_count'] == 0


def test_hold_scope_cannot_run_the_unfiltered_authoritative_pipeline(monkeypatch, tmp_path):
    pipe, pdf, _ = baseline(monkeypatch, tmp_path)
    pipe.permit_enabled = True
    with pytest.raises(ValueError, match='보류'):
        pipe.run(pdf, extracted_records=[ExtractedRecord(criteria='10 이하', result='5')])


def test_hold_baseline_excludes_permit_document_rules_without_marking_pass(monkeypatch, tmp_path):
    from sp_pdf_judger.policy_engine import PolicyContext, Rule, evaluate_policies
    from sp_pdf_judger.permit_pdf_store import PermitPdfStore
    pdf = tmp_path / 'sp.pdf'
    with fitz.open() as doc:
        doc.new_page()
        doc.save(pdf)
    records = [ExtractedRecord(test_name='함량', criteria='10 이하', result='15')]
    ctx = PolicyContext(pdf, records, [], '시험제품', None, permit_store=PermitPdfStore([]))
    ctx.permit_review_scope = 'hold_only'
    rules = [Rule(id='PERMIT', title='허가서 문서 전체 대조', instruction='허가서 비교', operation='permit_test',
                  params={'permit_pattern': r'(\d+)', 'sp_pattern': r'(\d+)', 'sp_attribute': 'criteria'}),
             Rule(id='SP', title='SP 수치', instruction='SP 수치 비교', operation='numeric_compare', criterion_source='sp')]
    findings = evaluate_policies(ctx, SimpleNamespace(rules=rules))
    assert findings[0].status == 'N/A'
    assert findings[0].details['review_scope_excluded'] is True
    assert '미실시' in findings[0].reason
    assert findings[1].status == 'FAIL'


def test_hold_artifact_key_is_distinct_and_content_sensitive(monkeypatch, tmp_path):
    import sp_judgement_bridge as bridge
    monkeypatch.setattr(bridge, 'extraction_fingerprint', lambda: 'extract')
    monkeypatch.setattr(bridge, 'policy_fingerprint', lambda: 'rules')
    monkeypatch.setattr(bridge, 'permit_evaluation_fingerprint', lambda: 'permit')
    pdf = tmp_path / 'sp.pdf'
    pdf.write_bytes(b'AAAA')
    monkeypatch.setenv('SP_PERMIT_REVIEW_SCOPE', 'all')
    all_key = bridge._artifact_key(pdf, [], None, llm_fingerprint='fake')
    monkeypatch.setenv('SP_PERMIT_REVIEW_SCOPE', 'hold_only')
    hold_key = bridge._artifact_key(pdf, [], None, llm_fingerprint='fake')
    stamp = pdf.stat()
    pdf.write_bytes(b'BBBB')
    os.utime(pdf, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    changed_key = bridge._artifact_key(pdf, [], None, llm_fingerprint='fake')
    assert all_key != hold_key
    assert hold_key != changed_key


def test_unknown_review_scope_is_rejected(monkeypatch, tmp_path):
    import sp_judgement_bridge as bridge
    monkeypatch.setenv('SP_PERMIT_REVIEW_SCOPE', 'typo')
    with pytest.raises(ValueError, match='scope'):
        bridge._artifact_key(tmp_path / 'sp.pdf', [], None, llm_fingerprint='fake')
