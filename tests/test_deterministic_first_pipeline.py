from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import fitz

import sp_pdf_judger.pipeline as pipeline_module
from sp_pdf_judger.clova_transport import ClovaServiceError
from sp_pdf_judger.config import FAIL_LABEL, HOLD_LABEL, PASS_LABEL
from sp_pdf_judger.hierarchy import build_document_tree
from sp_pdf_judger.permit_catalog import PermitPolicy, ResolvedPermit
from sp_pdf_judger.permit_llm_protocol import (
    GroundedConditionDecision,
    GroundedGroupBatchResponse,
    GroundedGroupBatchWireItem,
    GroundedGroupResponse,
)
from sp_pdf_judger.permit_grouping import BatchEvidence, PermitGroup, SPFact
from sp_pdf_judger.permit_models import PermitChunk, PermitCondition
from sp_pdf_judger.schemas import Evaluation, ExtractedRecord


TARGET_POLICY = PermitPolicy(
    "sky-grouped",
    True,
    True,
    True,
    "native",
    "deterministic_first",
)
LEGACY_POLICY = PermitPolicy("linked-legacy", True, True, True, "native")


def _chunk(evidence_id: str = "permit-evidence") -> PermitChunk:
    return PermitChunk(
        source_file="permit.pdf",
        page_number=4,
        section_number="2.1.1",
        title="생존율시험",
        text="생존율은 80% 이상이어야 한다.",
        page_start=4,
        page_end=4,
        section_path_titles=("Component A 중간체원액", "생존율시험"),
        normalized_test_name="생존율시험",
        normalized_stage_path="Component A 중간체원액",
        evidence_id=evidence_id,
    )


def _numeric_condition(evidence_id: str = "permit-evidence") -> PermitCondition:
    return PermitCondition(
        condition_id="survival-at-least-80",
        evidence_id=evidence_id,
        clause_index=0,
        kind="compare",
        operator=">=",
        value=Decimal("80"),
        unit="%",
        lower=None,
        upper=None,
        lower_inclusive=True,
        upper_inclusive=True,
        children=(),
        quote="생존율은 80% 이상이어야 한다.",
        stage_key="Component A 중간체원액",
        test_key="생존율시험",
        metric_key="survival_rate",
    )


def _semantic_condition(evidence_id: str = "permit-evidence") -> PermitCondition:
    return PermitCondition(
        condition_id="sequence-must-match",
        evidence_id=evidence_id,
        clause_index=0,
        kind="unparsed",
        operator=None,
        value=None,
        unit=None,
        lower=None,
        upper=None,
        lower_inclusive=True,
        upper_inclusive=True,
        children=(),
        quote="예상되는 염기서열과 일치하여야 한다.",
        stage_key="Component A 중간체원액",
        test_key="생존율시험",
        metric_key="unparsed",
    )


class _Store:
    def __init__(self, tmp_path: Path, policy: PermitPolicy, condition: PermitCondition):
        self.policy = policy
        self.permit_pdf_paths = [tmp_path / "private" / "permit.pdf"]
        self.chunks = [_chunk(condition.evidence_id)]
        self.conditions = [condition]
        self.source_documents = []
        self.extraction_errors = []
        self.index_diagnostics = []
        self.aliases = None

    @property
    def enabled(self):
        return bool(self.chunks)

    def configure_search_aliases(self, aliases):
        self.aliases = aliases

    def search(self, _record, top_k=12):
        return self.chunks[:top_k]

    def chunk_count_for_path(self, _path):
        return len(self.chunks)


class _Rag:
    docs = []
    loaded_sources = []

    def search(self, *_args, **_kwargs):
        return []


class _GroupClient:
    def __init__(self, *, semantic_status: str = PASS_LABEL, **_kwargs):
        self.enabled = True
        self.model = "offline-test"
        self.call_count = 0
        self.success_count = 0
        self.last_error = ""
        self.permit_audit = []
        self.semantic_status = semantic_status
        self.batch_sizes = []
        self.batch_calls = 0
        self.singleton_calls = 0

    def set_domain_detail_context(self, _context):
        return None

    def explain_permit_group_once(self, *, group, candidates, conditions, executions):
        self.singleton_calls += 1
        self.call_count += 1
        self.success_count += 1
        self.permit_audit.append({"group_id": group.group_id, "event": "group_response_parsed"})
        result_fact_ids = [fact.fact_id for fact in group.facts if fact.field == "result"]
        status = {
            PASS_LABEL: "pass",
            FAIL_LABEL: "fail",
            HOLD_LABEL: "hold",
        }[self.semantic_status]
        return GroundedGroupResponse(
            selected_evidence_id=candidates[0].evidence_id,
            decisions=[
                GroundedConditionDecision(
                    condition_id=condition.condition_id,
                    status=status,
                    fact_ids=result_fact_ids if status != "hold" else [],
                    quote=condition.quote,
                )
                for condition in conditions
            ],
            status=self.semantic_status,
            reason="provider text must not be persisted",
        )

    def explain_permit_group_batch_once(self, *, requests):
        requests = tuple(requests)
        self.batch_calls += 1
        self.batch_sizes.append(len(requests))
        self.call_count += 1
        self.success_count += 1
        status = {
            PASS_LABEL: "pass",
            FAIL_LABEL: "fail",
            HOLD_LABEL: "hold",
        }[self.semantic_status]
        items = []
        for request in requests:
            payload = request.payload
            candidates = payload["candidates"]
            conditions = payload["conditions"]
            result_fact_ids = [
                fact["fact_id"]
                for fact in payload["facts"]
                if fact["field"] == "result"
            ]
            items.append(
                GroundedGroupBatchWireItem(
                    group_id=request.group_id,
                    selected_evidence_id=candidates[0]["evidence_id"],
                    decisions=[
                        GroundedConditionDecision(
                            condition_id=condition["condition_id"],
                            status=status,
                            fact_ids=result_fact_ids if status != "hold" else [],
                            quote=condition["quote"],
                        ).model_dump()
                        for condition in conditions
                    ],
                    status=self.semantic_status,
                    reason="provider text must not be persisted",
                )
            )
            self.permit_audit.append(
                {
                    "group_id": request.group_id,
                    "event": "group_batch_response_parsed",
                }
            )
        return GroundedGroupBatchResponse(items=items)


