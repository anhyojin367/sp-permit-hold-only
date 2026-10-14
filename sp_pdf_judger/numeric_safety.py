"""Independent checks of source numbers. Never promotes an LLM verdict to PASS."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from .criteria_parser import parse_criteria_text
from .unit_normalizer import parse_number_and_unit, normalize_measurement_text


@dataclass(frozen=True)
class NumericVeto:
    status: str
    reason: str


def result_is_requirement(criteria: str | None, result: str | None) -> bool:
    compact = lambda value: re.sub(r"\s+", "", value or "")
    return bool(compact(result) and compact(criteria) == compact(result)
                and re.search(r"(?:이어야|않아야|되어야|해야|하여야)\s*(?:함|한다)", result or ""))


def labelled_numeric_check(criteria: str, result: str, expected_labels: list[str]) -> NumericVeto:
    """Compare explicitly labelled endpoints, never their appearance order."""
    from .judgement import deterministic_judge
    compact = lambda value: re.sub(r"\s+", "", normalize_measurement_text(value)).casefold()
    prefix = r"(?P<label>[^:\n;,]+)\s*:\s*(?P<value>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>개|[A-Za-zμ/%]+)"
    limits = list(re.finditer(prefix + r"\s*(?P<op>이하|미만|이상|초과)", normalize_measurement_text(criteria)))
    observed = list(re.finditer(prefix + r"(?=\s*(?:$|[,;\n]))", normalize_measurement_text(result)))
    limits_by_key, values_by_key = {}, {}
    for rows, target in [(limits, limits_by_key), (observed, values_by_key)]:
        for match in rows:
            target.setdefault(compact(match["label"]), []).append(match)
    expected = {compact(label) for label in expected_labels}
    if not expected or not expected <= limits_by_key.keys():
        return NumericVeto("검수보류", "MD에서 요구하는 측정대상별 시험기준을 모두 확인하지 못했습니다.")
    failures, missing = [], []
    for label, matches in limits_by_key.items():
        values = values_by_key.get(label, [])
        if len(matches) != 1 or len(values) != 1:
            missing.append(label)
            continue
        requirement, value = matches[0], values[0]
        status, reason, *_ = deterministic_judge(
            f"{requirement['value']} {requirement['unit']} {requirement['op']}",
            f"{value['value']} {value['unit']}")
        if status == "검수불합격":
            failures.append(f"{requirement['label'].strip()}: {value['value']} {value['unit']} / {requirement['value']} {requirement['unit']} {requirement['op']}")
        elif status != "검수합격":
            missing.append(label)
    if failures:
        return NumericVeto("검수불합격", "측정대상별 기준 위반: " + " / ".join(failures))
    if missing:
        return NumericVeto("검수보류", "측정대상별 결과가 누락·중복되었거나 단위가 불명확합니다: " + ", ".join(missing))
    return NumericVeto("검수합격", f"{len(limits_by_key)}개 측정대상의 기준·결과를 각각 확인했습니다.")


def minimum_count_check(criteria: str, result: str) -> NumericVeto | None:
    """One complete named-target calculation shared by MD and test judgement.

    None means this is not the supported strict quantification-count pattern.
    A recognized but incomplete pattern is HOLD, never a successful subset.
    """
    criteria, result = normalize_measurement_text(criteria), normalize_measurement_text(result)
    group = re.search(r"([A-Za-z][\w-]*(?:\s*,\s*[A-Za-z][\w-]*)+)\s*유전자\s*중\s*(\d+)\s*종\s*이상", criteria)
    number = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"
    threshold = re.search(r"정량한계\s*\(\s*(" + number + r")\s*([^\)]+)\)\s*미만", criteria)
    if not group or not threshold:
        if re.search(r"\d+\s*종\s*이상", criteria) and "정량한계" in criteria:
            return NumericVeto("검수보류", "최소 개수의 측정대상·정량한계·연산자를 온전하게 해석하지 못했습니다.")
        return None
    names = [v.strip().casefold() for v in group[1].split(",")]
    expected_unit = re.sub(r"\s+", "", threshold[2])
    required = int(group[2])
    if len(set(names)) != len(names) or not 1 <= required <= len(names) or not expected_unit:
        return NumericVeto("검수보류", "최소 충족 개수 또는 측정대상 목록이 유효하지 않습니다.")
    # Full rows prevent a missing target, a suffix annotation or a partial number
    # from being silently discarded by findall. Unit conversion is not inferred.
    pattern = r"\s*([A-Za-z][\w-]*)\s*:\s*(>=|<=|≥|≤|<|>)?\s*(" + number + r")\s*([A-Za-zμ].*?)\s*"
    matched = [re.fullmatch(pattern, part) for part in re.split(r"[,;\n]", result) if part.strip()]
    if not matched or any(row is None for row in matched):
        return NumericVeto("검수보류", "최소 충족 개수의 결과값 또는 필수 단위가 누락·불명확합니다.")
    rows = [(row[1].casefold(), row[2] or "", Decimal(row[3]), re.sub(r"\s+", "", row[4])) for row in matched]
    if len(rows) != len(names) or set(n for n, *_ in rows) != set(names) or any(u != expected_unit for *_, u in rows):
        return NumericVeto("검수보류", "최소 충족 개수의 대상·결과·단위 연결이 완전하지 않습니다.")
    limit = Decimal(threshold[1])
    satisfied = sum((op == "<" and value <= limit) or (op in {"", "<=", "≤"} and value < limit)
                    for _, op, value, _ in rows)
    if satisfied < required:
        return NumericVeto("검수불합격", f"기준의 {limit} 미만이 입증된 결과는 {satisfied}종으로 최소 {required}종에 미달합니다. 이하(≤)는 미만(<) 충족으로 보지 않습니다.")
    return NumericVeto("검수합격", f"전체 {len(names)}종의 결과·단위를 확인했고 {satisfied}종이 정량한계 {limit} 미만입니다(최소 {required}종).")


def minimum_count_veto(criteria: str, result: str) -> NumericVeto | None:
    """Never promote an LLM verdict; only return a failed/incomplete check."""
    checked = minimum_count_check(criteria, result)
    return checked if checked and checked.status != "검수합격" else None


def permit_numeric_veto(basis: str, result: str, *, acceptance_basis: list[str] | None = None) -> NumericVeto | None:
    """Veto definite numerical contradictions in the selected, grounded permit.

    Match only unique identical units, never positional guesses. Procedure values
    without a matching observation are not mistaken for result requirements.
    A clean numerical check does not prove the other qualitative conditions.
    """
    from .judgement import _extract_numeric_requirements, _extract_numeric_results, deterministic_judge

    grouped = minimum_count_veto(basis, result)
    if grouped:
        return grouped
    censored = bool(re.search(r"[<>≤≥]", result))
    observations = [(text, parse_number_and_unit(text)) for text in _extract_numeric_results(result)]
    errors, missing = [], []
    requirements = _extract_numeric_requirements("\n".join(acceptance_basis) if acceptance_basis is not None else basis)
    acceptance_compact = re.sub(r"\s+", "", normalize_measurement_text("\n".join(acceptance_basis or [basis])))
    for requirement in requirements:
        parsed = parse_criteria_text(requirement)
        if parsed.kind == "range" and acceptance_basis is not None:
            # An acceptance sentence may ALSO describe 15~20 g animals or
            # reagent preparation. A bare range is not itself an outcome limit.
            literal = re.sub(r"\s+", "", requirement)
            if not re.search(re.escape(literal) + r"(?:이어야|여야|이여야|이내|범위.*(?:이어야|여야))", acceptance_compact):
                continue
        boundary = parsed.threshold or parsed.lower
        if not boundary:
            continue
        matches = [(text, value) for text, value in observations if value and value.unit_canonical == boundary.unit_canonical]
        if len(matches) != 1:
            # Only require coverage of explicitly grounded acceptance sentences;
            # procedure ranges such as reagent temperatures must not veto a test.
            # Multiple same-unit endpoints still need semantic target matching.
            if not matches and acceptance_basis is not None and boundary.unit_canonical != "scalar":
                missing.append(f"{requirement} (단위 {boundary.unit or '미기재'})")
            continue
        if censored:
            continue  # Unit coverage still applies; do not invent an exact measurement.
        status, reason, *_ = deterministic_judge(requirement, matches[0][0])
        if status == "검수불합격":
            errors.append(f"{requirement} / {matches[0][0]}: {reason}")
    if errors:
        return NumericVeto("검수불합격", "허가서 원문 수치 재계산: " + " / ".join(errors))
    if missing:
        return NumericVeto("검수보류", "허가서 적합 조건에 대응하는 결과 단위를 확인할 수 없습니다. 단위 환산 근거 없이 숫자만 비교하지 않습니다: " + " / ".join(missing))
    return None
