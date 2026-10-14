from types import SimpleNamespace
import os
from sp_pdf_judger.review_presentation import file_sha256

import pytest

import sp_judgement_bridge as bridge
from test_review_retry_recovery import setup, outcome
from sp_pdf_judger.permit_semantic_settings import SemanticSettings, semantic_fingerprint


def test_shared_mode_cache_is_separate_but_legacy_suffix_preserves_key(tmp_path):
    pdf = tmp_path / 'submission.pdf'
    pdf.write_bytes(b'sp-one')
    original = bridge._artifact_key(pdf, [], None, llm_fingerprint='test')
    assert bridge._artifact_key(pdf, [], None, llm_fingerprint='test', semantic_fingerprint='') == original
    shared = bridge._artifact_key(pdf, [], None, llm_fingerprint='test', semantic_fingerprint=semantic_fingerprint(SemanticSettings('shared_evidence')))
    assert shared != original
    pdf.write_bytes(b'sp-two-changed')
    assert bridge._artifact_key(pdf, [], None, llm_fingerprint='test', semantic_fingerprint=semantic_fingerprint(SemanticSettings('shared_evidence'))) != shared


@pytest.mark.parametrize('changed', ['sp', 'permit'])
def test_shared_cache_detects_changed_bytes_even_when_stat_is_unchanged(tmp_path, changed):
    pdf, permit = tmp_path / 'sp.pdf', tmp_path / 'permit.pdf'
    pdf.write_bytes(b'original'); permit.write_bytes(b'original')
    fingerprint = semantic_fingerprint(SemanticSettings('shared_evidence'))
    key = bridge._artifact_key(pdf, [permit], None, llm_fingerprint='fake', semantic_fingerprint=fingerprint)
    legacy = bridge._artifact_key(pdf, [permit], None, llm_fingerprint='fake')
    target = pdf if changed == 'sp' else permit
    stat = target.stat()
    target.write_bytes(b'modified')
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert bridge._artifact_key(pdf, [permit], None, llm_fingerprint='fake', semantic_fingerprint=fingerprint) != key
    assert bridge._artifact_key(pdf, [permit], None, llm_fingerprint='fake') == legacy


def test_output_budget_changes_shared_identity_and_blocks_old_publication(monkeypatch):
    from sp_pdf_judger.permit_semantic_settings import read_semantic_settings, semantic_cache_fingerprint
    monkeypatch.setenv('SP_PERMIT_SEMANTIC_MODE', 'shared_evidence')
    monkeypatch.setenv('CLOVA_MAX_COMPLETION_TOKENS', '4096')
    initial = read_semantic_settings()
    monkeypatch.setenv('CLOVA_MAX_COMPLETION_TOKENS', '8192')
    assert semantic_cache_fingerprint(read_semantic_settings()) != semantic_cache_fingerprint(initial)
    with pytest.raises(bridge.JudgementCacheError):
        bridge._assert_semantic_settings(initial)


def test_job_captures_mode_without_rereading_it_in_worker(monkeypatch, tmp_path):
    pdf = tmp_path / 'sp.pdf'
    pdf.write_bytes(b'sp')
    monkeypatch.setattr(bridge, 'st', SimpleNamespace(session_state={}))
    monkeypatch.setattr(bridge, 'resolve_domain_detail_profile', lambda *a: SimpleNamespace(fingerprint='details'))
    monkeypatch.setattr(bridge, 'resolve_permits', lambda *a, **kw: SimpleNamespace(paths=[], fingerprint='permit'))
    monkeypatch.setattr(bridge, 'resolve_clova_execution_settings', lambda: ('', 'HCX-007', 'https://provider.invalid'))
    pending = []
    monkeypatch.setattr(bridge.review_jobs, 'start', lambda key, path, worker: pending.append((key, worker)))
    captured = []
    monkeypatch.setattr(bridge, 'ensure_judgement_artifacts', lambda **kw: captured.append(kw['_prepared']))
    monkeypatch.setenv('SP_PERMIT_SEMANTIC_MODE', 'shared_evidence')
    bridge.get_judgement_job(pdf_path=pdf, permit_paths=[], original_csv_dir=None)
    monkeypatch.setenv('SP_PERMIT_SEMANTIC_MODE', 'legacy')
    bridge.get_judgement_job(pdf_path=pdf, permit_paths=[], original_csv_dir=None)
    assert pending[0][0] != pending[1][0]
    pending[0][1](lambda message: None)
    assert captured[0][-1] == SemanticSettings('shared_evidence')


