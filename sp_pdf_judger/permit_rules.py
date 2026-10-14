from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import FAIL_LABEL, PASS_LABEL
from .schemas import ExtractedRecord
from .utils import clean_text

try:
    import yaml
except ImportError:
    yaml = None


@dataclass
class PermitDecision:
    status: str | None = None
    reason: str | None = None
    comparator: str | None = None
    normalized_criteria: str | None = None
    normalized_result: str | None = None
    rule_id: str | None = None
    rule_title: str | None = None


def _permit_paths() -> list[Path]:
    package_dir = Path(__file__).resolve().parent
    project_root = package_dir.parent

    return [
        package_dir / "permits" / "permit_rules.yaml",
        project_root / "sp_pdf_judger" / "permits" / "permit_rules.yaml",
        project_root / "permits" / "permit_rules.yaml",
    ]


@lru_cache(maxsize=1)
def _load_permit_rules() -> list[dict[str, Any]]:
    if yaml is None:
        return []

    for path in _permit_paths():
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return data.get("rules", []) or []

    return []


def _norm(text: str | None) -> str:
    text = clean_text(text)
    text = text.replace("µ", "μ")
    text = text.replace("％", "%")
    return text


def _compact(text: str | None) -> str:
    text = _norm(text)
    text = text.casefold()
    text = text.replace("e.coli", "ecoli")
    text = text.replace("e.coli", "ecoli")
    text = text.replace("e.coli", "ecoli")
    text = text.replace("e.coLi".casefold(), "ecoli")
    text = re.sub(r"[\s\-_·.,:：/()]+", "", text)
    return text


def _contains_any(target: str | None, candidates: list[str] | None) -> bool:
    if not candidates:
        return True

    target_norm = _norm(target)
    target_compact = _compact(target)

    for cand in candidates:
        cand_norm = _norm(cand)
        cand_compact = _compact(cand)

        if cand_norm and cand_norm in target_norm:
            return True

        if cand_compact and cand_compact in target_compact:
            return True

    return False


def _contains_all(target: str | None, candidates: list[str] | None) -> bool:
    if not candidates:
        return True

    return all(_contains_any(target, [cand]) for cand in candidates)


def _record_field(record: ExtractedRecord, key: str) -> str:
    if key == "test_name":
        return _norm(record.test_name)
    if key == "method":
        return _norm(record.method)
    if key == "criteria":
        return _norm(record.criteria)
    if key == "result":
        return _norm(record.result)
    if key == "section_title":
        return _norm(record.section_title)
    if key == "raw_text":
        return _norm(record.raw_text)

    return ""


def _matches_rule(record: ExtractedRecord, rule: dict[str, Any]) -> bool:
    match = rule.get("match", {}) or {}

    field_map = {
        "test_name_any": "test_name",
        "method_any": "method",
        "criteria_any": "criteria",
        "result_any": "result",
        "section_title_any": "section_title",
        "raw_text_any": "raw_text",
    }

    for condition_key, field_name in field_map.items():
        candidates = match.get(condition_key)
        if candidates and not _contains_any(_record_field(record, field_name), candidates):
            return False

    field_map_all = {
        "test_name_all": "test_name",
        "method_all": "method",
        "criteria_all": "criteria",
        "result_all": "result",
        "section_title_all": "section_title",
        "raw_text_all": "raw_text",
    }

    for condition_key, field_name in field_map_all.items():
        candidates = match.get(condition_key)
        if candidates and not _contains_all(_record_field(record, field_name), candidates):
            return False

    return True


def _format_reason(template: str, **kwargs) -> str:
    try:
        return template.format(**kwargs)
    except Exception:
        return template


def _extract_number_by_regex(text: str | None, pattern: str | None) -> float | None:
    text = _norm(text)
    if not text or not pattern:
        return None

    m = re.search(pattern, text, flags=re.I)
    if not m:
        return None

    for group in m.groups():
        if group:
            try:
                return float(group.replace(",", ""))
            except ValueError:
                continue

    return None


