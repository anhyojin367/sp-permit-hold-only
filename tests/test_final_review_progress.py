"""Final-page waits must not freeze navigation or duplicate paid reviews."""
from concurrent.futures import Future
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

import sp_judgement_bridge as bridge


def test_deferred_review_reuses_pending_future_then_existing_result(monkeypatch, tmp_path):
    future = Future()
    job = SimpleNamespace(future=future)
    calls = []
    state = {}
    monkeypatch.setattr(bridge, "st", SimpleNamespace(session_state=state, spinner=lambda *a: nullcontext()))
    monkeypatch.setattr(bridge, "_artifact_key", lambda *a: "same-input")
    monkeypatch.setattr(bridge, "get_judgement_job", lambda **kw: calls.append(kw) or job)
    prepared = (SimpleNamespace(fingerprint="details"), SimpleNamespace(paths=[], fingerprint="permit"), "same-input")
    args = dict(pdf_path=tmp_path / "doc.pdf", permit_paths=[], original_csv_dir=None,
                _prepared=prepared, _defer_pending=True)
    for _ in range(2):
        with pytest.raises(bridge.JudgementPending) as pending:
            bridge.ensure_judgement_artifacts(**args)
        assert pending.value.job is job and not future.done()
        assert not state  # A pending result must not be cached as complete.
    artifact = {"after_result": "unchanged stored result"}
    future.set_result(artifact)
    assert bridge.ensure_judgement_artifacts(**args) is artifact
    assert state["sp_direct_judgement_artifacts::same-input"] is artifact
    assert bridge.ensure_judgement_artifacts(**args) is artifact
    assert len(calls) == 3  # The last call is session-cached, no new job request.


def test_deferred_review_does_not_hide_a_completed_failure(monkeypatch, tmp_path):
    future = Future()
    future.set_exception(ValueError("controlled review failure"))
    monkeypatch.setattr(bridge, "st", SimpleNamespace(session_state={}, spinner=lambda *a: nullcontext()))
    monkeypatch.setattr(bridge, "_artifact_key", lambda *a: "same-input")
    monkeypatch.setattr(bridge, "get_judgement_job", lambda **kw: SimpleNamespace(future=future))
    prepared = (SimpleNamespace(fingerprint="details"), SimpleNamespace(paths=[], fingerprint="permit"), "same-input")
    with pytest.raises(ValueError, match="controlled review failure"):
        bridge.ensure_judgement_artifacts(pdf_path=tmp_path / "doc.pdf", permit_paths=[],
            original_csv_dir=None, _prepared=prepared, _defer_pending=True)


def test_pending_final_page_shows_progress_and_back_remains_responsive(monkeypatch):
    future = Future()
    job = SimpleNamespace(future=future, snapshot=lambda: ("허가서 기준 대조 12/92", 34.9))
    requests = []
    def defer(**kwargs):
        requests.append(kwargs)
        assert kwargs["_defer_pending"] is True
        raise bridge.JudgementPending(job)
    monkeypatch.setattr(bridge, "ensure_judgement_artifacts", defer)
    source = '''
from pathlib import Path
from types import SimpleNamespace
import streamlit as st
import sp_judgement_bridge as bridge
if "started" not in st.session_state:
    st.session_state["started"] = True
    st.query_params["view"] = "judge_final"
if st.query_params.get("view") == "judge_final":
    bridge.render_final_judgement_page(selected_doc=SimpleNamespace(path=Path("dummy.pdf")), original_csv_dir=None)
else:
    st.success("문서 목록")
'''
    page = AppTest.from_string(source, default_timeout=3).run()
    assert not page.exception and not page.error
    assert "허가서 기준 대조 12/92" in page.info[0].value
    assert "경과 34초" in page.info[0].value and "작업은 유지" in page.info[0].value
    assert not future.done()
    next(b for b in page.button if b.label == "← 첫 화면으로 돌아가기").click().run()
    assert not page.exception and page.success[0].value == "문서 목록"
    assert len(requests) == 1 and not future.cancelled()


def test_completed_progress_fragment_requests_full_app_rerender(monkeypatch):
    class RerunRequested(Exception):
        pass
    future = Future()
    future.set_result({"done": True})
    def rerun():
        raise RerunRequested()
    monkeypatch.setattr(bridge, "st", SimpleNamespace(rerun=rerun))
    with pytest.raises(RerunRequested):
        bridge._render_pending_final_review.__wrapped__(SimpleNamespace(future=future))
