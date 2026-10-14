"""Actual Streamlit button/rerun wiring, with isolated synthetic judgement I/O.

This is not a browser/pixel test or a real CLOVA success test. Reference lookup,
pipeline, and the simulation body are fixtures; the existing reference-panel
buttons, reruns, bridge cache reader, session handling, and job manager are real.
"""
import pickle
from types import SimpleNamespace

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import sp_app as app
import sp_judgement_bridge as bridge
from sp_review_jobs import ReviewJobs


def result(failed):
    return SimpleNamespace(metadata={"llm_last_error": "synthetic failure" if failed else "",
        "llm_call_count": 1, "llm_success_count": 0 if failed else 1}, extracted_records=[])


@pytest.mark.parametrize("kind", ["failed", "healthy", "corrupt", "failed_again", "interrupted"])
def test_existing_review_button_is_required_for_saved_failure_retry(monkeypatch, tmp_path, kind):
    pdf = tmp_path / "sp.pdf"
    pdf.write_bytes(b"synthetic input, not parsed")
    root = tmp_path / "sp_app_direct_judgement/key"
    root.mkdir(parents=True)
    cache = root / "judgement_artifacts.pkl"
    original = b"invalid pickle" if kind == "corrupt" else pickle.dumps({
        "cache_version": bridge.JUDGEMENT_CACHE_VERSION, "pdf_path": pdf,
        "before_result": result(False), "after_result": result(kind != "healthy")})
    if kind == "interrupted":
        original = b"interrupted before final cache publication"
        (root / "attempt_started.json").write_bytes(original)
    else:
        cache.write_bytes(original)
    jobs, calls = ReviewJobs(), []
    monkeypatch.setattr(bridge, "review_jobs", jobs)
    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(bridge, "_artifact_key", lambda *a: "key")
    monkeypatch.setattr(bridge, "resolve_domain_detail_profile", lambda *a: SimpleNamespace(fingerprint="d", sources=[]))
    monkeypatch.setattr(bridge, "resolve_permits", lambda *a, **kw: SimpleNamespace(paths=[], fingerprint="p", policy=None, errors=[]))
    monkeypatch.setattr(bridge, "_attach_rule_regression_log", lambda *a: None)
    monkeypatch.setattr(bridge, "_remember_judgement_artifacts", lambda *a: None)
    monkeypatch.setattr(bridge, "_write_summary_csv_from_result", lambda r, p: p.write_text("synthetic summary"))
    monkeypatch.setattr(bridge, "make_stage_csv_zip", lambda **k: None)
    class Pipeline:
        def __init__(self, **k):
            calls.append("synthetic")
        def run(self, *a, **k):
            return result(kind == "failed_again")
    monkeypatch.setattr(bridge, "DocumentJudgePipeline", Pipeline)

    monkeypatch.setattr(app, "_render_reference_filter_controls", lambda d: {})
    monkeypatch.setattr(app, "_filter_reference_documents", lambda f: [])
    monkeypatch.setattr(app, "_is_gate_filter_context", lambda d, f: True)
    monkeypatch.setattr(app, "_render_gate_message", lambda *a: None)
    def show_synthetic_result(product, pdf_path):
        try:
            saved = bridge.ensure_judgement_artifacts(pdf_path=pdf_path, permit_paths=[], original_csv_dir=None)
        except bridge.JudgementCacheError as exc:
            st.error(str(exc))
            return 0.0
        st.caption("saved-failed" if bridge._has_runtime_llm_failure(saved) else "saved-healthy")
        return 1.0
    monkeypatch.setattr(app, "_render_inline_simulation", show_synthetic_result)
    source = f'''
from pathlib import Path
from types import SimpleNamespace
import streamlit as st
import sp_app as app
pdf = Path({str(pdf)!r})
st.session_state.setdefault("run_sim", True)
st.session_state.setdefault("sim_pdf", str(pdf))
st.session_state.setdefault("sim_product", "fixture")
app._render_reference_panel(SimpleNamespace(path=pdf, company="fixture", product="fixture"), [], {{}})
'''
    try:
        page = AppTest.from_string(source, default_timeout=10).run()
        assert not page.exception
        assert calls == []
        evidence = root / "attempt_started.json" if kind == "interrupted" else cache
        assert evidence.read_bytes() == original
        if kind in {"corrupt", "interrupted"}:
            assert len(page.error) == 1 and "검수 진행" in page.error[0].value
        else:
            assert page.caption[0].value == ("saved-healthy" if kind == "healthy" else "saved-failed")
        page.run()  # Rerendering saved failure must not be treated as retry.
        assert not page.exception and calls == []
        next(b for b in page.button if b.label == "← 기존 문서 목록으로").click().run()
        assert not page.exception and calls == []
        next(b for b in page.button if b.label == "검수 진행").click().run()
        assert not page.exception
        assert page.session_state["run_sim"] is True
        assert len(calls) == (0 if kind == "healthy" else 1)
        assert page.caption[0].value == ("saved-failed" if kind == "failed_again" else "saved-healthy")
        page.run()
        assert not page.exception
        assert len(calls) == (0 if kind == "healthy" else 1)
        archives = list(root.parent.glob("key.interrupted-*" if kind == "interrupted" else "key.failed-*"))
        assert len(archives) == (0 if kind == "healthy" else 1)
        if archives:
            assert (archives[0] / evidence.name).read_bytes() == original
    finally:
        jobs.close()
