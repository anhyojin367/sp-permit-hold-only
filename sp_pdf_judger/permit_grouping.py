from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Iterable, Literal, Mapping, Sequence

from .schemas import ExtractedRecord


OrderIndex = int | float
BatchState = Literal["explicit", "missing", "conflict", "material_lot"]


@dataclass(frozen=True)
class BatchCandidate:
    value: str
    stage_key: str
    record_order_idx: OrderIndex
    source_kind: str
    quote: str
    page_start: int
    page_end: int


@dataclass(frozen=True)
class BatchEvidence:
    state: BatchState
    batch: str | None
    stage_key: str
    record_order_idxs: tuple[OrderIndex, ...]
    candidates: tuple[BatchCandidate, ...]


@dataclass(frozen=True)
class SPFact:
    fact_id: str
    record_order_idx: OrderIndex
    row_ref: str
    field: str
    value: str
    quote: str
    page_start: int
    page_end: int
    source_kind: str
    metric_key: str = ""
    numeric_value: Decimal | None = None
    unit: str | None = None
    method_key: str | None = None
    qualifier: str | None = None


@dataclass(frozen=True)
class PermitGroup:
    group_id: str
    stage_key: str
    test_key: str
    batch: str | None
    method_key: str | None
    record_order_idxs: tuple[OrderIndex, ...]
    facts: tuple[SPFact, ...]
    candidate_evidence_ids: tuple[str, ...]
    blocker_code: str | None
    batch_evidence: BatchEvidence
    compatible_test_keys: tuple[str, ...] = ()
    record_positions: tuple[int, ...] = ()


