"""Exact appearance alternatives must not depend on a semantic provider."""
from types import SimpleNamespace
import pickle

import pytest

from sp_pdf_judger.config import HOLD_LABEL, PASS_LABEL
from sp_pdf_judger.judgement import JudgeEngine
from sp_pdf_judger.schemas import ExtractedRecord


CRITERIA = "무색 투명 또는 약간 탁한 액상"


def evaluate(criteria, result):
    return JudgeEngine(None).judge_record(ExtractedRecord(
        record_type="test", test_name="성상", section_number="4.2",
        method="자기기시(육안관찰)", test_date="2025.07.01",
        criteria=criteria, result=result,
    ))


@pytest.mark.parametrize("result", ["약간 탁한 액상", "약간  탁한\n액상", "무색 투명", "무색 투명 액상"])
def test_exact_permitted_appearance_passes_without_provider(result):
    evaluation = evaluate(CRITERIA, result)
    assert evaluation.final_status == PASS_LABEL
    assert evaluation.source == "rule_before"
    assert evaluation.criteria == CRITERIA
    assert evaluation.result == result


def test_reason_identifies_actual_accepted_alternative():
    evaluation = evaluate(CRITERIA, "약간 탁한 액상")
    assert evaluation.final_status == PASS_LABEL
    assert "약간 탁한 액상" in evaluation.reason
    assert "또는" in evaluation.reason


def test_accepted_appearance_does_not_call_an_enabled_provider():
    def unexpected_call(**kwargs):
        pytest.fail("An exact appearance alternative must not spend a provider call")
    engine = JudgeEngine(None, llm_client=SimpleNamespace(enabled=True, explain=unexpected_call))
    engine.sp_only_review = True
    evaluation = engine.judge_record(ExtractedRecord(
        record_type="test", test_name="성상", criteria=CRITERIA, result="약간 탁한 액상"))
    assert evaluation.final_status == PASS_LABEL
    assert evaluation.source == "rule_before"


def test_hold_only_pipeline_preserves_pass_without_loading_permit(monkeypatch, tmp_path):
    from test_hold_only_pipeline import baseline
    from sp_pdf_judger.hold_only_review import apply_held_permit_review
    pipe, pdf, provider = baseline(monkeypatch, tmp_path)
    record = ExtractedRecord(record_type="test", order_idx=1, test_name="성상",
                             criteria=CRITERIA, result="약간 탁한 액상")
    before = pipe.run(pdf, extracted_records=[record])
    def unexpected_permit_load():
        pytest.fail("The already accepted appearance must not enter permit review")
    after = apply_held_permit_review(before, pipeline_factory=unexpected_permit_load)
    tests = [ev for ev in after.evaluations if ev.record_type == "test"]
    assert len(tests) == 1
    assert tests[0].final_status == PASS_LABEL
    assert tests[0].result == "약간 탁한 액상"
    assert provider.requests == []
    assert after.metadata["hold_only_review"]["eligible_count"] == 0


@pytest.mark.parametrize("result", [
    "심하게 탁한 액상", "탁한 액상", "약간 탁함", "약간 탁한 액상 아님",
    "무색 투명하지 않음", "무색 투명 또는 약간 탁한 액상",
    "약간 탁한 액상, 침전물 있음", "무색 투명, 이물 있음",
    "약간 탁한 액상인지 확인 불가", "약간 탁한 액상이어야 함", "", None,
])
def test_partial_negative_or_uncertain_observation_is_not_auto_passed(result):
    assert evaluate(CRITERIA, result).final_status != PASS_LABEL


@pytest.mark.parametrize("criteria,result", [
    ("무색 투명 또는 약간 탁한 액상이며 이물이 없어야 함", "약간 탁한 액상"),
    ("무색 투명 또는 약간 탁한 액상이며 이물이 없어야 함", "무색 투명"),
    ("무색 투명 또는 약간 탁한 액상, pH 6.0 이상", "약간 탁한 액상"),
    ("무색 투명 또는 약간 탁한 액상이 아니어야 함", "약간 탁한 액상"),
    ("무색 투명하고 약간 탁한 액상", "약간 탁한 액상"),
])
def test_extra_criteria_are_not_discarded(criteria, result):
    assert evaluate(criteria, result).final_status != PASS_LABEL


def test_unrelated_existing_numeric_and_qualitative_rules_are_preserved():
    assert evaluate("10 이하", "5").final_status == PASS_LABEL
    assert evaluate("세포가 뭉치지 않고 구형이어야 함", "뭉치지 않음, 구형임").final_status == PASS_LABEL
    assert evaluate("거의 무색이거나 황색, 담황색", "담황색").final_status == PASS_LABEL


def test_pre_fix_saved_hold_cannot_be_reused(tmp_path, monkeypatch):
    import sp_judgement_bridge as bridge
    monkeypatch.setenv("SP_PERMIT_REVIEW_SCOPE", "hold_only")
    cache = tmp_path / "old-hold.pkl"
    cache.write_bytes(pickle.dumps({
        "cache_version": "sp-app-hold-only-v1-20261014",
        "permit_review_scope": "hold_only",
        "after_result": SimpleNamespace(metadata={}, evaluations=[
            SimpleNamespace(final_status=HOLD_LABEL, criteria=CRITERIA, result="약간 탁한 액상")]),
    }))
    with pytest.raises(bridge.JudgementCacheError):
        bridge._read_judgement_cache(cache)
