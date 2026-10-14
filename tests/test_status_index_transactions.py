"""Status JSON publication must not lose another completed document."""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import sp_judgement_bridge as bridge
from test_review_retry_recovery import setup

REMEMBER = bridge._remember_judgement_artifacts
WORKER = Path(__file__).parent / "helpers/status_index_worker.py"


@pytest.fixture
def status(monkeypatch, tmp_path):
    folder = tmp_path / "status"
    folder.mkdir()
    index = folder / "status_index.json"
    monkeypatch.setattr(bridge, "JUDGEMENT_STATUS_DIR", folder)
    monkeypatch.setattr(bridge, "JUDGEMENT_STATUS_INDEX", index)
    return index


def artifact(folder, name="a"):
    pdf = folder / (name + ".pdf")
    pdf.write_bytes(b"synthetic")
    csv = folder / (name + ".csv")
    csv.write_text("제조명,검수합격,검수불합격,검수보류,총 계\nfixture,3,1,2,6\n", encoding="utf-8-sig")
    return {"pdf_path": pdf, "artifact_key": name, "summary_before_path": csv,
            "summary_after_path": csv, "after_result": SimpleNamespace(metadata={})}


@pytest.mark.parametrize("invalid", [b'{"documents":', b'[]', b'null', b'{}',
    b'{"documents":[]}', b'\xff', b'{"documents":{},"documents":{}}',
    b'{"documents":{"a":{},"a":{}}}'])
def test_corrupt_index_is_not_replaced_with_empty_history(status, tmp_path, invalid):
    status.write_bytes(invalid)
    with pytest.raises(RuntimeError):
        bridge._remember_judgement_artifacts(artifact(tmp_path))
    assert status.read_bytes() == invalid


def test_missing_index_initializes_and_preserves_counts(status, tmp_path):
    item = artifact(tmp_path)
    bridge._remember_judgement_artifacts(item)
    entry = bridge._read_status_index()["documents"][bridge._pdf_status_key(item["pdf_path"])]
    assert entry["summary_counts"] == {"pass": 3, "fail": 1, "hold": 2, "total": 6}
    assert entry["status"] == "completed"


def test_unknown_fields_and_other_documents_are_preserved(status, tmp_path):
    old = {"documents": {"other": {"custom": ["keep"]}}, "extension": {"version": "future"}}
    status.write_text(json.dumps(old), encoding="utf-8")
    bridge._remember_judgement_artifacts(artifact(tmp_path))
    after = bridge._read_status_index()
    assert after["extension"] == old["extension"] and after["documents"]["other"] == old["documents"]["other"]
    assert len(after["documents"]) == 2


def test_partial_serialization_failure_keeps_previous_bytes(status, monkeypatch):
    original = b'{"documents":{"old":{}}}'
    status.write_bytes(original)
    def partial(data, stream, **kwargs):
        stream.write('{"documents":')
        raise OSError("synthetic interrupted write")
    monkeypatch.setattr(bridge.json, "dump", partial)
    with pytest.raises(OSError):
        bridge._write_status_index({"documents": {"new": {}}})
    assert status.read_bytes() == original
    assert list(status.parent.iterdir()) == [status]


def test_failed_atomic_replace_keeps_previous_index(status, monkeypatch):
    original = b'{"documents":{"old":{}}}'
    status.write_bytes(original)
    monkeypatch.setattr(Path, "replace", lambda *a: (_ for _ in ()).throw(OSError("locked file")))
    with pytest.raises(OSError):
        bridge._write_status_index({"documents": {"new": {}}})
    assert status.read_bytes() == original and list(status.parent.iterdir()) == [status]


@pytest.mark.parametrize("has_previous", [False, True])
def test_fsync_failure_never_publishes_partial_index(status, monkeypatch, has_previous):
    original = b'{"documents":{"old":{}}}'
    if has_previous:
        status.write_bytes(original)
    monkeypatch.setattr(bridge.os, "fsync", lambda *a: (_ for _ in ()).throw(OSError("synthetic sync error")))
    with pytest.raises(OSError):
        bridge._write_status_index({"documents": {"new": {}}})
    assert status.exists() == has_previous
    if has_previous:
        assert status.read_bytes() == original
    assert list(status.parent.glob(".status-index-*.tmp")) == []


