"""C.3: compare full process timestamps and the complete stated duration."""
from types import SimpleNamespace

import pytest

from sp_pdf_judger.policy_engine import PolicyContext, Rule, OPERATIONS, RuleBook
from sp_pdf_judger.schemas import ExtractedRecord


def process(start="2026.09.25 23:45", end="2026.09.26 01:15", duration="1시간 30분"):
    return ExtractedRecord(record_type="content", section_title="배양 공정", page_start=4,
        content=f"시작일시: {start}\n종료일시: {end}\n처리시간: {duration}")


def judge(*records, **params):
    ctx = SimpleNamespace(records=list(records))
    ctx.select = lambda selector: PolicyContext.select(ctx, selector)
    ctx.evidence = lambda r: {"source": "sp", "page": r.page_start, "quote": r.content}
    rule = Rule(id="C3", title="공정 처리시간", aliases=["C.3"], instruction="경과시간을 비교한다.",
        operation="elapsed_time", selector={"type": "content"},
        params={"start_field": "시작일시", "end_field": "종료일시", "duration_field": "처리시간", **params})
    return OPERATIONS[rule.operation](rule, ctx)


@pytest.mark.parametrize("duration", ["1시간 30분", "90분", "1.5시간", "5400초", "1 h 30 min", "90 min", "5400 s"])
def test_equivalent_elapsed_units(duration):
    result = judge(process(duration=duration))
    assert result.status == "PASS"
    assert result.details["elapsed_checks"][0]["calculated_seconds"] == "5400"
    assert result.evidence[0]["page"] == 4


@pytest.mark.parametrize("start,end,duration", [
    ("2024.02.28 23:00", "2024.03.01 01:00", "1일 2시간"),
    ("2026/12/31 23:59:30", "2027/01/01 00:00:00", "30초"),
    ("2026-09-26T09:00:00", "2026-09-26T09:00:01", "1초"),
    ("2026년 9월 26일 9시 00분", "2026년 9월 26일 9시 30분", "30분"),
    ("2026년 9월 26일 9시 00분 01초", "2026년 9월 26일 9시 00분 31초", "30초"),
    ("2026.09.26 09:00", "2026.09.26 09:00", "0분"),
])
def test_full_timestamp_boundaries(start, end, duration):
    assert judge(process(start, end, duration)).status == "PASS"


@pytest.mark.parametrize("value", ["", "미정", "09:00", "2026.09.26", "2026.02.30 09:00",
    "2026.09.26 24:00", "2026.09.26 09:61", "2026.09.26 09:00 예정",
    "2026.09.26 09:00 ~ 미정", "2026-09-26T09:00Z", "2026.09.26 09:00 +09:00",
    "2026.09.26 09:00 / 2026.09.26 10:00"])
def test_partial_or_ambiguous_timestamp_never_passes(value):
    result = judge(process(start=value))
    assert result.status == "HOLD"
    assert result.details["incomplete_evidence"]


@pytest.mark.parametrize("value", ["", "미정", "90", "1:30", "-90분", "1~2시간", "약 90분", "90분 이하",
    "90분 또는 미정", "1시간 90분", "30분 1시간", "1시간 1시간", "NaN시간", "1개월", "1e2분"])
def test_duration_must_be_complete_and_unambiguous(value):
    result = judge(process(duration=value))
    assert result.status == "HOLD"
    assert result.details["incomplete_evidence"]


def test_mismatch_is_fail_and_records_actual_difference():
    result = judge(process(duration="89분"))
    assert result.status == "FAIL"
    assert result.details["elapsed_checks"][0]["difference_seconds"] == "60"


@pytest.mark.parametrize("tolerance,status", [(59, "FAIL"), (60, "PASS"), (61, "PASS")])
def test_tolerance_is_explicit_md_policy(tolerance, status):
    assert judge(process(duration="89분"), tolerance_seconds=tolerance).status == status


def test_unknown_duration_does_not_hide_reversed_timestamp():
    result = judge(process(start="2026.09.26 02:00", duration="미정"))
    assert result.status == "FAIL"
    assert result.details["incomplete_evidence"]


@pytest.mark.parametrize("field", ["시작일시", "종료일시", "처리시간"])
def test_duplicate_fields_are_not_first_value_wins(field):
    record = process()
    record.content += f"\n{field}: 미정"
    assert judge(record).status == "HOLD"


def test_valid_subset_does_not_hide_missing_or_proven_violation():
    incomplete = process(end="미정")
    assert judge(process(), incomplete).status == "HOLD"
    result = judge(process(duration="2시간"), incomplete)
    assert result.status == "FAIL"
    assert result.details["incomplete_evidence"]
    assert len(result.evidence) == 2


def test_empty_selection_is_not_pass():
    assert judge().status == "HOLD"


@pytest.mark.parametrize("params", [{"tolerance_seconds": -1}, {"tolerance_seconds": "60"},
    {"tolerance_seconds": True}, {"tolerance_seconds": 0.5}, {"start_field": "종료일시"},
    {"tolerence_seconds": 60}])
def test_invalid_md_configuration_is_rejected(params):
    with pytest.raises(ValueError):
        judge(process(), **params)


def test_md_only_tolerance_changes_decision_and_fingerprint(tmp_path):
    template = """---
rules:
  - id: C3
    title: 공정 시간
    operation: elapsed_time
    instruction: 모든 처리시간을 비교한다.
    params:
      start_field: 시작일시
      end_field: 종료일시
      duration_field: 처리시간
      tolerance_seconds: %s
---
"""
    path = tmp_path / "rules.md"
    path.write_text(template % 0, encoding="utf-8")
    strict = RuleBook(tmp_path)
    path.write_text(template % 60, encoding="utf-8")
    relaxed = RuleBook(tmp_path)
    assert strict.fingerprint != relaxed.fingerprint
    for book, status in [(strict, "FAIL"), (relaxed, "PASS")]:
        assert judge(process(duration="89분"), **book.rules[0].params).status == status
