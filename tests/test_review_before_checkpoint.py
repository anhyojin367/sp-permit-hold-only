"""SP/permit boundary recovery; synthetic files and no provider requests."""
from pathlib import Path
from types import SimpleNamespace

import pytest

# These are design contracts for a future feature, not implemented regressions.
# Keep them visible as skips instead of excluding the file in local commands.
pytestmark = pytest.mark.skip(
    reason="Pending v102: SP/permit stage checkpoint resume is not implemented; see docs/RELEASE_20260929.md"
)

import sp_judgement_bridge as bridge
from sp_pdf_judger.config import PASS_LABEL
from sp_pdf_judger.schemas import ProcessingResult, ExtractedRecord, Evaluation, Summary
from test_review_retry_recovery import setup


@pytest.fixture
def stage(setup, monkeypatch):
    evidence = setup.pdf.parent / "evidence"
    evidence.mkdir()
    preview = evidence / "preview.png"
    preview.write_bytes(b"synthetic preview")
    extract = evidence / "extract"
    extract.mkdir()
    (extract / "records.json").write_text("synthetic extraction")
    permit = setup.pdf.parent / "permit.pdf"
    permit.write_bytes(b"synthetic permit")
    monkeypatch.setattr(bridge, "resolve_permits", lambda *a, **kw: SimpleNamespace(
        paths=[permit], fingerprint="permit", policy=None, errors=[]))
    result = ProcessingResult(setup.pdf, preview,
        [ExtractedRecord(order_idx=1, test_name="test")],
        [Evaluation(order_idx=1, test_name="test", final_status=PASS_LABEL)], [], Summary(1,0,0,1,1),
        {"record_count":1, "extract_dir":str(extract), "llm_call_count":0,
         "llm_success_count":0, "llm_last_error":"", "rule_fingerprint":bridge.policy_fingerprint()})
    calls, interruptions = [], [True]
    class Pipeline:
        def __init__(self, **kw):
            self.before = kw.get("permit_enabled") is False
        def run(self, *a, **kw):
            calls.append("SP" if self.before else "permit")
            if not self.before and interruptions.pop() if (not self.before and interruptions) else False:
                raise RuntimeError("synthetic permit interruption")
            if not self.before:
                assert kw["static_result"].summary.total == 1
            return result
    monkeypatch.setattr(bridge, "DocumentJudgePipeline", Pipeline)
    return SimpleNamespace(setup=setup, calls=calls, result=result, preview=preview, extract=extract,
        permit=permit, checkpoint=setup.root / "before_checkpoint.json")


def interrupt(stage):
    with pytest.raises(RuntimeError, match="synthetic permit interruption"):
        stage.setup.ensure(_background=True)
    assert not stage.setup.cache.exists()


def test_explicit_retry_reuses_completed_sp_stage_only(stage):
    interrupt(stage)
    assert stage.checkpoint.is_file()
    with pytest.raises(bridge.JudgementCacheError):
        stage.setup.ensure(_background=True)
    output = stage.setup.ensure(_background=True, _retry_failed=True)
    assert stage.calls == ["SP", "permit", "permit"]
    assert output["before_checkpoint_reused"] is True
    assert stage.checkpoint.is_file()
    archive, = stage.setup.root.parent.glob("key.interrupted-*")
    assert (archive / "before_checkpoint.json").is_file()


@pytest.mark.parametrize("mutation", ["preview", "extract", "checkpoint", "input", "permit"])
def test_changed_evidence_or_input_is_not_reused(stage, mutation):
    interrupt(stage)
    path = {"preview":stage.preview, "extract":stage.extract / "records.json",
        "checkpoint":stage.checkpoint, "input":stage.setup.pdf, "permit":stage.permit}[mutation]
    path.write_bytes(b"changed")
    output = stage.setup.ensure(_background=True, _retry_failed=True)
    assert stage.calls == ["SP", "permit", "SP", "permit"]
    assert output["before_checkpoint_reused"] is False


def test_failed_sp_provider_result_is_not_checkpointed(stage):
    stage.result.metadata.update(llm_call_count=1, llm_success_count=0, llm_last_error="synthetic failure")
    interrupt(stage)
    assert not stage.checkpoint.exists()
    stage.setup.ensure(_background=True, _retry_failed=True)
    assert stage.calls == ["SP", "permit", "SP", "permit"]
