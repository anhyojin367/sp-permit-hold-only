"""Exercise actual bridge routing/cache boundaries without network or extraction."""
import pickle
import sys
from types import SimpleNamespace

import pytest

import sp_judgement_bridge as bridge
from sp_pdf_judger.permit_catalog import PermitPolicy, ResolvedPermit


def test_bridge_routes_completed_sp_baseline_to_lazy_hold_review(monkeypatch, tmp_path):
    monkeypatch.setenv('SP_PERMIT_REVIEW_SCOPE', 'hold_only')
    pdf = tmp_path / 'sp.pdf'
    pdf.write_bytes(b'SP')
    permit = tmp_path / 'permit.pdf'
    permit.write_bytes(b'permit')
    policy = PermitPolicy('target', True, True, True, 'native', 'deterministic_first')
    resolution = ResolvedPermit(policy, (permit,), 'permit-fp', ())
    before = SimpleNamespace(extracted_records=[], evaluations=[], metadata={})
    after = SimpleNamespace(extracted_records=[], evaluations=[], metadata={'permit_review_scope': 'hold_only'})
    construction, runs, seen_baselines = [], [], []
    class Pipeline:
        def __init__(self, **kwargs):
            construction.append(kwargs)
        def run(self, pdf_path, **kwargs):
            runs.append(kwargs)
            return before
    def held_review(value, *, pipeline_factory, progress_callback=None):
        seen_baselines.append(value)
        pipeline_factory()  # Lazy boundary: no second run/extraction permitted.
        return after
    monkeypatch.setitem(sys.modules, 'sp_pdf_judger.hold_only_review',
                        SimpleNamespace(apply_held_permit_review=held_review))
    monkeypatch.setattr(bridge, 'DocumentJudgePipeline', Pipeline)
    monkeypatch.setattr(bridge, '_write_summary_csv_from_result', lambda *args: None)
    monkeypatch.setattr(bridge, 'make_stage_csv_zip', lambda **kwargs: None)
    monkeypatch.setattr(bridge, '_attach_rule_regression_log', lambda result: result)
    monkeypatch.setattr(bridge, '_remember_judgement_artifacts', lambda result: None)
    monkeypatch.setattr(bridge, 'llm_execution_fingerprint', lambda *args: 'settings')
    result = bridge._ensure_disk_artifacts(key='held-test', root_dir=tmp_path / 'run', pdf_path=pdf,
        resolved_permit_paths=[permit], original_csv_dir=None, company='회사', product='제품',
        product_mapping_id='', detail_profile=SimpleNamespace(fingerprint='details', sources=[]),
        permit_resolution=resolution, progress=lambda message: None, _retry_failed=False,
        llm_execution_settings=('', 'HCX-007', 'https://unused.invalid'), attempt_llm_fingerprint='settings')
    assert seen_baselines == [before]
    assert len(runs) == 1
    assert runs[0]['primary_tests_only'] is False
    assert construction[0]['permit_enabled'] is False
    assert construction[0]['permit_review_scope'] == 'hold_only'
    assert construction[1]['permit_review_scope'] == 'hold_only'
    assert result['before_result'] is before and result['after_result'] is after
    assert result['permit_review_scope'] == 'hold_only'


def test_hold_app_rejects_an_all_items_saved_result(monkeypatch, tmp_path):
    monkeypatch.setenv('SP_PERMIT_REVIEW_SCOPE', 'hold_only')
    path = tmp_path / 'artifact.pkl'
    path.write_bytes(pickle.dumps({'cache_version': bridge.JUDGEMENT_CACHE_VERSION,
        'permit_review_scope': 'all', 'after_result': SimpleNamespace(metadata={})}))
    with pytest.raises(bridge.JudgementCacheError):
        bridge._read_judgement_cache(path)


