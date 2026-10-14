from types import SimpleNamespace

import pytest

from sp_pdf_judger.numeric_safety import permit_numeric_veto, result_is_requirement
from sp_pdf_judger.judgement import JudgeEngine
from sp_pdf_judger.llm import JudgeResponse
from sp_pdf_judger.permit_catalog import PermitPolicy
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.schemas import ExtractedRecord


BASIS = "자사의 세포배양법에 따르며, 5계대 이상 계대하였을 때 세포농도는 2.00 x 106 cells/mL 이상이어야 하고, 생존율은 90.0 ~ 100.0 % 이어야 한다."


@pytest.mark.parametrize("value", ["1.90 x 10^6 cells/mL, 78.0%", "3.00 x 10^6 cells/mL, 78.0%", "1.90 x 10^6 cells/mL, 95.0%"])
def test_independent_guard_rejects_each_failed_compound_condition(value):
    assert permit_numeric_veto(BASIS, value).status == "검수불합격"


def test_guard_does_not_promote_anything_to_pass_or_compare_unrelated_units():
    assert permit_numeric_veto(BASIS, "3.00 x 10^6 cells/mL, 95.0%") is None
    assert permit_numeric_veto("37 ℃에서 배양한 뒤 80% 이상이어야 한다.", "95%") is None
    assert permit_numeric_veto("10 mg 이하", "<10 mg") is None


def test_strict_minimum_count_uses_authoritative_threshold_and_all_named_targets():
    criteria = "Z, B, P 유전자 중 1종 이상이 정량한계(0.0050 pg/μL) 미만이어야 한다."
    assert permit_numeric_veto(criteria, "Z:≥0.0050 pg/μL, B:≤0.0050 pg/μL, P: 0.0150 pg/μL").status == "검수불합격"
    assert permit_numeric_veto(criteria, "Z:<0.0050 pg/μL, B:0.0060 pg/μL, P: 0.0150 pg/μL") is None
    assert permit_numeric_veto(criteria, "Z:<0.0050 pg/μL").status == "검수보류"
    engine = JudgeEngine(SimpleNamespace(search=lambda *a, **k: []))
    judged = engine.judge_record(ExtractedRecord(test_name="박테리오파지부정시험", criteria=criteria,
        result="Z:≥0.0050 pg/μL, B:≤0.0050 pg/μL, P: 0.0150 pg/μL"))
    assert judged.final_status == "검수불합격"


def test_incorrect_clova_pass_is_vetoed_at_final_judgement():
    policy = PermitPolicy("linked", True, True, True, "native")
    store = PermitPdfStore.from_page_texts([(1, "2.1. CHO 제조용 세포주\n2.1.1. 세포성장 및 증식확인시험\n" + BASIS)], policy=policy)
    answer = JudgeResponse(status="검수합격", reason="모든 조건 충족", permit_match_status="matched",
        matched_permit_test="CHO 제조용 세포주 > 세포성장 및 증식확인시험", permit_basis=BASIS, failed_requirements=[])
    fake = SimpleNamespace(enabled=True, explain=lambda **_: answer)
    engine = JudgeEngine(SimpleNamespace(search=lambda *a, **k: []), llm_client=fake, permit_store=store, permit_policy=policy)
    result = engine.judge_record(ExtractedRecord(test_name="세포성장 및 증식확인시험", section_title="CHO 제조용 세포주",
        criteria="2.00 x 10^6 cells/mL 이상, 90.0~100.0%", result="1.90 x 10^6 cells/mL, 78.0%"))
    assert result.final_status == "검수불합격"
    assert result.comparator == "permit_pdf_numeric_guard"
    assert "재계산" in result.reason


def test_sp_numeric_pass_is_explained_but_does_not_override_permit_unit_hold():
    basis = "엔도톡신은 820 EU/mg of protein 미만이어야 한다."
    policy = PermitPolicy("linked", True, True, True, "native")
    store = PermitPdfStore.from_page_texts([(1, "2.1. Component B 중간체원액\n2.1.1. 엔도톡신시험\n" + basis)], policy=policy)
    answer = JudgeResponse(status="검수합격", reason="충족", permit_match_status="matched",
        matched_permit_test="Component B 중간체원액 > 엔도톡신시험", permit_basis=basis,
        permit_acceptance_basis=[basis], failed_requirements=[])
    fake = SimpleNamespace(enabled=True, explain=lambda **_: answer)
    engine = JudgeEngine(SimpleNamespace(search=lambda *a, **k: []), llm_client=fake, permit_store=store, permit_policy=policy)
    judged = engine.judge_record(ExtractedRecord(test_name="엔도톡신시험", section_title="Component B 중간체원액",
        criteria="820 EU/mL 미만", result="760 EU/mL"))
    assert judged.final_status == "검수보류", (judged.reason, judged.source, engine.comparison_bases)
    assert "SP 기재 기준만 비교하면 충족" in judged.reason
    assert "820 EU/mL 미만" in judged.reason and "EU/mg of protein" in judged.reason


def test_requirement_sentence_is_not_an_observed_result():
    assert result_is_requirement("균이 확인되지 않아야 함", "균이 확인되지 않아야 함")
    assert not result_is_requirement("균이 확인되지 않아야 함", "균이 확인되지 않음")
    assert not result_is_requirement("3.20 x 10^8 PFU/mL", "3.20 x 10^8 PFU/mL")
    engine = JudgeEngine(SimpleNamespace(search=lambda *a, **k: []))
    result = engine.judge_record(ExtractedRecord(test_name="무균시험", criteria="균이 확인되지 않아야 함", result="균이 확인되지 않아야 함"))
    assert result.final_status == "검수보류"
    assert result.source == "extraction_quality"