def _record(order: int, result: str, *, batch: str | None = "LOT-A") -> ExtractedRecord:
    return ExtractedRecord(
        record_type="test",
        order_idx=order,
        section_number="5.2.1",
        section_title="Component A 중간체원액",
        test_name="생존율시험",
        criteria="제출 화면 원래 기준",
        result=result,
        method="명시 시험법",
        test_date="2026.08.01",
        test_period="2026.08.01 ~ 2026.08.04",
        remarks="원래 비고",
        page_start=order,
        page_end=order + 1,
        raw_text=f"원래 원문 {order}",
        batch_id=batch,
        batch_evidence=(
            {
                "source_kind": "explicit_table_column",
                "label": "제조번호",
                "quote": f"제조번호 {batch}",
            }
            if batch is not None
            else None
        ),
    )


def _stage_info(order: int, batch: str = "LOT-A") -> ExtractedRecord:
    text = f"제조번호 {batch}"
    return ExtractedRecord(
        record_type="content",
        order_idx=order,
        section_title="Component A 중간체원액 정보",
        section_path=[{"title": "Component A 중간체원액 정보"}],
        content=text,
        raw_text=text,
        normalized_text=text,
        page_start=order,
        page_end=order,
    )


def _static(pdf_path: Path):
    return SimpleNamespace(
        pdf_path=pdf_path,
        metadata={},
        preview_image_path=pdf_path.with_suffix(".png"),
        manufacturing_summary_page_numbers=[],
        manufacturing_summary_image_paths=[],
    )


def _write_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as document:
        document.new_page()
        document.save(path)


@pytest.fixture
def quiet_pipeline(monkeypatch):
    monkeypatch.setattr(pipeline_module, "UcumRagStore", _Rag)
    monkeypatch.setattr(pipeline_module, "evaluate_policies", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        pipeline_module,
        "summarize_manufacturing_policies",
        lambda *_args, **_kwargs: (None, None, {}),
    )
    monkeypatch.setattr(
        pipeline_module,
        "manufacturing_policy_cards",
        lambda *_args, **_kwargs: [],
    )

    def make(tmp_path, policy, condition, *, client_type=_GroupClient):
        store = _Store(tmp_path, policy, condition)
        client = client_type()
        monkeypatch.setattr(pipeline_module, "PermitPdfStore", lambda *_args, **_kwargs: store)
        monkeypatch.setattr(pipeline_module, "ClovaJudgeClient", lambda **_kwargs: client)
        resolution = ResolvedPermit(
            policy=policy,
            paths=(),
            fingerprint="permit-fingerprint",
            errors=(),
            catalog_valid=True,
            content_hashes=(),
        )
        pipeline = pipeline_module.DocumentJudgePipeline(
            company="SK bioscience",
            product="스카이코비원멀티주",
            permit_resolution=resolution,
        )
        return pipeline, store, client

    return make


def _test_evaluations(result):
    return [evaluation for evaluation in result.evaluations if evaluation.record_type == "test"]


def test_deterministic_group_fans_failure_to_every_member_without_llm(
    quiet_pipeline,
    tmp_path,
):
    pdf = tmp_path / "submission.pdf"
    _write_pdf(pdf)
    pipeline, _store, client = quiet_pipeline(
        tmp_path,
        TARGET_POLICY,
        _numeric_condition(),
    )
    records = [_record(10, "생존율 85%"), _record(20, "생존율 79%")]

    result = pipeline.run(
        pdf,
        extracted_records=records,
        static_result=_static(pdf),
    )

    evaluations = _test_evaluations(result)
    assert [evaluation.final_status for evaluation in evaluations] == [FAIL_LABEL, FAIL_LABEL]
    assert len(evaluations) == len(records)
    assert client.call_count == 0
    for record, evaluation in zip(records, evaluations):
        assert evaluation.criteria == record.criteria
        assert evaluation.result == record.result
        assert evaluation.method == record.method
        assert evaluation.test_date == record.test_date
        assert evaluation.test_period == record.test_period
        assert (evaluation.page_start, evaluation.page_end) == (record.page_start, record.page_end)
        assert evaluation.permit_group_id
        assert evaluation.permit_group_path == "deterministic"
        assert evaluation.permit_batch == "LOT-A"
        assert evaluation.permit_group_evidence["fact_ids"]

    assert result.metadata["permit_evaluation_mode"] == "deterministic_first"
    assert result.metadata["permit_group_raw_test_count"] == 2
    assert result.metadata["permit_group_count"] == 1
    assert result.metadata["permit_group_path_counts"] == {
        "deterministic": 1,
        "ambiguous_llm": 0,
        "hold_without_llm": 0,
        "primary_fallback": 0,
    }
    assert result.metadata["permit_group_provider_calls"] == 0
    assert result.metadata["permit_group_estimated_saved_calls"] == 2