def _extract_bp_values(text: str | None) -> list[int]:
    text = _norm(text)
    if not text:
        return []

    values: list[int] = []

    for m in re.finditer(r"([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)\s*bp", text, flags=re.I):
        try:
            values.append(int(m.group(1).replace(",", "")))
        except ValueError:
            continue

    return values


def _eval_text_pass(record: ExtractedRecord, rule: dict[str, Any]) -> PermitDecision | None:
    decision = rule.get("decision", {}) or {}

    reason_pass = decision.get("reason_pass") or "허가서 기준에 따라 시험결과가 기준을 만족하여 검수합격입니다."

    return PermitDecision(
        status=PASS_LABEL,
        reason=reason_pass,
        comparator="permit_text_pass",
        normalized_criteria=_norm(record.criteria),
        normalized_result=_norm(record.result),
        rule_id=rule.get("rule_id"),
        rule_title=rule.get("title"),
    )


def _eval_numeric_min_or_text(record: ExtractedRecord, rule: dict[str, Any]) -> PermitDecision | None:
    decision = rule.get("decision", {}) or {}
    result = _norm(record.result)

    pass_text_any = decision.get("pass_text_any") or []
    if pass_text_any and _contains_any(result, pass_text_any):
        threshold = decision.get("min_value")
        unit = decision.get("unit", "")
        reason = decision.get("reason_pass") or "허가서 기준에 따라 시험결과가 기준을 만족하여 검수합격입니다."

        return PermitDecision(
            status=PASS_LABEL,
            reason=_format_reason(reason, threshold=threshold, unit=unit, value="기준 만족"),
            comparator="permit_numeric_min_or_text",
            normalized_criteria=f"{threshold} {unit} 이상".strip() if threshold is not None else _norm(record.criteria),
            normalized_result=result,
            rule_id=rule.get("rule_id"),
            rule_title=rule.get("title"),
        )

    value = _extract_number_by_regex(result, decision.get("value_regex"))
    min_value = decision.get("min_value")
    unit = decision.get("unit", "")

    if value is None or min_value is None:
        return None

    try:
        min_value_float = float(min_value)
    except (TypeError, ValueError):
        return None

    ok = value >= min_value_float

    if ok:
        reason = decision.get("reason_pass") or "허가서 기준에 따라 시험결과가 기준값 이상으로 확인되어 검수합격입니다."
        return PermitDecision(
            status=PASS_LABEL,
            reason=_format_reason(reason, threshold=min_value, unit=unit, value=value),
            comparator="permit_numeric_min",
            normalized_criteria=f"{min_value} {unit} 이상".strip(),
            normalized_result=f"{value:g} {unit}".strip(),
            rule_id=rule.get("rule_id"),
            rule_title=rule.get("title"),
        )

    reason = decision.get("reason_fail") or "허가서 기준값을 만족하지 않아 검수불합격입니다."
    return PermitDecision(
        status=FAIL_LABEL,
        reason=_format_reason(reason, threshold=min_value, unit=unit, value=value),
        comparator="permit_numeric_min",
        normalized_criteria=f"{min_value} {unit} 이상".strip(),
        normalized_result=f"{value:g} {unit}".strip(),
        rule_id=rule.get("rule_id"),
        rule_title=rule.get("title"),
    )


