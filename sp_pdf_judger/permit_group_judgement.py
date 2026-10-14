"""Deterministic execution and grounded validation for grouped permit tests.

This module deliberately has no dependency on the legacy judgement engine.  It
only consumes immutable permit conditions and SP facts, and it never treats
criteria or duplicated raw document text as an observed result.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Iterable, Literal, Sequence

from .config import FAIL_LABEL, HOLD_LABEL, PASS_LABEL
from .permit_grouping import (
    PermitGroup,
    SPFact,
    _normalize_stage_key,
    _normalize_test_key,
)
from .permit_models import PermitChunk, PermitCondition
from .utils import clean_text


ExecutionStatus = Literal["pass", "fail", "hold"]
GatePath = Literal["deterministic", "ambiguous_llm", "hold_without_llm"]


@dataclass(frozen=True)
class ConditionExecution:
    condition_id: str
    status: ExecutionStatus
    fact_ids: tuple[str, ...]
    reason_code: str


@dataclass(frozen=True)
class GateDecision:
    path: GatePath
    reason_code: str


@dataclass(frozen=True)
class GroupVerdict:
    status: str
    reason: str
    source: str
    group_id: str
    gate_path: GatePath
    condition_ids: tuple[str, ...]
    fact_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class PreparedPermitGroup:
    """All trusted inputs needed to resolve one group after provider batching."""

    group: PermitGroup
    candidates: tuple[PermitChunk, ...]
    conditions: tuple[PermitCondition, ...]
    executions: tuple[ConditionExecution, ...]
    gate: GateDecision
    immediate_verdict: GroupVerdict | None


@dataclass(frozen=True)
class _Observation:
    """One consumable occurrence, anchored to exactly one source fact."""

    occurrence_id: str
    fact_id: str
    metric_key: str
    numeric_value: Decimal | None
    numeric_operator: str | None
    unit: str | None
    method_key: str | None
    qualifier: str | None


@dataclass(frozen=True)
class _NumericInterval:
    lower: Decimal | None
    upper: Decimal | None
    lower_inclusive: bool
    upper_inclusive: bool


_OBSERVATION_FIELDS = frozenset({"result", "test_period"})
_TIME_SCALES = {
    "s": Decimal(1),
    "sec": Decimal(1),
    "second": Decimal(1),
    "seconds": Decimal(1),
    "초": Decimal(1),
    "min": Decimal(60),
    "minute": Decimal(60),
    "minutes": Decimal(60),
    "분": Decimal(60),
    "h": Decimal(3600),
    "hr": Decimal(3600),
    "hour": Decimal(3600),
    "hours": Decimal(3600),
    "시간": Decimal(3600),
    "d": Decimal(86400),
    "day": Decimal(86400),
    "days": Decimal(86400),
    "일": Decimal(86400),
}
_UNIT_ALIASES = {
    "%": "%",
    "percent": "%",
    "퍼센트": "%",
    "count": "count",
    "개": "count",
    "ea": "count",
    "℃": "℃",
    "°c": "℃",
    "degc": "℃",
    "celsius": "℃",
    "ml": "mL",
    "l": "L",
    "mg": "mg",
    "g": "g",
    "kg": "kg",
    "μg": "μg",
    "ug": "μg",
    "ng": "ng",
    "mg/ml": "mg/mL",
    "μg/ml": "μg/mL",
    "ug/ml": "μg/mL",
    "ng/ml": "ng/mL",
    "eu/ml": "EU/mL",
    "iu/ml": "IU/mL",
    "cfu/ml": "CFU/mL",
    "cells/ml": "cells/mL",
    "cell/ml": "cells/mL",
}
_NUMBER = r"[+-]?\d[\d,]*(?:\.\d+)?"
_RESULT_QUANTITY = re.compile(
    rf"(?P<number>{_NUMBER})\s*(?P<unit>℃|°\s*[cC]|%|％|개|일|시간|분|초|"
    r"days?|hours?|minutes?|seconds?|d|hr|h|min|sec|s|"
    r"(?:[A-Za-zμµ]+(?:\s*/\s*[A-Za-zμµ0-9]+)?))",
    re.IGNORECASE,
)
_RESULT_PREFIX_OPERATOR = re.compile(
    r"(?P<operator>최소|최대|>=|<=|>|<|≥|≤)\s*$"
)
_RESULT_SUFFIX_OPERATOR = re.compile(
    r"^\s*(?P<operator>이상|이하|초과|미만)"
)
_RESULT_OPERATOR_ALIASES = {
    "최소": ">=",
    "이상": ">=",
    ">=": ">=",
    "≥": ">=",
    "최대": "<=",
    "이하": "<=",
    "<=": "<=",
    "≤": "<=",
    "초과": ">",
    ">": ">",
    "미만": "<",
    "<": "<",
}
_DATE_TOKEN = re.compile(
    r"(?P<year>20\d{2})\s*(?P<date_sep>[./-]|년)\s*"
    r"(?P<month>\d{1,2})\s*(?P<month_sep>[./-]|월)\s*"
    r"(?P<day>\d{1,2})\s*일?"
    r"(?:\s*(?:T|\s)\s*(?P<hour>\d{1,2})\s*(?::|시)\s*"
    r"(?P<minute>\d{1,2})(?:\s*(?::|분)\s*(?P<second>\d{1,2})\s*초?)?)?"
)


def _nfkc(value: Any) -> str:
    return unicodedata.normalize("NFKC", "" if value is None else str(value))


def _compact(value: Any) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", _nfkc(value).casefold())


def _method(value: Any) -> str | None:
    normalized = _compact(value)
    return normalized or None


def _metric(value: Any) -> str:
    return _nfkc(value).strip().casefold()


def _decimal(value: Any) -> Decimal | None:
    """Return a bounded exact decimal; binary floats are never accepted."""
    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str) and re.fullmatch(_NUMBER, value.strip()):
        try:
            result = Decimal(value.strip().replace(",", ""))
        except InvalidOperation:
            return None
    else:
        return None
    return result if result.is_finite() else None


def _canonical_unit(unit: Any) -> tuple[str | None, Decimal] | None:
    if unit is None or not _nfkc(unit).strip():
        return None, Decimal(1)
    text = _nfkc(unit).replace("µ", "μ").replace("％", "%")
    text = re.sub(r"\s+", "", text).casefold()
    if text in _TIME_SCALES:
        return "s", _TIME_SCALES[text]
    canonical = _UNIT_ALIASES.get(text)
    if canonical is not None:
        return canonical, Decimal(1)
    # No magnitude conversion is attempted for other units.  Normalization is
    # intentionally limited to compatibility glyphs and insignificant spaces.
    return text, Decimal(1)


def _quantity(value: Any, unit: Any) -> tuple[Decimal, str | None] | None:
    number = _decimal(value)
    normalized_unit = _canonical_unit(unit)
    if number is None or normalized_unit is None:
        return None
    canonical, scale = normalized_unit
    with localcontext() as context:
        context.prec = 50
        return number * scale, canonical


def _qualifier(value: Any) -> str | None:
    text = _compact(value)
    if not text:
        return None
    direct = {
        "absent": "absent",
        "없음": "absent",
        "무": "absent",
        "exists": "exists",
        "present": "exists",
        "있음": "exists",
        "존재": "exists",
        "negative": "negative",
        "음성": "negative",
        "positive": "positive",
        "양성": "positive",
        "notdetected": "not_detected",
        "불검출": "not_detected",
        "미검출": "not_detected",
        "detected": "detected",
        "검출": "detected",
        "fit": "fit",
        "적합": "fit",
        "일치": "fit",
        "unfit": "unfit",
        "부적합": "unfit",
        "불일치": "unfit",
    }
    if text in direct:
        return direct[text]
    # Result fields often include a label (for example, "판정: 음성").  These
    # anchors remain explicit; order prevents positive substrings in negatives.
    patterns = (
        (r"부적합|불일치|unfit", "unfit"),
        (r"검출되지않|불검출|미검출|notdetected", "not_detected"),
        (r"음성|negative", "negative"),
        (r"혈구응집반응(?:이)?없|반응없|없음|absent", "absent"),
        (r"양성|positive", "positive"),
        (r"검출됨|detected", "detected"),
        (r"존재함|있음|exists|present", "exists"),
        (r"적합|일치함|fit", "fit"),
    )
    for pattern, name in patterns:
        if re.search(pattern, text):
            return name
    return None


def _qualitative_metric(text: str, fallback: str = "") -> str:
    normalized = _compact(text)
    if "혈구응집" in normalized:
        return "haemagglutination"
    if "계대" in normalized:
        return "passage"
    return fallback or "qualitative_result"


def _complete_date_duration(text: str) -> Decimal | None:
    matches = list(_DATE_TOKEN.finditer(_nfkc(text)))
    if len(matches) != 2:
        return None
    remainder_parts: list[str] = []
    offset = 0
    for match in matches:
        remainder_parts.append(_nfkc(text)[offset:match.start()])
        offset = match.end()
    remainder_parts.append(_nfkc(text)[offset:])
    remainder = "".join(remainder_parts)
    if not re.search(r"~|～|–|—|-|부터|까지", remainder):
        return None
    if re.sub(r"[\s.,~～–—\-부터까지]", "", remainder):
        return None
    values: list[datetime] = []
    time_flags: list[bool] = []
    for match in matches:
        has_time = match.group("hour") is not None
        time_flags.append(has_time)
        date_separator = match.group("date_sep")
        month_separator = match.group("month_sep")
        if (
            (date_separator == "년" and month_separator != "월")
            or (date_separator != "년" and month_separator != date_separator)
        ):
            return None
        try:
            values.append(
                datetime(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                    int(match.group("hour") or 0),
                    int(match.group("minute") or 0),
                    int(match.group("second") or 0),
                )
            )
        except ValueError:
            return None
    if time_flags[0] != time_flags[1] or values[1] < values[0]:
        return None
    delta = values[1] - values[0]
    return Decimal(delta.days * 86400 + delta.seconds)


def _typed_observation(fact: SPFact) -> _Observation | None:
    metric = _metric(fact.metric_key)
    number = _decimal(fact.numeric_value)
    qualifier = _qualifier(fact.qualifier)
    if not metric or (number is None and qualifier is None):
        return None
    return _Observation(
        occurrence_id=f"{fact.fact_id}:typed",
        fact_id=fact.fact_id,
        metric_key=metric,
        numeric_value=number,
        numeric_operator="==" if number is not None else None,
        unit=fact.unit,
        method_key=_method(fact.method_key),
        qualifier=qualifier,
    )


def _raw_numeric_operator(text: str, match: re.Match[str]) -> str | None:
    """Return the assertion attached to one raw result quantity.

    A result such as ``<90%`` describes a set of possible observed values, not
    the exact boundary value 90.  Prefix and Korean suffix spellings are both
    supported.  Conflicting spellings stay indeterminate instead of silently
    selecting one of them.
    """
    prefix = _RESULT_PREFIX_OPERATOR.search(text[:match.start()])
    suffix = _RESULT_SUFFIX_OPERATOR.search(text[match.end():])
    operators = {
        _RESULT_OPERATOR_ALIASES[item.group("operator")]
        for item in (prefix, suffix)
        if item is not None
    }
    if not operators:
        return "=="
    if len(operators) == 1:
        return operators.pop()
    return None


def _raw_result_observations(fact: SPFact) -> tuple[_Observation, ...]:
    text = _nfkc(fact.value)
    compact_text = _compact(text)
    # A requirement copied into the result cell is not an observation.  This
    # guard is intentionally applied before numeric or qualitative extraction;
    # otherwise text such as "80% 이상이어야 한다" could prove itself.
    if re.search(r"이어야|되어야|없어야|않아야|하여야|해야", compact_text) or re.search(
        r"\b(?:must|shall|required)\b", text.casefold()
    ):
        return ()
    metric_hint = _metric(fact.metric_key)
    method = _method(fact.method_key)
    result: list[_Observation] = []

    qualifier = _qualifier(text)
    if qualifier is not None:
        result.append(
            _Observation(
                occurrence_id=f"{fact.fact_id}:qualitative:0",
                fact_id=fact.fact_id,
                metric_key=_qualitative_metric(text, metric_hint),
                numeric_value=None,
                numeric_operator=None,
                unit=None,
                method_key=method,
                qualifier=qualifier,
            )
        )

    for index, match in enumerate(_RESULT_QUANTITY.finditer(text)):
        prefix = text[max(0, match.start() - 3):match.start()]
        suffix = text[match.end():match.end() + 3]
        if re.search(r"[±~～] *$", prefix) or re.match(r"^\s*[±~～]", suffix):
            # A tolerance/range fragment is not a single observed value.
            continue
        number = _decimal(match.group("number"))
        if number is None:
            continue
        unit = match.group("unit")
        canonical = _canonical_unit(unit)
        if canonical is None:
            continue
        canonical_unit = canonical[0]
        if metric_hint:
            metric = metric_hint
        elif canonical_unit == "count":
            metric = "sample_count"
        elif canonical_unit == "℃":
            metric = "incubation_temperature"
        elif canonical_unit == "s":
            metric = "sample_age" if "유정란" in text[:match.start()] else "incubation_duration"
        elif canonical_unit == "%" and re.search(r"생존[률율]?", text):
            metric = "survival_rate"
        else:
            metric = "measurement"
        result.append(
            _Observation(
                occurrence_id=f"{fact.fact_id}:numeric:{index}",
                fact_id=fact.fact_id,
                metric_key=metric,
                numeric_value=number,
                numeric_operator=_raw_numeric_operator(text, match),
                unit=unit,
                method_key=method,
                qualifier=None,
            )
        )

    if not result and re.fullmatch(rf"\s*{_NUMBER}\s*", text):
        number = _decimal(text.strip())
        if number is not None:
            result.append(
                _Observation(
                    occurrence_id=f"{fact.fact_id}:scalar:0",
                    fact_id=fact.fact_id,
                    metric_key=metric_hint or "measurement",
                    numeric_value=number,
                    numeric_operator="==",
                    unit=None,
                    method_key=method,
                    qualifier=None,
                )
            )
    return tuple(result)


def _observations(facts: Sequence[SPFact]) -> tuple[_Observation, ...]:
    result: list[_Observation] = []
    for fact in facts:
        field = _nfkc(fact.field).strip().casefold()
        if field not in _OBSERVATION_FIELDS:
            continue
        raw_result = (
            _raw_result_observations(fact)
            if field == "result"
            else ()
        )
        typed = _typed_observation(fact)
        if typed is not None:
            raw_numeric = tuple(
                item for item in raw_result if item.numeric_value is not None
            )
            if typed.numeric_value is not None and (
                len(raw_numeric) > 1
                or any(item.numeric_operator != "==" for item in raw_numeric)
            ):
                # Structured extraction may keep only the first numeric scalar.
                # Preserve bounds and repeated measurements from the source cell.
                result.extend(raw_result)
                continue
            result.append(typed)
            continue
        if field in {"test_period", "test_date"}:
            duration = _complete_date_duration(fact.value)
            if duration is not None:
                result.append(
                    _Observation(
                        occurrence_id=f"{fact.fact_id}:date-duration:0",
                        fact_id=fact.fact_id,
                        metric_key="incubation_duration",
                        numeric_value=duration,
                        numeric_operator="==",
                        unit="s",
                        method_key=_method(fact.method_key),
                        qualifier=None,
                    )
                )
            continue
        result.extend(raw_result)
    return tuple(result)


def _condition_observations(
    condition: PermitCondition,
    observations: Sequence[_Observation],
    consumed: set[str],
) -> tuple[list[_Observation], str | None]:
    metric = _metric(condition.metric_key)
    relevant_all = [item for item in observations if item.metric_key == metric]
    relevant = [item for item in relevant_all if item.occurrence_id not in consumed]
    required_method = _method(condition.method_key)
    if required_method is None:
        return relevant, None
    scoped_all = [item for item in relevant_all if item.method_key == required_method]
    scoped = [item for item in relevant if item.method_key == required_method]
    if scoped:
        return scoped, None
    if scoped_all:
        return [], "fact_missing"
    if relevant_all:
        return [], "method_incompatible"
    return [], "fact_missing"


def _operator_interval(value: Decimal, operator: str | None) -> _NumericInterval | None:
    normalized = {"=": "==", "≥": ">=", "≤": "<="}.get(operator or "", operator)
    if normalized == "==":
        return _NumericInterval(value, value, True, True)
    if normalized == ">=":
        return _NumericInterval(value, None, True, False)
    if normalized == ">":
        return _NumericInterval(value, None, False, False)
    if normalized == "<=":
        return _NumericInterval(None, value, False, True)
    if normalized == "<":
        return _NumericInterval(None, value, False, False)
    return None


def _condition_interval(condition: PermitCondition) -> _NumericInterval | None:
    if condition.kind == "range":
        lower = _quantity(condition.lower, condition.unit) if condition.lower is not None else None
        upper = _quantity(condition.upper, condition.unit) if condition.upper is not None else None
        if lower is None or upper is None or lower[1] != upper[1]:
            return None
        return _NumericInterval(
            lower[0],
            upper[0],
            condition.lower_inclusive,
            condition.upper_inclusive,
        )
    expected = _quantity(condition.value, condition.unit) if condition.value is not None else None
    if expected is None:
        return None
    return _operator_interval(
        expected[0],
        condition.operator or ("==" if condition.kind == "exact" else None),
    )


def _interval_is_subset(inner: _NumericInterval, outer: _NumericInterval) -> bool:
    if outer.lower is not None:
        if inner.lower is None or inner.lower < outer.lower:
            return False
        if (
            inner.lower == outer.lower
            and inner.lower_inclusive
            and not outer.lower_inclusive
        ):
            return False
    if outer.upper is not None:
        if inner.upper is None or inner.upper > outer.upper:
            return False
        if (
            inner.upper == outer.upper
            and inner.upper_inclusive
            and not outer.upper_inclusive
        ):
            return False
    return True


def _intervals_are_disjoint(left: _NumericInterval, right: _NumericInterval) -> bool:
    if left.upper is not None and right.lower is not None:
        if left.upper < right.lower:
            return True
        if (
            left.upper == right.lower
            and not (left.upper_inclusive and right.lower_inclusive)
        ):
            return True
    if right.upper is not None and left.lower is not None:
        if right.upper < left.lower:
            return True
        if (
            right.upper == left.lower
            and not (right.upper_inclusive and left.lower_inclusive)
        ):
            return True
    return False


def _numeric_scope(condition: PermitCondition) -> tuple[str, str | None, str | None]:
    normalized_unit = _canonical_unit(condition.unit)
    return (
        _metric(condition.metric_key),
        _method(condition.method_key),
        normalized_unit[0] if normalized_unit is not None else None,
    )


def _numeric_execution(
    condition: PermitCondition,
    candidates: Sequence[_Observation],
    consumed: set[str],
    *,
    consume_all: bool = False,
) -> ConditionExecution:
    expected_unit = _canonical_unit(condition.unit)
    if expected_unit is None:
        return ConditionExecution(condition.condition_id, "hold", (), "condition_invalid")
    compatible: list[tuple[_Observation, Decimal]] = []
    saw_numeric = False
    for observation in candidates:
        if observation.numeric_value is None:
            continue
        saw_numeric = True
        actual = _quantity(observation.numeric_value, observation.unit)
        if actual is None or actual[1] != expected_unit[0]:
            continue
        compatible.append((observation, actual[0]))
    if not compatible:
        reason = "unit_incompatible" if saw_numeric else "fact_missing"
        return ConditionExecution(condition.condition_id, "hold", (), reason)

    required = _condition_interval(condition)
    if required is None:
        return ConditionExecution(condition.condition_id, "hold", (), "condition_invalid")

    selected = compatible if consume_all else compatible[:1]
    statuses: list[ExecutionStatus] = []
    for observation, actual in selected:
        observed = _operator_interval(actual, observation.numeric_operator)
        if observed is None:
            statuses.append("hold")
        elif _interval_is_subset(observed, required):
            statuses.append("pass")
        elif _intervals_are_disjoint(observed, required):
            statuses.append("fail")
        else:
            statuses.append("hold")
        consumed.add(observation.occurrence_id)

    if any(status == "fail" for status in statuses):
        status: ExecutionStatus = "fail"
        reason = "condition_violated"
    elif any(status == "hold" for status in statuses):
        status = "hold"
        reason = "condition_indeterminate"
    else:
        status = "pass"
        reason = "condition_satisfied"
    fact_ids = tuple(dict.fromkeys(observation.fact_id for observation, _ in selected))
    return ConditionExecution(
        condition.condition_id,
        status,
        fact_ids,
        reason,
    )


_QUALITATIVE_POLARITY = {
    "absent": "negative",
    "not_detected": "negative",
    "negative": "negative",
    "exists": "positive",
    "detected": "positive",
    "positive": "positive",
    "fit": "fit",
    "unfit": "unfit",
}


def _qualitative_execution(
    condition: PermitCondition,
    candidates: Sequence[_Observation],
    consumed: set[str],
) -> ConditionExecution:
    expected = _qualifier(condition.operator)
    if expected is None:
        return ConditionExecution(condition.condition_id, "hold", (), "operator_unsupported")
    available = [item for item in candidates if item.qualifier is not None]
    if not available:
        return ConditionExecution(condition.condition_id, "hold", (), "qualifier_missing")
    observation = available[0]
    consumed.add(observation.occurrence_id)
    actual = observation.qualifier or ""
    expected_polarity = _QUALITATIVE_POLARITY.get(expected)
    actual_polarity = _QUALITATIVE_POLARITY.get(actual)
    if expected_polarity is None or actual_polarity is None:
        return ConditionExecution(condition.condition_id, "hold", (), "qualifier_unsupported")
    passed = expected_polarity == actual_polarity
    return ConditionExecution(
        condition.condition_id,
        "pass" if passed else "fail",
        (observation.fact_id,),
        "condition_satisfied" if passed else "condition_violated",
    )


def _execute_member_conditions(
    conditions: Sequence[PermitCondition],
    facts: Sequence[SPFact],
) -> tuple[ConditionExecution, ...]:
    """Execute conditions against one member without borrowing observations."""
    condition_ids = [condition.condition_id for condition in conditions]
    if len(condition_ids) != len(set(condition_ids)):
        return tuple(
            ConditionExecution(
                condition.condition_id,
                "hold",
                (),
                "condition_duplicate",
            )
            for condition in conditions
        )

    by_id = {condition.condition_id: condition for condition in conditions}

    def compound_graph_valid(condition: PermitCondition, trail: frozenset[str]) -> bool:
        if condition.kind != "and":
            return True
        if (
            condition.operator != "and"
            or not condition.children
            or len(condition.children) != len(set(condition.children))
            or condition.condition_id in trail
        ):
            return False
        next_trail = trail | {condition.condition_id}
        for child_id in condition.children:
            child = by_id.get(child_id)
            if (
                child is None
                or child.evidence_id != condition.evidence_id
                or not compound_graph_valid(child, next_trail)
            ):
                return False
        return True

    invalid_compounds = {
        condition.condition_id
        for condition in conditions
        if condition.kind == "and" and not compound_graph_valid(condition, frozenset())
    }
    observations = _observations(facts)
    consumed: set[str] = set()
    atomic: dict[str, ConditionExecution] = {}
    remaining_numeric_scopes: dict[tuple[str, str | None, str | None], int] = {}
    for condition in conditions:
        if condition.kind not in {"exact", "compare", "range"}:
            continue
        scope = _numeric_scope(condition)
        remaining_numeric_scopes[scope] = remaining_numeric_scopes.get(scope, 0) + 1

    for condition in conditions:
        if condition.kind == "and":
            if condition.condition_id in invalid_compounds:
                atomic[condition.condition_id] = ConditionExecution(
                    condition.condition_id,
                    "hold",
                    (),
                    "compound_invalid",
                )
            continue
        if condition.kind == "unparsed":
            atomic[condition.condition_id] = ConditionExecution(
                condition.condition_id, "hold", (), "unparsed_condition"
            )
            continue
        candidates, scope_error = _condition_observations(condition, observations, consumed)
        if scope_error is not None:
            atomic[condition.condition_id] = ConditionExecution(
                condition.condition_id, "hold", (), scope_error
            )
            continue
        if condition.kind in {"exact", "compare", "range"}:
            scope = _numeric_scope(condition)
            remaining_numeric_scopes[scope] -= 1
            atomic[condition.condition_id] = _numeric_execution(
                condition,
                candidates,
                consumed,
                consume_all=remaining_numeric_scopes[scope] == 0,
            )
        elif condition.kind == "qualitative":
            atomic[condition.condition_id] = _qualitative_execution(condition, candidates, consumed)
        else:
            atomic[condition.condition_id] = ConditionExecution(
                condition.condition_id, "hold", (), "condition_unsupported"
            )

    resolving: set[str] = set()

    def resolve(condition_id: str) -> ConditionExecution:
        if condition_id in atomic:
            return atomic[condition_id]
        condition = by_id.get(condition_id)
        if condition is None or condition.kind != "and" or condition_id in resolving:
            return ConditionExecution(condition_id, "hold", (), "child_missing")
        resolving.add(condition_id)
        children = [resolve(child_id) for child_id in condition.children]
        resolving.discard(condition_id)
        if not children or any(child.reason_code == "child_missing" for child in children):
            status: ExecutionStatus = "hold"
            reason = "child_missing"
        elif any(child.status == "fail" for child in children):
            status = "fail"
            reason = "compound_violated"
        elif any(child.status == "hold" for child in children):
            status = "hold"
            reason = "compound_incomplete"
        else:
            status = "pass"
            reason = "compound_satisfied"
        fact_ids = tuple(dict.fromkeys(fact_id for child in children for fact_id in child.fact_ids))
        execution = ConditionExecution(condition_id, status, fact_ids, reason)
        atomic[condition_id] = execution
        return execution

    return tuple(resolve(condition.condition_id) for condition in conditions)


def execute_permit_conditions(
    conditions: Sequence[PermitCondition],
    facts: Sequence[SPFact],
) -> tuple[ConditionExecution, ...]:
    """Execute each SP member independently, then aggregate fail > hold > pass.

    ``row_ref`` is the stable member boundary emitted by permit grouping.  This
    prevents a complete result in one batch row from filling a missing result
    in another row.  A single-member call retains the same execution behavior.
    """
    members: dict[tuple[Any, str], list[SPFact]] = {}
    for fact in facts:
        key = (fact.record_order_idx, clean_text(fact.row_ref))
        members.setdefault(key, []).append(fact)
    if len(members) <= 1:
        return _execute_member_conditions(conditions, facts)

    per_member = tuple(
        _execute_member_conditions(conditions, tuple(member_facts))
        for member_facts in members.values()
    )
    aggregated: list[ConditionExecution] = []
    for index, condition in enumerate(conditions):
        executions = tuple(member[index] for member in per_member)
        if any(item.status == "fail" for item in executions):
            status: ExecutionStatus = "fail"
            reason = next(item.reason_code for item in executions if item.status == "fail")
        elif any(item.status == "hold" for item in executions):
            status = "hold"
            reason = next(item.reason_code for item in executions if item.status == "hold")
        else:
            status = "pass"
            reason = executions[0].reason_code
        fact_ids = tuple(
            dict.fromkeys(
                fact_id
                for execution in executions
                for fact_id in execution.fact_ids
            )
        )
        aggregated.append(
            ConditionExecution(condition.condition_id, status, fact_ids, reason)
        )
    return tuple(aggregated)


def _candidate_identity(candidate: PermitChunk) -> str:
    return clean_text(candidate.evidence_id)


def _compatible_candidate(group: PermitGroup, candidate: PermitChunk) -> bool:
    group_stage = _normalize_stage_key(group.stage_key)
    group_test = _normalize_test_key(group.test_key)
    candidate_test = _normalize_test_key(
        candidate.normalized_test_name or candidate.title
    )
    candidate_stages = tuple(
        dict.fromkeys(
            _normalize_stage_key(value)
            for value in (
                candidate.normalized_stage_path,
                *candidate.section_path_titles[:-1],
            )
            if _normalize_stage_key(value)
        )
    )
    stage_ok = bool(group_stage and candidate_stages) and any(
        stage == group_stage
        or stage.endswith(group_stage)
        for stage in candidate_stages
    )
    compatible_tests = {
        _normalize_test_key(value)
        for value in (group.compatible_test_keys or (group_test,))
        if _normalize_test_key(value)
    }
    return (
        stage_ok
        and bool(group_test and candidate_test)
        and candidate_test in compatible_tests
    )


def _compatible_condition(group: PermitGroup, condition: PermitCondition) -> bool:
    condition_stage = _normalize_stage_key(condition.stage_key)
    condition_test = _normalize_test_key(condition.test_key)
    group_stage = _normalize_stage_key(group.stage_key)
    group_test = _normalize_test_key(group.test_key)
    compatible_tests = {
        _normalize_test_key(value)
        for value in (group.compatible_test_keys or (group_test,))
        if _normalize_test_key(value)
    }
    return (
        bool(condition_stage and group_stage)
        and (
            condition_stage == group_stage
            or condition_stage.endswith(group_stage)
        )
        and bool(condition_test and group_test)
        and condition_test in compatible_tests
    )


def _eligible_facts(group: PermitGroup) -> tuple[SPFact, ...]:
    return tuple(
        fact
        for fact in group.facts
        if _nfkc(fact.field).strip().casefold() in _OBSERVATION_FIELDS
        and bool(clean_text(fact.value))
    )


def _fact_record_position(fact: SPFact) -> int | None:
    match = re.search(r"(?:^|:)position:(\d+)(?::|$)", clean_text(fact.row_ref))
    return int(match.group(1)) if match else None


def classify_group(
    group: PermitGroup,
    candidates: Sequence[PermitChunk],
    conditions: Sequence[PermitCondition],
    executions: Sequence[ConditionExecution],
) -> GateDecision:
    """Choose deterministic, one-shot semantic, or fail-closed handling."""
    if group.blocker_code:
        return GateDecision("hold_without_llm", group.blocker_code)
    if (
        group.batch is None
        or group.batch_evidence.state != "explicit"
        or group.batch_evidence.batch != group.batch
    ):
        reason = {
            "conflict": "batch_conflict",
            "material_lot": "material_lot",
        }.get(group.batch_evidence.state, "batch_missing")
        return GateDecision("hold_without_llm", reason)

    requested_ids = tuple(dict.fromkeys(clean_text(item) for item in group.candidate_evidence_ids if clean_text(item)))
    if not requested_ids:
        return GateDecision("hold_without_llm", "candidate_missing")
    supplied = {
        evidence_id: [
            candidate for candidate in candidates
            if _candidate_identity(candidate) == evidence_id
        ]
        for evidence_id in requested_ids
    }
    if any(len(items) != 1 for items in supplied.values()):
        return GateDecision("hold_without_llm", "candidate_missing")
    compatible = [supplied[item][0] for item in requested_ids if _compatible_candidate(group, supplied[item][0])]
    if len(compatible) != len(requested_ids):
        return GateDecision("hold_without_llm", "candidate_incompatible")
    if not _eligible_facts(group):
        return GateDecision("hold_without_llm", "fact_missing")
    eligible = _eligible_facts(group)
    if group.record_positions:
        eligible_member_positions = {
            position
            for fact in eligible
            if (position := _fact_record_position(fact)) is not None
        }
        if any(
            position not in eligible_member_positions
            for position in group.record_positions
        ):
            return GateDecision("hold_without_llm", "fact_missing")
    else:
        eligible_member_orders = {fact.record_order_idx for fact in eligible}
        if any(order not in eligible_member_orders for order in group.record_order_idxs):
            return GateDecision("hold_without_llm", "fact_missing")
    # Candidate failures are alternatives until the candidate is selected.  A
    # flattened union must never turn one rejected alternative into group FAIL.
    if len(compatible) > 1:
        return GateDecision("ambiguous_llm", "multiple_candidates")

    evidence_id = _candidate_identity(compatible[0])
    selected_conditions = tuple(
        condition for condition in conditions
        if condition.evidence_id == evidence_id and condition.mandatory
    )
    if not selected_conditions:
        return GateDecision("hold_without_llm", "condition_missing")
    if any(not _compatible_condition(group, condition) for condition in selected_conditions):
        return GateDecision("hold_without_llm", "condition_incompatible")
    execution_by_id = {item.condition_id: item for item in executions}
    selected_executions = [execution_by_id.get(item.condition_id) for item in selected_conditions]
    if any(item is None for item in selected_executions):
        return GateDecision("hold_without_llm", "condition_missing")
    typed_executions = [item for item in selected_executions if item is not None]
    if any(item.status == "fail" for item in typed_executions):
        return GateDecision("deterministic", "condition_failed")
    if all(item.status == "pass" for item in typed_executions):
        return GateDecision("deterministic", "all_conditions_pass")

    reasons = {item.reason_code for item in typed_executions if item.status == "hold"}
    if "method_incompatible" in reasons:
        return GateDecision("hold_without_llm", "method_incompatible")
    if "unit_incompatible" in reasons:
        return GateDecision("hold_without_llm", "unit_incompatible")
    if reasons & {"condition_invalid", "condition_unsupported", "operator_unsupported", "child_missing"}:
        return GateDecision("hold_without_llm", sorted(reasons)[0])
    if any(condition.kind == "unparsed" for condition in selected_conditions):
        return GateDecision("ambiguous_llm", "unparsed_condition")
    if any(
        condition.kind == "qualitative"
        and execution_by_id[condition.condition_id].status == "hold"
        for condition in selected_conditions
    ):
        return GateDecision("ambiguous_llm", "semantic_qualitative")
    return GateDecision("hold_without_llm", "fact_missing")


def _status_label(statuses: Iterable[ExecutionStatus]) -> str:
    statuses = tuple(statuses)
    if any(status == "fail" for status in statuses):
        return FAIL_LABEL
    if any(status == "hold" for status in statuses):
        return HOLD_LABEL
    return PASS_LABEL


def _source_quote_equal(left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    if left == right:
        return True
    if "\n" not in right and "\r" not in right:
        return False
    # Ignore PDF wrapping, not numbers, decimal precision, units or word tokens:
    # 6.00 != 6.0, mOsm != nmOsm, "1 0" != "10", "no positive" != "nopositive".
    token_pattern = r"\d+(?:[.,]\d+)*|[A-Za-z]+|[^\s]"
    return re.findall(token_pattern, left) == re.findall(token_pattern, right)


def _hold_verdict(
    group: PermitGroup,
    reason_code: str,
    *,
    conditions: Sequence[PermitCondition] = (),
    facts: Sequence[SPFact] = (),
    evidence_ids: Sequence[str] = (),
) -> GroupVerdict:
    return GroupVerdict(
        status=HOLD_LABEL,
        reason=f"허가서와 SP 근거 연결을 확인할 수 없어 검수보류입니다. ({reason_code})",
        source="permit_group_grounding",
        group_id=group.group_id,
        gate_path="hold_without_llm",
        condition_ids=tuple(condition.condition_id for condition in conditions),
        fact_ids=tuple(dict.fromkeys(fact.fact_id for fact in facts)),
        evidence_ids=tuple(dict.fromkeys(evidence_ids)),
    )


def _trusted_reason(
    status: str,
    conditions: Sequence[PermitCondition],
    statuses: dict[str, ExecutionStatus],
    fact_ids: Sequence[str],
    facts: Sequence[SPFact],
) -> str:
    if status == FAIL_LABEL:
        selected = [condition for condition in conditions if statuses.get(condition.condition_id) == "fail"]
        prefix = "허가서 필수 조건과 SP 시험결과가 일치하지 않아 검수불합격입니다."
    elif status == HOLD_LABEL:
        selected = [condition for condition in conditions if statuses.get(condition.condition_id) == "hold"]
        prefix = "허가서 필수 조건을 입증할 SP 결과가 부족하여 검수보류입니다."
    else:
        selected = list(conditions)
        prefix = "허가서 필수 조건과 SP 시험결과가 일치하여 검수합격입니다."
    quotes = tuple(dict.fromkeys(clean_text(item.quote) for item in selected if clean_text(item.quote)))
    fact_by_id = {fact.fact_id: fact for fact in facts}
    fact_quotes = tuple(
        dict.fromkeys(
            clean_text(fact_by_id[item].quote)
            for item in fact_ids
            if item in fact_by_id and clean_text(fact_by_id[item].quote)
        )
    )
    suffixes = []
    if quotes:
        suffixes.append("허가 기준: " + " / ".join(quotes))
    if fact_quotes:
        suffixes.append("SP 결과: " + " / ".join(fact_quotes))
    return " ".join((prefix, *suffixes))


def validate_group_response(
    response: Any,
    group: PermitGroup,
    candidates: Sequence[PermitChunk],
    conditions: Sequence[PermitCondition],
    executions: Sequence[ConditionExecution],
) -> GroupVerdict:
    """Validate a one-shot response, then recompute every structured result.

    ``executions`` is accepted as part of the public contract, but conditions
    are deliberately re-executed here so an earlier or provider-supplied status
    cannot override the source facts.
    """
    del executions
    if group.blocker_code:
        return _hold_verdict(group, group.blocker_code)
    if (
        group.batch is None
        or group.batch_evidence.state != "explicit"
        or group.batch_evidence.batch != group.batch
    ):
        return _hold_verdict(group, "batch_incompatible")

    selected_id = clean_text(getattr(response, "selected_evidence_id", ""))
    requested_ids = set(group.candidate_evidence_ids)
    selected_candidates = [
        candidate for candidate in candidates
        if _candidate_identity(candidate) == selected_id
    ]
    if (
        not selected_id
        or selected_id not in requested_ids
        or len(selected_candidates) != 1
        or not _compatible_candidate(group, selected_candidates[0])
    ):
        return _hold_verdict(group, "candidate_invalid")

    selected_conditions = tuple(
        condition for condition in conditions
        if condition.evidence_id == selected_id and condition.mandatory
    )
    if not selected_conditions:
        return _hold_verdict(group, "condition_missing", evidence_ids=(selected_id,))
    if any(not _compatible_condition(group, condition) for condition in selected_conditions):
        return _hold_verdict(group, "condition_incompatible", conditions=selected_conditions, evidence_ids=(selected_id,))
    expected_ids = [condition.condition_id for condition in selected_conditions]
    if len(expected_ids) != len(set(expected_ids)):
        return _hold_verdict(group, "condition_duplicate", conditions=selected_conditions, evidence_ids=(selected_id,))

    decisions = list(getattr(response, "decisions", ()) or ())
    received_ids = [clean_text(getattr(decision, "condition_id", "")) for decision in decisions]
    if len(received_ids) != len(set(received_ids)) or set(received_ids) != set(expected_ids):
        return _hold_verdict(group, "condition_coverage_invalid", conditions=selected_conditions, evidence_ids=(selected_id,))
    decision_by_id = {clean_text(getattr(item, "condition_id", "")): item for item in decisions}
    condition_by_id = {condition.condition_id: condition for condition in selected_conditions}
    known_facts = {fact.fact_id: fact for fact in group.facts}
    eligible_fact_ids = {fact.fact_id for fact in _eligible_facts(group)}
    recomputed = execute_permit_conditions(selected_conditions, group.facts)
    execution_by_id = {item.condition_id: item for item in recomputed}
    cited_ids: list[str] = []
    for condition_id in expected_ids:
        condition = condition_by_id[condition_id]
        decision = decision_by_id[condition_id]
        if not _source_quote_equal(getattr(decision, "quote", ""), condition.quote):
            return _hold_verdict(group, "quote_invalid", conditions=selected_conditions, evidence_ids=(selected_id,))
        decision_fact_ids = [clean_text(value) for value in (getattr(decision, "fact_ids", ()) or ())]
        if (
            len(decision_fact_ids) != len(set(decision_fact_ids))
            or any(item not in known_facts or item not in eligible_fact_ids for item in decision_fact_ids)
        ):
            return _hold_verdict(group, "fact_id_invalid", conditions=selected_conditions, evidence_ids=(selected_id,))
        required_method = _method(condition.method_key)
        if required_method is not None and any(
            _method(known_facts[item].method_key) != required_method
            for item in decision_fact_ids
        ):
            return _hold_verdict(group, "fact_method_invalid", conditions=selected_conditions, evidence_ids=(selected_id,))
        decision_status = clean_text(getattr(decision, "status", "")).casefold()
        if decision_status not in {"pass", "fail", "hold"}:
            return _hold_verdict(group, "decision_status_invalid", conditions=selected_conditions, evidence_ids=(selected_id,))
        if decision_status in {"pass", "fail"} and not decision_fact_ids:
            return _hold_verdict(group, "fact_id_missing", conditions=selected_conditions, evidence_ids=(selected_id,))
        execution = execution_by_id[condition_id]
        semantic_decision = condition.kind == "unparsed" or (
            condition.kind == "qualitative"
            and execution.status == "hold"
            and execution.reason_code in {"qualifier_missing", "fact_missing"}
        )
        if semantic_decision and decision_status in {"pass", "fail"}:
            if group.record_positions:
                cited_members = {
                    position
                    for item in decision_fact_ids
                    if (position := _fact_record_position(known_facts[item])) is not None
                }
                missing_member = any(
                    position not in cited_members for position in group.record_positions
                )
            else:
                cited_members = {
                    known_facts[item].record_order_idx for item in decision_fact_ids
                }
                missing_member = any(
                    order not in cited_members for order in group.record_order_idxs
                )
            if missing_member:
                return _hold_verdict(
                    group,
                    "member_fact_coverage_invalid",
                    conditions=selected_conditions,
                    evidence_ids=(selected_id,),
                )
        cited_ids.extend(decision_fact_ids)

    statuses: dict[str, ExecutionStatus] = {}
    semantic_ids: set[str] = set()
    for condition in selected_conditions:
        if condition.kind == "and":
            continue
        execution = execution_by_id[condition.condition_id]
        decision = decision_by_id[condition.condition_id]
        if condition.kind == "unparsed":
            statuses[condition.condition_id] = clean_text(decision.status).casefold()  # type: ignore[assignment]
            semantic_ids.add(condition.condition_id)
        elif condition.kind == "qualitative" and execution.status == "hold" and execution.reason_code in {
            "qualifier_missing", "fact_missing"
        }:
            statuses[condition.condition_id] = clean_text(decision.status).casefold()  # type: ignore[assignment]
            semantic_ids.add(condition.condition_id)
        else:
            statuses[condition.condition_id] = execution.status

    # Compound decisions are present for grounding, but their status is always
    # recomputed from children.  Nested AND nodes are resolved to a fixed point.
    unresolved = [condition for condition in selected_conditions if condition.kind == "and"]
    while unresolved:
        progressed = False
        for condition in tuple(unresolved):
            if not condition.children or not all(child in statuses for child in condition.children):
                continue
            child_statuses = [statuses[child] for child in condition.children]
            statuses[condition.condition_id] = (
                "fail" if "fail" in child_statuses
                else "hold" if "hold" in child_statuses
                else "pass"
            )
            unresolved.remove(condition)
            progressed = True
        if not progressed:
            return _hold_verdict(
                group,
                "compound_invalid",
                conditions=selected_conditions,
                facts=tuple(known_facts[item] for item in dict.fromkeys(cited_ids) if item in known_facts),
                evidence_ids=(selected_id,),
            )

    final_status = _status_label(statuses[condition.condition_id] for condition in selected_conditions)
    if clean_text(getattr(response, "status", "")) != final_status:
        return _hold_verdict(
            group,
            "top_status_mismatch",
            conditions=selected_conditions,
            facts=tuple(known_facts[item] for item in dict.fromkeys(cited_ids) if item in known_facts),
            evidence_ids=(selected_id,),
        )

    used_ids: list[str] = []
    for condition in selected_conditions:
        execution = execution_by_id[condition.condition_id]
        if condition.condition_id in semantic_ids:
            used_ids.extend(getattr(decision_by_id[condition.condition_id], "fact_ids", ()) or ())
        else:
            used_ids.extend(execution.fact_ids)
    used_ids = list(dict.fromkeys(clean_text(item) for item in used_ids if clean_text(item)))
    return GroupVerdict(
        status=final_status,
        reason=_trusted_reason(final_status, selected_conditions, statuses, used_ids, group.facts),
        source="permit_group_llm_grounded",
        group_id=group.group_id,
        gate_path="ambiguous_llm",
        condition_ids=tuple(expected_ids),
        fact_ids=tuple(used_ids),
        evidence_ids=(selected_id,),
    )


__all__ = [
    "ConditionExecution",
    "GateDecision",
    "GroupVerdict",
    "PreparedPermitGroup",
    "execute_permit_conditions",
    "classify_group",
    "validate_group_response",
]