def test_queued_review_cannot_change_scope_before_worker_runs(monkeypatch, tmp_path):
    monkeypatch.setenv('SP_PERMIT_REVIEW_SCOPE', 'hold_only')
    pdf = tmp_path / 'sp.pdf'
    pdf.write_bytes(b'SP')
    monkeypatch.setattr(bridge, 'resolve_domain_detail_profile',
                        lambda *args: SimpleNamespace(fingerprint='details'))
    monkeypatch.setattr(bridge, 'resolve_permits',
                        lambda *args, **kwargs: ResolvedPermit(None, (), 'none', ()))
    monkeypatch.setattr(bridge, 'resolve_clova_execution_settings',
                        lambda: ('', 'HCX-007', 'https://unused.invalid'))
    monkeypatch.setattr(bridge, 'llm_execution_fingerprint', lambda *args: 'settings')
    monkeypatch.setattr(bridge, 'st', SimpleNamespace(session_state={}))
    monkeypatch.setattr(bridge, 'review_jobs', SimpleNamespace(start=lambda key, path, work: work))
    executed = []
    monkeypatch.setattr(bridge, 'ensure_judgement_artifacts', lambda **kw: executed.append(kw))
    work = bridge.get_judgement_job(pdf_path=pdf, permit_paths=[], original_csv_dir=None)
    monkeypatch.setenv('SP_PERMIT_REVIEW_SCOPE', 'all')
    with pytest.raises(bridge.JudgementCacheError, match='범위'):
        work(lambda message: None)
    assert executed == []


def test_failed_hold_review_retains_scope_and_content_identity(monkeypatch, tmp_path):
    from sp_pdf_judger.clova_transport import ClovaServiceError
    monkeypatch.setenv('SP_PERMIT_REVIEW_SCOPE', 'hold_only')
    pdf = tmp_path / 'sp.pdf'
    pdf.write_bytes(b'SP')
    permit = tmp_path / 'permit.pdf'
    permit.write_bytes(b'permit')
    run = tmp_path / 'run'
    run.mkdir()
    saved = []
    monkeypatch.setattr(bridge, '_store_status_entry', lambda path, entry: saved.append(entry))
    bridge._remember_service_failure(run, 'key', pdf, ClovaServiceError('timeout'),
        ResolvedPermit(None, (permit,), 'permit-fp', ()), SimpleNamespace(fingerprint='details'),
        execution_fingerprint='settings')
    assert saved[0]['status'] == 'failed'
    assert saved[0].get('permit_review_scope') == 'hold_only'
    assert saved[0]['sp_sha256'] == bridge.file_sha256(pdf)
    assert saved[0]['permit_file_hashes'] == {str(permit.resolve()): bridge.file_sha256(permit)}


def test_cached_hold_result_is_not_published_after_scope_changes(monkeypatch, tmp_path):
    monkeypatch.setenv('SP_PERMIT_REVIEW_SCOPE', 'hold_only')
    pdf = tmp_path / 'sp.pdf'
    pdf.write_bytes(b'SP')
    run = tmp_path / 'run'
    run.mkdir()
    (run / 'judgement_artifacts.pkl').write_bytes(pickle.dumps({
        'cache_version': bridge.JUDGEMENT_CACHE_VERSION, 'permit_review_scope': 'hold_only',
        'after_result': SimpleNamespace(metadata={}), 'rule_regression_run_id': 'existing'}))
    def changed_settings(*args):
        monkeypatch.setenv('SP_PERMIT_REVIEW_SCOPE', 'all')
        return 'settings'
    monkeypatch.setattr(bridge, 'llm_execution_fingerprint', changed_settings)
    published = []
    monkeypatch.setattr(bridge, '_remember_judgement_artifacts', published.append)
    with pytest.raises(bridge.JudgementCacheError, match='범위'):
        bridge._ensure_disk_artifacts(key='key', root_dir=run, pdf_path=pdf,
            resolved_permit_paths=[], original_csv_dir=None, company='', product='', product_mapping_id='',
            detail_profile=SimpleNamespace(fingerprint='details', sources=[]),
            permit_resolution=ResolvedPermit(None, (), 'none', ()), progress=lambda message: None,
            _retry_failed=False, llm_execution_settings=('', 'HCX-007', 'https://unused.invalid'),
            attempt_llm_fingerprint='settings')
    assert published == []
