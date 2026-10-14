"""Pure, bounded batching utilities for grounded permit-group requests."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from pydantic import ValidationError

from .permit_llm_protocol import (
    GroundedGroupBatchItem,
    GroundedGroupBatchResponse,
    GroundedGroupResponse,
    build_group_batch_prompt,
    build_group_payload,
)


MAX_GROUPS_PER_BATCH = 6
MAX_BATCH_PROMPT_CHARS = 60_000


@dataclass(frozen=True)
class PermitGroupBatchRequest:
    group_id: str
    payload: dict[str, Any]
    serialized_payload: str


@dataclass(frozen=True)
class IndexedBatchItem:
    response: GroundedGroupResponse | None
    error_code: str | None


def compact_equivalent_candidates(payload: dict[str, Any]) -> dict[str, Any]:
    """Coalesce exact repeated sources, never merely similar requirements.

    Keep full quotes, stage/test/method, parsed condition graph and deterministic
    results in the identity. Only source positions/opaque IDs may differ. The
    complete originals stay in the store/prepared group for grounding and audit.
    """
    by_evidence: dict[str, list[dict]] = {}
    by_condition: dict[str, list[dict]] = {}
    for item in payload["conditions"]:
        by_evidence.setdefault(item["evidence_id"], []).append(item)
    for item in payload["deterministic_executions"]:
        by_condition.setdefault(item["condition_id"], []).append(item)
    signatures: dict[str, str] = {}
    counts: dict[str, int] = {}
    kept = []
    for candidate in payload["candidates"]:
        evidence_id = candidate["evidence_id"]
        conditions = by_evidence.get(evidence_id, [])
        indices = {item["condition_id"]: i for i, item in enumerate(conditions)}
        graph = []
        valid = len(indices) == len(conditions)
        for condition in conditions:
            children = condition.get("children", [])
            if any(child not in indices for child in children):
                valid = False
                break
            graph.append({
                "condition": {k: v for k, v in condition.items()
                              if k not in {"condition_id", "evidence_id", "children"}},
                "children": [indices[child] for child in children],
                "executions": [{k: v for k, v in execution.items() if k != "condition_id"}
                               for execution in by_condition.get(condition["condition_id"], [])],
            })
        signature = json.dumps({
            "candidate": {k: v for k, v in candidate.items()
                          if k not in {"evidence_id", "page_start", "page_end"}},
            "graph": graph,
            "invalid_identity": None if valid else evidence_id,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        canonical = signatures.get(signature)
        if canonical is not None:
            counts[canonical] += 1
        else:
            signatures[signature] = evidence_id
            counts[evidence_id] = 1
            kept.append(candidate)
    if len(kept) == len(payload["candidates"]):
        return payload
    kept_ids = {candidate["evidence_id"] for candidate in kept}
    conditions = [item for item in payload["conditions"] if item["evidence_id"] in kept_ids]
    condition_ids = {item["condition_id"] for item in conditions}
    return {
        **payload,
        "candidates": kept,
        "conditions": conditions,
        "deterministic_executions": [item for item in payload["deterministic_executions"]
                                     if item["condition_id"] in condition_ids],
        "equivalent_candidate_counts": {key: value for key, value in counts.items() if value > 1},
    }


def make_group_batch_request(
    *,
    group,
    candidates,
    conditions,
    executions,
) -> PermitGroupBatchRequest:
    """Capture one canonical payload once for stable sizing and transport."""
    payload = build_group_payload(
        group=group,
        candidates=candidates,
        conditions=conditions,
        executions=executions,
    )
    payload = compact_equivalent_candidates(payload)
    return PermitGroupBatchRequest(
        group_id=str(getattr(group, "group_id", "") or ""),
        payload=payload,
        serialized_payload=json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def partition_group_batch_requests(
    requests: Sequence[PermitGroupBatchRequest],
    *,
    max_groups: int = MAX_GROUPS_PER_BATCH,
    max_prompt_chars: int = MAX_BATCH_PROMPT_CHARS,
) -> tuple[tuple[PermitGroupBatchRequest, ...], ...]:
    """Greedily preserve input order while bounding the exact final prompt."""
    if max_groups < 1:
        raise ValueError("max_groups must be at least one")
    if max_prompt_chars < 1:
        raise ValueError("max_prompt_chars must be at least one")

    batches: list[tuple[PermitGroupBatchRequest, ...]] = []
    current: list[PermitGroupBatchRequest] = []
    for request in requests:
        if len(build_group_batch_prompt((request,))) > max_prompt_chars:
            raise ValueError("oversized permit group must be resolved before batching")
        candidate = (*current, request)
        within_count = len(candidate) <= max_groups
        within_prompt = len(build_group_batch_prompt(candidate)) <= max_prompt_chars
        if current and (not within_count or not within_prompt):
            batches.append(tuple(current))
            current = [request]
        else:
            current.append(request)
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def _batch_error(
    requests: Sequence[PermitGroupBatchRequest],
    error_code: str,
) -> dict[str, IndexedBatchItem]:
    return {
        request.group_id: IndexedBatchItem(response=None, error_code=error_code)
        for request in requests
    }


def index_group_batch_response(
    requests: Sequence[PermitGroupBatchRequest],
    response: GroundedGroupBatchResponse | None,
) -> dict[str, IndexedBatchItem]:
    """Validate envelope framing globally, then isolate attributable item faults."""
    requested_ids = [request.group_id for request in requests]
    if response is None:
        return _batch_error(requests, "llm_batch_response_unavailable")
    if (
        any(not group_id for group_id in requested_ids)
        or len(set(requested_ids)) != len(requested_ids)
    ):
        return _batch_error(requests, "llm_batch_framing_invalid")

    requested_set = set(requested_ids)
    item_ids = [item.group_id for item in response.items]
    if any(
        not isinstance(group_id, str)
        or not group_id
        or group_id not in requested_set
        for group_id in item_ids
    ):
        return _batch_error(requests, "llm_batch_framing_invalid")

    by_group: dict[str, list[Any]] = {group_id: [] for group_id in requested_ids}
    for item in response.items:
        by_group[item.group_id].append(item)

    indexed: dict[str, IndexedBatchItem] = {}
    for group_id in requested_ids:
        matches = by_group[group_id]
        if not matches:
            indexed[group_id] = IndexedBatchItem(
                response=None,
                error_code="llm_batch_group_id_missing",
            )
            continue
        if len(matches) > 1:
            indexed[group_id] = IndexedBatchItem(
                response=None,
                error_code="llm_batch_group_id_duplicate",
            )
            continue
        try:
            strict_item = GroundedGroupBatchItem.model_validate(
                matches[0].model_dump()
            )
            strict_response = GroundedGroupResponse.model_validate(
                strict_item.model_dump(exclude={"group_id"})
            )
        except ValidationError:
            indexed[group_id] = IndexedBatchItem(
                response=None,
                error_code="llm_batch_item_invalid",
            )
            continue
        indexed[group_id] = IndexedBatchItem(
            response=strict_response,
            error_code=None,
        )
    return indexed