def test_grouping_uses_non_test_stage_inventory_before_fan_out(
    quiet_pipeline,
    tmp_path,
):
    pdf = tmp_path / "stage-inventory.pdf"
    _write_pdf(pdf)
    pipeline, _store, client = quiet_pipeline(
        tmp_path,
        TARGET_POLICY,
        _numeric_condition(),
    )
    records = [
        _stage_info(1),
        _record(10, "생존율 85%", batch=None),
        _record(20, "생존율 79%", batch=None),
    ]

    result = pipeline.run(
        pdf,
        extracted_records=records,
        static_result=_static(pdf),
    )

    evaluations = _test_evaluations(result)
    assert len(evaluations) == 2
    assert {evaluation.permit_batch for evaluation in evaluations} == {"LOT-A"}
    assert {evaluation.permit_group_id for evaluation in evaluations} != {None}
    assert len({evaluation.permit_group_id for evaluation in evaluations}) == 1
    assert result.metadata["permit_group_count"] == 1
    assert result.metadata["permit_group_raw_test_count"] == 2
    assert client.call_count == 0


def test_one_semantic_multi_member_group_spends_exactly_one_call(
    quiet_pipeline,
    tmp_path,
):
    pdf = tmp_path / "semantic.pdf"
    _write_pdf(pdf)
    pipeline, _store, client = quiet_pipeline(
        tmp_path,
        TARGET_POLICY,
        _semantic_condition(),
    )
    records = [
        _record(1, "예상 염기서열과 일치함"),
        _record(2, "예상 염기서열과 일치함"),
    ]

    result = pipeline.run(pdf, extracted_records=records, static_result=_static(pdf))

    assert [item.final_status for item in _test_evaluations(result)] == [PASS_LABEL, PASS_LABEL]
    assert client.call_count == client.success_count == 1
    assert result.metadata["permit_group_provider_calls"] == 1
    assert result.metadata["permit_group_estimated_saved_calls"] == 1
    assert result.metadata["permit_group_path_counts"]["ambiguous_llm"] == 1


def test_missing_member_result_keeps_the_whole_group_on_hold(
    quiet_pipeline,
    tmp_path,
):
    pdf = tmp_path / "missing.pdf"
    _write_pdf(pdf)
    pipeline, _store, client = quiet_pipeline(tmp_path, TARGET_POLICY, _numeric_condition())
    records = [_record(1, "생존율 85%"), _record(2, "")]

    result = pipeline.run(pdf, extracted_records=records, static_result=_static(pdf))

    assert [item.final_status for item in _test_evaluations(result)] == [HOLD_LABEL, HOLD_LABEL]
    assert client.call_count == 0
    assert result.metadata["permit_group_path_counts"]["hold_without_llm"] == 1


@pytest.mark.parametrize("authoritative", [True, False])
def test_missing_permit_match_cannot_pass_an_authoritative_review(
    quiet_pipeline,
    tmp_path,
    authoritative,
):
    pdf = tmp_path / "not-found.pdf"
    _write_pdf(pdf)
    pipeline, store, client = quiet_pipeline(
        tmp_path,
        replace(TARGET_POLICY, authoritative=authoritative),
        _numeric_condition(),
    )
    store.search = lambda *_args, **_kwargs: []
    record = _record(1, "적합")

    result = pipeline.run(
        pdf,
        extracted_records=[record],
        static_result=_static(pdf),
    )

    evaluation = _test_evaluations(result)[0]
    assert evaluation.final_status == (HOLD_LABEL if authoritative else PASS_LABEL)
    assert evaluation.source == ("permit_group_hold" if authoritative else "rule_before")
    assert evaluation.result == "적합"
    assert evaluation.permit_group_id
    assert evaluation.permit_group_path == ("hold_without_llm" if authoritative else "primary_fallback")
    assert result.metadata["permit_group_path_counts"]["primary_fallback"] == int(not authoritative)
    if authoritative:
        assert not evaluation.comparison_completed
        assert "허가" in evaluation.reason
    assert client.call_count == 0


