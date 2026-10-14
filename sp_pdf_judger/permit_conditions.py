"""Conservative, source-bound conditions, not applicability or verdict decisions.

Operators: numeric ==/>=/>/<=/< and between; qualitative predicates use
explicit positive/negative/detected/not_detected/exists/absent/fit/unfit names.
Durations (including sample ages) use seconds, percentages retain percent units.
Unknown normative predicates remain mandatory unparsed evidence.
"""
from __future__ import annotations

from decimal import Decimal, localcontext
import hashlib
import json
import re

from .criteria_parser import parse_criteria_text
from .permit_models import PermitChunk, PermitCondition
from .policy_time import complete_duration_seconds
from .unit_normalizer import parse_number_and_unit


CONDITION_PARSER_SCHEMA_VERSION = "permit-conditions-v1"
_NUMBER = r"[+-]?\d+(?:,\d{3})*(?:\.\d+)?"
_UNIT = r"(?:℃|°\s*C|일|시간|분|개|%|mg/mL|ng/mL|μg/mL|mL|mg|ng)"
_QUANTITY = rf"{_NUMBER}\s*(?:{_UNIT})?"
_ATOM = re.compile(
    rf"(?P<tolerance>{_NUMBER}\s*±\s*{_NUMBER}\s*{_UNIT})"
    rf"|(?P<range>{_QUANTITY}\s*[~～]\s*{_QUANTITY})"
    rf"|(?P<bounds>{_QUANTITY}\s*(?:이상|초과)\s*{_QUANTITY}\s*(?:이하|미만))"
    rf"|(?P<number>(?:(?:최소|최대|>=|<=|>|<|≥|≤|=)\s*)?{_QUANTITY}\s*(?:이상|이하|초과|미만)?)"
)
_QUANTITY_PART = re.compile(rf"(?P<number>{_NUMBER})\s*(?P<unit>{_UNIT})?")
_NORMATIVE = re.compile(r"이어야|되어야|없어야|않아야|해야|하여야")
_QUALITATIVE = re.compile(
    r"(?P<not_detected>불검출(?=이어야)|검출되지\s*않아야)"
    r"|(?P<negative>음성(?=이어야))|(?P<positive>양성(?=이어야))"
    r"|(?P<detected>검출되어야)|(?P<absent>없어야)"
    r"|(?P<exists>존재하여야|있어야)|(?P<unfit>부적합(?=이어야|하여야))"
    r"|(?P<fit>적합(?=이어야|하여야))"
)
_NUMERIC_PREDICATE_TAIL = re.compile(r"\s*(?:[이가은는]\s*)?(?:생존\s*)?(?:이어야|되어야|해야|하여야)$")


def _digest(payload) -> str:
    def encode(value):
        if isinstance(value, Decimal):
            # No float round trip, and numerically equal Decimal values hash alike.
            text = format(value, "f")
            return text.rstrip("0").rstrip(".") if "." in text else text
        raise TypeError(type(value).__name__)
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=encode)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _quantity(number: str, unit: str | None):
    value = Decimal(number.replace(",", ""))
    if unit in {"일", "시간", "분"}:
        seconds = complete_duration_seconds(f"{number} {unit}")
        if seconds is not None:
            return seconds, "s"
        return None, None
    if unit and re.fullmatch(r"°\s*C", unit):
        return value, "℃"
    if unit == "개":
        return value, "count"
    if unit and unit not in {"℃", "%"}:
        # Legacy helpers are used for unit spelling only, never their float values.
        parsed = parse_number_and_unit(f"1 {unit}")
        unit = parsed.unit_canonical
    return value, unit


def _numeric(match):
    text = match.group()
    quantities = list(_QUANTITY_PART.finditer(text))
    unit = next((m["unit"] for m in reversed(quantities) if m["unit"]), None)
    pairs = [_quantity(m["number"], m["unit"] or unit) for m in quantities]
    if any(v is None for v, _ in pairs) or len({u for _, u in pairs}) != 1:
        return None
    if match.lastgroup in {"range", "bounds", "tolerance"}:
        lower, upper = (p[0] for p in pairs)
        if match.lastgroup == "tolerance":
            with localcontext() as context:
                context.prec = max(40, len(text) * 2)
                lower, upper = lower - upper, lower + upper
        if lower > upper:
            return None
        return dict(kind="range", operator="between", lower=lower, upper=upper,
                    unit=pairs[0][1], lower_inclusive="초과" not in text,
                    upper_inclusive="미만" not in text)
    operator = None
    if text.lstrip().startswith("최소"):
        operator = ">="
    elif text.lstrip().startswith("최대"):
        operator = "<="
    if operator is None:
        # Comparator semantics are shared; Decimal lexemes above remain authoritative.
        operator = parse_criteria_text(text).comparator or "=="
    return dict(kind="exact" if operator == "==" else "compare", operator=operator,
                value=pairs[0][0], unit=pairs[0][1])


