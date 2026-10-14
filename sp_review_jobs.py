"""Process-local, single-flight reviews; no Streamlit calls in worker threads.

One worker also avoids parallel CLOVA requests across browser reruns/documents.
Completed/failed attempts stay inspectable until an explicit retry or restart.
Disk artifact identity/validation remains owned by sp_judgement_bridge.
"""
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from typing import Any, Callable


@dataclass
class ReviewJob:
    document: str
    future: Future | None = None
    started: float = field(default_factory=monotonic)
    message: str = "판정 대기 중"
    _stage_message: str = field(default="", repr=False)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def progress(self, message: str) -> None:
        with self._lock:
            message = str(message)
            if message.startswith("CLOVA "):
                self.message = " · ".join(filter(None, (self._stage_message, message)))
            else:
                self._stage_message = self.message = message

    def snapshot(self) -> tuple[str, float]:
        with self._lock:
            return self.message, monotonic() - self.started


class ReviewJobs:
    def __init__(self):
        self._lock = Lock()
        self._jobs: dict[str, ReviewJob] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sp-review")

    def start(self, key: str, document: str, worker: Callable) -> ReviewJob:
        with self._lock:
            if key not in self._jobs:
                job = ReviewJob(document)
                job.future = self._executor.submit(worker, job.progress)
                self._jobs[key] = job
            return self._jobs[key]

    def get(self, key: str) -> ReviewJob | None:
        with self._lock:
            return self._jobs.get(key)

    def forget_failed(self, document: str, is_failed: Callable[[Any], bool]) -> None:
        with self._lock:
            for key, job in list(self._jobs.items()):
                if job.document != document or not job.future.done():
                    continue
                if job.future.exception() is not None or is_failed(job.future.result()):
                    del self._jobs[key]

    def close(self) -> None:
        self._executor.shutdown(wait=True)


review_jobs = ReviewJobs()
