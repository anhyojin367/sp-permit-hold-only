"""Incomplete attempts survive process-local state loss without implicit work."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import sp_judgement_bridge as bridge
from test_review_retry_recovery import setup, outcome


@pytest.mark.parametrize("phase", ["constructor", "run", "permit", "summary", "publication"])
def test_interruption_cannot_restart_on_fresh_request(setup, monkeypatch, phase):
    if phase == "permit":
        monkeypatch.setattr(bridge, "resolve_permits", lambda *a, **kw: SimpleNamespace(
            paths=[setup.pdf], fingerprint="permit", policy=None, errors=[]))
    class Pipeline:
        def __init__(self, **kw):
            setup.calls.append("pipeline")
            self.permit_stage = kw.get("permit_enabled") is not False
            if phase == "constructor":
                raise RuntimeError("synthetic interruption")
        def run(self, *a, **kw):
            if phase == "run" or (phase == "permit" and self.permit_stage):
                raise RuntimeError("synthetic interruption")
            return outcome()
    monkeypatch.setattr(bridge, "DocumentJudgePipeline", Pipeline)
    if phase == "publication":
        monkeypatch.setattr(bridge, "_write_judgement_cache",
            lambda *a: (_ for _ in ()).throw(RuntimeError("synthetic interruption")))
    if phase == "summary":
        monkeypatch.setattr(bridge, "_write_summary_csv_from_result",
            lambda *a: (_ for _ in ()).throw(RuntimeError("synthetic interruption")))
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        setup.ensure(_background=True)
    assert not setup.cache.exists()
    for _ in range(2):
        with pytest.raises(bridge.JudgementCacheError, match="검수 진행"):
            setup.ensure(_background=True)
    assert setup.calls == ["pipeline"] * (2 if phase == "permit" else 1)


@pytest.mark.parametrize("leftover", ["summary_after.csv", "after_stage_csv/stale.csv",
    "attempt_started.json", "before_stage_csv/"])
def test_legacy_or_corrupt_incomplete_workspace_is_not_silently_reused(setup, leftover):
    path = setup.root / leftover
    if leftover.endswith("/"):
        path.mkdir()
    else:
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"partial old evidence")
    with pytest.raises(bridge.JudgementCacheError, match="검수 진행"):
        setup.ensure(_background=True)
    assert setup.calls == [] and path.exists()
    if path.is_file():
        assert path.read_bytes() == b"partial old evidence"


def test_explicit_retry_preserves_incomplete_attempt_in_clean_workspace(setup):
    (setup.root / "attempt_started.json").write_bytes(b"original attempt")
    (setup.root / "stale.csv").write_bytes(b"must not leak into retry")
    bridge.forget_failed_judgement_attempt(setup.pdf)
    setup.ensure()
    setup.ensure()
    assert setup.calls == ["new pipeline"]
    archive, = setup.root.parent.glob("key.interrupted-*")
    assert (archive / "attempt_started.json").read_bytes() == b"original attempt"
    assert (archive / "stale.csv").read_bytes() == b"must not leak into retry"
    assert not (setup.root / "stale.csv").exists()
    assert setup.cache.is_file()


def test_incomplete_archive_failure_prevents_new_work(setup, monkeypatch):
    marker = setup.root / "attempt_started.json"
    marker.write_bytes(b"keep")
    monkeypatch.setattr(Path, "rename", lambda *a: (_ for _ in ()).throw(OSError("archive blocked")))
    with pytest.raises(OSError, match="archive blocked"):
        setup.ensure(_background=True, _retry_failed=True)
    assert setup.calls == [] and marker.read_bytes() == b"keep"


def test_attempt_marker_is_durable_before_constructing_pipeline(setup, monkeypatch):
    class Pipeline:
        def __init__(self, **kw):
            marker = json.loads((setup.root / "attempt_started.json").read_text(encoding="utf-8"))
            assert marker["artifact_key"] == "key"
            assert marker["cache_version"] == bridge.JUDGEMENT_CACHE_VERSION
            assert marker["pdf_path"] == str(setup.pdf.resolve())
            assert marker["pdf_signature"] == bridge._file_sig(setup.pdf)
            assert marker["started_at"]
            setup.calls.append("marked before pipeline")
        def run(self, *a, **kw):
            return outcome()
    monkeypatch.setattr(bridge, "DocumentJudgePipeline", Pipeline)
    setup.ensure(_background=True)
    assert setup.calls == ["marked before pipeline"]


def test_marker_sync_failure_stops_before_pipeline_and_blocks_next_visit(setup, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(bridge.os, "fsync", lambda *a: (_ for _ in ()).throw(OSError("marker sync failed")))
        with pytest.raises(OSError, match="marker sync failed"):
            setup.ensure(_background=True)
    assert setup.calls == []
    with pytest.raises(bridge.JudgementCacheError, match="검수 진행"):
        setup.ensure(_background=True)
    assert setup.calls == []


def test_completed_cache_takes_precedence_over_attempt_start_record(setup):
    setup.save(False)
    original = setup.cache.read_bytes()
    (setup.root / "attempt_started.json").write_bytes(b"start record, not proof of incompleteness")
    setup.ensure(_background=True)
    assert setup.calls == [] and setup.cache.read_bytes() == original
    assert not list(setup.root.parent.glob("key.interrupted-*"))


def test_explicit_retry_that_interrupts_again_does_not_authorize_later_visit(setup, monkeypatch):
    (setup.root / "attempt_started.json").write_bytes(b"first attempt")
    class Pipeline:
        def __init__(self, **kw):
            setup.calls.append("retry")
        def run(self, *a, **kw):
            raise RuntimeError("retry interrupted")
    monkeypatch.setattr(bridge, "DocumentJudgePipeline", Pipeline)
    bridge.forget_failed_judgement_attempt(setup.pdf)
    with pytest.raises(RuntimeError, match="retry interrupted"):
        setup.ensure()
    assert bridge._RETRY_REQUEST_SESSION_KEY not in setup.state
    with pytest.raises(bridge.JudgementCacheError, match="검수 진행"):
        setup.ensure(_background=True)
    archive, = setup.root.parent.glob("key.interrupted-*")
    assert (archive / "attempt_started.json").read_bytes() == b"first attempt"
    assert setup.calls == ["retry"] and not setup.cache.exists()
