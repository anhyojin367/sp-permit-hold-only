"""No paid retry on a page visit, including after process-local jobs are lost."""
from contextlib import nullcontext
from pathlib import Path
import pickle
from types import SimpleNamespace
from threading import Event

import pytest

import sp_judgement_bridge as bridge
from sp_review_jobs import ReviewJobs


def outcome(failed=False):
    return SimpleNamespace(metadata={"llm_last_error": "Connection error" if failed else "",
        "llm_call_count": 1, "llm_success_count": 0 if failed else 1},
        extracted_records=[], summary=SimpleNamespace(held=2))


@pytest.fixture
def setup(monkeypatch, tmp_path):
    pdf = tmp_path / "sp.pdf"
    pdf.write_bytes(b"synthetic")
    root = tmp_path / "sp_app_direct_judgement" / "key"
    root.mkdir(parents=True)
    state, calls = {}, []
    jobs = ReviewJobs()
    monkeypatch.setattr(bridge, "review_jobs", jobs)
    monkeypatch.setattr(bridge, "st", SimpleNamespace(session_state=state, spinner=lambda *a: nullcontext()))
    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(bridge, "_artifact_key", lambda *a: "key")
    monkeypatch.setattr(bridge, "resolve_domain_detail_profile", lambda *a: SimpleNamespace(fingerprint="detail", sources=[]))
    monkeypatch.setattr(
        bridge,
        "resolve_permits",
        lambda *a, **kw: SimpleNamespace(
            paths=[], fingerprint="permit", policy=None, errors=[]
        ),
    )
    monkeypatch.setattr(bridge, "_attach_rule_regression_log", lambda *a: None)
    monkeypatch.setattr(bridge, "_remember_judgement_artifacts", lambda *a: None)
    monkeypatch.setattr(bridge, "_write_summary_csv_from_result", lambda result, path: path.write_text("new summary"))
    monkeypatch.setattr(bridge, "make_stage_csv_zip", lambda **k: None)
    class Pipeline:
        def __init__(self, **kwargs):
            calls.append("new pipeline")
        def run(self, *a, **k):
            return outcome()
    monkeypatch.setattr(bridge, "DocumentJudgePipeline", Pipeline)
    def save(failed=True):
        artifacts = {"cache_version": bridge.JUDGEMENT_CACHE_VERSION,
            "before_result": outcome(), "after_result": outcome(failed), "pdf_path": pdf}
        (root / "judgement_artifacts.pkl").write_bytes(pickle.dumps(artifacts))
        (root / "summary_after.csv").write_text("old failure summary")
        stage = root / "after_stage_csv"
        stage.mkdir(exist_ok=True)
        (stage / "stage.csv").write_text("old stage evidence")
        return artifacts
    def ensure(**kwargs):
        return bridge.ensure_judgement_artifacts(pdf_path=pdf, permit_paths=[], original_csv_dir=None, **kwargs)
    yield SimpleNamespace(pdf=pdf, root=root, cache=root / "judgement_artifacts.pkl", state=state,
                          calls=calls, jobs=jobs, save=save, ensure=ensure)
    jobs.close()


@pytest.mark.parametrize("failed_stage", ["before_result", "after_result"])
def test_new_session_and_job_manager_only_read_saved_failure(setup, monkeypatch, failed_stage):
    artifacts = setup.save(False)
    artifacts[failed_stage] = outcome(True)
    setup.cache.write_bytes(pickle.dumps(artifacts))
    original = setup.cache.read_bytes()
    for _ in range(2):
        fresh_jobs = ReviewJobs()
        monkeypatch.setattr(bridge, "review_jobs", fresh_jobs)
        setup.state.clear()
        try:
            assert bridge._has_runtime_llm_failure(setup.ensure())
        finally:
            fresh_jobs.close()
    assert setup.calls == []
    assert setup.cache.read_bytes() == original


