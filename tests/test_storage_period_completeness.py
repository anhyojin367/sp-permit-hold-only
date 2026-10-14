"""Storage checks cannot pass only the complete subset or hide a known failure."""
from types import SimpleNamespace

import pytest

from sp_pdf_judger.policy_engine import Rule, storage_period
from sp_pdf_judger.schemas import ExtractedRecord


def evaluate(*periods):
    records = [ExtractedRecord(record_type="content", section_title=f"원액{i}",
        content=f"제조년월일: 2026.01.01\n저장기간: {period}\n허가된 저장기간: 12개월")
        for i, period in enumerate(periods)]
    ctx = SimpleNamespace(select=lambda _: records,
        evidence=lambda r: {"source": "sp", "quote": r.content})
    rule = Rule(id="STORAGE", title="저장기간", operation="storage_period", instruction="선택 대상의 시작·종료일 확인")
    return storage_period(rule, ctx)


@pytest.mark.parametrize("incomplete", ["", "미정", "2026.01.01 ~ 미정",
    "2026.01.01 ~ 2026.02.01 또는 미정", "2026.01.01 ~ 2026.02.01, 2026.02.30"])
def test_missing_or_invalid_storage_dates_never_pass_as_a_subset(incomplete):
    result = evaluate("2026.01.01 ~ 2026.02.01", incomplete)
    assert result.status == "HOLD"
    assert result.details["incomplete_evidence"]


@pytest.mark.parametrize("incomplete", ["", "미정", "2026.01.01 ~ 미정"])
def test_proven_storage_failure_survives_another_incomplete_period(incomplete):
    for periods in [("2026.01.02 ~ 2026.02.01", incomplete), (incomplete, "2026.01.02 ~ 2026.02.01")]:
        result = evaluate(*periods)
        assert result.status == "FAIL"
        assert result.details["incomplete_evidence"]


def test_complete_storage_dates_and_explicit_exceeded_limit():
    assert evaluate("2026.01.01 ~ 2026.02.01").status == "PASS"
    assert evaluate("2026.01.01 ~ 2027.01.02").status == "FAIL"
    assert evaluate().status == "HOLD"
