from types import SimpleNamespace

import pytest

import sp_app as app


@pytest.mark.parametrize('change', ['none', 'modified', 'deleted', 'missing_signature', 'missing_summary', 'summary_is_directory', 'llm_execution'])
def test_summary_shortcut_requires_the_same_pdf_and_real_summary_files(monkeypatch, tmp_path, change):
    pdf = tmp_path / 'sp.pdf'
    pdf.write_bytes(b'original')
    before, after = tmp_path / 'before.csv', tmp_path / 'after.csv'
    before.write_text('before', encoding='utf-8')
    after.write_text('after', encoding='utf-8')
    stat = pdf.stat()
    record = {'status':'completed', 'cache_version':app.JUDGEMENT_STATUS_CACHE_VERSION,
              'rule_fingerprint':'r', 'permit_fingerprint':'p',
              'detail_fingerprint':'d', 'extraction_fingerprint':'e',
              'llm_execution_fingerprint':'llm-offline', 'artifact_key':'test',
              'pdf_sig':{'size':stat.st_size, 'mtime_ns':stat.st_mtime_ns},
              'summary_before_path':str(before), 'summary_after_path':str(after)}
    if change == 'modified':
        pdf.write_bytes(b'changed after the last judgement')
    elif change == 'deleted':
        pdf.unlink()
    elif change == 'missing_signature':
        record.pop('pdf_sig')
    elif change == 'missing_summary':
        record['summary_after_path'] = ''
    elif change == 'summary_is_directory':
        record['summary_after_path'] = str(tmp_path)
    monkeypatch.setattr(app, '_load_judgement_status_index', lambda: {
        'documents': {app._pdf_status_key(pdf): record}})
    monkeypatch.setattr(app, 'policy_fingerprint', lambda: 'r')
    monkeypatch.setattr(app, 'extraction_fingerprint', lambda: 'e')
    monkeypatch.setattr(app, 'llm_execution_fingerprint',
                        lambda: 'llm-online' if change == 'llm_execution' else 'llm-offline')
    monkeypatch.setattr(app, '_related_permit_files', lambda _path: [])
    monkeypatch.setattr(app, 'resolve_submission_permits',
                        lambda *_args, **_kwargs: SimpleNamespace(fingerprint='p'))
    monkeypatch.setattr(app, 'resolve_domain_detail_profile', lambda *a: SimpleNamespace(fingerprint='d'))
    monkeypatch.setattr(app, 'JUDGEMENT_STATUS_DIR', tmp_path / 'missing-status-fallback')
    value = app._cached_simulation_artifacts_for_doc(SimpleNamespace(path=pdf,company='x',product='y'),
        pdf_path=pdf, permit_paths=[], csv_dir=tmp_path)
    assert (value is not None) == (change == 'none')
