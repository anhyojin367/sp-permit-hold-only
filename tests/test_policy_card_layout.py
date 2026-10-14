"""Keep the original document-stage grouping without re-running judgements."""
from copy import deepcopy
from types import SimpleNamespace

from sp_pdf_judger.policy_engine import Finding
from sp_pdf_judger.policy_summary import manufacturing_policy_cards
from sp_pdf_judger.schemas import ExtractedRecord


def fixture():
    records = [ExtractedRecord(record_type="diagram", diagram_data={"nodes": [
        {"name": "원액"}, {"name": "완제의약품"}]}),
        ExtractedRecord(section_number="5.2", section_title="항원바이알 시험", page_start=4,
                        test_name="성상", section_path=[{"title": "완제의약품"}, {"title": "항원바이알 시험"}])]
    checks = [
        {"stage_name": "동일 제조번호 문서 전체 정합성", "kind": "field", "field_name": "제조일",
         "summary_value": "2026.01.01", "document_value": "2026.01.01", "status": "합격", "reason": "동일"},
        {"stage_name": "항원바이알", "kind": "test_date", "test_name": "성상", "date_text": "2026.01.02",
         "section_number": "5.2", "page_number": 4, "status": "보류", "reason": "근거 부족"},
        {"stage_name": "완제의약품", "kind": "field", "field_name": "제조번호",
         "summary_value": "B", "document_value": "B", "status": "합격", "reason": "동일"},
        {"stage_name": "원액", "kind": "field", "field_name": "제조번호",
         "summary_value": "A", "document_value": "A", "status": "합격", "reason": "동일"},
    ]
    findings = [Finding("T", "정보", "HOLD", "근거 부족", details={"manufacturing_checks": checks})]
    book = SimpleNamespace(rules=[SimpleNamespace(id="T", report_group="manufacturing_info")])
    return records, findings, book


def test_cards_follow_diagram_order_and_child_tests_stay_in_parent_stage():
    records, findings, book = fixture()
    before = deepcopy(findings)
    cards = manufacturing_policy_cards(findings, book, records=records)
    assert [c.stage_name for c in cards] == ["원액", "완제의약품", "동일 제조번호 문서 전체 정합성"]
    assert len(cards[1].fields) == 1 and len(cards[1].test_dates) == 1
    assert cards[1].status == "보류" and cards[1].test_dates[0].reason == "근거 부족"
    assert sum(len(c.fields) + len(c.test_dates) for c in cards) == 4
    assert findings == before


def test_unknown_or_ambiguous_parent_keeps_separate_card_and_evidence():
    records, findings, book = fixture()
    records[1].section_path = [{"title": "기타 영역"}]
    cards = manufacturing_policy_cards(findings, book, records=records)
    assert any(c.stage_name == "항원바이알" and c.status == "보류" for c in cards)
    records[1].section_path = [{"title": "원액 및 완제의약품"}]
    cards = manufacturing_policy_cards(findings, book, records=records)
    assert any(c.stage_name == "항원바이알" for c in cards)
    assert sum(len(c.fields) + len(c.test_dates) for c in cards) == 4


def test_another_section_or_test_cannot_reassign_unrelated_stage():
    records, findings, book = fixture()
    for attr, wrong in [("section_number", "6.2"), ("test_name", "다른시험"), ("page_start", 99)]:
        variant = deepcopy(records)
        setattr(variant[1], attr, wrong)
        cards = manufacturing_policy_cards(findings, book, records=variant)
        assert any(c.stage_name == "항원바이알" for c in cards)