_PRIMARY_LABEL_PATTERN = r"(?:제조\s*번호|배치\s*번호|로트\s*번호|lot\s*no\.?)"
_PRIMARY_LINE_RE = re.compile(
    rf"^\s*(?P<label>{_PRIMARY_LABEL_PATTERN})(?:\s*[:：=|]\s*|\s+)(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
_ANY_BATCH_LABEL_RE = re.compile(_PRIMARY_LABEL_PATTERN, re.IGNORECASE)
_LEADING_SECTION_RE = re.compile(
    r"^\s*(?:(?:\d+(?:\.\d+)*\.?|[①②③④⑤⑥⑦⑧⑨⑩])\s*)+"
)
_BATCH_TOKEN_RE = re.compile(r"^[0-9A-Z가-힣][0-9A-Z가-힣._/-]{0,127}$")
_EMPTY_BATCH_VALUES = {
    "",
    "-",
    "--",
    "N/A",
    "NA",
    "NONE",
    "미기재",
    "미확인",
    "해당없음",
    "없음",
}
_GENERIC_STAGES = {"", "시험", "정보", "기준", "시험기준", "시험정보", "결과"}
_GENERIC_TESTS = {"", "시험", "검사", "항목", "시험항목"}
_FACT_FIELDS = (
    "criteria",
    "result",
    "method",
    "test_date",
    "test_period",
    "remarks",
    "raw_text",
)


def _nfkc(value: Any) -> str:
    return unicodedata.normalize("NFKC", "" if value is None else str(value))


def _stable_hash(payload: Any) -> str:
    def normalize(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                _nfkc(key): normalize(item)
                for key, item in sorted(value.items(), key=lambda pair: _nfkc(pair[0]))
            }
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, str):
            return _nfkc(value)
        return value

    encoded = json.dumps(
        normalize(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _order_idx(record: ExtractedRecord, fallback: int = 0) -> OrderIndex:
    value = getattr(record, "order_idx", fallback)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return int(parsed) if parsed.is_integer() else parsed


def _normalize_stage_key(value: Any) -> str:
    text = _LEADING_SECTION_RE.sub("", _nfkc(value).strip()).casefold()
    previous = None
    while text != previous:
        previous = text
        text = re.sub(r"(?:에\s*대한\s*시험|대한\s*시험|시험|정보)\s*$", "", text)
        text = text.strip()
    text = re.sub(r"[^0-9a-z가-힣]+", "", text)
    return "" if text in _GENERIC_STAGES else text


def _stage_key(record: ExtractedRecord) -> str:
    direct = _normalize_stage_key(getattr(record, "section_title", None))
    if direct:
        return direct
    for item in reversed(getattr(record, "section_path", None) or []):
        if isinstance(item, str):
            candidates = [item]
        elif isinstance(item, Mapping):
            candidates = [
                item.get(name)
                for name in ("title", "section_title", "name", "label")
                if item.get(name)
            ]
        else:
            candidates = []
        for candidate in candidates:
            normalized = _normalize_stage_key(candidate)
            if normalized:
                return normalized
    return ""


def _normalize_test_key(value: Any) -> str:
    text = _LEADING_SECTION_RE.sub("", _nfkc(value).strip()).casefold()
    text = re.sub(r"[^0-9a-z가-힣]+", "", text)
    return "" if text in _GENERIC_TESTS else text


def _normalize_method_key(value: Any) -> str | None:
    text = re.sub(r"\s+", " ", _nfkc(value).strip()).casefold()
    return text or None


def _normalize_batch_value(value: Any) -> str | None:
    text = _nfkc(value).strip(" \t\r\n:：=|;,，。'\"[](){}")
    text = re.sub(r"[‐‑‒–—−]", "-", text)
    text = re.sub(r"\s*-\s*", "-", text)
    if re.search(r"\s", text):
        return None
    text = text.upper()
    if text in _EMPTY_BATCH_VALUES or not _BATCH_TOKEN_RE.fullmatch(text):
        return None
    return text


def _record_text_lines(record: ExtractedRecord) -> Iterable[tuple[str, str]]:
    seen: set[str] = set()
    for field in ("content", "raw_text", "remarks", "normalized_text"):
        value = getattr(record, field, None)
        if not value:
            continue
        for raw_line in _nfkc(value).splitlines():
            line = raw_line.strip()
            identity = re.sub(r"\s+", " ", line)
            if not identity or identity in seen:
                continue
            seen.add(identity)
            yield field, line


def _candidate(
    record: ExtractedRecord,
    *,
    value: str,
    stage_key: str,
    source_kind: str,
    quote: str,
) -> BatchCandidate:
    return BatchCandidate(
        value=value,
        stage_key=stage_key,
        record_order_idx=_order_idx(record),
        source_kind=source_kind,
        quote=_nfkc(quote).strip(),
        page_start=int(getattr(record, "page_start", 0) or 0),
        page_end=int(getattr(record, "page_end", 0) or 0),
    )


def _dedupe_candidates(candidates: Iterable[BatchCandidate]) -> tuple[BatchCandidate, ...]:
    result: list[BatchCandidate] = []
    seen: set[tuple[Any, ...]] = set()
    for candidate in candidates:
        identity = (
            candidate.value,
            candidate.stage_key,
            candidate.record_order_idx,
            candidate.quote,
            candidate.page_start,
            candidate.page_end,
        )
        if identity in seen:
            continue
        seen.add(identity)
        result.append(candidate)
    return tuple(result)


def _structured_batch_candidate(
    record: ExtractedRecord,
    stage_key: str,
) -> BatchCandidate | None:
    evidence = getattr(record, "batch_evidence", None)
    if not isinstance(evidence, Mapping):
        return None
    source_kind = _nfkc(evidence.get("source_kind")).strip()
    if not (source_kind == "explicit_table_column" or source_kind.startswith("explicit_")):
        return None
    label = _nfkc(evidence.get("label")).strip()
    if label:
        compact_label = re.sub(r"\s+", "", label).casefold().rstrip(".")
        if compact_label not in {"제조번호", "배치번호", "로트번호", "lotno"}:
            return None
    value = _normalize_batch_value(getattr(record, "batch_id", None))
    if value is None:
        return None
    quote = evidence.get("quote") or f"{label or 'batch_id'}: {value}"
    return _candidate(
        record,
        value=value,
        stage_key=stage_key,
        source_kind=source_kind,
        quote=_nfkc(quote),
    )


def _primary_batch_candidates(
    record: ExtractedRecord,
    stage_key: str,
) -> tuple[BatchCandidate, ...]:
    candidates: list[BatchCandidate] = []
    structured = _structured_batch_candidate(record, stage_key)
    if structured is not None:
        candidates.append(structured)
    for field, line in _record_text_lines(record):
        match = _PRIMARY_LINE_RE.fullmatch(line)
        if match is None:
            continue
        value = _normalize_batch_value(match.group("value"))
        if value is None:
            continue
        candidates.append(
            _candidate(
                record,
                value=value,
                stage_key=stage_key,
                source_kind=f"exact_{field}_label",
                quote=line,
            )
        )
    return _dedupe_candidates(candidates)


def _qualified_batch_value(line: str, match: re.Match[str]) -> str | None:
    remainder = line[match.end():].lstrip(" \t:：=|")
    value = _normalize_batch_value(remainder)
    if value is not None:
        return value
    token_match = re.match(r"[0-9A-Za-z가-힣][0-9A-Za-z가-힣._/‐‑‒–—−-]*", remainder)
    return _normalize_batch_value(token_match.group(0)) if token_match else None


def _material_batch_candidates(
    record: ExtractedRecord,
    stage_key: str,
) -> tuple[BatchCandidate, ...]:
    candidates: list[BatchCandidate] = []
    for field, line in _record_text_lines(record):
        if _PRIMARY_LINE_RE.fullmatch(line):
            continue
        for match in _ANY_BATCH_LABEL_RE.finditer(line):
            value = _qualified_batch_value(line, match)
            if value is None:
                continue
            candidates.append(
                _candidate(
                    record,
                    value=value,
                    stage_key=stage_key,
                    source_kind=f"qualified_{field}_lot",
                    quote=line,
                )
            )
    for control in getattr(record, "controls", None) or []:
        if not isinstance(control, Mapping):
            continue
        value = _normalize_batch_value(control.get("lot_no"))
        if value is None:
            continue
        candidates.append(
            _candidate(
                record,
                value=value,
                stage_key=stage_key,
                source_kind="control_lot",
                quote=f"controls.lot_no: {control.get('lot_no')}",
            )
        )
    return _dedupe_candidates(candidates)


def _evidence_from_candidates(
    *,
    stage_key: str,
    primary: Sequence[BatchCandidate] = (),
    material: Sequence[BatchCandidate] = (),
) -> BatchEvidence:
    primary_candidates = _dedupe_candidates(primary)
    material_candidates = _dedupe_candidates(material)
    distinct = tuple(dict.fromkeys(candidate.value for candidate in primary_candidates))
    if len(distinct) == 1:
        state: BatchState = "explicit"
        batch = distinct[0]
        selected = primary_candidates
    elif len(distinct) > 1:
        state = "conflict"
        batch = None
        selected = primary_candidates
    elif material_candidates:
        state = "material_lot"
        batch = None
        selected = material_candidates
    else:
        state = "missing"
        batch = None
        selected = ()
    return BatchEvidence(
        state=state,
        batch=batch,
        stage_key=stage_key,
        record_order_idxs=tuple(
            dict.fromkeys(candidate.record_order_idx for candidate in selected)
        ),
        candidates=tuple(selected),
    )


def build_stage_batch_inventory(
    records: Sequence[ExtractedRecord],
) -> Mapping[str, BatchEvidence]:
    """Collect explicit batches by exact normalized information-stage identity."""
    primary_by_stage: OrderedDict[str, list[BatchCandidate]] = OrderedDict()
    material_by_stage: OrderedDict[str, list[BatchCandidate]] = OrderedDict()
    for record in records:
        record_type = _nfkc(getattr(record, "record_type", "")).strip().casefold()
        if record_type not in {"content", "info", "information"}:
            continue
        stage = _stage_key(record)
        if not stage:
            continue
        primary = _primary_batch_candidates(record, stage)
        material = _material_batch_candidates(record, stage)
        if primary:
            primary_by_stage.setdefault(stage, []).extend(primary)
        if material:
            material_by_stage.setdefault(stage, []).extend(material)

    inventory: OrderedDict[str, BatchEvidence] = OrderedDict()
    for stage in dict.fromkeys((*primary_by_stage.keys(), *material_by_stage.keys())):
        inventory[stage] = _evidence_from_candidates(
            stage_key=stage,
            primary=primary_by_stage.get(stage, ()),
            material=material_by_stage.get(stage, ()),
        )
    return inventory


def _record_batch_evidence(
    record: ExtractedRecord,
    *,
    stage_key: str,
    inventory: Mapping[str, BatchEvidence],
) -> BatchEvidence:
    direct = _primary_batch_candidates(record, stage_key)
    if direct:
        return _evidence_from_candidates(stage_key=stage_key, primary=direct)
    inherited = inventory.get(stage_key)
    if inherited is not None and inherited.state != "missing":
        return inherited
    material = _material_batch_candidates(record, stage_key)
    return _evidence_from_candidates(stage_key=stage_key, material=material)


def resolve_record_batch_evidence(record, *, inventory):
    """Resolve explicit lot context without grouping records or reusing verdicts."""
    return _record_batch_evidence(record, stage_key=_stage_key(record), inventory=inventory)


def _record_facts(
    record: ExtractedRecord,
    *,
    record_position: int,
    method_key: str | None,
) -> tuple[SPFact, ...]:
    order = _order_idx(record, record_position)
    subtest = getattr(record, "subtest_order", None)
    row_ref = f"record:{order}:position:{record_position}"
    if subtest not in (None, ""):
        row_ref += f":subtest:{_nfkc(subtest)}"
    facts: list[SPFact] = []
    for field in _FACT_FIELDS:
        raw_value = getattr(record, field, None)
        if raw_value in (None, ""):
            continue
        value = _nfkc(raw_value)
        facts.append(
            SPFact(
                fact_id=_stable_hash(
                    {
                        "kind": "sp_fact",
                        "record_position": record_position,
                        "record_order_idx": order,
                        "row_ref": row_ref,
                        "field": field,
                        "value": value,
                        "page_start": int(getattr(record, "page_start", 0) or 0),
                        "page_end": int(getattr(record, "page_end", 0) or 0),
                    }
                ),
                record_order_idx=order,
                row_ref=row_ref,
                field=field,
                value=value,
                quote=value,
                page_start=int(getattr(record, "page_start", 0) or 0),
                page_end=int(getattr(record, "page_end", 0) or 0),
                source_kind="sp_record_field",
                method_key=method_key,
            )
        )
    return tuple(facts)


def _candidate_identity(
    permit_store: Any,
    record: ExtractedRecord,
) -> tuple[str, ...]:
    identity_record = replace(
        record,
        section_number=None,
        content=None,
        criteria=None,
        result=None,
        test_date=None,
        test_period=None,
        remarks=None,
        raw_text=None,
        normalized_text=None,
        result_table=None,
        tables=[],
        controls=[],
    )
    candidate_limit = max(12, len(getattr(permit_store, "chunks", ()) or ()))
    candidates = permit_store.search(identity_record, top_k=candidate_limit)
    identities = {
        _nfkc(getattr(candidate, "evidence_id", "")).strip()
        for candidate in candidates
    }
    identities.discard("")
    return tuple(sorted(identities))


def _blocker_code(
    *,
    stage_key: str,
    test_key: str,
    batch_evidence: BatchEvidence,
) -> str | None:
    if not stage_key:
        return "stage_missing"
    if not test_key:
        return "test_missing"
    return {
        "missing": "batch_missing",
        "conflict": "batch_conflict",
        "material_lot": "material_lot",
    }.get(batch_evidence.state)


def _compatible_test_keys(permit_store: Any, test_key: str) -> tuple[str, ...]:
    keys = {_normalize_test_key(test_key)}
    aliases = getattr(permit_store, "search_aliases", {})
    if isinstance(aliases, Mapping):
        for alias in aliases.get(_normalize_test_key(test_key), ()) or ():
            keys.add(_normalize_test_key(alias))
    keys.discard("")
    return tuple(sorted(keys))


def _combine_batch_evidence(
    evidence_items: Sequence[BatchEvidence],
    *,
    stage_key: str,
) -> BatchEvidence:
    if not evidence_items:
        return _evidence_from_candidates(stage_key=stage_key)
    states = {item.state for item in evidence_items}
    primary = [
        candidate
        for item in evidence_items
        if item.state in {"explicit", "conflict"}
        for candidate in item.candidates
    ]
    material = [
        candidate
        for item in evidence_items
        if item.state == "material_lot"
        for candidate in item.candidates
    ]
    combined = _evidence_from_candidates(
        stage_key=stage_key,
        primary=primary,
        material=material,
    )
    if len(states) == 1:
        return combined
    return BatchEvidence(
        state="conflict",
        batch=None,
        stage_key=stage_key,
        record_order_idxs=combined.record_order_idxs,
        candidates=combined.candidates,
    )


def build_permit_groups(
    records: Sequence[ExtractedRecord],
    permit_store: Any,
) -> tuple[tuple[PermitGroup, ...], list[dict[str, Any]]]:
    """Build conservative groups without mutating or merging source records."""
    inventory = build_stage_batch_inventory(records)
    buckets: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()

    for position, record in enumerate(records):
        if _nfkc(getattr(record, "record_type", "")).strip().casefold() != "test":
            continue
        stage = _stage_key(record)
        test = _normalize_test_key(getattr(record, "test_name", None))
        method = _normalize_method_key(getattr(record, "method", None))
        compatible_tests = _compatible_test_keys(permit_store, test)
        evidence = _record_batch_evidence(
            record,
            stage_key=stage,
            inventory=inventory,
        )
        blocker = _blocker_code(
            stage_key=stage,
            test_key=test,
            batch_evidence=evidence,
        )
        candidate_ids: tuple[str, ...] = ()
        if blocker is None:
            candidate_ids = _candidate_identity(permit_store, record)
        order = _order_idx(record, position)
        if blocker is None and candidate_ids:
            key = ("eligible", stage, test, evidence.batch, method, candidate_ids)
        elif blocker is None:
            # A genuine permit miss preserves the existing per-record SP
            # verdict.  Keep such rows separate so one row can never supply
            # another row's primary result.
            key = ("permit_not_found", position, order)
        else:
            key = ("blocked", position, order, blocker)
        bucket = buckets.setdefault(
            key,
            {
                "stage": stage,
                "test": test,
                "batch": evidence.batch,
                "method": method,
                "candidate_ids": candidate_ids,
                "compatible_tests": compatible_tests,
                "blocker": blocker,
                "orders": [],
                "positions": [],
                "facts": [],
                "batch_evidence": [],
            },
        )
        bucket["orders"].append(order)
        bucket["positions"].append(position)
        bucket["facts"].extend(
            _record_facts(record, record_position=position, method_key=method)
        )
        bucket["batch_evidence"].append(evidence)

    groups: list[PermitGroup] = []
    audit: list[dict[str, Any]] = []
    for bucket in buckets.values():
        combined_evidence = _combine_batch_evidence(
            bucket["batch_evidence"],
            stage_key=bucket["stage"],
        )
        group_id = _stable_hash(
            {
                "kind": "permit_group",
                "stage_key": bucket["stage"],
                "test_key": bucket["test"],
                "batch": bucket["batch"],
                "method_key": bucket["method"],
                "candidate_evidence_ids": bucket["candidate_ids"],
                "compatible_test_keys": bucket["compatible_tests"],
                "blocker_code": bucket["blocker"],
                "record_positions": bucket["positions"],
                "record_order_idxs": bucket["orders"],
            }
        )
        group = PermitGroup(
            group_id=group_id,
            stage_key=bucket["stage"],
            test_key=bucket["test"],
            batch=bucket["batch"],
            method_key=bucket["method"],
            record_order_idxs=tuple(bucket["orders"]),
            facts=tuple(bucket["facts"]),
            candidate_evidence_ids=tuple(bucket["candidate_ids"]),
            blocker_code=bucket["blocker"],
            batch_evidence=combined_evidence,
            compatible_test_keys=tuple(bucket["compatible_tests"]),
            record_positions=tuple(bucket["positions"]),
        )
        groups.append(group)
        audit.append(
            {
                "group_id": group.group_id,
                "stage_key": group.stage_key,
                "test_key": group.test_key,
                "batch": group.batch,
                "method_key": group.method_key,
                "record_positions": list(bucket["positions"]),
                "record_order_idxs": list(group.record_order_idxs),
                "candidate_evidence_ids": list(group.candidate_evidence_ids),
                "compatible_test_keys": list(group.compatible_test_keys),
                "blocker_code": group.blocker_code,
                "batch_state": group.batch_evidence.state,
                "fact_ids": [fact.fact_id for fact in group.facts],
            }
        )
    return tuple(groups), audit
