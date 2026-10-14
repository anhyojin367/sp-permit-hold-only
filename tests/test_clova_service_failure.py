"""Provider outages abort the review, survive reruns, and never publish counts."""
import json
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

import sp_judgement_bridge as bridge
from sp_pdf_judger.clova_transport import ClovaServiceError
from sp_pdf_judger import policy_engine as policy
from sp_pdf_judger.schemas import ExtractedRecord
from test_review_retry_recovery import setup
from test_pipeline_workspace import isolated_pipeline
from test_sky_covione_authoritative_permit import _engine, _record, FakePermitLLM


def test_authoritative_judge_never_swallows_provider_failure():
    with pytest.raises(ClovaServiceError, match="CLOVA"):
        _engine(FakePermitLLM(error=ClovaServiceError("timeout"))).judge_record(_record())


def test_pipeline_stops_before_next_record(isolated_pipeline, tmp_path, monkeypatch):
    monkeypatch.setattr("sp_pdf_judger.pipeline.RuleBook", policy.RuleBook)
    obj = isolated_pipeline()
    calls = []
    def judge(record):
        calls.append(record.order_idx)
        raise ClovaServiceError("rate_limit", 429)
    obj.judge_engine.judge_record = judge
    records = [ExtractedRecord(order_idx=i, record_type="test", test_name="시험", criteria="1 이상", result="2") for i in (1,2)]
    with pytest.raises(ClovaServiceError):
        obj.run(tmp_path / "fake.pdf", extracted_records=records)
    assert calls == [1]


def test_semantic_rule_propagates_service_error_through_rule_dispatch(monkeypatch):
    ctx = SimpleNamespace(product="", llm=SimpleNamespace(enabled=True, client=object(),
        model="fixture", call_count=0, success_count=0), select=lambda _: [object()], evidence=lambda _: {"quote": "test"})
    rule = SimpleNamespace(products=[], operation="semantic_review", selector={}, instruction="rule")
    def fail(**kw):
        raise ClovaServiceError("authentication",401)
    monkeypatch.setattr("sp_pdf_judger.clova_client.request_structured_response", fail)
    with pytest.raises(ClovaServiceError):
        policy.evaluate_policies(ctx, SimpleNamespace(rules=[rule,rule]))
    assert ctx.llm.call_count == 1 and ctx.llm.success_count == 0


@pytest.fixture
def failed_attempt(setup, monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "JUDGEMENT_STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(bridge, "JUDGEMENT_STATUS_INDEX", tmp_path / "status/index.json")
    class Pipeline:
        def __init__(self, **kw):
            setup.calls.append("pipeline")
        def run(self, *a, **kw):
            raise ClovaServiceError("rate_limit", 429, "42901", 23)
    monkeypatch.setattr(bridge, "DocumentJudgePipeline", Pipeline)
    return setup


def test_failure_saved_without_completed_pickle_or_summary(failed_attempt):
    run = failed_attempt
    with pytest.raises(ClovaServiceError):
        run.ensure(_background=True)
    assert not run.cache.exists() and not (run.root / "summary_after.csv").exists()
    assert json.loads((run.root / "service_failure.json").read_text())["provider_code"] == "42901"
    entry, = bridge._read_status_index()["documents"].values()
    assert entry["status"] == "failed" and entry["summary_counts"] == {}
    assert "summary_after_path" not in entry
    with pytest.raises(ClovaServiceError):
        run.ensure(_background=True)
    assert run.calls == ["pipeline"]  # Even after lost process jobs, no auto retry.


def test_explicit_retry_archives_service_failure(failed_attempt):
    run = failed_attempt
    with pytest.raises(ClovaServiceError):
        run.ensure(_background=True)
    original = (run.root / "service_failure.json").read_bytes()
    with pytest.raises(ClovaServiceError):
        run.ensure(_background=True, _retry_failed=True)
    assert run.calls == ["pipeline", "pipeline"]
    archive, = run.root.parent.glob("key.service-failed-*")
    assert (archive / "service_failure.json").read_bytes() == original


def test_new_service_failure_overrides_stale_failed_pickle(failed_attempt):
    run = failed_attempt
    run.save()
    with pytest.raises(ClovaServiceError):
        run.ensure(_background=True, _retry_failed=True)
    assert run.cache.exists()  # Previous bytes kept; they are not returned as current.
    with pytest.raises(ClovaServiceError, match="42901"):
        run.ensure(_background=True)
    assert run.calls == ["pipeline"]


def test_corrupt_service_failure_never_starts_api(failed_attempt):
    run = failed_attempt
    (run.root / "service_failure.json").write_text("broken")
    with pytest.raises(bridge.JudgementCacheError):
        run.ensure(_background=True)
    assert not run.calls


def test_actual_final_page_shows_safe_cause_and_no_verdict_counts(monkeypatch):
    calls = []
    def fail(**kwargs):
        calls.append(kwargs)
        raise ClovaServiceError("rate_limit",429,"42901")
    monkeypatch.setattr(bridge, "ensure_judgement_artifacts", fail)
    page = AppTest.from_string('''
from pathlib import Path
from types import SimpleNamespace
import sp_judgement_bridge as bridge
bridge.render_final_judgement_page(selected_doc=SimpleNamespace(path=Path("fake.pdf")), original_csv_dir=None)
''').run()
    assert not page.exception and len(page.error) == 1
    assert "42901" in page.error[0].value and "남은 API 호출을 중단" in page.error[0].value
    assert not page.metric and not page.success


def test_batch_client_service_error_propagates(monkeypatch):
    from sp_pdf_judger.llm import ClovaJudgeClient
    from sp_pdf_judger.permit_group_batching import PermitGroupBatchRequest

    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        raise ClovaServiceError("rate_limit", 429, "42901")

    monkeypatch.setattr("sp_pdf_judger.llm.request_structured_response", fail)
    client = SimpleNamespace(
        enabled=True,
        client=object(),
        model="test",
        max_completion_tokens=1024,
        call_count=0,
        success_count=0,
        last_error="",
        permit_audit=[],
    )
    request = PermitGroupBatchRequest(
        group_id="group-1",
        payload={"group_id": "group-1"},
        serialized_payload='{"group_id":"group-1"}',
    )

    with pytest.raises(ClovaServiceError) as failed:
        ClovaJudgeClient.explain_permit_group_batch_once(
            client,
            requests=(request,),
        )

    assert failed.value.kind == "rate_limit"
    assert failed.value.status_code == 429
    assert failed.value.provider_code == "42901"
    assert len(calls) == client.call_count == 1
    assert client.success_count == 0
    assert "ClovaServiceError" in client.last_error
    assert "42901" not in client.last_error
    assert client.permit_audit == [
        {
            "group_id": "group-1",
            "event": "group_batch_request_failed",
            "error_type": "ClovaServiceError",
        }
    ]
