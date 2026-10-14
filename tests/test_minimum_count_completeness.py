"""The MD count and the per-test numeric guard must check the same targets."""
from types import SimpleNamespace

import pytest

from sp_pdf_judger.policy_engine import Rule, minimum_count
from sp_pdf_judger.schemas import ExtractedRecord


CRITERIA = "Z, B, P 유전자 중 1종 이상이 정량한계(0.0050 pg/μL) 미만"
VALID = "Z: <0.0050 pg/μL, B: 0.0060 pg/μL, P: 0.0150 pg/μL"


def evaluate(*results):
    records = [ExtractedRecord(order_idx=i, test_name="박테리오파지부정시험", criteria=CRITERIA, result=value)
               for i, value in enumerate(results)]
    ctx = SimpleNamespace(select=lambda _: records,
        evidence=lambda r: {"source": "sp", "quote": r.result or "결과 미기재"})
    policy = Rule(id="COUNT", title="최소 개수", operation="minimum_count", instruction="전체 측정대상의 결과와 단위를 확인")
    return minimum_count(policy, ctx)


@pytest.mark.parametrize("incomplete", [
    "Z: <0.0050 pg/μL, B: 0.0060 pg/μL",  # P missing
    "Q: <0.0050 pg/μL, B: 0.0060 pg/μL, P: 0.0150 pg/μL",  # unrelated target
    "Z: <0.0050 mg/mL, B: 0.0060 pg/μL, P: 0.0150 pg/μL",  # different dimension/scale
    "Z: <0.0050, B: 0.0060, P: 0.0150",  # no units
    "Z: <0.0050 pg/μL, B: 미정, P: 0.0150 pg/μL",
    VALID + ", Q: 0.001 pg/μL",  # unexpected target
    VALID + ", Z: 0.001 pg/μL",  # duplicate target
])
def test_all_named_targets_and_their_units_are_required(incomplete):
    result = evaluate(VALID, incomplete)
    assert result.status == "HOLD"
    assert result.details["incomplete_evidence"]


@pytest.mark.parametrize("incomplete", ["Z: <0.0050 pg/μL", "", None])
def test_proven_failure_is_not_erased_by_another_incomplete_record(incomplete):
    result = evaluate("Z: 0.0060 pg/μL, B: 0.0070 pg/μL, P: 0.0150 pg/μL", incomplete)
    assert result.status == "FAIL"
    assert result.details["incomplete_evidence"]


def test_complete_count_and_empty_selection_remain_distinct():
    assert evaluate(VALID).status == "PASS"
    assert evaluate().status == "HOLD"


@pytest.mark.parametrize("value", ["Z: <0.0050pg/μL, B: 0.0060pg/μL, P: 0.0150pg/μL",
    "Z: 3e-3 pg/µL, B: 6e-3 pg/µL, P: 1.5e-2 pg/µL"])
def test_equivalent_spacing_micro_glyph_and_decimal_scientific_values(value):
    assert evaluate(value).status == "PASS"