@pytest.mark.parametrize(
    "failure_mode",
    ("store_unavailable", "catalog_invalid", "index_unusable"),
)
def test_permit_infrastructure_failure_does_not_use_primary_pass_fallback(
    quiet_pipeline,
    tmp_path,
    failure_mode,
):
    pdf = tmp_path / f"{failure_mode}.pdf"
    _write_pdf(pdf)
    pipeline, store, client = quiet_pipeline(
        tmp_path,
        TARGET_POLICY,
        _numeric_condition(),
    )
    store.search = lambda *_args, **_kwargs: []
    if failure_mode == "store_unavailable":
        store.chunks = []
    elif failure_mode == "catalog_invalid":
        pipeline.judge_engine.permit_catalog_valid = False
    else:
        store.extraction_errors = ["permit index unusable"]

    result = pipeline.run(
        pdf,
        extracted_records=[_record(1, "적합")],
        static_result=_static(pdf),
    )

    evaluation = _test_evaluations(result)[0]
    assert evaluation.final_status == HOLD_LABEL
    assert evaluation.source == "permit_group_hold"
    assert evaluation.permit_group_path == "hold_without_llm"
    assert result.metadata["permit_group_path_counts"]["primary_fallback"] == 0
    assert client.call_count == 0


def test_group_fan_out_preserves_primary_table_lot_rows(
    quiet_pipeline,
    tmp_path,
):
    pdf = tmp_path / "table-rows.pdf"
    _write_pdf(pdf)
    pipeline, _store, _client = quiet_pipeline(
        tmp_path,
        TARGET_POLICY,
        _numeric_condition(),
    )
    lot_rows = [
        {
            "item_label": "바이러스",
            "item_value": "BpyV",
            "result": "적합",
            "status": PASS_LABEL,
            "display_columns": ["바이러스", "시험결과"],
            "display_values": {"바이러스": "BpyV", "시험결과": "적합"},
        }
    ]

    def primary(record):
        return Evaluation(
            order_idx=record.order_idx,
            record_type="test",
            test_name=record.test_name or "",
            criteria=record.criteria,
            result=record.result,
            final_status=PASS_LABEL,
            reason="기존 표 행 판정",
            source="rule_before",
            comparison_completed=True,
            lot_judgements=lot_rows,
        )

    pipeline.judge_engine.judge_record_primary = primary

    result = pipeline.run(
        pdf,
        extracted_records=[_record(1, "생존율 79%")],
        static_result=_static(pdf),
    )

    evaluation = _test_evaluations(result)[0]
    assert evaluation.final_status == FAIL_LABEL
    assert len(evaluation.lot_judgements) == 1
    judged_row = evaluation.lot_judgements[0]
    for key in (
        "item_label",
        "item_value",
        "result",
        "display_columns",
        "display_values",
    ):
        assert judged_row[key] == lot_rows[0][key]
    assert judged_row["status"] == FAIL_LABEL
    assert judged_row["source"] == evaluation.source
    assert judged_row["reason"] == evaluation.reason
    assert result.summary.passed == 0
    assert result.summary.failed == 1
    assert result.summary.held == 0


def test_legacy_policy_never_enters_group_branch(
    quiet_pipeline,
    monkeypatch,
    tmp_path,
):
    pdf = tmp_path / "legacy.pdf"
    _write_pdf(pdf)
    pipeline, _store, client = quiet_pipeline(tmp_path, LEGACY_POLICY, _numeric_condition())
    calls = []

    def legacy(record):
        calls.append(record.order_idx)
        return Evaluation(
            order_idx=record.order_idx,
            record_type="test",
            test_name=record.test_name or "",
            criteria=record.criteria,
            result=record.result,
            final_status=PASS_LABEL,
            reason=f"legacy-{record.order_idx}",
            source="legacy-source",
        )

    pipeline.judge_engine.judge_record = legacy
    monkeypatch.setattr(
        pipeline_module,
        "build_permit_groups",
        lambda *_args, **_kwargs: pytest.fail("legacy policy entered grouped branch"),
        raising=False,
    )
    records = [_record(3, "생존율 85%"), _record(7, "생존율 90%")]

    result = pipeline.run(pdf, extracted_records=records, static_result=_static(pdf))

    evaluations = _test_evaluations(result)
    assert calls == [3, 7]
    assert [(item.final_status, item.reason, item.source) for item in evaluations] == [
        (PASS_LABEL, "legacy-3", "legacy-source"),
        (PASS_LABEL, "legacy-7", "legacy-source"),
    ]
    assert "permit_group_count" not in result.metadata
    assert client.call_count == 0


def test_target_group_metadata_contains_no_absolute_permit_path_or_provider_reason(
    quiet_pipeline,
    tmp_path,
):
    pdf = tmp_path / "private" / "submission.pdf"
    _write_pdf(pdf)
    pipeline, store, _client = quiet_pipeline(tmp_path, TARGET_POLICY, _semantic_condition())

    result = pipeline.run(
        pdf,
        extracted_records=[_record(1, "예상 염기서열과 일치함")],
        static_result=_static(pdf),
    )

    serialized = repr(result.metadata["permit_group_audit"])
    assert str(tmp_path) not in serialized
    assert "LOT-A" not in serialized
    assert all(
        "batch" not in row
        for row in result.metadata["permit_group_audit"]
    )
    assert "provider text must not be persisted" not in serialized
    assert result.metadata["permit_paths"] == [Path(store.permit_pdf_paths[0]).name]
    assert result.metadata["resolved_permit_paths"] == []
    assert result.metadata["permit_llm_audit"] == [
        {
            "group_id": result.metadata["permit_group_audit"][0]["group_id"],
            "event": "group_batch_response_parsed",
        }
    ]
    assert result.metadata["extract_dir"] is None
    assert result.metadata["run_workspace"] is None
    assert str(tmp_path) not in repr(result.metadata)
    assert Path(result.runtime_extract_dir).is_absolute()
    assert Path(result.runtime_workspace).is_absolute()


