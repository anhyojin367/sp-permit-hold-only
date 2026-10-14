import os

from sp_pdf_judger import llm


def test_resolving_provider_settings_does_not_overwrite_mode_or_cold_cache(monkeypatch, tmp_path):
    # Actual dotenv parsing, not an environment-mutating fake.
    (tmp_path / '.env').write_text('CLOVA_API_KEY=fake-key\nCLOVA_MODEL=HCX-007\n'
        'SP_PERMIT_SEMANTIC_MODE=legacy\nSP_PERMIT_INDEX_DIR=user-index\n'
        'CLOVA_RESPONSE_CACHE_DIR=user-responses\nUNRELATED_VALUE=private\n', encoding='utf-8')
    monkeypatch.setattr(llm, 'BASE_DIR', tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('SP_PERMIT_SEMANTIC_MODE', 'shared_evidence')
    monkeypatch.setenv('SP_PERMIT_INDEX_DIR', 'isolated-index')
    monkeypatch.setenv('CLOVA_RESPONSE_CACHE_DIR', 'isolated-responses')
    monkeypatch.delenv('UNRELATED_VALUE', raising=False)
    before = dict(os.environ)
    resolved = llm.resolve_clova_execution_settings()
    assert dict(os.environ) == before
    assert resolved[:2] == ('fake-key', 'HCX-007')
