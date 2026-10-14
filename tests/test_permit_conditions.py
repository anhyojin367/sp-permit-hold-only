"""Each case protects source requirements from omission or numeric reinterpretation."""
from collections import Counter
from dataclasses import replace
from decimal import Decimal

import pytest

from sp_pdf_judger.permit_conditions import (
    CONDITION_PARSER_SCHEMA_VERSION, parse_permit_conditions,
)
from sp_pdf_judger.permit_models import PermitChunk, PermitCondition


def chunk(text):
    return PermitChunk("permit.pdf", 1, "1.1", "시험", text,
                       normalized_stage_path="원액", normalized_test_name="시험",
                       evidence_id="evidence-1")


@pytest.mark.parametrize("text,kind,op,value,unit,lower,upper,li,ui", [
    ("최소 10개", "compare", ">=", "10", "count", None, None, True, True),
    ("최대 12개", "compare", "<=", "12", "count", None, None, True, True),
    ("10 이상", "compare", ">=", "10", None, None, None, True, True),
    ("10 이하", "compare", "<=", "10", None, None, None, True, True),
    ("10 초과", "compare", ">", "10", None, None, None, True, True),
    ("10 미만", "compare", "<", "10", None, None, None, True, True),
    ("≤ 10", "compare", "<=", "10", None, None, None, True, True),
    ("0.123456789012345678901 이상", "compare", ">=", "0.123456789012345678901", None, None, None, True, True),
    ("10~11일", "range", "between", None, "s", "864000", "950400", True, True),
    ("10일 초과 11일 미만", "range", "between", None, "s", "864000", "950400", False, False),
    ("10 이상 11 미만", "range", "between", None, None, "10", "11", True, False),
    ("36 ± 2 ℃", "range", "between", None, "℃", "34", "38", True, True),
    ("3일", "exact", "==", "259200", "s", None, None, True, True),
    ("2시간", "exact", "==", "7200", "s", None, None, True, True),
    ("1.5분", "exact", "==", "90", "s", None, None, True, True),
    ("80% 이상", "compare", ">=", "80", "%", None, None, True, True),
    ("7.5", "exact", "==", "7.5", None, None, None, True, True),
])
def test_numeric_forms_do_not_drop_bounds_units_or_decimal_precision(text, kind, op, value, unit, lower, upper, li, ui):
    result = parse_permit_conditions(chunk(text))
    assert len(result) == 1
    condition = result[0]
    assert isinstance(condition, PermitCondition)
    assert (condition.kind, condition.operator, condition.unit) == (kind, op, unit)
    assert (condition.value, condition.lower, condition.upper) == tuple(
        Decimal(v) if v is not None else None for v in (value, lower, upper))
    assert (condition.lower_inclusive, condition.upper_inclusive) == (li, ui)
    assert all(v is None or isinstance(v, Decimal) for v in (condition.value, condition.lower, condition.upper))
    assert condition.quote == text


@pytest.mark.parametrize("text,op", [
    ("결과는 음성이어야 한다.", "negative"),
    ("결과는 양성이어야 한다.", "positive"),
    ("인자가 검출되지 않아야 한다.", "not_detected"),
    ("인자가 불검출이어야 한다.", "not_detected"),
    ("인자가 검출되어야 한다.", "detected"),
    ("반응이 없어야 한다.", "absent"),
    ("반응이 존재하여야 한다.", "exists"),
    ("결과는 적합하여야 한다.", "fit"),
    ("결과는 부적합이어야 한다.", "unfit"),
])
def test_qualitative_predicates_preserve_negation(text, op):
    result = parse_permit_conditions(chunk(text))
    assert len(result) == 1
    assert (result[0].kind, result[0].operator) == ("qualitative", op)
    assert result[0].mandatory and result[0].quote == text


