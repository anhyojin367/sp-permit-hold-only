from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from sp_pdf_judger.extractor import _record_from_dict
from sp_pdf_judger.judgement import JudgeEngine
from sp_pdf_judger.permit_catalog import PermitPolicy
from sp_pdf_judger.permit_grouping import (
    BatchEvidence,
    build_permit_groups,
    build_stage_batch_inventory,
)
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.schemas import ExtractedRecord


def _record(
    order: int,
    *,
    stage: str | None = "Component A 중간체원액에 대한 시험",
    test: str | None = "확인시험",
    result: str | None = "적합",
    method: str | None = "자가기시(Dot Blot시험법)",
    raw: str = "",
    controls: list[dict] | None = None,
    batch_id: str | None = None,
    batch_evidence: dict | None = None,
) -> ExtractedRecord:
    return ExtractedRecord(
        order_idx=order,
        record_type="test",
        section_title=stage,
        section_path=[{"title": stage}] if stage else [],
        test_name=test,
        criteria="확인되어야 한다",
        result=result,
        method=method,
        raw_text=raw or f"시험명: {test}\n시험결과: {result or ''}",
        normalized_text=raw or f"시험명: {test}\n시험결과: {result or ''}",
        controls=list(controls or []),
        batch_id=batch_id,
        batch_evidence=batch_evidence,
        page_start=order,
        page_end=order,
    )


def _info(order: int, stage: str, batch: str, *, label: str = "제조번호") -> ExtractedRecord:
    text = f"{label} {batch}"
    return ExtractedRecord(
        order_idx=order,
        record_type="content",
        section_title=stage,
        section_path=[{"title": stage}],
        content=text,
        raw_text=text,
        normalized_text=text,
        page_start=order,
        page_end=order,
    )


class _Store:
    def __init__(self, by_order: dict[int | float, tuple[str, ...]] | None = None):
        self.by_order = by_order or {}

    def search(self, record, top_k=12):
        evidence_ids = self.by_order.get(record.order_idx, ("permit-evidence",))
        return [
            SimpleNamespace(
                evidence_id=value,
                text="허가서 예시 제조번호 PERMIT-EXAMPLE-001",
            )
            for value in evidence_ids
        ]


def _group_for(groups, order_idx):
    return next(group for group in groups if order_idx in group.record_order_idxs)


def test_schema_adds_optional_batch_fields_without_breaking_existing_constructors():
    names = {field.name for field in fields(ExtractedRecord)}
    record = ExtractedRecord(test_name="기존 생성자")
    assert {"batch_id", "batch_evidence"} <= names
    assert record.batch_id is None
    assert record.batch_evidence is None


def test_extractor_preserves_structured_batch_metadata():
    record = _record_from_dict(
        {
            "record_type": "test",
            "order_idx": 7.0001,
            "test_name": "확인시험",
            "batch_id": "ROW-LOT-7",
            "batch_evidence": {
                "source_kind": "explicit_table_column",
                "label": "Lot No.",
                "quote": "Lot No.: ROW-LOT-7",
            },
        },
        order_idx=99,
    )

    assert record.batch_id == "ROW-LOT-7"
    assert record.batch_evidence == {
        "source_kind": "explicit_table_column",
        "label": "Lot No.",
        "quote": "Lot No.: ROW-LOT-7",
    }


def test_grouping_metadata_does_not_change_legacy_llm_record_context():
    baseline = _record(7)
    enriched = _record(
        7,
        batch_id="ROW-LOT-7",
        batch_evidence={
            "source_kind": "explicit_table_column",
            "label": "Lot No.",
            "quote": "Lot No.: ROW-LOT-7",
        },
    )

    assert JudgeEngine._record_context(enriched) == JudgeEngine._record_context(baseline)


