from types import SimpleNamespace

import pytest

from sp_pdf_judger.policy_engine import Rule, numeric_compare
from sp_pdf_judger.schemas import ExtractedRecord


def evaluate(*results):
    rows = [ExtractedRecord(test_name=f"대상{i}", criteria="10 ng/mL 미만", result=result)
            for i, result in enumerate(results)]
    context = SimpleNamespace(select=lambda _: rows,
        evidence=lambda r: {"source": "sp", "quote": r.result or "결과 미기재"})
    rule = Rule(id="T", title="수치", operation="numeric_compare", instruction="모든 선택 대상의 수치 확인")
    return numeric_compare(rule, context)


@pytest.mark.parametrize("unknown", [None, "", "확인 필요", "5"])
def test_one_good_number_cannot_hide_another_missing_or_unresolved_result(unknown):
    finding = evaluate("5 ng/mL", unknown)
    assert finding.status == "HOLD"
    assert finding.details["incomplete_evidence"]
    assert len(finding.evidence) == 2


def test_known_numeric_failure_is_preserved_with_incomplete_evidence():
    finding = evaluate("15 ng/mL", None)
    assert finding.status == "FAIL"
    assert finding.details["incomplete_evidence"]


def test_all_complete_results_pass_but_no_record_never_does():
    assert evaluate("5 ng/mL", "9 ng/mL").status == "PASS"
    assert evaluate().status == "HOLD"
