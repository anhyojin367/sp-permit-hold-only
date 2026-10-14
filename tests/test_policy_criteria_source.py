"""A result is compared with one grounded source, not contradictory limits."""
from types import SimpleNamespace

import pytest

from sp_pdf_judger.policy_engine import Rule, numeric_compare, minimum_count, labelled_numeric_compare
from sp_pdf_judger.schemas import ExtractedRecord


def context(record, *basis, authoritative=True):
    return SimpleNamespace(select=lambda _: [record], permit_authoritative=authoritative,
        comparison_bases={record.order_idx: list(basis)},
        evidence=lambda r: {"source": "sp", "file": "sp.pdf", "page": 2, "quote": r.criteria})


def permit(text):
    return {"source": "permit", "criteria": text, "reason": "원문 연결 확인",
            "evidence": [{"source": "permit", "file": "permit.pdf", "page": 4, "quote": text}]}


def rule(operation="numeric_compare", **params):
    return Rule(id="T", title="결과 수치", operation=operation, criterion_source="permit_first",
                instruction="허가서 기준이 있으면 우선하고 문서 기준 불일치는 별도로 검증", params=params)


@pytest.mark.parametrize("sp,approved,expected", [("5 ng/mL 이하", "10 ng/mL 이하", "PASS"),
                                                 ("10 ng/mL 이하", "5 ng/mL 이하", "FAIL")])
def test_result_uses_authoritative_limit_even_when_sp_limit_differs(sp, approved, expected):
    record = ExtractedRecord(criteria=sp, result="8 ng/mL")
    found = numeric_compare(rule(), context(record, permit(approved)))
    assert found.status == expected
    assert any(item.get("source") == "permit" and item.get("page") == 4 for item in found.evidence)
    assert found.details["comparison_bases"][0]["source"] == "permit"


def test_md_can_explicitly_select_sp_for_an_independent_document_check():
    record = ExtractedRecord(criteria="5 ng/mL 이하", result="8 ng/mL")
    policy = rule()
    policy.criterion_source = "sp"
    assert numeric_compare(policy, context(record, permit("10 ng/mL 이하"))).status == "FAIL"


@pytest.mark.parametrize("bases", [[], [{"source": "unresolved", "reason": "매칭 불명확"}],
                                    [permit("10 ng/mL 이하"), permit("5 ng/mL 이하")],
                                    [permit("10 ng/mL 이하"), {"source": "sp"}]])
def test_missing_or_conflicting_permit_basis_never_silently_falls_back(bases):
    record = ExtractedRecord(criteria="10 ng/mL 이하", result="8 ng/mL")
    assert numeric_compare(rule(), context(record, *bases)).status == "HOLD"


def test_validated_method_only_or_no_permit_and_offline_paths_keep_sp_basis():
    record = ExtractedRecord(criteria="10 ng/mL 이하", result="8 ng/mL")
    assert numeric_compare(rule(), context(record, {"source": "sp", "reason": "허가서는 방법만 지정"})).status == "PASS"
    assert numeric_compare(rule(), context(record, authoritative=False)).status == "PASS"


def test_minimum_count_uses_approved_count_not_sp_count():
    record = ExtractedRecord(criteria="Z, B, P 유전자 중 1종 이상이 정량한계(0.0050 pg/μL) 미만",
                             result="Z: <0.0050 pg/μL, B: 0.0200 pg/μL, P: 0.0150 pg/μL")
    found = minimum_count(rule("minimum_count"), context(record, permit(record.criteria.replace("1종", "2종"))))
    assert found.status == "FAIL"
    assert found.details["comparison_bases"][0]["source"] == "permit"


def test_labelled_limits_use_permit_and_sp_supplement_requires_explicit_md_permission():
    record = ExtractedRecord(criteria="용기당 25 μm 이상: 600개 이하", result="용기당 25 μm 이상: 700개")
    policy = rule("labelled_numeric_compare", expected_labels=["용기당 25 μm 이상"])
    assert labelled_numeric_compare(policy, context(record, permit("용기당 25 μm 이상: 800개 이하"))).status == "PASS"
    reference = permit("대한민국약전의 불용성미립자시험법의 제1법에 적합하여야 한다.")
    assert labelled_numeric_compare(policy, context(record, reference)).status == "HOLD"
    policy.params["sp_supplement_if_no_numeric_limit"] = True
    found = labelled_numeric_compare(policy, context(record, reference))
    assert found.status == "FAIL"
    assert found.details["comparison_bases"][0]["source"] == "sp_supplement"
    assert found.details["test_guards"][0]["status"] == "검수불합격"


@pytest.mark.parametrize("unknown", ["입자 수 maximum 800 units", "입자는 육백개 이하여야 한다.", "일반시험법 USP <788> 참조"])
def test_unparsed_limit_or_unknown_number_is_not_treated_as_no_numeric_limit(unknown):
    record = ExtractedRecord(criteria="용기당 25 μm 이상: 600개 이하", result="용기당 25 μm 이상: 500개")
    policy = rule("labelled_numeric_compare", expected_labels=["용기당 25 μm 이상"], sp_supplement_if_no_numeric_limit=True)
    found = labelled_numeric_compare(policy, context(record, permit(unknown)))
    assert found.status == "HOLD" and found.details["comparison_bases"][0]["source"] == "permit"


def test_another_records_basis_cannot_be_borrowed():
    record = ExtractedRecord(order_idx=7, criteria="10 ng/mL 이하", result="8 ng/mL")
    ctx = context(record)
    ctx.comparison_bases = {8: [permit("10 ng/mL 이하")]}
    assert numeric_compare(rule(), ctx).status == "HOLD"


def test_position_scoped_basis_never_falls_back_to_duplicate_display_index():
    record = ExtractedRecord(order_idx=7, criteria='5 ng/mL 이하', result='8 ng/mL')
    ctx = context(record, permit('10 ng/mL 이하'))
    ctx.record_position = lambda _: 1
    ctx.comparison_bases_by_position = {0: [permit('10 ng/mL 이하')]}
    assert numeric_compare(rule(), ctx).status == 'HOLD'
