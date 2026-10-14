"""A complete item must not hide incomplete siblings or a proven violation."""
from types import SimpleNamespace

import pytest

from sp_pdf_judger.policy_engine import (
    PolicyContext, Rule, mass_balance, permit_field, permit_test, precision,
    process_order, storage_window,
)
from sp_pdf_judger.schemas import ExtractedRecord


def context(*records):
    ctx = SimpleNamespace(records=list(records))
    ctx.select = lambda selector: PolicyContext.select(ctx, selector)
    ctx.evidence = lambda r: {"source": "sp", "page": r.page_start,
                              "quote": r.content or r.result or r.test_period or ""}
    return ctx


def policy(operation, selector=None, **params):
    return Rule(id="SUBSET", title="전체 근거 검사", instruction="선택 대상 전체를 확인한다.",
                operation=operation, selector=selector or {}, params=params)


def source(start="2026.01.01", months="12개월"):
    return ExtractedRecord(record_type="content", section_title="최종원액",
        content=f"제조년월일: {start}\n허가된 저장기간: {months}")


def target(value):
    return ExtractedRecord(record_type="test", section_title="완제의약품",
                           test_name="무균시험", test_period=value)


def storage(*records):
    return storage_window(policy("storage_window", {"context": ["최종원액"]},
        target={"context": ["완제의약품"]}), context(*records))


@pytest.mark.parametrize("value", ["", "미정", "2026.02.01 ~ 미정", "2026.02.01, 2026.02.30"])
def test_storage_window_checks_every_target(value):
    result = storage(source(), target("2026.02.01"), target(value))
    assert result.status == "HOLD"
    assert result.details["incomplete_evidence"]


@pytest.mark.parametrize("bad_source", [source(start=""), source(start="2026.01.01 또는 미정"),
    source(start="2026.01.01, 2026.02.30"), source(months=""), source(months="12개월 또는 미정")])
def test_storage_window_checks_every_source(bad_source):
    assert storage(source(), bad_source, target("2026.02.01")).status == "HOLD"


def test_storage_violation_kept_with_missing_sibling():
    result = storage(source(), target("2027.01.02"), target(""))
    assert result.status == "FAIL"
    assert result.details["incomplete_evidence"]


def test_storage_window_complete_boundary():
    assert storage(source(), target("2027.01.01")).status == "PASS"
    assert storage(source(), target("2027.01.02")).status == "FAIL"


def test_storage_heading_is_not_a_missing_manufacturing_record():
    heading = ExtractedRecord(record_type="heading", section_title="완제의약품")
    assert storage(source(), heading, target("2026.02.01")).status == "PASS"
    assert storage(source(), heading).status == "HOLD"


