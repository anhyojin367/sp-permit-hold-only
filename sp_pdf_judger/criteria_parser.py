from __future__ import annotations

import re
from dataclasses import dataclass

from .unit_normalizer import ParsedMeasurement, parse_number_and_unit
from .utils import clean_text


@dataclass
class ParsedCriteria:
    kind: str
    comparator: str | None = None
    threshold: ParsedMeasurement | None = None
    lower: ParsedMeasurement | None = None
    upper: ParsedMeasurement | None = None
    raw: str = ""


IGNORE_UNITS = {"회", "종"}


def _normalize_criteria_text(criteria: str | None) -> str:
    text = clean_text(criteria)

    if not text:
        return ""

    text = text.replace("\\n", " ")
    text = text.replace("\r\n", " ")
    text = text.replace("\n", " ")

    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("％", "%")
    text = text.replace("µ", "μ")
    text = text.replace("×", "x")

    text = text.replace("≥", ">=")
    text = text.replace("≤", "<=")
    text = text.replace("＞", ">")
    text = text.replace("＜", "<")
    text = text.replace("＝", "=")

    text = re.sub(r"\s*>=\s*", " >= ", text)
    text = re.sub(r"\s*<=\s*", " <= ", text)
    text = re.sub(r"(?<![<>=])\s*>\s*(?!=)", " > ", text)
    text = re.sub(r"(?<![<>=])\s*<\s*(?!=)", " < ", text)

    text = re.sub(r"\s+", " ", text).strip()

    text = re.sub(
        r"기준\s*\(([^)]+)\)\s*(이상|이하|미만|초과)",
        r"\1 \2",
        text,
    )

    text = re.sub(
        r"\(([0-9]+(?:\.[0-9]+)?(?:\s*[A-Za-zμ%/²0-9\-\.·\[\]]*)?)\)\s*(이상|이하|미만|초과)",
        r"\1 \2",
        text,
    )

    return clean_text(text)


def _measurement_quality(parsed: ParsedMeasurement | None) -> int:
    if parsed is None:
        return -1

    unit = clean_text(parsed.unit)
    canonical = clean_text(parsed.unit_canonical)

    if unit in IGNORE_UNITS or canonical in IGNORE_UNITS:
        return -1

    if canonical == "scalar":
        return 0

    return 10


def _number_start_positions(text: str) -> list[int]:
    # A scientific value is one number: never reparse its exponent as a threshold.
    return [m.start() for m in re.finditer(
        r"[+-]?\d[\d,]*(?:\.\d+)?(?:\s*[xX×]\s*10\s*\^?\s*[+\-]?[\d⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)?", text)]


def _parse_last_measurement(text: str) -> ParsedMeasurement | None:
    positions = _number_start_positions(text)

    for pos in reversed(positions):
        parsed = parse_number_and_unit(text[pos:])
        if parsed:
            return parsed

    return None


def _parse_first_measurement(text: str) -> ParsedMeasurement | None:
    return parse_number_and_unit(text)


def _best_candidate(candidates: list[tuple[int, ParsedMeasurement, str]]) -> tuple[ParsedMeasurement, str] | None:
    valid = []

    for position, parsed, comparator in candidates:
        quality = _measurement_quality(parsed)
        if quality < 0:
            continue

        valid.append((quality, position, parsed, comparator))

    if not valid:
        return None

    valid.sort(key=lambda x: (x[0], x[1]), reverse=True)
    _, _, parsed, comparator = valid[0]
    return parsed, comparator


def _parse_range(text: str) -> tuple[ParsedMeasurement, ParsedMeasurement] | None:
    """
    지원:
    - 50.0~100.0%
    - 671.9 mOsm/kg ~ 716.1 mOsm/kg
    - 5,000 ~ 8,000 μg/mL
    """
    num = r"[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?"
    unit = r"[A-Za-zμ%/²0-9\-\.·]+(?:/[A-Za-zμ%/²0-9\-\.·]+)?"

    m = re.search(
        rf"(?P<lo>{num})\s*(?P<unit1>{unit})?\s*~\s*(?P<hi>{num})\s*(?P<unit2>{unit})?",
        text,
    )

    if not m:
        return None

    unit1 = clean_text(m.group("unit1") or "")
    unit2 = clean_text(m.group("unit2") or "")
    final_unit = unit2 or unit1

    lower = parse_number_and_unit(f"{m.group('lo')} {final_unit}")
    upper = parse_number_and_unit(f"{m.group('hi')} {final_unit}")

    if lower and upper:
        return lower, upper

    return None


def _collect_symbol_candidates(raw: str) -> list[tuple[int, ParsedMeasurement, str]]:
    candidates: list[tuple[int, ParsedMeasurement, str]] = []

    for symbol, op in [
        (">=", ">="),
        ("<=", "<="),
        (">", ">"),
        ("<", "<"),
    ]:
        for m in re.finditer(re.escape(symbol), raw):
            parsed = _parse_first_measurement(raw[m.end():])
            if parsed:
                candidates.append((m.start(), parsed, op))

    return candidates


def _collect_korean_candidates(raw: str) -> list[tuple[int, ParsedMeasurement, str]]:
    candidates: list[tuple[int, ParsedMeasurement, str]] = []

    for ko, op in [
        ("이하", "<="),
        ("미만", "<"),
        ("이상", ">="),
        ("초과", ">"),
    ]:
        for m in re.finditer(ko, raw):
            before = raw[:m.start()]
            after = raw[m.end():]

            parsed_before = _parse_last_measurement(before)
            if parsed_before:
                candidates.append((m.start(), parsed_before, op))

            # "1회 투여량 이상(0.25 mL 이상)" 같은 경우 방어
            parsed_after = _parse_first_measurement(after)
            if parsed_after:
                candidates.append((m.start(), parsed_after, op))

    return candidates


def parse_criteria_text(criteria: str | None) -> ParsedCriteria:
    raw = _normalize_criteria_text(criteria)

    if not raw:
        return ParsedCriteria(kind="unknown", raw=clean_text(criteria))

    # 1) 범위 기준 우선 처리
    range_pair = _parse_range(raw)
    if range_pair:
        lower, upper = range_pair
        return ParsedCriteria(
            kind="range",
            lower=lower,
            upper=upper,
            raw=raw,
        )

    # 2) >=, <=, >, < 처리
    symbol_best = _best_candidate(_collect_symbol_candidates(raw))
    if symbol_best:
        threshold, comparator = symbol_best
        return ParsedCriteria(
            kind="compare",
            comparator=comparator,
            threshold=threshold,
            raw=raw,
        )

    # 3) 이상, 이하, 미만, 초과 처리
    korean_best = _best_candidate(_collect_korean_candidates(raw))
    if korean_best:
        threshold, comparator = korean_best
        return ParsedCriteria(
            kind="compare",
            comparator=comparator,
            threshold=threshold,
            raw=raw,
        )

    # 4) 단순 수치 기준
    threshold = _parse_first_measurement(raw)
    if threshold and _measurement_quality(threshold) >= 0:
        return ParsedCriteria(
            kind="exact",
            comparator="==",
            threshold=threshold,
            raw=raw,
        )

    return ParsedCriteria(kind="unknown", raw=raw)
