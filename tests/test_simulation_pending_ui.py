"""Real Streamlit panel/reruns with a deliberately blocked, isolated graph loader."""
from threading import Event
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

import sp_app as app
import sp_simulation_jobs as simulation
from sp_review_jobs import ReviewJobs


@pytest.mark.parametrize('failure', [False, True])
def test_pending_graph_keeps_back_button_and_explicit_retry(monkeypatch, tmp_path, failure):
    pdf, summary = tmp_path / 'sp.pdf', tmp_path / 'summary_after.csv'
    pdf.write_bytes(b'synthetic PDF is never parsed')
    summary.write_text('synthetic summary', encoding='utf-8')
    doc = SimpleNamespace(path=pdf, company='fixture', product='fixture', permit_files=[])
    artifacts = {'runtime_csv_dir':tmp_path, 'summary_after_path':summary}
    jobs, release, started, calls = ReviewJobs(), Event(), Event(), []
    monkeypatch.setattr(simulation, 'simulation_jobs', jobs)
    def load(*a, **k):
        calls.append('graph')
        started.set()
        assert release.wait(5)
        if failure and len(calls) == 1:
            raise ValueError('synthetic graph failure')
        return SimpleNamespace(product_name='fixture'), {}
    monkeypatch.setattr(simulation, 'load_simulation_graph', load)
    monkeypatch.setattr(simulation, 'simulation_graph_to_json', lambda *a: '{"nodes":[{"id":"one"}],"edges":[]}')
    monkeypatch.setattr(app, '_find_simulation_csv_dir', lambda _: tmp_path)
    monkeypatch.setattr(app, '_document_from_pdf', lambda _: doc)
    monkeypatch.setattr(app, '_cached_simulation_artifacts_for_doc', lambda *a, **k: artifacts)
    monkeypatch.setattr(app, '_load_judgement_bridge', lambda: {
        'forget_failed_judgement_attempt':lambda _: None,
        'get_judgement_job':lambda **k: pytest.fail('This cached graph must not start judgement')})
    monkeypatch.setattr(app, '_render_reference_filter_controls', lambda d: {})
    monkeypatch.setattr(app, '_filter_reference_documents', lambda f: [])
    monkeypatch.setattr(app, '_is_gate_filter_context', lambda d, f: True)
    monkeypatch.setattr(app, '_render_gate_message', lambda *a: None)
    source = f'''
from pathlib import Path
from types import SimpleNamespace
import streamlit as st
import sp_app as app
pdf=Path({str(pdf)!r})
st.session_state.setdefault('run_sim', True)
st.session_state.setdefault('sim_pdf',str(pdf))
st.session_state.setdefault('sim_product','fixture')
app._render_reference_panel(SimpleNamespace(path=pdf,company='fixture',product='fixture'), [], {{}})
'''
    try:
        page = AppTest.from_string(source, default_timeout=2).run()
        assert not page.exception and started.wait(1)
        assert not release.is_set() and calls == ['graph']
        assert any('시뮬레이션 준비 중' in m.value for m in page.markdown)
        assert not any('class="final-judge-after-sim' in m.value for m in page.markdown)
        back = next(b for b in page.button if b.label == '← 기존 문서 목록으로')
        back.click().run()
        assert not page.exception and page.session_state['run_sim'] is False
        next(b for b in page.button if b.label == '검수 진행').click().run()
        assert not page.exception and calls == ['graph']
        job = simulation.get_simulation_job(tmp_path, pdf, summary, version=app.APP_CACHE_VERSION)
        release.set()
        if failure:
            with pytest.raises(ValueError):
                job.future.result(timeout=2)
            page.run()
            assert not page.exception and len(page.error) == 1
            assert 'synthetic graph failure' in page.error[0].value
            page.run()
            assert calls == ['graph']
            next(b for b in page.button if b.label == '← 기존 문서 목록으로').click().run()
            next(b for b in page.button if b.label == '검수 진행').click().run()
            simulation.get_simulation_job(tmp_path, pdf, summary, version=app.APP_CACHE_VERSION).future.result(timeout=2)
        else:
            job.future.result(timeout=2)
        page.run()
        assert not page.exception
        assert any('class="final-judge-after-sim is-visible"' in m.value for m in page.markdown)
        second_session = AppTest.from_string(source, default_timeout=2).run()
        assert not second_session.exception
        assert calls == ['graph'] * (2 if failure else 1)
    finally:
        release.set()
        jobs.close()
