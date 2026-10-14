"""No partial month parsing, silently skipped limits, or lost date evidence."""
from types import SimpleNamespace

import pytest

from sp_pdf_judger.policy_engine import (
    PolicyContext, Rule, RuleBook, expiry, storage_period, storage_window,
    test_after_manufacture as after_manufacture,
)
from sp_pdf_judger.policy_summary import manufacturing_policy_cards
from sp_pdf_judger.schemas import ExtractedRecord


def context(*records):
    ctx = SimpleNamespace(records=list(records))
    ctx.select = lambda selector: PolicyContext.select(ctx, selector)
    ctx.evidence = lambda r: {"source": "sp", "page": r.page_start,
                              "quote": r.content or r.test_period or "missing date"}
    return ctx


def policy(operation, **params):
    return Rule(id="CALENDAR", title="기간 확인", instruction="전체 기간 근거를 확인한다.",
                operation=operation, params=params)


def source(months="12개월", made="2026.01.01", end="2026.02.01"):
    return ExtractedRecord(record_type="content", section_title="원액", page_start=2,
        content=f"제조년월일: {made}\n유효일: {end}\n사용기간: {months}\n"
                f"저장기간: {made} ~ {end}\n허가된 저장기간: {months}")


def expiry_rule(**params):
    return policy("expiry", start_field="제조년월일", end_field="유효일",
                  duration_field="사용기간", **params)


@pytest.mark.parametrize("operation", ["expiry", "storage_period", "storage_window"])
@pytest.mark.parametrize("months", ["-12개월", "1.5개월", "12~18개월", "12개월 또는 미정",
                                    "12개월 (예정)", "999999999개월", "", "미정"])
def test_partial_or_unsupported_months_never_pass(operation, months):
    record = source(months)
    if operation == "expiry":
        result = expiry(expiry_rule(), context(record))
    elif operation == "storage_period":
        result = storage_period(policy(operation), context(record))
    else:
        rule = Rule(id="CALENDAR", title="저장 한도", instruction="한도를 확인한다.",
                    operation=operation, selector={"type": "content"}, params={"target": {"type": "test"}})
        target = ExtractedRecord(test_name="시험", test_period="2026.02.01")
        result = storage_window(rule, context(record, target))
    assert result.status == "HOLD"
    assert result.details["incomplete_evidence"]


@pytest.mark.parametrize("months", ["12개월", "제조일로부터 12개월", " 제조일로부터  12 개월 "])
def test_complete_month_duration_is_supported(months):
    assert expiry(expiry_rule(), context(source(months))).status == "PASS"
    assert storage_period(policy("storage_period"), context(source(months))).status == "PASS"


def test_expiry_before_manufacture_is_an_impossible_interval():
    result = expiry(expiry_rule(), context(source(end="2025.12.31")))
    assert result.status == "FAIL"


@pytest.mark.parametrize("months", ["12개월", "미정"])
def test_impossible_expiry_is_not_hidden_by_unknown_duration(months):
    result = expiry(expiry_rule(), context(source(months, end="2025.12.31")))
    assert result.status == "FAIL"
    if months == "미정":
        assert result.details["incomplete_evidence"]


def test_storage_invalid_limit_is_audited_even_with_proven_bad_start():
    record = source("미정")
    record.content = record.content.replace("저장기간: 2026.01.01", "저장기간: 2026.01.02")
    result = storage_period(policy("storage_period"), context(record))
    assert result.status == "FAIL"
    assert result.details["incomplete_evidence"]


def test_absent_storage_limit_is_not_optional_by_accident():
    record = source()
    record.content = record.content.split("허가된 저장기간:")[0]
    assert storage_period(policy("storage_period"), context(record)).status == "HOLD"


@pytest.mark.parametrize("operation, label", [("expiry", "유효일"), ("expiry", "사용기간"),
                                              ("storage_period", "허가된 저장기간")])
def test_duplicate_field_must_not_select_first_value(operation, label):
    record = source()
    record.content += f"\n{label}: 미정"
    rule = expiry_rule() if operation == "expiry" else policy(operation)
    result = {"expiry": expiry, "storage_period": storage_period}[operation](rule, context(record))
    assert result.status == "HOLD"


@pytest.mark.parametrize("made, months, end, status", [
    ("2024.01.31", "1개월", "2024.02.29", "PASS"),
    ("2023.01.31", "1개월", "2023.02.28", "PASS"),
    ("2024.02.29", "12개월", "2025.02.28", "PASS"),
    ("2024.02.29", "12개월", "2025.03.01", "FAIL"),
    ("2025.12.31", "2개월", "2026.02.28", "PASS"),
])
def test_calendar_boundary(made, months, end, status):
    assert expiry(expiry_rule(), context(source(months, made, end))).status == status