def test_tree_uses_record_position_when_order_idx_repeats():
    records = [
        replace(
            _record(1, "첫 행"),
            section_path=[{"number": "5.2.1", "title": "시험"}],
        ),
        replace(
            _record(1, "둘째 행"),
            section_path=[{"number": "5.2.1", "title": "시험"}],
        ),
    ]
    evaluations = [
        Evaluation(
            order_idx=1,
            record_position=0,
            record_type="test",
            test_name="생존율시험",
            result="첫 행",
            final_status=PASS_LABEL,
            comparison_completed=True,
        ),
        Evaluation(
            order_idx=1,
            record_position=1,
            record_type="test",
            test_name="생존율시험",
            result="둘째 행",
            final_status=FAIL_LABEL,
            comparison_completed=True,
        ),
    ]

    roots = build_document_tree(records, evaluations)
    leaves = [
        child
        for root in roots
        for child in root.children
        if child.node_type == "test"
    ]

    assert [leaf.evaluation.result for leaf in leaves] == ["첫 행", "둘째 행"]
    assert len({leaf.key for leaf in leaves}) == 2


def test_target_metadata_sanitizer_reduces_nested_absolute_paths(tmp_path):
    private = tmp_path / "private" / "source.pdf"

    sanitized = pipeline_module._sanitize_target_metadata(
        {
            "policy_audit": [
                {"evidence": [{"file": str(private), "quote": "근거"}]}
            ]
        }
    )

    assert sanitized["policy_audit"][0]["evidence"][0]["file"] == "source.pdf"
    assert str(tmp_path) not in repr(sanitized)


def test_target_runtime_extract_dir_supports_reuse_and_raw_record_loaders(
    quiet_pipeline,
    tmp_path,
):
    from sp_pdf_judger.manufacturing_info_validator import (
        _load_records_json_from_result,
    )
    from sp_pdf_judger.ui_html import _load_records_json_for_ui

    pdf = tmp_path / "runtime-path.pdf"
    _write_pdf(pdf)
    pipeline, _store, _client = quiet_pipeline(
        tmp_path,
        TARGET_POLICY,
        _numeric_condition(),
    )
    first = pipeline.run(
        pdf,
        extracted_records=[_record(1, "생존율 85%")],
        static_result=_static(pdf),
    )
    extract_dir = Path(first.runtime_extract_dir)
    raw_records = [{"record_type": "content", "diagram_data": {"nodes": [1]}}]
    (extract_dir / "05_records.json").write_text(
        json.dumps(raw_records, ensure_ascii=False),
        encoding="utf-8",
    )

    second = pipeline.run(
        pdf,
        extracted_records=first.extracted_records,
        static_result=first,
    )

    assert second.metadata["extract_dir"] is None
    assert second.metadata["extraction_reused"] is True
    assert Path(second.runtime_extract_dir) == extract_dir
    assert _load_records_json_for_ui(second) == raw_records
    assert _load_records_json_from_result(second) == raw_records


def test_evaluation_group_fields_are_additive_defaults():
    evaluation = Evaluation()
    assert evaluation.permit_group_id is None
    assert evaluation.permit_group_path is None
    assert evaluation.permit_batch is None
    assert evaluation.permit_group_evidence is None


def test_generic_method_rules_do_not_overwrite_grouped_authoritative_verdict():
    evaluation = Evaluation(
        record_type="test",
        test_name="무균시험",
        result="균의 발육이 확인되지 않음",
        test_period="2026.08.01 ~ 2026.08.02",
        final_status=PASS_LABEL,
        reason="허가서 그룹 판정",
        source="permit_group_deterministic",
        permit_group_id="group-1",
        permit_group_path="deterministic",
    )

    metadata = pipeline_module._apply_general_test_method_rules([evaluation])

    assert evaluation.final_status == PASS_LABEL
    assert evaluation.reason == "허가서 그룹 판정"
    assert evaluation.source == "permit_group_deterministic"
    assert metadata["applied_count"] == 0


