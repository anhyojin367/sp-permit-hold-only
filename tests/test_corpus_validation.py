from types import SimpleNamespace

from scripts.validate_corpus import assess_result
from sp_pdf_judger.schemas import Evaluation


def result(evaluations, rules=()):
    return SimpleNamespace(evaluations=evaluations, metadata={
        "llm_success_count": 5,
        "policy_audit": [{"rule_id": r, "status": "FAIL"} for r in rules],
    })


def test_hold_is_not_live_acceptance_even_without_false_positive():
    audit = assess_result("D00", result([Evaluation(test_name="무균시험", final_status="검수보류")]), True)
    assert not audit["normal_false_positives"]
    assert not audit["live_acceptance_complete"]
    assert len(audit["holds"]) == 1


def test_rule_ids_alone_do_not_hide_wrong_individual_test_passes():
    audit = assess_result("D05", result([], ["R07", "R08"]), True)
    assert not audit["missing_rules"]
    assert len(audit["missing_test_failures"]) == 3
    assert not audit["live_acceptance_complete"]


def test_false_positive_is_not_live_acceptance():
    audit = assess_result("D00", result([Evaluation(test_name="정상 항목", final_status="검수불합격")]), True)
    assert audit["normal_false_positives"] == ["정상 항목"]
    assert not audit["live_acceptance_complete"]
