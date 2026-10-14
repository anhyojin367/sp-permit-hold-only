import errno

import pytest

import sp_process_lock as locks
import sp_judgement_bridge as bridge
from test_review_retry_recovery import setup


def test_busy_lock_times_out_without_deleting_owner_file(tmp_path):
    path, notices = tmp_path / "a.lock", []
    with locks.artifact_process_lock(path) as waited:
        assert waited is False
        inode = path.stat().st_ino
        with pytest.raises(locks.ReviewLockTimeout, match="중복 실행하지 않았습니다"):
            with locks.artifact_process_lock(path, timeout=.02, poll_interval=.005,
                                             on_wait=lambda: notices.append("wait")):
                pytest.fail("busy lock entered")
        assert notices == ["wait"] and path.stat().st_ino == inode
    with locks.artifact_process_lock(path, timeout=0) as waited:
        assert waited is False
    assert path.is_file() and path.stat().st_ino == inode


def test_other_path_is_not_blocked(tmp_path):
    with locks.artifact_process_lock(tmp_path / "a.lock"):
        with locks.artifact_process_lock(tmp_path / "b.lock", timeout=0) as waited:
            assert waited is False


def test_worker_exception_releases_lock(tmp_path):
    path = tmp_path / "a.lock"
    with pytest.raises(RuntimeError, match="synthetic"):
        with locks.artifact_process_lock(path):
            raise RuntimeError("synthetic")
    with locks.artifact_process_lock(path, timeout=0):
        pass


def test_unexpected_io_error_is_not_a_busy_lock(tmp_path, monkeypatch):
    def broken(stream):
        raise OSError(errno.EIO, "synthetic disk error")
    monkeypatch.setattr(locks, "_try_lock", broken)
    notices = []
    with pytest.raises(OSError) as error:
        with locks.artifact_process_lock(tmp_path / "a.lock", on_wait=lambda: notices.append(1)):
            pytest.fail("I/O failure entered")
    assert error.value.errno == errno.EIO and notices == []


def test_wait_callback_exception_does_not_release_another_owner(tmp_path):
    path = tmp_path / "a.lock"
    def broken():
        raise RuntimeError("synthetic callback")
    with locks.artifact_process_lock(path):
        with pytest.raises(RuntimeError, match="callback"):
            with locks.artifact_process_lock(path, on_wait=broken):
                pytest.fail("entered")
        with pytest.raises(locks.ReviewLockTimeout):
            with locks.artifact_process_lock(path, timeout=0):
                pytest.fail("owner lock was lost")
    with locks.artifact_process_lock(path, timeout=0):
        pass


@pytest.mark.parametrize("options", [{"timeout": -1}, {"timeout": float("nan")},
    {"timeout": float("inf")}, {"poll_interval": 0}, {"poll_interval": float("inf")}])
def test_invalid_wait_parameters_do_not_create_files(tmp_path, options):
    path = tmp_path / "a.lock"
    with pytest.raises(ValueError):
        with locks.artifact_process_lock(path, **options):
            pytest.fail("invalid parameters entered")
    assert not path.exists()


def test_bridge_lock_failure_cannot_fall_through_to_pipeline(setup, monkeypatch):
    def timeout(*a, **k):
        raise locks.ReviewLockTimeout("synthetic timeout")
    monkeypatch.setattr(bridge, "artifact_process_lock", timeout)
    with pytest.raises(locks.ReviewLockTimeout):
        setup.ensure(_background=True)
    assert setup.calls == [] and not setup.cache.exists()


def test_changed_input_while_acquiring_lock_is_not_reviewed_under_old_key(setup, monkeypatch):
    keys = iter(["old-key", "new-key"])
    monkeypatch.setattr(bridge, "_artifact_key", lambda *a: next(keys))
    with pytest.raises(bridge.JudgementCacheError, match="대기 중 입력"):
        setup.ensure(_background=True)
    assert setup.calls == []