def _synthetic_group_shape(store, modes):
    groups = []
    audits = []
    records = []
    chunks = []
    conditions = []
    stage_key = "componenta중간체원액"
    for position, mode in enumerate(modes):
        order = position + 1
        batch = f"LOT-{order:03d}"
        candidate_ids = ()
        blocker = None
        facts = ()
        batch_evidence = BatchEvidence(
            state="explicit",
            batch=batch,
            stage_key=stage_key,
            record_order_idxs=(order,),
            candidates=(),
        )
        result = "적합"
        if mode == "deterministic":
            evidence_id = f"numeric-evidence-{position}"
            candidate_ids = (evidence_id,)
            chunks.append(_chunk(evidence_id))
            conditions.append(
                replace(
                    _numeric_condition(evidence_id),
                    condition_id=f"numeric-condition-{position}",
                )
            )
            result = "생존율 79%"
            facts = (
                SPFact(
                    fact_id=f"fact-{position}",
                    record_order_idx=order,
                    row_ref=f"record:{order}:position:{position}",
                    field="result",
                    value=result,
                    quote=result,
                    page_start=order,
                    page_end=order,
                    source_kind="test",
                    metric_key="survival_rate",
                    numeric_value=Decimal("79"),
                    unit="%",
                ),
            )
        elif mode == "ambiguous":
            evidence_id = f"semantic-evidence-{position}"
            candidate_ids = (evidence_id,)
            chunks.append(_chunk(evidence_id))
            conditions.append(
                replace(
                    _semantic_condition(evidence_id),
                    condition_id=f"semantic-condition-{position}",
                )
            )
            result = "예상되는 염기서열과 일치함"
            facts = (
                SPFact(
                    fact_id=f"fact-{position}",
                    record_order_idx=order,
                    row_ref=f"record:{order}:position:{position}",
                    field="result",
                    value=result,
                    quote=result,
                    page_start=order,
                    page_end=order,
                    source_kind="test",
                    metric_key="unparsed",
                    qualifier="fit",
                ),
            )
        elif mode == "hold":
            blocker = "batch_missing"
            batch = None
            batch_evidence = BatchEvidence(
                state="missing",
                batch=None,
                stage_key=stage_key,
                record_order_idxs=(),
                candidates=(),
            )

        groups.append(
            PermitGroup(
                group_id=f"group-{position}",
                stage_key=stage_key,
                test_key="생존율시험",
                batch=batch,
                method_key=None,
                record_order_idxs=(order,),
                facts=facts,
                candidate_evidence_ids=candidate_ids,
                blocker_code=blocker,
                batch_evidence=batch_evidence,
                record_positions=(position,),
            )
        )
        audits.append({"record_positions": [position]})
        records.append(_record(order, result, batch=batch))
    store.chunks = chunks
    store.conditions = conditions
    return groups, audits, records


def test_sp_baseline_uses_local_tests_before_authoritative_group_review(
    quiet_pipeline, monkeypatch, tmp_path,
):
    pdf = tmp_path / "baseline.pdf"
    _write_pdf(pdf)
    pipeline, _store, client = quiet_pipeline(tmp_path, LEGACY_POLICY, _numeric_condition())
    monkeypatch.setattr(pipeline.judge_engine, "judge_record", lambda *_: pytest.fail("row LLM path"))
    result = pipeline.run(pdf, extracted_records=[_record(1, "적합")],
                          static_result=_static(pdf), primary_tests_only=True)
    assert client.call_count == 0
    assert len(_test_evaluations(result)) == 1
    assert result.metadata["test_evaluation_mode"] == "primary_only"


def test_92_group_shape_uses_nine_batches_and_preserves_classification_counts(
    quiet_pipeline,
    monkeypatch,
    tmp_path,
):
    pdf = tmp_path / "measured-92-shape.pdf"
    _write_pdf(pdf)
    pipeline, store, client = quiet_pipeline(
        tmp_path,
        TARGET_POLICY,
        _numeric_condition(),
    )
    modes = (
        ["deterministic"] * 11
        + ["ambiguous"] * 53
        + ["hold"] * 27
        + ["fallback"]
    )
    groups, audits, records = _synthetic_group_shape(store, modes)
    monkeypatch.setattr(
        pipeline_module,
        "build_permit_groups",
        lambda *_args, **_kwargs: (groups, audits),
    )

    result = pipeline.run(
        pdf,
        extracted_records=records,
        static_result=_static(pdf),
    )

    assert client.singleton_calls == 0
    assert client.batch_sizes == [6, 6, 6, 6, 6, 6, 6, 6, 5]
    assert client.call_count == client.batch_calls == 9
    assert result.metadata["permit_group_batch_count"] == 9
    assert result.metadata["permit_group_batch_sizes"] == client.batch_sizes
    assert result.metadata["permit_group_provider_calls"] == 9
    assert result.metadata["permit_group_estimated_saved_calls"] == 83
    assert result.metadata["permit_group_path_counts"] == {
        "deterministic": 11,
        "ambiguous_llm": 53,
        "hold_without_llm": 28,
        "primary_fallback": 0,
    }
    assert sum(result.metadata["permit_group_path_counts"].values()) == 92
    assert result.metadata["permit_group_count"] == 92
    assert result.metadata["permit_group_raw_test_count"] == 92
    assert len(_test_evaluations(result)) == 92


def test_deterministic_and_structural_hold_groups_make_zero_provider_calls(
    quiet_pipeline,
    monkeypatch,
    tmp_path,
):
    pdf = tmp_path / "local-only-groups.pdf"
    _write_pdf(pdf)
    pipeline, store, client = quiet_pipeline(
        tmp_path,
        TARGET_POLICY,
        _numeric_condition(),
    )
    groups, audits, records = _synthetic_group_shape(
        store,
        ["deterministic", "hold", "deterministic", "hold"],
    )
    monkeypatch.setattr(
        pipeline_module,
        "build_permit_groups",
        lambda *_args, **_kwargs: (groups, audits),
    )

    result = pipeline.run(pdf, extracted_records=records, static_result=_static(pdf))

    assert client.call_count == client.singleton_calls == client.batch_calls == 0
    assert result.metadata["permit_group_batch_count"] == 0
    assert result.metadata["permit_group_batch_sizes"] == []
    assert result.metadata["permit_group_path_counts"] == {
        "deterministic": 2,
        "ambiguous_llm": 0,
        "hold_without_llm": 2,
        "primary_fallback": 0,
    }