@pytest.mark.parametrize("glyph", ["㎍", "μg", "µg"])
def test_compatibility_unit_glyph_cannot_hide_numeric_failure(glyph):
    assert permit_numeric_veto(f"단백질함량은 6,200 {glyph}/mL 이상이어야 한다.", "520 μg/mL").status == "검수불합격"


def test_grounded_acceptance_unit_mismatch_cannot_pass():
    basis = "엔도톡신은 820 EU/mg of protein 미만이어야 한다."
    veto = permit_numeric_veto(basis, "760 EU/mL", acceptance_basis=[basis])
    assert veto.status == "검수보류"
    assert "단위" in veto.reason
    assert permit_numeric_veto(basis, "760 EU/mg of protein", acceptance_basis=[basis]) is None
    assert permit_numeric_veto(basis, "<760 EU/mL", acceptance_basis=[basis]).status == "검수보류"


def test_procedure_ranges_are_not_required_observations():
    acceptance = "순도는 35% 이상이어야 한다."
    basis = "검체를 135 ~ 562 ㎍/mL로 희석한다. " + acceptance
    assert permit_numeric_veto(basis, "90%", acceptance_basis=[acceptance]) is None
    mixed = "15 ~ 20 g 마우스에 접종하고 생존율은 80% 이상이어야 한다."
    assert permit_numeric_veto(mixed, "85%", acceptance_basis=[mixed]) is None
    required = "생존율은 90.0 ~ 100.0 % 이어야 한다."
    assert permit_numeric_veto(required, "85%", acceptance_basis=[required]).status == "검수불합격"
    assert permit_numeric_veto(required, "3 cells/mL", acceptance_basis=[required]).status == "검수보류"


def test_page_footer_inside_derived_clause_does_not_break_source_grounding():
    raw = "자사 방법으로 검사한 검\n17/60\n문서확인번호 : TEST\n- 14 -\n체의 항원함량은 180 ㎍/mL 이상이어야 한다."
    clean = "자사 방법으로 검사한 검\n체의 항원함량은 180 ㎍/mL 이상이어야 한다."
    policy = PermitPolicy("linked", True, True, True, "native")
    store = PermitPdfStore.from_page_texts([(1, "2.1. 나노파티클 원액\n2.1.1. 항원함량시험\n" + raw)], policy=policy)
    answer = JudgeResponse(status="검수합격", reason="모든 조건 충족", permit_match_status="matched",
        matched_permit_test="나노파티클 원액 > 항원함량시험", permit_basis=raw,
        permit_acceptance_basis=[clean], failed_requirements=[])
    engine = JudgeEngine(SimpleNamespace(search=lambda *a, **k: []), llm_client=SimpleNamespace(enabled=True, explain=lambda **_: answer),
                         permit_store=store, permit_policy=policy)
    judged = engine.judge_record(ExtractedRecord(test_name="항원함량시험", section_title="나노파티클 원액",
        criteria="180 μg/mL 이상", result="191 μg/mL"))
    assert judged.final_status == "검수합격"
    answer.permit_acceptance_basis = [clean.replace("180", "18")]
    assert engine.judge_record(ExtractedRecord(test_name="항원함량시험", section_title="나노파티클 원액",
        criteria="180 μg/mL 이상", result="191 μg/mL")).final_status == "검수보류"


@pytest.mark.parametrize("result, expected", [
    ("용기당 10 μm 이상: 5,000개,\n용기당 25 μm 이상: 455개", "검수합격"),
    ("용기당 25 μm 이상: 455개, 용기당 10 μm 이상: 5,000개", "검수합격"),
    ("용기당 10 μm 이상: 5,000개, 용기당 25 μm 이상: 601개", "검수불합격"),
    ("용기당 10 μm 이상: 6,001개, 용기당 25 μm 이상: 455개", "검수불합격"),
    ("용기당 10 μm 이상: 5,000개", "검수보류"),
    ("용기당 10 μm 이상: 5,000개, 용기당 10 μm 이상: 455개", "검수보류"),
    ("용기당 10 μm 이상: 5,000개, 용기당 25 μm 이상: <600개", "검수보류"),
])
def test_all_labelled_endpoints_must_be_compared(result, expected):
    from sp_pdf_judger.numeric_safety import labelled_numeric_check
    criteria = "용기당 10 μm 이상: 6,000개 이하,\n용기당 25 μm 이상: 600개 이하"
    assert labelled_numeric_check(criteria, result, ["용기당 10 μm 이상", "용기당 25 μm 이상"]).status == expected


def test_labelled_endpoint_scope_comes_from_md_and_limit_from_source():
    from sp_pdf_judger.numeric_safety import labelled_numeric_check
    assert labelled_numeric_check("A: 4개 이하, B: 5개 이하", "A: 3개, B: 3개", ["A", "B"]).status == "검수합격"
    assert labelled_numeric_check("A: 2개 이하, B: 5개 이하", "A: 3개, B: 3개", ["A", "B"]).status == "검수불합격"
    assert labelled_numeric_check("A: 4개 이하, B: 5개 이하", "A: 3개, B: 3개", ["A", "B", "C"]).status == "검수보류"
