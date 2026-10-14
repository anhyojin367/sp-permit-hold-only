import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from sp_pdf_judger.extractor import _record_from_dict
from sp_pdf_judger.judgement import deterministic_judge
from sp_pdf_judger.permit_catalog import resolve_submission_permits
from sp_pdf_judger.policy_engine import (
    RuleBook, PolicyContext, evaluate_policies, policy_fingerprint,
    normalize_record_units, Rule, semantic_review, add_months,
)
from datetime import date

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "D00": set(), "D01": {"R01"}, "D02": {"R02", "R10"},
    "D03": {"R03"}, "D04": {"R04", "R05", "R06", "R12"},
    "D05": {"R07", "R08"}, "D06": {"R13", "R14"},
    "D07": {"R11", "R15", "R16", "R17"}, "D08": {"R18"},
    "D09": {"R19", "R20"}, "D10": {"R21"}, "D11": {"R22"}, "D12": {"R23"},
}


@pytest.mark.parametrize("key", EXPECTED)
def test_actual_dummy_pdf_policies(key):
    corpus = ROOT / ".local_validation" / "corpus" / f"{key}.records.json"
    pdfs = sorted((ROOT / "더미데이터").glob(f"{key}_*.pdf"))
    permits = sorted((ROOT / "더미데이터").glob("*허가문서*.pdf"))
    if not corpus.exists() or not pdfs or not permits:
        pytest.skip("Local private dummy corpus not installed; run inspect_dummy_corpus.py --extract")
    rows = json.loads(corpus.read_text(encoding="utf-8"))
    records = [_record_from_dict(row, i) for i, row in enumerate(rows)]
    book = RuleBook()
    ctx = PolicyContext(pdfs[0], records, permits, "스카이코비원멀티주")
    findings = evaluate_policies(ctx, book)
    failed = {f.rule_id for f in findings if f.status == "FAIL"}
    assert EXPECTED[key] <= failed
    holds = {f.rule_id for f in findings if f.status == "HOLD"}
    if key == "D02":
        # This historical extraction still contains the finished-product lot
        # footer inside two upstream records. Never pick the first of conflicting
        # lots as a valid value. Fresh PDF extraction is tested separately in
        # test_extraction_page_furniture and the D00-D16 corpus runner.
        assert holds == {"R04"}
    else:
        assert not holds
    assert not [f for f in findings if f.details.get("execution_error")]
    if key == "D00":
        assert failed == set()
    assert all(f.evidence for f in findings if f.status == "FAIL")


@pytest.mark.parametrize("result, expected", [
    ("1.90 x 10^6 cells/mL", "검수불합격"),
    ("2.00 x 10^6 cells/mL", "검수합격"),
    ("3.00 x 10^6 cells/mL", "검수합격"),
])
def test_scientific_boundary(result, expected):
    assert deterministic_judge("2.00 x 10^6 cells/mL 이상", result)[0] == expected


def test_md_edit_invalidates_fingerprint_and_changes_parameter(tmp_path):
    path = tmp_path / "rule.md"
    text = "---\nrules:\n  - id: T1\n    title: 기간\n    instruction: 기간 확인\n    operation: duration\n    params: {minimum_days: 28}\n---\n"
    path.write_text(text, encoding="utf-8")
    first = RuleBook(tmp_path)
    path.write_text(text.replace("28", "30"), encoding="utf-8")
    second = RuleBook(tmp_path)
    assert first.fingerprint != second.fingerprint
    assert second.rules[0].params["minimum_days"] == 30
    assert second.fingerprint == policy_fingerprint(tmp_path)


def test_invalid_or_empty_rules_do_not_silently_pass(tmp_path):
    with pytest.raises(ValueError, match="No executable"):
        RuleBook(tmp_path)
    (tmp_path / "bad.md").write_text("---\nrules:\n  - id: X\n    title: X\n    instruction: X\n    operation: arbitrary_python\n---\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown operation"):
        RuleBook(tmp_path)


def test_submission_permit_does_not_mix_with_catalog(tmp_path):
    linked = tmp_path / "permit.pdf"
    linked.write_bytes(b"%PDF-mocked")
    resolution = resolve_submission_permits([linked], "에스케이바이오사이언스", "스카이코비원멀티주")
    assert resolution.paths == (linked.resolve(),)
    assert resolution.policy.authoritative
    linked.write_bytes(b"%PDF-changed")
    assert resolution.fingerprint != resolve_submission_permits([linked], "에스케이바이오사이언스", "스카이코비원멀티주").fingerprint


def test_unit_alias_is_scoped_and_does_not_mutate_evidence():
    record = _record_from_dict({"record_type": "test", "test_name": "엔도톡신시험", "criteria": "800 EU/mL 이하", "result": "760 IU/mL"}, 1)
    assert normalize_record_units(record, RuleBook(), "스카이코비원멀티주").result == "760 EU/mL"
    assert normalize_record_units(record, RuleBook(), "다른제품").result == record.result
    assert record.result == "760 IU/mL"


def test_semantic_rule_cannot_pass_without_evidence():
    ctx = SimpleNamespace(llm=SimpleNamespace(enabled=True), select=lambda _: [], evidence=lambda r: r)
    rule = Rule(id="X", title="임의 의미 규칙", operation="semantic_review", instruction="모든 조건 확인")
    assert semantic_review(rule, ctx).status == "HOLD"


def test_calendar_month_arithmetic():
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2025, 7, 18), 18) == date(2027, 1, 18)


def test_qualitative_permit_basis_with_assay_number_is_concrete():
    from sp_pdf_judger.judgement import _is_concrete_permit_basis
    assert _is_concrete_permit_basis("CO1 유전자 분석 시 햄스터 유래 세포로 확인되어야 한다.")
    assert not _is_concrete_permit_basis("시험 123")


@pytest.mark.parametrize("selector", [
    {"contxt": ["CHO"]}, {"context": "CHO"}, {"context": [""]},
    {"context": [12]}, {"test": ""}, {"test": 7},
])
def test_invalid_md_selector_is_rejected(selector):
    with pytest.raises(ValueError):
        Rule(id="X", title="X", instruction="X", operation="semantic_review", selector=selector)


def test_nested_selector_is_also_validated():
    from sp_pdf_judger.policy_engine import validated_selector
    assert validated_selector({"context": ["CHO"], "type": "test"}) == {"context": ["CHO"], "type": "test"}
    with pytest.raises(ValueError):
        # params.tests / params.target filters must not expand to all records.
        PolicyContext.select(SimpleNamespace(records=[]), {"contxt": ["CHO"]})


def test_semantic_proof_fixtures_change_only_natural_language():
    first = RuleBook(ROOT / "tests" / "fixtures" / "semantic_proof" / "2")
    second = RuleBook(ROOT / "tests" / "fixtures" / "semantic_proof" / "4")
    a, b = first.rules[0].model_dump(), second.rules[0].model_dump()
    assert a.pop("instruction").replace("2.00", "4.00") == b.pop("instruction")
    assert a == b
    assert first.fingerprint != second.fingerprint
