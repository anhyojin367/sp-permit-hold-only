from threading import Event

import pytest

from sp_review_jobs import ReviewJob, ReviewJobs


def test_transport_updates_keep_stage_count_without_accumulating_old_waits():
    job = ReviewJob("uploaded real document")
    stage = "허가서 의미 판정 묶음 2/6"
    job.progress(stage)
    job.progress("CLOVA 응답 대기 · 시도 1/3")
    assert job.snapshot()[0] == stage + " · CLOVA 응답 대기 · 시도 1/3"
    job.progress("CLOVA 호출 간격 조절 · 10초")
    assert job.snapshot()[0] == stage + " · CLOVA 호출 간격 조절 · 10초"
    job.progress("문서 정책 검사")
    assert job.snapshot()[0] == "문서 정책 검사"


def test_reruns_share_one_job_and_progress_without_blocking():
    jobs = ReviewJobs()
    gate, started = Event(), Event()
    calls = []
    def worker(progress):
        calls.append(1)
        progress("허가서 대조 2/100")
        started.set()
        assert gate.wait(5)
        return {"complete": True}
    try:
        job = jobs.start("same", "doc", worker)
        assert started.wait(2)
        assert jobs.start("same", "doc", worker) is job
        assert job.message == "허가서 대조 2/100" and not job.future.done()
        gate.set()
        assert job.future.result(timeout=2) == {"complete": True}
        assert calls == [1]
    finally:
        gate.set()
        jobs.close()


def test_failed_job_is_not_automatically_retried():
    jobs = ReviewJobs()
    def worker(progress):
        raise ValueError("controlled failure")
    try:
        job = jobs.start("bad", "doc", worker)
        with pytest.raises(ValueError):
            job.future.result(timeout=2)
        assert jobs.start("bad", "doc", worker) is job
        jobs.forget_failed("other", lambda _: False)
        assert jobs.get("bad") is job
        jobs.forget_failed("doc", lambda _: False)
        assert jobs.get("bad") is None
    finally:
        jobs.close()


def test_changed_inputs_have_different_jobs_and_pending_is_not_cleared():
    jobs = ReviewJobs()
    gate = Event()
    try:
        first = jobs.start("v1", "doc", lambda p: gate.wait(5))
        second = jobs.start("v2", "doc", lambda p: "second")
        jobs.forget_failed("doc", lambda _: True)
        assert jobs.get("v1") is first and jobs.get("v2") is second
        gate.set()
        assert second.future.result(timeout=2) == "second"
    finally:
        gate.set()
        jobs.close()