def test_same_stage_test_batch_method_and_candidates_share_one_group_without_mutation():
    records = [
        _info(1, "Component A 중간체 원액 정보", "SKY-CA220705"),
        _record(2),
        _record(3),
    ]
    originals = [(row.criteria, row.result, row.raw_text) for row in records]

    groups, audit = build_permit_groups(records, _Store())

    assert len(groups) == 1
    group = groups[0]
    assert group.batch == "SKY-CA220705"
    assert group.record_order_idxs == (2, 3)
    assert group.candidate_evidence_ids == ("permit-evidence",)
    assert group.blocker_code is None
    assert {fact.record_order_idx for fact in group.facts} == {2, 3}
    assert len({fact.fact_id for fact in group.facts}) == len(group.facts)
    assert [(row.criteria, row.result, row.raw_text) for row in records] == originals
    assert audit[0]["group_id"] == group.group_id
    assert str(Path.cwd()) not in json.dumps(audit, ensure_ascii=False)


def test_fuzzy_search_result_does_not_become_its_own_compatibility_alias():
    class FuzzyStore:
        search_aliases = {}

        def search(self, _record, top_k=12):
            return [
                SimpleNamespace(
                    evidence_id="fuzzy-evidence",
                    normalized_test_name="최종확인시험",
                    title="최종확인시험",
                    text="80% 이상이어야 한다.",
                )
            ]

    record = _record(
        2,
        batch_id="LOT-A",
        batch_evidence={
            "source_kind": "explicit_table_column",
            "label": "제조번호",
            "quote": "제조번호 LOT-A",
        },
    )

    groups, _audit = build_permit_groups([record], FuzzyStore())

    assert groups[0].candidate_evidence_ids == ("fuzzy-evidence",)
    assert groups[0].compatible_test_keys == ("확인시험",)


def test_permit_candidate_search_uses_identity_not_submitted_result_text():
    class IdentityOnlyStore:
        def search(self, record, top_k=12):
            assert record.section_number is None
            assert record.criteria is None
            assert record.result is None
            assert record.raw_text is None
            assert record.normalized_text is None
            assert record.remarks is None
            return [SimpleNamespace(evidence_id="same-permit-test")]

    records = [
        _record(
            1,
            result="적합",
            batch_id="LOT-A",
            batch_evidence={"source_kind": "explicit_table_column", "quote": "LOT-A"},
        ),
        _record(
            2,
            result="부적합",
            batch_id="LOT-A",
            batch_evidence={"source_kind": "explicit_table_column", "quote": "LOT-A"},
        ),
    ]

    groups, _ = build_permit_groups(records, IdentityOnlyStore())

    assert len(groups) == 1
    assert groups[0].record_order_idxs == (1, 2)


def test_permit_candidate_search_preserves_more_than_twelve_matching_clauses():
    class ThirteenCandidateStore:
        chunks = tuple(range(13))

        def search(self, _record, top_k=12):
            return [
                SimpleNamespace(evidence_id=f"evidence-{index:02d}")
                for index in range(13)
            ][:top_k]

    record = _record(
        1,
        batch_id="LOT-A",
        batch_evidence={
            "source_kind": "explicit_table_column",
            "quote": "제조번호 LOT-A",
        },
    )

    groups, _ = build_permit_groups([record], ThirteenCandidateStore())

    assert groups[0].candidate_evidence_ids == tuple(
        f"evidence-{index:02d}" for index in range(13)
    )


@pytest.mark.parametrize("dimension", ["stage", "batch", "method", "candidate"])
def test_incompatible_group_dimensions_never_join(dimension):
    first = _record(
        10,
        batch_id="LOT-A",
        batch_evidence={"source_kind": "explicit_table_column", "quote": "Lot No.: LOT-A"},
    )
    second = _record(
        11,
        batch_id="LOT-A",
        batch_evidence={"source_kind": "explicit_table_column", "quote": "Lot No.: LOT-A"},
    )
    store = _Store()
    if dimension == "stage":
        second.section_title = "Component B 중간체원액에 대한 시험"
        second.section_path = [{"title": second.section_title}]
    elif dimension == "batch":
        second.batch_id = "LOT-B"
        second.batch_evidence = {"source_kind": "explicit_table_column", "quote": "Lot No.: LOT-B"}
    elif dimension == "method":
        second.method = None
    else:
        store = _Store({10: ("permit-a",), 11: ("permit-b",)})

    groups, _ = build_permit_groups([first, second], store)

    assert len(groups) == 2
    assert {group.record_order_idxs for group in groups} == {(10,), (11,)}


