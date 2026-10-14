"""A valid subset must not hide missing/invalid dates in another selected item."""
from types import SimpleNamespace

import pytest

from sp_pdf_judger.extractor import _record_from_dict
from sp_pdf_judger.policy_engine import PolicyContext, Rule, duration, expiry, signature_order


def context(*rows):
    records = [_record_from_dict(row, i) for i, row in enumerate(rows)]
    ctx = SimpleNamespace(records=records, evidence=lambda r: {"source": "sp", "page": 1, "quote": r.content or r.test_period or ""})
    ctx.select = lambda selector: PolicyContext.select(ctx, selector)
    return ctx


def rule(operation, **params):
    return Rule(id="DATES", title="날짜 검사", instruction="선택된 모든 대상 확인", operation=operation,
                selector={"type": "test"} if operation == "duration" else {"type": "content"}, params=params)


@pytest.mark.parametrize("incomplete", ["", "2026.01.01", "2026.02.30 ~ 2026.03.30", "2026.01.01 ~ 미정",
                                         "2026.01.01 ~ 2026.02.01, 2026.02.30"])
def test_duration_requires_complete_dates_for_every_selected_test(incomplete):
    ctx = context({"record_type": "test", "test_name": "A", "test_period": "2026.01.01 ~ 2026.02.01"},
                  {"record_type": "test", "test_name": "B", "test_period": incomplete})
    result = duration(rule("duration", minimum_days=28), ctx)
    assert result.status == "HOLD"
    assert result.details["incomplete_evidence"]


def test_known_duration_failure_survives_another_missing_date():
    ctx = context({"record_type": "test", "test_name": "A", "test_period": "2026.01.01 ~ 2026.01.20"},
                  {"record_type": "test", "test_name": "B"})
    result = duration(rule("duration", minimum_days=28), ctx)
    assert result.status == "FAIL"
    assert result.details["incomplete_evidence"]


@pytest.mark.parametrize("end", ["", "2026.02.30", "2026.01.31, 2026.02.30", "2026.01.31 또는 미정"])
def test_expiry_requires_complete_fields_for_every_selected_record(end):
    ctx = context({"record_type": "content", "content": "제조일: 2025.01.31\n유효일: 2026.01.31\n사용기간: 12개월"},
                  {"record_type": "content", "content": f"제조일: 2025.01.31\n유효일: {end}\n사용기간: 12개월"})
    result = expiry(rule("expiry", start_field="제조일", end_field="유효일", duration_field="사용기간"), ctx)
    assert result.status == "HOLD"


@pytest.mark.parametrize("bad_test_date", ["", "2026.02.30", "2026.01.31 ~ 미정", "2026.01.31, 2026.02.30", "2026.01.31 -"])
def test_signature_cannot_ignore_one_missing_or_invalid_test_date(bad_test_date):
    ctx = context({"record_type": "content", "content": "날짜: 2026.03.10"},
                  {"record_type": "test", "test_name": "A", "test_period": "2026.03.01"},
                  {"record_type": "test", "test_name": "B", "test_period": bad_test_date})
    assert signature_order(rule("signature_order", tests={"type": "test"}), ctx).status == "HOLD"


def test_signature_cannot_ignore_one_undated_signer():
    ctx = context({"record_type": "content", "content": "날짜: 2026.03.10"},
                  {"record_type": "content", "content": "날짜: 미정"},
                  {"record_type": "test", "test_name": "A", "test_period": "2026.03.01"})
    assert signature_order(rule("signature_order", tests={"type": "test"}), ctx).status == "HOLD"


def test_known_early_signature_is_not_hidden_by_another_missing_test_date():
    ctx = context({"record_type": "content", "content": "날짜: 2026.03.10"},
                  {"record_type": "test", "test_name": "A", "test_period": "2026.03.11"},
                  {"record_type": "test", "test_name": "B"})
    result = signature_order(rule("signature_order", tests={"type": "test"}), ctx)
    assert result.status == "FAIL"
    assert result.details["incomplete_evidence"]


def test_complete_multi_day_and_comma_dates_remain_supported():
    ctx = context({"record_type": "content", "content": "날짜: 2026년 03월 10일"},
                  {"record_type": "test", "test_name": "A", "test_period": "2026.03.01 ~ 2026.03.03"},
                  {"record_type": "test", "test_name": "B", "test_period": "2026.03.04, 2026.03.05"})
    assert signature_order(rule("signature_order", tests={"type": "test"}), ctx).status == "PASS"


def test_leap_month_end_uses_calendar_months_not_fixed_days():
    ctx = context({"record_type": "content", "content": "제조일: 2024.01.31\n유효일: 2024.02.29\n사용기간: 1개월"})
    policy = rule("expiry", start_field="제조일", end_field="유효일", duration_field="사용기간")
    assert expiry(policy, ctx).status == "PASS"
    ctx.records[0].content = ctx.records[0].content.replace("2024.02.29", "2024.03.01")
    assert expiry(policy, ctx).status == "FAIL"
