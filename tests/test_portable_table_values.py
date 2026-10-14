"""Layout variations observed in cloud extraction; values are never supplied by gold data."""
from types import SimpleNamespace

import pytest

from sp_pdf_judger.policy_engine import (
    Rule, expiry, field_value, mass_balance, permit_field, unique_field_value,
)
from sp_pdf_judger.schemas import ExtractedRecord


def record(text):
    return ExtractedRecord(record_type="content", content=text, raw_text=text, page_start=2)


def context(text):
    r = record(text)
    return SimpleNamespace(select=lambda _: [r], evidence=lambda _: {"source": "sp", "page": 2, "quote": text},
                           permit_pages=[("permit.pdf", 1, "제품명: 새로운제품\n주소: 서울시 예시로 12")])


@pytest.mark.parametrize("line", [
    "제조번호 | | LOT-42", "| 제 조 번 호 | | LOT-42 |", "제조번호：LOT-42",
    "제조번호 |  |  LOT-42 | |", "제조번호 : LOT-42",
])
def test_boundary_empty_cells_are_not_part_of_a_field(line):
    assert field_value(record(line), "제조번호") == "LOT-42"


def test_inner_separators_and_conflicting_duplicate_values_are_not_silently_erased():
    assert field_value(record("제품명 | A | B"), "제품명") != "AB"
    assert field_value(record("제조번호 | LOT-1\n제조번호 | LOT-2"), "제조번호") == ""
    assert unique_field_value(record("제조일 | 2025.01.01\n제조일 | 2025.01.01"), "제조일") == ""
    assert field_value(record("제조번호확인 | LOT-1"), "제조번호") == ""


def test_manufacturer_nested_labels_retain_name_and_address():
    text = "제조사 | 명칭 | | 새로운회사㈜ | 주소 | | 서울시 예시로 12"
    assert field_value(record(text), "명칭 제조사 주소") == "새로운회사㈜ 서울시 예시로 12"
    assert field_value(record(text), "주소") == "서울시 예시로 12"


def test_vertical_cells_preserve_pairs_without_borrowing_next_label():
    text = "제조사 | 명칭\n| | 새로운회사㈜\n| 주소\n| | 서울시 예시로 12"
    assert field_value(record(text), "명칭 제조사 주소") == "새로운회사㈜ 서울시 예시로 12"
    assert field_value(record("유효일\n| | 2026.01.01\n제조번호 | LOT-42"), "유효일") == "2026.01.01"
    assert field_value(record("유효일\n제조번호 | LOT-42"), "유효일") == ""


def test_manufacturer_address_can_be_on_the_next_physical_row():
    text = "제조사 | 명칭 | 새로운회사㈜\n| 주소 | 서울시 예시로 12\n제품명 | 새로운제품"
    assert field_value(record(text), "명칭 제조사 주소") == "새로운회사㈜ 서울시 예시로 12"
    assert field_value(record(text), "제품명") == "새로운제품"


def test_markdown_rows_are_not_mistaken_for_continuation_cells():
    text = "| 제품명 | 제품 A |\n| 제조번호 | LOT-42 |"
    assert field_value(record(text), "제품명") == "제품 A"
    assert field_value(record(text), "제조번호") == "LOT-42"


def test_empty_nested_address_is_not_replaced_by_company_or_other_row():
    text = "제조사 | 명칭 | | 새로운회사㈜ | 주소 | |\n신청자 | 누군가"
    assert not field_value(record(text), "주소")


def test_cloud_product_and_expiry_are_compared_without_layout_delimiters():
    rule = Rule(id="P", title="제품명", operation="permit_field", instruction="같은 제품 확인",
                params={"sp_field": "제품명", "permit_pattern": r"제품명:\s*([^\n]+)"})
    assert permit_field(rule, context("제 품 명 | | 새로운제품")).status == "PASS"
    assert permit_field(rule, context("제 품 명 | | 다른제품")).status == "FAIL"
    rule = Rule(id="D", title="유효일", operation="expiry", instruction="유효일 확인",
                params={"start_field": "제조일", "end_field": "유효일", "duration_field": "사용기간"})
    assert expiry(rule, context("제조일 | | 2025.01.18\n유효일 | | 2026.07.18\n사용기간 | | 18개월")).status == "PASS"


def balance(rows, output="285 L"):
    text = f"제조량 | | {output}\n직접투입\n원료명 | 성분명 | 제조번호 | 분량\n{rows}\n하위조성"
    rule = Rule(id="M", title="합산", operation="mass_balance", instruction="직접투입 합산",
                params={"output_field": "제조량", "start_marker": "직접투입", "end_marker": "하위조성", "comparison": "equal"})
    return mass_balance(rule, context(text))


def test_wrapped_material_description_is_not_an_extra_quantity_row():
    rows = "원액 | 단백질 | LOT- | 270 L\n| RBD 항원 | 42 |\n완충액 | 완충액 | B-1 | 15 L"
    assert balance(rows).status == "PASS"
    assert balance(rows, "286 L").status == "FAIL"
    assert balance(rows.replace("42 |\n", "42\n")).status == "PASS"


@pytest.mark.parametrize("extra", [
    "새원료 | 첨가제 | C-1 |", "| 첨가제 | C-1 |", "| 첨가제 | | 미정",
    "알수없는별도행", "| 첨가제 | C-1 | 2 L",
])
def test_missing_or_additional_materials_cannot_disappear_as_wrapping(extra):
    rows = "원액 | 단백질 | LOT-42 | 270 L\n" + extra + "\n완충액 | 완충액 | B-1 | 15 L"
    assert balance(rows).status != "PASS"


def test_repeated_header_and_markdown_outer_border_are_supported():
    rows = "| 원액 | 단백질 | LOT-42 | 270 L |\n원료명 | 성분명 | 제조번호 | 분량\n| 완충액 | 완충액 | B-1 | 15 L |"
    assert balance(rows).status == "PASS"