def test_missing_batch_records_are_isolated_and_cannot_borrow_each_other():
    groups, _ = build_permit_groups([_record(1), _record(2)], _Store())
    assert len(groups) == 2
    assert all(group.blocker_code == "batch_missing" for group in groups)
    assert {group.record_order_idxs for group in groups} == {(1,), (2,)}


def test_blocked_record_does_not_search_permit_candidates():
    class FailIfSearched:
        def search(self, record, top_k=12):
            raise AssertionError("blocked records must not spend permit-search work")

    groups, _ = build_permit_groups([_record(1)], FailIfSearched())

    assert groups[0].blocker_code == "batch_missing"
    assert groups[0].candidate_evidence_ids == ()


def test_two_distinct_same_stage_information_batches_are_conflict_not_nearest_value():
    stage = "Component A 중간체 원액 정보"
    records = [
        _info(1, stage, "LOT-A"),
        _info(2, stage, "LOT-B"),
        _record(3),
        _record(4),
    ]
    inventory = build_stage_batch_inventory(records)
    groups, _ = build_permit_groups(records, _Store())

    evidence = next(item for item in inventory.values() if item.state == "conflict")
    assert isinstance(evidence, BatchEvidence)
    assert {candidate.value for candidate in evidence.candidates} == {"LOT-A", "LOT-B"}
    assert len(groups) == 2
    assert all(group.blocker_code == "batch_conflict" for group in groups)


def test_repeated_same_stage_primary_batch_retains_sources_without_conflict():
    records = [
        _info(1, "Component A 중간체 원액 정보", "LOT-A"),
        _info(2, "Component A 중간체 원액 정보", "LOT-A"),
        _record(3),
    ]
    inventory = build_stage_batch_inventory(records)
    evidence = next(iter(inventory.values()))
    assert evidence.state == "explicit"
    assert evidence.batch == "LOT-A"
    assert len(evidence.candidates) == 2


def test_only_information_records_can_seed_stage_inventory():
    flowchart = _info(1, "Component A 중간체 원액 정보", "LOT-WRONG")
    flowchart.record_type = "flowchart"
    records = [
        flowchart,
        _info(2, "Component A 중간체 원액 정보", "LOT-RIGHT"),
        _record(3),
    ]

    groups, _ = build_permit_groups(records, _Store())

    assert groups[0].batch == "LOT-RIGHT"


def test_collapsed_extra_fields_are_not_accepted_as_a_batch_value():
    records = [
        _info(
            1,
            "Component A 중간체 원액 정보",
            "LOT-A 제조년월일 2025.04.20",
        ),
        _record(2),
    ]

    groups, _ = build_permit_groups(records, _Store())

    assert groups[0].batch is None
    assert groups[0].blocker_code == "batch_missing"


def test_control_and_qualified_material_lots_are_never_primary_batches():
    raw = "양성대조군 제조번호 CONTROL-001\n시험결과 적합"
    record = _record(5, raw=raw, controls=[{"lot_no": "CONTROL-002"}])

    groups, _ = build_permit_groups([record], _Store())

    assert groups[0].batch is None
    assert groups[0].blocker_code == "material_lot"
    assert {candidate.value for candidate in groups[0].batch_evidence.candidates} == {
        "CONTROL-001",
        "CONTROL-002",
    }


def test_unique_stage_batch_wins_over_control_lot_and_component_lot():
    raw = (
        "양성대조군 제조번호 SKY-M220805-001A\n"
        "Component A 제조번호 SKY-CA220705\n시험결과 적합"
    )
    records = [
        _info(1, "항원바이알 정보", "SKY-M220805-001"),
        _record(
            2,
            stage="항원바이알 시험",
            raw=raw,
            controls=[{"lot_no": "SKY-M220805-001A"}],
        ),
    ]

    groups, _ = build_permit_groups(records, _Store())

    assert groups[0].batch == "SKY-M220805-001"
    assert groups[0].blocker_code is None


