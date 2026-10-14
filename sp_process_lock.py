"""Cooperative, per-artifact OS lock for local review workers.

Keep the lockfile: deleting/replacing it can let waiters lock different inodes.
Do not put it inside a cache folder that gets archived or replaced.
"""
from contextlib import contextmanager
import errno
import math
import os
from pathlib import Path
from time import monotonic, sleep

if os.name == "nt":
    import msvcrt

    def _try_lock(stream):
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(stream):
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(stream):
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(stream):
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class ReviewLockTimeout(TimeoutError):
    """Never start another review after a lock wait times out."""


@contextmanager
def artifact_process_lock(path: Path, *, on_wait=None, timeout=1800.0, poll_interval=.1):
    """Yield whether this worker joined an already-running owner.

    Runs only on a worker thread. No Streamlit/session/provider calls. An OS
    close/process exit releases the lock; a stale filename is not a stale lock.
    The bounded timeout fails closed and must not be used to bypass the lock.
    """
    if not math.isfinite(timeout) or timeout < 0 or not math.isfinite(poll_interval) or poll_interval <= 0:
        raise ValueError("Lock timeout/poll interval must be finite and non-negative/positive")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline, waited = monotonic() + timeout, False
    with path.open("a+b") as stream:
        while True:
            try:
                _try_lock(stream)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if not waited:
                    waited = True
                    if on_wait is not None:
                        on_wait()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise ReviewLockTimeout(
                        "다른 프로세스의 동일 문서 판정을 기다리는 시간이 초과되어 중복 실행하지 않았습니다. "
                        "기존 검수 상태를 확인한 뒤 다시 열어주세요."
                    ) from exc
                sleep(min(poll_interval, remaining))
        try:
            yield waited
        finally:
            _unlock(stream)