EGG = (
    "요막강 내 접종의 경우, 유정란(10 ~ 11일) 최소 10개에 각각 검체를 요막강 내 주사하고 "
    "36 ± 2 ℃에서 3일 간 배양한 뒤 생존률을 검사했을 때 80% 이상이 생존해야 한다. "
    "생존한 유정란의 요막액을 혼합하여 다른 유정란의 요막액에 계대하여 36 ± 2 ℃에서 "
    "3일 간 배양한 뒤 생존률을 검사했을 때 80% 이상이 생존해야 하고, 배양 후 유정란의 "
    "요막액에 대해 기니픽, 닭 및 사람 O형 적혈구를 이용하여 혈구응집반응을 시험했을 때 "
    "혈구응집반응이 없어야 한다. "
    "난황낭 접종의 경우, 유정란(6 ~ 7 일) 최소 10개에 각각 검체를 난황낭에 주사하고 "
    "36 ± 2 ℃에서 9일 간 배양한 뒤 생존률을 검사했을 때 80% 이상이 생존해야 한다. "
    "생존한 유정란의 난황낭을 혼합하여 다른 유정란에 계대하고 36 ± 2 ℃에서 9일 간 "
    "배양한 뒤 생존율을 검사했을 때 80% 이상이 생존해야 한다."
)


def test_complete_egg_paragraph_preserves_repeated_passage_requirements_and_scopes():
    result = parse_permit_conditions(chunk(EGG))
    assert result == parse_permit_conditions(chunk(EGG))
    atoms = [c for c in result if c.kind != "and"]
    assert Counter(c.metric_key for c in atoms) == {
        "sample_age": 2, "sample_count": 2, "incubation_temperature": 4,
        "incubation_duration": 4, "survival_rate": 4, "passage": 2,
        "haemagglutination": 1,
    }
    assert len({c.condition_id for c in result}) == len(result)
    assert all(c.quote in EGG and c.scope_text and c.mandatory for c in result)
    assert all((c.stage_key, c.test_key) == ("원액", "시험") for c in result)
    assert {c.method_key for c in atoms} == {"요막강내접종", "난황낭접종"}
    groups = [c for c in result if c.kind == "and"]
    assert len(groups) == 4
    assert Counter(key for c in groups for key in c.children) == Counter(c.condition_id for c in atoms)
    assert [c.operator for c in atoms if c.metric_key == "haemagglutination"] == ["absent"]
    assert [c.value for c in atoms if c.metric_key == "incubation_duration"] == [
        Decimal(259200), Decimal(259200), Decimal(777600), Decimal(777600)]


@pytest.mark.parametrize("text", [
    "별도 검증절차를 충족하여야 한다.",
    "예상되는 염기서열과 일치하여야 한다.",
    "반응이 나타나지 않아야 한다.",
])
def test_unknown_normative_predicate_cannot_disappear(text):
    result = parse_permit_conditions(chunk(text))
    assert len(result) == 1
    assert (result[0].kind, result[0].metric_key, result[0].mandatory) == ("unparsed", "unparsed", True)
    assert result[0].quote == text


def test_known_numeric_clause_does_not_hide_unknown_mandatory_clause():
    result = parse_permit_conditions(chunk("최소 10개이어야 하고, 별도 검증절차를 충족하여야 한다."))
    assert Counter(c.kind for c in result) == {"compare": 1, "unparsed": 1, "and": 1}


def test_ids_bind_source_meaning_and_order_without_collapsing_repeats():
    original = chunk("80% 이상이어야 한다. 80% 이상이어야 한다.")
    result = parse_permit_conditions(original)
    assert len(result) == 2 and result[0].condition_id != result[1].condition_id
    assert result == parse_permit_conditions(original)
    assert {c.condition_id for c in result}.isdisjoint(
        c.condition_id for c in parse_permit_conditions(replace(original, evidence_id="evidence-2")))
    assert result[0].condition_id != parse_permit_conditions(replace(original, text="81% 이상이어야 한다."))[0].condition_id
    assert CONDITION_PARSER_SCHEMA_VERSION


def test_hashless_legacy_conditions_do_not_collide_across_sources():
    original = replace(chunk("3일"), evidence_id="")
    renamed = replace(original, source_file="other.pdf")
    assert parse_permit_conditions(original)[0].condition_id != parse_permit_conditions(renamed)[0].condition_id


def test_parser_uses_owned_text_not_descendant_expanded_search_text():
    from sp_pdf_judger.permit_catalog import PermitPolicy
    from sp_pdf_judger.permit_pdf_store import PermitPdfStore
    store = PermitPdfStore.from_page_texts([(1, "1. 원액\n80% 이상이어야 한다.\n1.1. 시험\n3일")],
        policy=PermitPolicy("test", True, True, True, "auto"))
    result = parse_permit_conditions(store.chunks[0])
    assert len(result) == 1 and result[0].value == Decimal(80)