def test_filename_permit_example_and_neighbor_stage_are_not_batch_evidence():
    record = _record(9, stage="Component B 중간체원액에 대한 시험")
    record.pdf_name = "SKY-CB-FILENAME-999.pdf"
    records = [_info(1, "Component A 중간체 원액 정보", "SKY-CA220705"), record]

    groups, _ = build_permit_groups(records, _Store())

    assert groups[0].batch is None
    assert groups[0].blocker_code == "batch_missing"


@pytest.mark.parametrize(
    "stage, test, expected",
    [
        (None, "확인시험", "stage_missing"),
        ("Component A 중간체원액에 대한 시험", None, "test_missing"),
    ],
)
def test_missing_stage_or_test_isolated_with_specific_blocker(stage, test, expected):
    record = _record(
        1,
        stage=stage,
        test=test,
        batch_id="LOT-A",
        batch_evidence={"source_kind": "explicit_table_column", "quote": "LOT-A"},
    )
    groups, _ = build_permit_groups([record], _Store())
    assert groups[0].blocker_code == expected


def test_record_explicit_batch_is_used_without_reading_controls():
    record = _record(
        4,
        batch_id="ROW-LOT-7",
        batch_evidence={
            "source_kind": "explicit_table_column",
            "label": "Lot No.",
            "quote": "Lot No.: ROW-LOT-7",
        },
        controls=[{"lot_no": "CONTROL-7"}],
    )
    groups, _ = build_permit_groups([record], _Store())
    assert groups[0].batch == "ROW-LOT-7"
    assert groups[0].batch_evidence.state == "explicit"
    assert all(candidate.value != "CONTROL-7" for candidate in groups[0].batch_evidence.candidates)


def test_d00_tracked_extraction_binds_stage_primary_batches_not_control_lots():
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "historical"
        / "corpus"
        / "D00.records.json"
    )
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    records = [
        _record_from_dict(raw, order_idx=index)
        for index, raw in enumerate(payload)
    ]
    permit_payload = json.loads(
        (fixture_path.parent / "permit.pages.json").read_text(encoding="utf-8")
    )
    permit_store = PermitPdfStore.from_page_texts(
        [(item["page"], item["text"]) for item in permit_payload],
        policy=PermitPolicy(
            policy_id="d00-grouping",
            authoritative=True,
            ignore_section_numbers=True,
            require_llm=True,
            ocr_mode="native",
            evaluation_mode="deterministic_first",
        ),
        source_file="permit-fixture.pdf",
    )

    groups, _ = build_permit_groups(records, permit_store)

    assert {_group_for(groups, order).batch for order in range(23, 41)} == {
        "CHO-MCB-211012"
    }
    assert {_group_for(groups, order).batch for order in range(82, 92)} == {
        "SKY-CA220705"
    }
    assert {_group_for(groups, order).batch for order in range(93, 104)} == {
        "SKY-CB220705"
    }
    assert {_group_for(groups, order).batch for order in range(126, 140)} == {
        "SKY-M220805-001"
    }
    assert all(
        _group_for(groups, order).blocker_code == "batch_missing"
        for order in range(77, 81)
    )
    antigen_groups = [group for group in groups if "항원바이알" in group.stage_key]
    assert antigen_groups
    assert {group.batch for group in antigen_groups} == {"SKY-M220805-001"}
    assert all(group.batch != "SKY-M220805-001A" for group in groups)


def test_group_and_fact_ids_are_stable_for_same_input():
    records = [
        _info(1, "Component A 중간체 원액 정보", "LOT-A"),
        _record(2),
    ]
    first, _ = build_permit_groups(records, _Store())
    second, _ = build_permit_groups(records, _Store())
    assert [group.group_id for group in first] == [group.group_id for group in second]
    assert [[fact.fact_id for fact in group.facts] for group in first] == [
        [fact.fact_id for fact in group.facts] for group in second
    ]
