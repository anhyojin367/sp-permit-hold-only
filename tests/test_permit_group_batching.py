import json
from types import SimpleNamespace

import pytest

from sp_pdf_judger.permit_group_batching import (
    MAX_BATCH_PROMPT_CHARS,
    IndexedBatchItem,
    PermitGroupBatchRequest,
    index_group_batch_response,
    make_group_batch_request,
    partition_group_batch_requests,
)
from sp_pdf_judger.permit_llm_protocol import (
    PERMIT_GROUP_BATCH_PROTOCOL_VERSION,
    GroundedGroupBatchResponse,
    GroundedGroupBatchWireItem,
    build_group_batch_prompt,
)


def _request(group_id: str, padding: int = 0) -> PermitGroupBatchRequest:
    payload = {"group": {"group_id": group_id}, "padding": "x" * padding}
    return PermitGroupBatchRequest(
        group_id=group_id,
        payload=payload,
        serialized_payload=json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _wire(group_id, *, selected="evidence-1", reason="확인"):
    return GroundedGroupBatchWireItem(
        group_id=group_id,
        selected_evidence_id=selected,
        decisions=[],
        status="검수보류",
        reason=reason,
    )


def test_make_request_uses_canonical_group_payload():
    group = SimpleNamespace(
        group_id="group-1",
        stage_key="stage",
        test_key="test",
        method_key=None,
        batch="LOT-A",
        batch_evidence=None,
        facts=(),
    )

    request = make_group_batch_request(
        group=group,
        candidates=(),
        conditions=(),
        executions=(),
    )

    assert request.group_id == "group-1"
    assert request.payload["group"]["group_id"] == "group-1"
    assert request.serialized_payload == json.dumps(
        request.payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def test_partition_is_stable_and_never_exceeds_six_groups():
    requests = tuple(_request(f"group-{index}") for index in range(7))

    batches = partition_group_batch_requests(requests)

    assert [[item.group_id for item in batch] for batch in batches] == [
        [f"group-{index}" for index in range(6)],
        ["group-6"],
    ]


def test_partition_measures_the_final_prompt_against_60000_characters():
    requests = (_request("group-a", 30_000), _request("group-b", 30_000))

    batches = partition_group_batch_requests(requests)

    assert [len(batch) for batch in batches] == [1, 1]
    assert all(
        len(build_group_batch_prompt(batch)) <= MAX_BATCH_PROMPT_CHARS
        for batch in batches
    )


def test_oversized_single_group_is_rejected_without_truncation():
    request = _request("oversized", 70_000)

    with pytest.raises(ValueError, match="oversized"):
        partition_group_batch_requests((request,))
    assert len(request.payload["padding"]) == 70_000


def test_exact_repeated_permit_candidates_are_compacted_but_conflicts_remain():
    from dataclasses import replace
    from decimal import Decimal
    from sp_pdf_judger.permit_models import PermitChunk, PermitCondition
    base = PermitChunk("permit.pdf", 1, "1.1", "시험", "최소 21일", 1, 1,
                       normalized_stage_path="Stage A", normalized_test_name="기간시험",
                       evidence_id="e0")
    condition = PermitCondition("c0", "e0", 0, "compare", ">=", Decimal(21), "day",
                                None, None, True, True, (), "최소 21일", "Stage A", "기간시험", "duration")
    candidates = [replace(base, evidence_id=f"e{i}", page_number=i+1, page_start=i+1, page_end=i+1)
                  for i in range(300)]
    conditions = [replace(condition, condition_id=f"c{i}", evidence_id=f"e{i}") for i in range(300)]
    candidates.append(replace(base, evidence_id="conflict", text="최소 28일"))
    conditions.append(replace(condition, condition_id="different", evidence_id="conflict", value=Decimal(28), quote="최소 28일"))
    group = SimpleNamespace(group_id="g", facts=(), batch_evidence=None)
    request = make_group_batch_request(group=group, candidates=candidates, conditions=conditions, executions=())
    assert [x["evidence_id"] for x in request.payload["candidates"]] == ["e0", "conflict"]
    assert [x["condition_id"] for x in request.payload["conditions"]] == ["c0", "different"]
    assert request.payload["equivalent_candidate_counts"] == {"e0": 300}
    assert len(candidates) == len(conditions) == 301  # Local source/audit is never discarded.


def test_same_candidate_text_with_different_parsed_condition_is_not_deduplicated():
    from sp_pdf_judger.permit_group_batching import compact_equivalent_candidates
    payload = {"candidates": [{"evidence_id": "a", "quote": "same"}, {"evidence_id": "b", "quote": "same"}],
               "conditions": [{"evidence_id":"a", "condition_id":"ca", "value":"21", "children":[]},
                              {"evidence_id":"b", "condition_id":"cb", "value":"28", "children":[]}],
               "deterministic_executions": []}
    assert compact_equivalent_candidates(payload)["candidates"] == payload["candidates"]


@pytest.mark.parametrize("difference", ["method_key", "unit", "mandatory", "children", "execution"])
def test_exact_compaction_keeps_method_scope_graph_and_execution_differences(difference):
    from sp_pdf_judger.permit_group_batching import compact_equivalent_candidates
    payload = {"candidates": [{"evidence_id": key, "quote": "same source", "stage_key": "A"}
                               for key in ("a", "b")],
               "conditions": [], "deterministic_executions": []}
    for key in ("a", "b"):
        payload["conditions"].extend([
            {"evidence_id": key, "condition_id": key + "0", "children": [key + "1"],
             "method_key": None, "unit": "day", "mandatory": True},
            {"evidence_id": key, "condition_id": key + "1", "children": []},
        ])
        payload["deterministic_executions"].append({"condition_id": key + "1", "status": "pass"})
    assert len(compact_equivalent_candidates(payload)["candidates"]) == 1
    if difference == "execution":
        payload["deterministic_executions"][1]["status"] = "fail"
    else:
        payload["conditions"][2][difference] = {
            "method_key": "yolk-sac", "unit": "hour", "mandatory": False, "children": [],
        }[difference]
    assert len(compact_equivalent_candidates(payload)["candidates"]) == 2


def test_batch_prompt_preserves_independent_group_ids_and_protocol_version():
    prompt = build_group_batch_prompt((_request("group-a"), _request("group-b")))

    assert PERMIT_GROUP_BATCH_PROTOCOL_VERSION in prompt
    assert '"group_id":"group-a"' in prompt
    assert '"group_id":"group-b"' in prompt
    assert prompt.count('"group_id"') == 2


def test_batch_index_accepts_exact_unique_coverage():
    requests = (_request("group-a"), _request("group-b"))
    response = GroundedGroupBatchResponse(
        items=[_wire("group-a"), _wire("group-b", selected="evidence-2")]
    )

    indexed = index_group_batch_response(requests, response)

    assert set(indexed) == {"group-a", "group-b"}
    assert all(isinstance(item, IndexedBatchItem) for item in indexed.values())
    assert indexed["group-a"].response.selected_evidence_id == "evidence-1"
    assert indexed["group-b"].response.selected_evidence_id == "evidence-2"
    assert indexed["group-a"].error_code is None


def test_missing_or_duplicate_known_id_invalidates_only_that_group():
    requests = tuple(_request(name) for name in ("group-a", "group-b", "group-c"))
    response = GroundedGroupBatchResponse(
        items=[_wire("group-a"), _wire("group-a"), _wire("group-c")]
    )

    indexed = index_group_batch_response(requests, response)

    assert indexed["group-a"].response is None
    assert indexed["group-a"].error_code == "llm_batch_group_id_duplicate"
    assert indexed["group-b"].response is None
    assert indexed["group-b"].error_code == "llm_batch_group_id_missing"
    assert indexed["group-c"].response is not None
    assert indexed["group-c"].error_code is None


def test_recognizable_malformed_item_invalidates_only_that_group():
    requests = (_request("group-a"), _request("group-b"))
    response = GroundedGroupBatchResponse(
        items=[_wire("group-a", selected=123), _wire("group-b")]
    )

    indexed = index_group_batch_response(requests, response)

    assert indexed["group-a"].response is None
    assert indexed["group-a"].error_code == "llm_batch_item_invalid"
    assert indexed["group-b"].response is not None


@pytest.mark.parametrize(
    "malformed_fields",
    [
        {"reason": {"text": "object values are invalid"}},
        {"unexpected": "extra fields are invalid"},
        {
            "decisions": [
                {
                    "condition_id": "condition-2",
                    "status": "hold",
                    "fact_ids": [],
                    "quote": "근거",
                    "unexpected": True,
                }
            ]
        },
    ],
)
def test_raw_attributable_malformed_item_does_not_poison_healthy_sibling(malformed_fields):
    requests = (_request("group-a"), _request("group-b"))
    healthy = _wire("group-a").model_dump()
    malformed = _wire("group-b").model_dump()
    malformed.update(malformed_fields)

    response = GroundedGroupBatchResponse.model_validate(
        {"items": [healthy, malformed]}
    )
    indexed = index_group_batch_response(requests, response)

    assert indexed["group-a"].response is not None
    assert indexed["group-a"].error_code is None
    assert indexed["group-b"].response is None
    assert indexed["group-b"].error_code == "llm_batch_item_invalid"


@pytest.mark.parametrize("extra_id", ["group-extra", "", 42])
def test_extra_unknown_or_unattributable_id_invalidates_the_batch(extra_id):
    requests = (_request("group-a"), _request("group-b"))
    response = GroundedGroupBatchResponse(
        items=[_wire("group-a"), _wire("group-b"), _wire(extra_id)]
    )

    indexed = index_group_batch_response(requests, response)

    assert all(item.response is None for item in indexed.values())
    assert {
        item.error_code for item in indexed.values()
    } == {"llm_batch_framing_invalid"}


def test_unavailable_batch_response_invalidates_every_requested_group():
    indexed = index_group_batch_response(
        (_request("group-a"), _request("group-b")),
        None,
    )

    assert all(item.response is None for item in indexed.values())
    assert {
        item.error_code for item in indexed.values()
    } == {"llm_batch_response_unavailable"}