def test_status_lock_timeout_does_not_read_or_write_index(status, tmp_path, monkeypatch):
    original = b'{"documents":{"old":{}}}'
    status.write_bytes(original)
    @contextmanager
    def timeout(path, **kwargs):
        assert path == status.parent / ".status-index.lock" and kwargs["timeout"] == 30.0
        raise bridge.ReviewLockTimeout("synthetic contention")
        yield
    def forbidden(*a, **kw):
        pytest.fail("status index accessed without acquiring its lock")
    monkeypatch.setattr(bridge, "artifact_process_lock", timeout)
    monkeypatch.setattr(bridge, "_read_status_index", forbidden)
    monkeypatch.setattr(bridge, "_write_status_index", forbidden)
    with pytest.raises(bridge.JudgementStatusError, match="시간.*초과"):
        bridge._remember_judgement_artifacts(artifact(tmp_path))
    assert status.read_bytes() == original


def test_read_io_error_is_not_treated_as_missing_index(status, monkeypatch):
    status.write_text('{"documents":{}}')
    original = Path.open
    def unreadable(path, *a, **kw):
        if path == status:
            raise PermissionError("synthetic access failure")
        return original(path, *a, **kw)
    monkeypatch.setattr(Path, "open", unreadable)
    with pytest.raises(RuntimeError):
        bridge._read_status_index()


def test_reader_sees_old_complete_json_until_publication(status, monkeypatch):
    old, new = {"documents": {"old": {}}}, {"documents": {"new": {}}}
    status.write_text(json.dumps(old), encoding="utf-8")
    original, observed = Path.replace, []
    def inspect(path, target):
        observed.append(json.loads(status.read_text()))
        assert json.loads(path.read_text(encoding="utf-8")) == new
        return original(path, target)
    monkeypatch.setattr(Path, "replace", inspect)
    bridge._write_status_index(new)
    assert observed == [old] and json.loads(status.read_text()) == new


def test_status_failure_after_cache_publication_does_not_repeat_pipeline(setup, status, monkeypatch):
    status.write_bytes(b"corrupt status history")
    monkeypatch.setattr(bridge, "_remember_judgement_artifacts", REMEMBER)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            setup.ensure(_background=True)
    assert setup.cache.is_file() and setup.calls == ["new pipeline"]
    assert status.read_bytes() == b"corrupt status history"
    # Repair only this deliberately corrupt fixture, then reuse its completed cache.
    status.write_text('{"documents":{}}', encoding="utf-8")
    setup.ensure(_background=True)
    assert setup.calls == ["new pipeline"]


def wait_any(*paths):
    deadline = time.monotonic() + 30
    while not any(p.exists() for p in paths):
        if time.monotonic() >= deadline:
            raise AssertionError(f"fixture checkpoint missing: {paths}")
        time.sleep(.02)


@pytest.fixture
def workers(tmp_path, status):
    children = []
    def start(name, phase="read"):
        executable = sys._base_executable if os.name == "nt" else sys.executable
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
        child = subprocess.Popen([executable, str(WORKER), str(tmp_path), name, phase],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(child)
        return child
    yield start
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_two_processes_keep_both_documents_and_previous_history(workers, status, tmp_path):
    status.write_text('{"documents":{"existing":{"keep":true}},"extra":7}', encoding="utf-8")
    first = workers("a")
    wait_any(tmp_path / "read_a")
    second = workers("b")
    wait_any(tmp_path / "read_b", tmp_path / "waiting_b")
    (tmp_path / "release_b").touch()
    if (tmp_path / "read_b").exists():
        assert second.wait(timeout=20) == 0  # Old bug: B writes before A publishes its stale copy.
    (tmp_path / "release_a").touch()
    assert first.wait(timeout=20) == second.wait(timeout=20) == 0
    index = bridge._read_status_index()
    assert len(index["documents"]) == 3, "one completed document disappeared"
    assert index["extra"] == 7 and index["documents"]["existing"] == {"keep": True}
    for name in ("a", "b"):
        row = index["documents"][bridge._pdf_status_key(tmp_path / (name + ".pdf"))]
        assert row["summary_counts"] == {"pass": 3, "fail": 1, "hold": 2, "total": 6}


def test_terminated_publisher_preserves_old_index_and_releases_status_lock(workers, status, tmp_path):
    original = b'{"documents":{"existing":{}}}'
    status.write_bytes(original)
    first = workers("a", "publish")
    wait_any(tmp_path / "pending_a", tmp_path / "result_a.json")
    assert (tmp_path / "pending_a").exists(), "index was written in place instead of atomically"
    assert int((tmp_path / "pid_a").read_text()) == first.pid
    assert status.read_bytes() == original
    first.kill()
    first.wait(timeout=10)
    assert status.read_bytes() == original
    second = workers("b", "none")
    assert second.wait(timeout=20) == 0
    index = bridge._read_status_index()
    assert set(index["documents"]) == {"existing", bridge._pdf_status_key(tmp_path / "b.pdf")}