@pytest.mark.parametrize("mode,expected", [("deterministic_first", True), ("legacy", False)])
def test_bridge_uses_local_sp_baseline_only_for_grouped_authority(setup, monkeypatch, mode, expected):
    from sp_pdf_judger.permit_catalog import PermitPolicy
    permit = setup.pdf.parent / "linked.pdf"
    permit.write_bytes(b"fixture")
    policy = PermitPolicy("submission-linked", True, True, True, "native", mode)
    monkeypatch.setattr(bridge, "resolve_permits", lambda *a, **kw: SimpleNamespace(
        paths=[permit], fingerprint="fixture", policy=policy, errors=[]))
    received = []
    class Pipeline:
        def __init__(self, **kwargs):
            self.before = kwargs.get("permit_enabled") is False
        def run(self, *args, **kwargs):
            received.append((self.before, kwargs.get("primary_tests_only", False)))
            return outcome()
    monkeypatch.setattr(bridge, "DocumentJudgePipeline", Pipeline)
    setup.ensure(_background=True)
    assert received == [(True, expected), (False, False)]


def test_explicit_retry_preserves_failed_files_and_runs_only_once(setup):
    setup.save()
    original = setup.cache.read_bytes()
    bridge.forget_failed_judgement_attempt(setup.pdf)
    assert not bridge._has_runtime_llm_failure(setup.ensure())
    assert not bridge._has_runtime_llm_failure(setup.ensure())
    assert setup.calls == ["new pipeline"]
    backups = list(setup.root.parent.glob("key.failed-*"))
    assert len(backups) == 1
    assert (backups[0] / "judgement_artifacts.pkl").read_bytes() == original
    assert (backups[0] / "summary_after.csv").read_text() == "old failure summary"
    assert (backups[0] / "after_stage_csv" / "stage.csv").read_text() == "old stage evidence"
    assert not bridge._has_runtime_llm_failure(pickle.loads(setup.cache.read_bytes()))


@pytest.mark.parametrize("click", [False, True])
def test_genuine_holds_are_reused_even_on_explicit_review(setup, click):
    setup.save(False)
    original = setup.cache.read_bytes()
    if click:
        bridge.forget_failed_judgement_attempt(setup.pdf)
    assert setup.ensure()["after_result"].summary.held == 2
    assert setup.calls == []
    assert setup.cache.read_bytes() == original
    assert not list(setup.root.parent.glob("key.failed-*"))


@pytest.mark.parametrize("invalid", [b"broken pickle", pickle.dumps([]), pickle.dumps({}),
    pickle.dumps({"cache_version": "old", "after_result": outcome()})])
def test_unreadable_or_incompatible_cache_stops_without_implicit_api(setup, invalid):
    setup.cache.write_bytes(invalid)
    with pytest.raises(bridge.JudgementCacheError, match="검수 진행"):
        setup.ensure()
    assert setup.calls == []
    assert setup.cache.read_bytes() == invalid


def test_explicit_retry_of_unreadable_cache_preserves_bytes(setup):
    setup.cache.write_bytes(b"broken pickle")
    bridge.forget_failed_judgement_attempt(setup.pdf)
    assert not bridge._has_runtime_llm_failure(setup.ensure())
    assert setup.calls == ["new pipeline"]
    backup, = setup.root.parent.glob("key.failed-*")
    assert (backup / "judgement_artifacts.pkl").read_bytes() == b"broken pickle"


def test_failure_to_archive_prevents_new_api_attempt(setup, monkeypatch):
    setup.save()
    original = setup.cache.read_bytes()
    monkeypatch.setattr(bridge.shutil, "copytree", lambda *a, **k: (_ for _ in ()).throw(OSError("no space")))
    bridge.forget_failed_judgement_attempt(setup.pdf)
    with pytest.raises(OSError, match="no space"):
        setup.ensure()
    assert setup.calls == []
    assert setup.cache.read_bytes() == original