def test_disabled_client_holds_ambiguous_groups_without_attempting_batches(
    quiet_pipeline,
    monkeypatch,
    tmp_path,
):
    pdf = tmp_path / "disabled-client.pdf"
    _write_pdf(pdf)
    pipeline, store, client = quiet_pipeline(
        tmp_path,
        TARGET_POLICY,
        _semantic_condition(),
    )
    client.enabled = False
    groups, audits, records = _synthetic_group_shape(
        store,
        ["ambiguous", "ambiguous"],
    )
    monkeypatch.setattr(
        pipeline_module,
        "build_permit_groups",
        lambda *_args, **_kwargs: (groups, audits),
    )

    result = pipeline.run(pdf, extracted_records=records, static_result=_static(pdf))

    assert [item.final_status for item in _test_evaluations(result)] == [
        HOLD_LABEL,
        HOLD_LABEL,
    ]
    assert client.call_count == client.singleton_calls == client.batch_calls == 0
    assert result.metadata["permit_group_batch_count"] == 0
    assert result.metadata["permit_group_batch_sizes"] == []
    assert result.metadata["permit_group_path_counts"] == {
        "deterministic": 0,
        "ambiguous_llm": 2,
        "hold_without_llm": 0,
        "primary_fallback": 0,
    }
    assert result.metadata["permit_group_planned_batch_sizes"] == [2]
    assert result.metadata["permit_group_planned_batch_count"] == 1
    assert result.metadata["permit_group_planned_prompt_chars"][0] > 0
    # Disabled API is not a saving: report the plan, not a fictitious zero-call review.
    assert result.metadata["permit_group_estimated_saved_calls"] == 1


def test_oversized_group_does_not_send_huge_request_or_pass(quiet_pipeline, monkeypatch, tmp_path):
    from sp_pdf_judger.permit_group_batching import PermitGroupBatchRequest
    pdf = tmp_path / "oversized.pdf"
    _write_pdf(pdf)
    pipeline, store, client = quiet_pipeline(tmp_path, TARGET_POLICY, _semantic_condition())
    groups, audits, records = _synthetic_group_shape(store, ["ambiguous", "ambiguous"])
    monkeypatch.setattr(pipeline_module, "build_permit_groups", lambda *a, **k: (groups, audits))
    make = pipeline_module.make_group_batch_request
    def sized(**kwargs):
        request = make(**kwargs)
        if request.group_id == "group-0":
            return PermitGroupBatchRequest(request.group_id, request.payload, request.serialized_payload + " " * 70000)
        return request
    monkeypatch.setattr(pipeline_module, "make_group_batch_request", sized)
    result = pipeline.run(pdf, extracted_records=records, static_result=_static(pdf))
    evaluations = _test_evaluations(result)
    assert evaluations[0].final_status == HOLD_LABEL
    assert not evaluations[0].comparison_completed
    assert "llm_request_too_large" in evaluations[0].reason
    assert client.batch_sizes == [1]
    assert result.metadata["permit_group_oversized_count"] == 1
    assert result.metadata["permit_group_planned_batch_count"] == 1


def test_batch_service_failure_aborts_result_publication(
    quiet_pipeline,
    monkeypatch,
    tmp_path,
):
    class FailingBatchClient(_GroupClient):
        def explain_permit_group_batch_once(self, *, requests):
            requests = tuple(requests)
            self.batch_calls += 1
            self.batch_sizes.append(len(requests))
            self.call_count += 1
            raise ClovaServiceError("rate_limit", 429, "42901")

    pdf = tmp_path / "batch-service-failure.pdf"
    _write_pdf(pdf)
    pipeline, store, client = quiet_pipeline(
        tmp_path,
        TARGET_POLICY,
        _semantic_condition(),
        client_type=FailingBatchClient,
    )
    groups, audits, records = _synthetic_group_shape(store, ["ambiguous"])
    monkeypatch.setattr(
        pipeline_module,
        "build_permit_groups",
        lambda *_args, **_kwargs: (groups, audits),
    )
    original_result = pipeline_module.ProcessingResult
    published = []

    def track_result(*args, **kwargs):
        published.append(True)
        return original_result(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "ProcessingResult", track_result)

    with pytest.raises(ClovaServiceError, match="42901"):
        pipeline.run(pdf, extracted_records=records, static_result=_static(pdf))

    assert published == []
    assert client.batch_sizes == [1]
    assert client.singleton_calls == 0