def test_large_decimal_identity_is_exact_without_default_context_rounding():
    # Break caught: canonical ID encoding performs arithmetic under 28-digit precision.
    text = "123456789012345678901234567890.123456789 이상"
    condition, = parse_permit_conditions(chunk(text))
    assert condition.value == Decimal("123456789012345678901234567890.123456789")
    assert condition.condition_id == parse_permit_conditions(chunk(text))[0].condition_id


def test_physical_line_wraps_preserve_all_egg_requirements_and_original_quotes():
    # Break caught: extraction wraps cause partial keyword recognition or lost negation.
    wrapped = EGG.replace("유정란", "유정\n란").replace("혈구응집반응", "혈구응집반\n응")
    wrapped = wrapped.replace("계대", "계\n대").replace("이상", "이\n상")
    result = parse_permit_conditions(chunk(wrapped))
    assert Counter(c.metric_key for c in result) == {
        "sample_age": 2, "sample_count": 2, "incubation_temperature": 4,
        "incubation_duration": 4, "survival_rate": 4, "passage": 2,
        "haemagglutination": 1, "compound": 4,
    }
    assert all(c.quote in wrapped for c in result)


@pytest.mark.parametrize("text", [
    "결과는 음성이어야 하거나 양성이어야 한다.",
    "결과는 음성 또는 양성이어야 한다.",
    "3일 또는 5일이어야 한다.",
])
def test_unsupported_alternatives_remain_unparsed_not_mandatory_conjunctions(text):
    # Break caught: a permissive regex turns alternatives into incompatible obligations.
    result = parse_permit_conditions(chunk(text))
    assert len(result) == 1
    assert result[0].kind == "unparsed" and result[0].mandatory
    assert result[0].quote == text


@pytest.mark.parametrize("text", [
    "3일 혹은 5일이어야 한다.",
    "3일 또는 5일",
    "3일 혹은 5일",
    "음성 또는 양성",
    "3일이거나 5일이어야 한다.",
    "3일 내지 5일이어야 한다.",
    "요막강 내 접종의 경우, 3일 혹은 5일",
])
def test_all_unsupported_alternative_criteria_retain_one_scoped_mandate(text):
    # Break caught: unmatched OR spelling becomes AND, or a bare criterion disappears.
    result = parse_permit_conditions(chunk(text))
    assert len(result) == 1
    condition = result[0]
    assert (condition.kind, condition.metric_key, condition.mandatory) == ("unparsed", "unparsed", True)
    assert (condition.quote, condition.scope_text) == (text, text)
    assert (condition.evidence_id, condition.stage_key, condition.test_key) == ("evidence-1", "원액", "시험")
    assert condition.children == ()
    assert condition.method_key == ("요막강내접종" if text.startswith("요막강") else None)


@pytest.mark.parametrize("text", [
    "배양시간은 1시간 30분이어야 한다.",
    "1시간30분",
    "배양시간은 1일 2시간 30분이어야 한다.",
    "농도는 1 x 10^3 이상이어야 한다.",
    "1x10^3 이상",
    "1 × 10³ 이상",
    "1e3 이상",
    "1E-3 이상",
    "농도는 10^3 이상이어야 한다.",
    "농도는 1:2 이상이어야 한다.",
    "최소 10개로 시험하고 배양시간은 1시간 30분이어야 한다.",
])
def test_partial_numeric_expressions_never_become_independent_mandates(text):
    # Break caught: finditer accepts pieces of one quantity as complete independent criteria.
    result = parse_permit_conditions(chunk(text))
    assert len(result) == 1
    condition = result[0]
    assert (condition.kind, condition.metric_key, condition.mandatory) == ("unparsed", "unparsed", True)
    assert (condition.value, condition.lower, condition.upper, condition.children) == (None, None, None, ())
    assert (condition.quote, condition.scope_text) == (text, text)
    assert (condition.evidence_id, condition.stage_key, condition.test_key) == ("evidence-1", "원액", "시험")
