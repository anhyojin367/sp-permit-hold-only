"""Real OS processes, synthetic judgement only; no CLOVA requests."""
import json
import os
from pathlib import Path
import pickle
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import sp_judgement_bridge as bridge

WORKER = Path(__file__).parent / "helpers/review_lock_worker.py"


def wait_any(*paths):
    deadline = time.monotonic() + 30
    while not any(p.exists() for p in paths):
        if time.monotonic() >= deadline:
            raise AssertionError(f"worker did not reach fixture checkpoint: {paths}")
        time.sleep(.02)


@pytest.fixture
def workers(tmp_path):
    (tmp_path / "fixture.pdf").write_bytes(b"synthetic, never parsed")
    children = []

    def start(name, key="shared", mode="success"):
        # Windows venv python.exe is a launcher: killing it need not kill the
        # actual interpreter. Start that interpreter directly with our same
        # module search path so termination assertions target the lock owner.
        executable = sys._base_executable if os.name == "nt" else sys.executable
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
        child = subprocess.Popen([executable, str(WORKER), str(tmp_path), name, key, mode],
                                 env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(child)
        return child

    yield start
    for child in children:
        if child.poll() is None:
            child.kill()  # Only this fixture's own child, never an app/server.
        child.wait(timeout=10)


def finish(child, folder, name):
    (folder / ("release_" + name)).touch()
    assert child.wait(timeout=30) == 0
    return json.loads((folder / ("result_" + name + ".json")).read_text(encoding="utf-8"))


@pytest.mark.parametrize("mode", ["success", "fail", "raise"])
def test_same_artifact_waiter_does_not_duplicate_owner(workers, tmp_path, mode):
    owner = workers("owner", mode=mode)
    wait_any(tmp_path / "entered_owner")
    waiter = workers("waiter")
    wait_any(tmp_path / "waiting_waiter", tmp_path / "entered_waiter")
    assert not (tmp_path / "entered_waiter").exists(), "two processes entered the same pipeline"
    first = finish(owner, tmp_path, "owner")
    second = finish(waiter, tmp_path, "waiter")
    assert first["pid"] != second["pid"] and first["calls"] == 1 and second["calls"] == 0
    if mode == "raise":
        assert second["error"] == "JudgementCacheError"
    else:
        assert first["failed"] == second["failed"] == (mode == "fail")
        assert second["key"] == "shared"


@pytest.mark.parametrize("mode", ["success", "fail"])
def test_overlapping_explicit_retries_preserve_one_previous_attempt(workers, tmp_path, mode):
    root = tmp_path / "sp_app_direct_judgement/shared"
    root.mkdir(parents=True)
    failed = SimpleNamespace(metadata={"llm_last_error": "old failure"})
    cache = root / "judgement_artifacts.pkl"
    cache.write_bytes(pickle.dumps({"cache_version": bridge.JUDGEMENT_CACHE_VERSION,
                                   "before_result": failed, "after_result": failed}))
    original = cache.read_bytes()
    (root / "summary_after.csv").write_text("previous evidence", encoding="utf-8")
    owner = workers("owner", mode=mode)
    wait_any(tmp_path / "entered_owner")
    waiter = workers("waiter")
    wait_any(tmp_path / "waiting_waiter", tmp_path / "entered_waiter")
    assert not (tmp_path / "entered_waiter").exists()
    finish(owner, tmp_path, "owner")
    assert finish(waiter, tmp_path, "waiter")["calls"] == 0
    archive, = root.parent.glob("shared.failed-*")
    assert (archive / "judgement_artifacts.pkl").read_bytes() == original
    assert (archive / "summary_after.csv").read_text(encoding="utf-8") == "previous evidence"
    assert not list(archive.rglob("*.lock"))


def test_different_artifacts_can_enter_independently(workers, tmp_path):
    first = workers("first", "a")
    wait_any(tmp_path / "entered_first")
    second = workers("second", "b")
    wait_any(tmp_path / "entered_second")
    assert first.poll() is None  # The first pipeline is still waiting on its gate.
    assert finish(second, tmp_path, "second")["key"] == "b"
    assert finish(first, tmp_path, "first")["key"] == "a"


def test_terminated_owner_releases_os_lock_without_deleting_lockfile(workers, tmp_path):
    owner = workers("owner")
    wait_any(tmp_path / "entered_owner")
    lockfile = tmp_path / "sp_app_direct_judgement/.locks/shared.lock"
    assert lockfile.is_file()
    assert int((tmp_path / "pid_owner").read_text()) == owner.pid
    inode = lockfile.stat().st_ino
    owner.kill()
    owner.wait(timeout=10)
    fresh = workers("fresh")
    wait_any(tmp_path / "entered_fresh")
    assert finish(fresh, tmp_path, "fresh")["calls"] == 1
    assert lockfile.is_file() and lockfile.stat().st_ino == inode


def test_already_waiting_request_does_not_auto_restart_killed_owner(workers, tmp_path):
    owner = workers("owner")
    wait_any(tmp_path / "entered_owner")
    assert int((tmp_path / "pid_owner").read_text()) == owner.pid
    waiter = workers("waiter")
    wait_any(tmp_path / "waiting_waiter")
    owner.kill()
    owner.wait(timeout=10)
    second = finish(waiter, tmp_path, "waiter")
    assert second["calls"] == 0 and second["error"] == "JudgementCacheError"


def test_new_process_visit_after_owner_killed_needs_explicit_retry(workers, tmp_path):
    owner = workers("owner")
    wait_any(tmp_path / "entered_owner")
    assert int((tmp_path / "pid_owner").read_text()) == owner.pid
    owner.kill()
    owner.wait(timeout=10)
    viewer = workers("viewer", mode="view")
    viewed = finish(viewer, tmp_path, "viewer")
    assert viewed["calls"] == 0 and viewed["error"] == "JudgementCacheError"
    retry = workers("retry")
    assert finish(retry, tmp_path, "retry")["calls"] == 1
    root = tmp_path / "sp_app_direct_judgement"
    archive, = root.glob("shared.interrupted-*")
    assert (archive / "attempt_started.json").is_file()
    assert (root / "shared/judgement_artifacts.pkl").is_file()