def _metric(values, sentence, start):
    unit = values["unit"]
    if unit == "count":
        return "sample_count"
    if unit == "℃":
        return "incubation_temperature"
    if unit == "s":
        prefix = sentence[:start]
        return "sample_age" if re.search(r"유정란\s*\(\s*$", prefix) else "incubation_duration"
    if unit == "%" and re.search(r"생존[률율]?", sentence):
        return "survival_rate"
    return "measurement"


def _complete_numeric_matches(sentence):
    """Reject fragments of an expression the supported atom grammar cannot own.

    A range/tolerance is one match. Adjacent quantities or quantities connected
    only by numeric syntax are not independent requirements. Likewise, touching
    unit/scientific/operator characters means the match ended inside a token.
    Natural-language clauses can still separate the egg procedure's quantities.
    """
    matches = tuple(_ATOM.finditer(sentence))
    boundary = r"[0-9A-Za-z_×*/^±~⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]"
    for match in matches:
        if ((match.start() and re.fullmatch(boundary, sentence[match.start() - 1]))
                or (match.end() < len(sentence) and re.fullmatch(boundary, sentence[match.end()]))):
            return None
    for previous, current in zip(matches, matches[1:]):
        gap = sentence[previous.end():current.start()]
        if re.fullmatch(r"[\s0-9A-Za-z_.,:+\-×*/^±~⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]*", gap):
            return None
    return matches


def parse_permit_conditions(chunk: PermitChunk) -> tuple[PermitCondition, ...]:
    """Parse explicitly supported predicates and retain every unknown mandate.

    Each atomic quote is its complete source sentence, retaining context and
    physical line wraps. Sentence-scoped AND nodes reference, never copy, atoms.
    Explicit inoculation routes propagate until the next explicit route.
    """
    source = "\n".join(s.text for s in chunk.owned_spans if s.kind == "body") if chunk.owned_spans else chunk.text
    # Headings are identity/context, never numeric criteria. Do not parse a
    # descendant-expanded chunk body when positional ownership is available.
    evidence_id = chunk.evidence_id or _digest([
        "legacy-permit-evidence", chunk.source_file, chunk.page_number,
        chunk.section_number, chunk.section_path_titles, chunk.text,
    ])
    result = []
    method_key = None
    for quote in re.split(r"(?<=[.!?])\s+|(?<=[.!?])(?=[가-힣])", source):
        quote = quote.strip()
        if not quote:
            continue
        sentence = re.sub(r"(?<=[가-힣])\s*\n\s*(?=[가-힣])", "", quote)
        route = re.search(r"(요막강\s*내\s*접종|난황낭\s*접종)의\s*경우", sentence)
        if route:
            method_key = re.sub(r"\s+", "", route[1])
        atoms, recognized = [], []

        def append(values, metric):
            fields = dict(condition_id="", evidence_id=evidence_id,
                          clause_index=len(result) + len(atoms), kind="unparsed", operator=None,
                          value=None, unit=None, lower=None, upper=None,
                          lower_inclusive=True, upper_inclusive=True, children=(),
                          quote=quote, stage_key=chunk.normalized_stage_path,
                          test_key=chunk.normalized_test_name, metric_key=metric,
                          method_key=method_key, scope_text=quote)
            fields.update(values)
            fields["condition_id"] = _digest([CONDITION_PARSER_SCHEMA_VERSION, fields])
            atoms.append(PermitCondition(**fields))

        # OR semantics are deliberately outside this initial IR parser. Never
        # promote alternatives to individually mandatory atomic requirements.
        if re.search(r"또는|혹은|거나|내지", sentence):
            append({}, "unparsed")
            result.extend(atoms)
            continue
        numeric_matches = _complete_numeric_matches(sentence)
        if numeric_matches is None:
            append({}, "unparsed")
            result.extend(atoms)
            continue
        candidates = []
        for match in numeric_matches:
            values = _numeric(match)
            if values is not None:
                candidates.append((match.start(), values, _metric(values, sentence, match.start())))
                recognized.append((match.start(), match.end(), "numeric"))
        for match in _QUALITATIVE.finditer(sentence):
            metric = "haemagglutination" if "혈구응집반응" in sentence else "qualitative_result"
            candidates.append((match.start(), dict(kind="qualitative", operator=match.lastgroup), metric))
            recognized.append((match.start(), match.end(), "qualitative"))
        for match in re.finditer(r"계대(?:하여|하고)", sentence):
            candidates.append((match.start(), dict(kind="qualitative", operator="exists"), "passage"))
        for mandate in _NORMATIVE.finditer(sentence):
            covered = any(
                start <= mandate.start() < end
                or (kind == "qualitative" and not sentence[end:mandate.start()].strip())
                or (kind == "numeric" and _NUMERIC_PREDICATE_TAIL.fullmatch(sentence[end:mandate.end()]))
                for start, end, kind in recognized if start <= mandate.start()
            )
            if not covered:
                candidates.append((mandate.start(), {}, "unparsed"))
        for _, values, metric in sorted(candidates, key=lambda c: c[0]):
            append(values, metric)
        if len(atoms) > 1:
            append(dict(kind="and", operator="and", children=tuple(c.condition_id for c in atoms)), "compound")
        result.extend(atoms)
    return tuple(result)