def after_rule():
    return Rule(id="AFTER", title="시험일 순서", instruction="모든 제조·시험일을 확인한다.",
        operation="test_after_manufacture", report_group="manufacturing_info",
        params={"stages": [{"source": ["원액"], "target": ["원액"]}]})


def dated_test(value, name="시험"):
    return ExtractedRecord(test_name=name, section_title="원액", page_start=3, test_period=value)


@pytest.mark.parametrize("value", ["2026.02.01 ~ 미정", "2026.02.01, 2026.02.30",
                                  "2026.02.01 또는 미정", "2026.02.01 -", "2026.02.01 12:00"])
def test_after_manufacture_rejects_partial_test_date(value):
    result = after_manufacture(after_rule(), context(source(), dated_test(value)))
    assert result.status == "HOLD"
    assert result.details["incomplete_evidence"]
    assert len(result.evidence) == 2


@pytest.mark.parametrize("value", ["", "2026.01.01 ~ 미정", "2026.01.01, 2026.02.30",
                                  "2026.01.01, 2026.01.02"])
def test_after_manufacture_cannot_drop_an_invalid_source(value):
    result = after_manufacture(after_rule(), context(source(), source(made=value), dated_test("2026.02.01")))
    assert result.status == "HOLD"
    assert result.details["incomplete_evidence"]
    assert len(result.evidence) == 3


def test_after_failure_preserves_missing_audit_and_ui_hold_row():
    rule = after_rule()
    result = after_manufacture(rule, context(source(), dated_test("2025.12.31", "앞선시험"), dated_test("", "누락시험")))
    assert result.status == "FAIL"
    assert result.details["incomplete_evidence"]
    assert len(result.evidence) == 3
    cards = manufacturing_policy_cards([result], SimpleNamespace(rules=[rule]))
    assert cards[0].status == "불합격"
    assert {t.status for t in cards[0].test_dates} == {"불합격", "보류"}


def test_after_complete_single_range_and_list_dates_remain_supported():
    result = after_manufacture(after_rule(), context(source(), dated_test("2026.01.01"),
        dated_test("2026.02.01 ~ 2026.03.01"), dated_test("2026.02.01, 2026.03.01")))
    assert result.status == "PASS"


def test_md_only_switch_between_expiry_ceiling_and_exact_end(tmp_path):
    text = """---
rules:
  - id: CALENDAR
    title: 유효일
    instruction: 사용기간에 맞는 유효일을 확인한다.
    operation: expiry
    params: {start_field: 제조년월일, end_field: 유효일, duration_field: 사용기간, end_policy: not_after}
---
"""
    path = tmp_path / "calendar.md"
    path.write_text(text, encoding="utf-8")
    first = RuleBook(tmp_path)
    assert expiry(first.rules[0], context(source())).status == "PASS"
    path.write_text(text.replace("not_after", "exact"), encoding="utf-8")
    second = RuleBook(tmp_path)
    assert second.fingerprint != first.fingerprint
    assert expiry(second.rules[0], context(source())).status == "FAIL"
    assert expiry(second.rules[0], context(source(end="2027.01.01"))).status == "PASS"


def test_storage_duration_field_is_md_configurable():
    record = source()
    record.content = record.content.replace("허가된 저장기간", "허가 한도")
    assert storage_period(policy("storage_period", duration_field="허가 한도"), context(record)).status == "PASS"


@pytest.mark.parametrize("reverse", [False, True])
def test_known_expiry_failure_survives_incomplete_sibling(reverse):
    records = [source(end="2027.01.02"), source("미정")]
    if reverse:
        records.reverse()
    result = expiry(expiry_rule(), context(*records))
    assert result.status == "FAIL"
    assert result.details["incomplete_evidence"]


def test_conflicting_manufacturing_dates_cannot_be_assigned_to_one_test():
    result = after_manufacture(after_rule(), context(source(), source(made="2026.01.02"), dated_test("2026.02.01")))
    assert result.status == "HOLD"
    assert len(result.evidence) == 3


def test_duplicate_storage_target_manufacturing_date_is_not_first_value_wins():
    target = source()
    target.section_title = "완제품"
    target.content += "\n제조년월일: 미정"
    rule = Rule(id="CALENDAR", title="저장 한도", instruction="한도를 확인한다.",
        operation="storage_window", selector={"context": ["원액"]}, params={"target": {"context": ["완제품"]}})
    assert storage_window(rule, context(source(), target)).status == "HOLD"
