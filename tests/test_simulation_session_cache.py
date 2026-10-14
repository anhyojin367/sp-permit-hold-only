"""A new browser session must not evict other sessions' parsed diagrams."""
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest
import pypdf

import sp_app as app
import sp_simulation_jobs as simulation
from sp_review_jobs import ReviewJobs


def test_new_streamlit_session_reuses_pdf_metadata_cache(monkeypatch, tmp_path):
    calls = []
    pdf = tmp_path / 'fixture.pdf'
    pdf.write_bytes(b'fixture')
    monkeypatch.setattr(pypdf, 'PdfReader', lambda *a, **k:
        calls.append('extract') or SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: 'fixture')]))
    source = f'''
from pathlib import Path
import sp_app as app
app._ensure_current_cache_version()
app._extract_pdf_text(Path({str(pdf)!r}), (7, 1))
'''
    app._extract_pdf_text.clear()
    try:
        first = AppTest.from_string(source).run()
        second = AppTest.from_string(source).run()
        assert not first.exception and not second.exception
        assert calls == ['extract']
    finally:
        app._extract_pdf_text.clear()


@pytest.mark.parametrize('previous,expected_clears', [(None, 0), ('older', 1), (app.APP_CACHE_VERSION, 0)])
def test_cache_version_migration_only_clears_for_existing_old_session(monkeypatch, previous, expected_clears):
    state = {'sp_direct_judgement_artifacts::old': 'old', 'latest_judgement_artifact_key': 'old'}
    if previous is not None:
        state['_sp_app_cache_version'] = previous
    clears = []
    monkeypatch.setattr(app.st, 'session_state', state)
    monkeypatch.setattr(app.st.cache_data, 'clear', lambda: clears.append(True))
    app._ensure_current_cache_version()
    assert len(clears) == expected_clears
    assert state['_sp_app_cache_version'] == app.APP_CACHE_VERSION
    if previous != app.APP_CACHE_VERSION:
        assert 'sp_direct_judgement_artifacts::old' not in state
        assert 'latest_judgement_artifact_key' not in state
    app._ensure_current_cache_version()
    assert len(clears) == expected_clears


@pytest.mark.parametrize('changed', ['sp.pdf', 'summary_after.csv', 'stage.csv'])
def test_changed_simulation_input_is_still_reloaded(monkeypatch, tmp_path, changed):
    pdf, summary = tmp_path / 'sp.pdf', tmp_path / 'summary_after.csv'
    stage = tmp_path / 'stage.csv'
    for path in (pdf, summary, stage):
        path.write_text('original', encoding='utf-8')
    calls = []
    jobs = ReviewJobs()
    monkeypatch.setattr(simulation, 'simulation_jobs', jobs)
    monkeypatch.setattr(simulation, 'load_simulation_graph', lambda *a, **k:
        (calls.append('extract') or SimpleNamespace(product_name='fixture'), {}))
    monkeypatch.setattr(simulation, 'simulation_graph_to_json', lambda *a: '{"nodes":[{"id":"one"}]}')
    def read():
        return simulation.get_simulation_job(tmp_path, pdf, summary, version=app.APP_CACHE_VERSION).future.result(timeout=2)
    try:
        read()
        read()
        assert calls == ['extract']
        (tmp_path / changed).write_text('new larger contents', encoding='utf-8')
        read()
        assert calls == ['extract', 'extract']
    finally:
        jobs.close()