def process(first="2026.01.01", second="2026.02.01"):
    record = ExtractedRecord(record_type="diagram", diagram_data={"nodes": [
        {"node_id": "a", "name": "A", "fields": {"제조년월일": first}},
        {"node_id": "b", "name": "B", "fields": {"제조년월일": second}},
        {"node_id": "c", "name": "C", "fields": {"제조년월일": "2026.03.01"}},
    ], "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]})
    return process_order(policy("process_order"), context(record))


@pytest.mark.parametrize("value", ["2026.01.01 또는 미정", "2026.01.01, 2026.02.30", "2026.01.01 ~ 미정"])
def test_process_order_rejects_partially_parsed_node_date(value):
    result = process(first=value)
    assert result.status == "HOLD"
    assert result.details["incomplete_evidence"]


def test_process_violation_kept_with_missing_other_edge():
    result = process(first="", second="2026.04.01")
    assert result.status == "FAIL"
    assert result.details["incomplete_evidence"]


def field(*values):
    ctx = context(*(ExtractedRecord(record_type="content", section_title="신청제품정보",
        content=f"제품명: {value}") for value in values))
    ctx.permit_pages = [("permit.pdf", 1, "제품명: 승인제품\n")]
    return permit_field(policy("permit_field", sp_field="제품명", permit_pattern=r"제품명:\s*([^\n]+)"), ctx)


def test_permit_field_cannot_pass_only_present_fields():
    result = field("승인제품", "")
    assert result.status == "HOLD"
    assert result.details["incomplete_evidence"]


def test_permit_field_violation_survives_missing_value():
    for values in [("다른제품", ""), ("", "다른제품")]:
        result = field(*values)
        assert result.status == "FAIL"
        assert result.details["incomplete_evidence"]


def test_permit_field_control():
    assert field("승인제품", "승인제품").status == "PASS"
    assert field("다른제품").status == "FAIL"


def permit_methods(*methods):
    records = [ExtractedRecord(record_type="test", test_name="무균시험", method=m) for m in methods]
    ctx = context(*records)
    candidate = SimpleNamespace(title="무균시험", normalized_stage_path="원액",
                               text="유럽약전", source_file="permit.pdf", page_number=1)
    ctx.permit_store = SimpleNamespace(enabled=True, search=lambda r, top_k: [candidate])
    return permit_test(policy("permit_test", permit_pattern=r"(유럽약전|대한민국약전)",
        sp_pattern=r"(유럽약전|대한민국약전)", sp_attribute="method"), ctx)


@pytest.mark.parametrize("methods", [("대한민국약전", ""), ("", "대한민국약전")])
def test_permit_test_missing_evidence_cannot_erase_known_mismatch(methods):
    result = permit_methods(*methods)
    assert result.status == "FAIL"
    assert result.details["incomplete_evidence"]


def test_permit_test_control():
    assert permit_methods("유럽약전", "유럽약전").status == "PASS"
    assert permit_methods("유럽약전", "").status == "HOLD"


def mixture(output="150 L", rows="성분A | 100 L\n성분B | 50 L"):
    return ExtractedRecord(record_type="content", section_title="최종원액", content=(
        f"제조량: {output}\n직접 투입\n{rows}\n하위 조성\n하위성분 | 100 L"))


def balance(*records):
    return mass_balance(policy("mass_balance", output_field="제조량",
                              start_marker="직접 투입", end_marker="하위 조성"), context(*records))


@pytest.mark.parametrize("incomplete", [mixture(output=""), mixture(rows="성분A | 미정"),
    mixture(rows="성분A | 100 mL"), mixture(rows="성분A | 150 L\n성분B | 미정")])
def test_mass_balance_cannot_ignore_incomplete_amounts(incomplete):
    result = balance(mixture(), incomplete)
    assert result.status == "HOLD"
    assert result.details["incomplete_evidence"]


def test_mass_balance_requires_explicit_direct_input_boundaries():
    record = mixture()
    record.content = record.content.replace("직접 투입", "목록 제목 누락")
    assert balance(record).status == "HOLD"


def test_mass_balance_keeps_known_violation_and_missing_audit():
    result = balance(mixture(output="151 L"), mixture(output=""))
    assert result.status == "FAIL"
    assert result.details["incomplete_evidence"]


def test_mass_balance_control_excludes_nested_ingredients():
    assert balance(mixture()).status == "PASS"
    assert balance(mixture(output="151 L")).status == "FAIL"


def digit_check(*results):
    records = [ExtractedRecord(record_type="test", test_name="함량시험",
        criteria="90.0% 이상", result=value) for value in results]
    return precision(policy("precision"), context(*records))


@pytest.mark.parametrize("result", ["", "미정", "95.0", "95.0 mg/mL"])
def test_precision_requires_result_for_each_applicable_item(result):
    value = digit_check("95.0%", result)
    assert value.status == "HOLD"
    assert value.details["incomplete_evidence"]


def test_precision_keeps_mismatch_and_missing_audit():
    value = digit_check("95.00%", "")
    assert value.status == "FAIL"
    assert value.details["incomplete_evidence"]


def test_precision_control():
    assert digit_check("95.0%", "99.0%").status == "PASS"
    assert digit_check("95.00%").status == "FAIL"


@pytest.mark.parametrize("criteria,result,expected", [
    ("8.0 미만", "7.66", "FAIL"),
    ("8.0 미만", "7.6", "PASS"),
    ("8.0 이상", "8.2", "PASS"),
    ("≤ 8.0", "7.6", "PASS"),
])
def test_precision_applies_to_unitless_operator_criteria(criteria, result, expected):
    record = ExtractedRecord(record_type="test", test_name="pH측정시험", criteria=criteria, result=result)
    assert precision(policy("precision"), context(record)).status == expected