def test_malformed_real_client_response_aborts_before_processing_result(
    quiet_pipeline, monkeypatch, tmp_path,
):
    from sp_pdf_judger.llm import ClovaJudgeClient
    from sp_pdf_judger.clova_transport import clova_request_context

    pdf = tmp_path / "malformed-batch.pdf"
    _write_pdf(pdf)
    pipeline, store, _ = quiet_pipeline(tmp_path, TARGET_POLICY, _semantic_condition())
    groups, audits, records = _synthetic_group_shape(store, ["ambiguous"])
    monkeypatch.setattr(pipeline_module, "build_permit_groups", lambda *a, **k: (groups, audits))
    monkeypatch.setattr("sp_pdf_judger.clova_transport._GATES", {})
    monkeypatch.delenv("CLOVA_RESPONSE_CACHE_DIR", raising=False)
    response = {"items": [{"group_id": groups[0].group_id,
        "selected_evidence_id": "permit-evidence", "decisions": [],
        "status": "pass", "reason": None}]}
    provider = SimpleNamespace(create=lambda **kwargs: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(response)))]))
    client = ClovaJudgeClient(execution_settings=("", "fixture", ""))
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=provider))
    client.enabled = True
    pipeline.llm_client = pipeline.judge_engine.llm_client = client
    published = []
    original_result = pipeline_module.ProcessingResult

    def track_result(*args, **kwargs):
        published.append(True)
        return original_result(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "ProcessingResult", track_result)
    cache = tmp_path / "responses"
    with clova_request_context(cache_dir=cache):
        with pytest.raises(ClovaServiceError) as failed:
            pipeline.run(pdf, extracted_records=records, static_result=_static(pdf))
    assert failed.value.kind == "response"
    assert client.call_count == 1 and client.success_count == 0
    assert not published
    assert not list(cache.glob("*.json"))


def test_duplicate_order_idx_rows_fan_out_by_distinct_record_position(
    quiet_pipeline,
    tmp_path,
):
    pdf = tmp_path / "duplicate-order-rows.pdf"
    _write_pdf(pdf)
    pipeline, _store, client = quiet_pipeline(
        tmp_path,
        TARGET_POLICY,
        _semantic_condition(),
    )
    records = [
        _record(1, "예상되는 염기서열과 일치함", batch="LOT-A"),
        _record(1, "예상되는 염기서열과 일치함", batch="LOT-B"),
    ]

    result = pipeline.run(pdf, extracted_records=records, static_result=_static(pdf))
    evaluations = _test_evaluations(result)

    assert [item.record_position for item in evaluations] == [0, 1]
    assert [item.result for item in evaluations] == [item.result for item in records]
    assert len({item.permit_group_id for item in evaluations}) == 2
    assert client.batch_sizes == [2]
    assert client.singleton_calls == 0


def test_non_target_legacy_policy_does_not_use_batch_client(
    quiet_pipeline,
    tmp_path,
):
    pdf = tmp_path / "legacy-never-batched.pdf"
    _write_pdf(pdf)
    pipeline, _store, client = quiet_pipeline(
        tmp_path,
        LEGACY_POLICY,
        _numeric_condition(),
    )
    pipeline.judge_engine.judge_record = lambda record: Evaluation(
        order_idx=record.order_idx,
        record_type="test",
        test_name=record.test_name or "",
        result=record.result,
        final_status=PASS_LABEL,
        reason="legacy unchanged",
        source="legacy-source",
    )
    client.explain_permit_group_batch_once = lambda **_kwargs: pytest.fail(
        "legacy policy attempted batch transport"
    )

    result = pipeline.run(
        pdf,
        extracted_records=[_record(1, "생존율 85%")],
        static_result=_static(pdf),
    )

    assert _test_evaluations(result)[0].reason == "legacy unchanged"
    assert "permit_group_batch_count" not in result.metadata
    assert client.call_count == client.singleton_calls == client.batch_calls == 0


def test_batch_progress_and_metadata_are_safe_and_bounded(
    quiet_pipeline,
    monkeypatch,
    tmp_path,
):
    class UnsafeAuditClient(_GroupClient):
        def explain_permit_group_batch_once(self, *, requests):
            result = super().explain_permit_group_batch_once(requests=requests)
            for entry in self.permit_audit[-len(tuple(requests)) :]:
                entry["prompt"] = "PRIVATE-PROMPT"
                entry["path"] = str(tmp_path / "private" / "permit.pdf")
            return result

    pdf = tmp_path / "seven-groups.pdf"
    _write_pdf(pdf)
    pipeline, store, client = quiet_pipeline(
        tmp_path,
        TARGET_POLICY,
        _semantic_condition(),
        client_type=UnsafeAuditClient,
    )
    groups, audits, records = _synthetic_group_shape(
        store,
        ["ambiguous"] * 7,
    )
    monkeypatch.setattr(
        pipeline_module,
        "build_permit_groups",
        lambda *_args, **_kwargs: (groups, audits),
    )
    messages = []

    result = pipeline.run(
        pdf,
        extracted_records=records,
        static_result=_static(pdf),
        progress_callback=messages.append,
    )

    assert [message for message in messages if message.startswith("허가서 의미 판정 묶음")] == [
        "허가서 의미 판정 묶음 1/2",
        "허가서 의미 판정 묶음 2/2",
    ]
    assert result.metadata["permit_group_batch_count"] == 2
    assert result.metadata["permit_group_batch_sizes"] == [6, 1]
    assert max(result.metadata["permit_group_batch_sizes"]) <= 6
    assert sum(result.metadata["permit_group_batch_sizes"]) == 7
    serialized = repr(result.metadata["permit_llm_audit"])
    assert "PRIVATE-PROMPT" not in serialized
    assert str(tmp_path) not in serialized
    assert all(
        set(item) <= {"group_id", "event", "error_type"}
        for item in result.metadata["permit_llm_audit"]
    )