def _eval_bp_contains_all(record: ExtractedRecord, rule: dict[str, Any]) -> PermitDecision | None:
    decision = rule.get("decision", {}) or {}

    expected_bp = decision.get("expected_bp") or []
    tolerance_pct = float(decision.get("tolerance_pct", 0.0))
    result_values = _extract_bp_values(record.result)

    result_text = _norm(record.result)
    result_pass_any = decision.get("result_pass_any") or []

    if not expected_bp:
        return None

    matched_all = True
    mismatch_lines: list[str] = []

    for expected in expected_bp:
        try:
            expected_int = int(expected)
        except (TypeError, ValueError):
            continue

        if not result_values:
            matched_all = False
            mismatch_lines.append(f"기준 {expected_int} bp에 대응하는 결과 밴드가 확인되지 않음")
            continue

        nearest = min(result_values, key=lambda x: abs(x - expected_int))
        diff_ratio = abs(nearest - expected_int) / expected_int if expected_int else 1.0

        if diff_ratio > tolerance_pct:
            matched_all = False
            mismatch_lines.append(
                f"기준 {expected_int} bp에 대응하는 결과 {nearest} bp가 허용범위를 벗어남"
            )

    text_supports_pass = _contains_any(result_text, result_pass_any) if result_pass_any else True

    expected_text = ", ".join(f"{int(x)} bp" for x in expected_bp)
    result_text_norm = ", ".join(f"{x} bp" for x in result_values) if result_values else result_text

    if matched_all and text_supports_pass:
        reason = decision.get("reason_pass") or "허가서 기준에 따라 필요한 밴드가 확인되어 검수합격입니다."
        return PermitDecision(
            status=PASS_LABEL,
            reason=_format_reason(reason, expected_bp=expected_text, result_bp=result_text_norm),
            comparator="permit_bp_contains_all",
            normalized_criteria=expected_text,
            normalized_result=result_text_norm,
            rule_id=rule.get("rule_id"),
            rule_title=rule.get("title"),
        )

    reason = decision.get("reason_fail") or "허가서 기준의 밴드가 시험결과에서 확인되지 않아 검수불합격입니다."
    return PermitDecision(
        status=FAIL_LABEL,
        reason=_format_reason(reason, expected_bp=expected_text, result_bp=result_text_norm) + (
            " " + " ".join(mismatch_lines) if mismatch_lines else ""
        ),
        comparator="permit_bp_contains_all",
        normalized_criteria=expected_text,
        normalized_result=result_text_norm,
        rule_id=rule.get("rule_id"),
        rule_title=rule.get("title"),
    )


def _eval_terms_confirmed(record: ExtractedRecord, rule: dict[str, Any]) -> PermitDecision | None:
    decision = rule.get("decision", {}) or {}

    result = _norm(record.result)

    required_terms = decision.get("required_terms") or []
    evidence_terms_any = decision.get("evidence_terms_any") or []

    has_required = _contains_all(result, required_terms)
    has_evidence = _contains_any(result, evidence_terms_any) if evidence_terms_any else True

    if has_required and has_evidence:
        reason = decision.get("reason_pass") or "허가서 기준에 따라 필요한 항목이 시험결과에서 확인되어 검수합격입니다."
        return PermitDecision(
            status=PASS_LABEL,
            reason=reason,
            comparator="permit_terms_confirmed",
            normalized_criteria=_norm(record.criteria),
            normalized_result=result,
            rule_id=rule.get("rule_id"),
            rule_title=rule.get("title"),
        )

    reason = decision.get("reason_fail") or "허가서 기준의 필수 항목이 시험결과에서 확인되지 않아 검수불합격입니다."
    return PermitDecision(
        status=FAIL_LABEL,
        reason=reason,
        comparator="permit_terms_confirmed",
        normalized_criteria=_norm(record.criteria),
        normalized_result=result,
        rule_id=rule.get("rule_id"),
        rule_title=rule.get("title"),
    )


def _evaluate_rule(record: ExtractedRecord, rule: dict[str, Any]) -> PermitDecision | None:
    decision_type = ((rule.get("decision") or {}).get("type") or "").strip()

    if decision_type == "text_pass":
        return _eval_text_pass(record, rule)

    if decision_type == "numeric_min_or_text":
        return _eval_numeric_min_or_text(record, rule)

    if decision_type == "bp_contains_all":
        return _eval_bp_contains_all(record, rule)

    if decision_type == "terms_confirmed":
        return _eval_terms_confirmed(record, rule)

    return None


def apply_permit_rule(record: ExtractedRecord) -> PermitDecision | None:
    for rule in _load_permit_rules():
        if not _matches_rule(record, rule):
            continue

        decision = _evaluate_rule(record, rule)
        if decision is not None and decision.status is not None:
            return decision

    return None