def test_saved_result_postprocessing_error_does_not_fall_through_to_api(setup, monkeypatch):
    setup.save(False)
    monkeypatch.setattr(bridge, "_remember_judgement_artifacts",
        lambda *a: (_ for _ in ()).throw(OSError("status write failed")))
    with pytest.raises(OSError, match="status write failed"):
        setup.ensure()
    assert setup.calls == []


def test_retry_permission_is_document_specific_and_one_use(setup, monkeypatch):
    setup.save()
    other = setup.pdf.with_name("other.pdf")
    bridge.forget_failed_judgement_attempt(other)
    assert bridge._has_runtime_llm_failure(setup.ensure())
    assert setup.calls == []
    bridge.forget_failed_judgement_attempt(setup.pdf)
    assert not bridge._has_runtime_llm_failure(setup.ensure())
    assert setup.calls == ["new pipeline"]
    setup.save()  # A later, separate failure must not inherit the old click.
    setup.state.clear()
    new_jobs = ReviewJobs()
    monkeypatch.setattr(bridge, "review_jobs", new_jobs)
    try:
        assert bridge._has_runtime_llm_failure(setup.ensure())
        assert setup.calls == ["new pipeline"]
    finally:
        new_jobs.close()


def test_changed_pdf_does_not_inherit_retry_click(setup):
    setup.save()
    bridge.forget_failed_judgement_attempt(setup.pdf)
    setup.pdf.write_bytes(b"changed synthetic document")
    assert bridge._has_runtime_llm_failure(setup.ensure())
    assert setup.calls == []


def test_atomic_cache_write_failure_keeps_previous_bytes(tmp_path, monkeypatch):
    cache = tmp_path / "judgement_artifacts.pkl"
    cache.write_bytes(b"previous bytes")
    monkeypatch.setattr(Path, "replace", lambda *a: (_ for _ in ()).throw(OSError("locked")))
    with pytest.raises(OSError, match="locked"):
        bridge._write_judgement_cache(cache, {"new": True})
    assert cache.read_bytes() == b"previous bytes"
    assert list(tmp_path.iterdir()) == [cache]


def test_click_during_running_job_never_duplicates_or_carries_over(setup):
    started, release = Event(), Event()
    def worker(progress):
        started.set()
        assert release.wait(5)
        return {"after_result": outcome(True)}
    job = setup.jobs.start("key", str(setup.pdf.resolve()), worker)
    try:
        assert started.wait(2)
        bridge.forget_failed_judgement_attempt(setup.pdf)
        assert bridge.get_judgement_job(pdf_path=setup.pdf, permit_paths=[], original_csv_dir=None) is job
        release.set()
        assert bridge._has_runtime_llm_failure(job.future.result(timeout=2))
        assert bridge.get_judgement_job(pdf_path=setup.pdf, permit_paths=[], original_csv_dir=None) is job
        assert setup.calls == []
        assert bridge._RETRY_REQUEST_SESSION_KEY not in setup.state
    finally:
        release.set()


def test_retry_that_fails_again_stays_failed_without_another_click(setup, monkeypatch):
    setup.save()
    class FailingPipeline:
        def __init__(self, **k):
            setup.calls.append("failed pipeline")
        def run(self, *a, **k):
            return outcome(True)
    monkeypatch.setattr(bridge, "DocumentJudgePipeline", FailingPipeline)
    bridge.forget_failed_judgement_attempt(setup.pdf)
    for _ in range(2):
        assert bridge._has_runtime_llm_failure(setup.ensure())
    assert setup.calls == ["failed pipeline"]
    assert len(list(setup.root.parent.glob("key.failed-*"))) == 1


def test_cache_publication_failure_does_not_mark_review_completed(setup, monkeypatch):
    remembered = []
    monkeypatch.setattr(bridge, "_remember_judgement_artifacts", remembered.append)
    monkeypatch.setattr(bridge, "_write_judgement_cache",
        lambda *a: (_ for _ in ()).throw(OSError("cache is locked")))
    with pytest.raises(OSError, match="cache is locked"):
        setup.ensure()
    assert setup.calls == ["new pipeline"]
    assert remembered == []