def test_changed_mode_prevents_publication(monkeypatch):
    monkeypatch.setenv('SP_PERMIT_SEMANTIC_MODE', 'shared_evidence')
    bridge._assert_semantic_settings(SemanticSettings('shared_evidence'))
    monkeypatch.setenv('SP_PERMIT_SEMANTIC_MODE', 'legacy')
    with pytest.raises(bridge.JudgementCacheError):
        bridge._assert_semantic_settings(SemanticSettings('shared_evidence'))


def test_mode_change_during_pipeline_leaves_no_completed_artifact(setup, monkeypatch):
    monkeypatch.setenv('SP_PERMIT_SEMANTIC_MODE', 'shared_evidence')
    captured = []
    class Pipeline:
        def __init__(self, **kwargs):
            captured.append(kwargs['semantic_settings'])
        def run(self, *args, **kwargs):
            monkeypatch.setenv('SP_PERMIT_SEMANTIC_MODE', 'legacy')
            return outcome()
    monkeypatch.setattr(bridge, 'DocumentJudgePipeline', Pipeline)
    with pytest.raises(bridge.JudgementCacheError):
        setup.ensure(_background=True)
    assert captured == [SemanticSettings('shared_evidence')]
    assert not setup.cache.exists()
    assert not (setup.root / 'summary_after.csv').exists()


def test_mode_fingerprint_changes_with_protocol_versions(monkeypatch):
    import sp_pdf_judger.permit_semantic_settings as settings
    original = semantic_fingerprint(SemanticSettings('shared_evidence'))
    for name in ['RETRIEVAL_VERSION', 'BUNDLE_VERSION', 'SEMANTIC_BATCH_VERSION']:
        with monkeypatch.context() as context:
            context.setattr(settings, name, 'changed-version')
            assert semantic_fingerprint(SemanticSettings('shared_evidence')) != original


def test_two_permits_with_same_basename_do_not_reuse_aggregate(tmp_path):
    pdf = tmp_path / 'sp.pdf'
    pdf.write_bytes(b'sp')
    left, right = tmp_path / 'left', tmp_path / 'right'
    left.mkdir(); right.mkdir()
    a, b = left / 'permit.pdf', right / 'permit.pdf'
    a.write_bytes(b'permit-one'); b.write_bytes(b'permit-two')
    fingerprint = semantic_fingerprint(SemanticSettings('shared_evidence'))
    assert bridge._artifact_key(pdf, [a], None, llm_fingerprint='fake', semantic_fingerprint=fingerprint) != bridge._artifact_key(pdf, [b], None, llm_fingerprint='fake', semantic_fingerprint=fingerprint)


def test_document_list_does_not_show_legacy_completed_counts_in_shared_mode(monkeypatch, tmp_path):
    import sp_app as app
    pdf = tmp_path / 'sp.pdf'
    pdf.write_bytes(b'pdf')
    doc = SimpleNamespace(path=pdf, company='회사', product='제품')
    monkeypatch.setattr(app, 'policy_fingerprint', lambda: 'rules')
    monkeypatch.setattr(app, 'llm_execution_fingerprint', lambda: 'llm')
    monkeypatch.setattr(app, '_related_permit_files', lambda _: [])
    monkeypatch.setattr(app, 'resolve_submission_permits', lambda *a, **kw: SimpleNamespace(fingerprint='permit'))
    monkeypatch.setattr(app, 'resolve_domain_detail_profile', lambda *a: SimpleNamespace(fingerprint='detail'))
    entry = {'cache_version': app.JUDGEMENT_STATUS_CACHE_VERSION, 'rule_fingerprint': 'rules',
             'llm_execution_fingerprint': 'llm', 'permit_fingerprint': 'permit', 'detail_fingerprint': 'detail',
             'extraction_fingerprint': app.extraction_fingerprint()}
    index = {'documents': {app._pdf_status_key(pdf): entry}}
    monkeypatch.setenv('SP_PERMIT_SEMANTIC_MODE', 'legacy')
    assert app._status_record_for_doc(doc, index) is entry
    monkeypatch.setenv('SP_PERMIT_SEMANTIC_MODE', 'shared_evidence')
    assert app._status_record_for_doc(doc, index) is None
    entry['semantic_fingerprint'] = semantic_fingerprint(SemanticSettings('shared_evidence'))
    entry['sp_sha256'] = file_sha256(pdf)
    assert app._status_record_for_doc(doc, index) is entry
    stat = pdf.stat()
    pdf.write_bytes(b'new')
    os.utime(pdf, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert app._status_record_for_doc(doc, index) is None
