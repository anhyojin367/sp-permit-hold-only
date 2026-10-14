"""Regression contracts from the assistant's deployed D00-D16 review."""
from types import SimpleNamespace

import pytest

from sp_pdf_judger.config import FAIL_LABEL, HOLD_LABEL, PASS_LABEL
from sp_pdf_judger.policy_engine import (Rule, RuleBook, Finding, required_tests,
    normalize_record_units, unit_consistency, storage_window, evaluate_policies)
from sp_pdf_judger.policy_summary import apply_policy_test_guards
from sp_pdf_judger.schemas import Evaluation, ExtractedRecord


def ctx(rows):
    return SimpleNamespace(records=rows, product="스카이코비원멀티주", pages=[],
        select=lambda selector: [r for r in rows if not selector.get("type") or r.record_type == selector["type"]],
        evidence=lambda r: {"source": "sp", "page": r.page_start, "quote": r.raw_text or r.content or "test"})


def test_failure_explanation_replaces_pass_and_merges_distinct_failures():
    ev = Evaluation(order_idx=1, final_status=PASS_LABEL, reason="결과 85%이므로 충족")
    findings = [Finding(rid, rid, "FAIL", msg, details={"test_guards": [
        {"order_idx": 1, "status": FAIL_LABEL, "reason": msg}]})
        for rid, msg in [("R10", "12일은 28일 미달"), ("R20", "방법 불일치")]]
    audit = apply_policy_test_guards([ev], findings)
    assert ev.final_status == FAIL_LABEL
    assert ev.reason == "R10: 12일은 28일 미달\nR20: 방법 불일치"
    assert audit[0]["previous_reason"] == "결과 85%이므로 충족"
    assert apply_policy_test_guards([ev], findings) == []


@pytest.mark.parametrize("product,test,result,normalized", [
    ("스카이코비원멀티주", "엔도톡신시험", "760 IU/mg of protein", "760 EU/mg of protein"),
    ("다른 제품", "엔도톡신시험", "760 IU/mg of protein", "760 IU/mg of protein"),
    ("스카이코비원멀티주", "다른시험", "760 IU/mg of protein", "760 IU/mg of protein"),
    ("스카이코비원멀티주", "엔도톡신시험", "760 IU/mL", "760 EU/mL"),
])
def test_unit_alias_is_product_test_and_denominator_scoped(product, test, result, normalized):
    row = ExtractedRecord(test_name=test, criteria="820 EU/mg of protein 미만", result=result)
    assert normalize_record_units(row, RuleBook(), product).result == normalized
    assert row.result == result


@pytest.mark.parametrize("criterion,result,status", [
    ("820 EU/mg of protein 미만", "760 IU/mg of protein", "PASS"),
    ("820 EU/mg of protein 미만", "760 EU/mL", "FAIL"),
    ("820 EU/mg of protein 미만", "760 EU/mg", "FAIL"),
    ("10 μg/mg of protein 이하", "9 ug/mg", "FAIL"),
    ("10 μg/mg of protein 이하", "9 ug/mg of protein", "PASS"),
    ("3 CFU/mL 이상", "4 PFU/mL", "FAIL"),
])
def test_full_unit_denominator_is_not_dropped(criterion, result, status):
    rule = next(r for r in RuleBook().rules if r.id == "R13")
    row = ExtractedRecord(test_name="엔도톡신시험", criteria=criterion, result=result)
    assert unit_consistency(rule, ctx([row])).status == status


def test_storage_deadline_vetoes_only_the_offending_test():
    source = ExtractedRecord(record_type="content", content="제조년월일: 2025.06.30\n허가된 저장기간: 18 개월")
    tests = [ExtractedRecord(order_idx=2, test_name="pH", test_date=day)
             for day in ("2025.07.18", "2027.07.18")]
    context = ctx([source, *tests])
    context.select = lambda selector: [source] if selector.get("type") == "content" else tests
    positions = {id(record): position for position, record in enumerate([source, *tests])}
    context.record_position = lambda record: positions[id(record)]
    rule = Rule(id="R16", title="저장기한", instruction="저장기한 검사", operation="storage_window",
                selector={"type": "content"}, params={"target": {"type": "test"}})
    result = storage_window(rule, context)
    assert result.status == "FAIL"
    assert [g["order_idx"] for g in result.details["test_guards"]] == [2]
    assert result.details["test_guards"][0]["record_position"] == 2
    evs = [Evaluation(order_idx=2, record_position=position,
                      final_status=PASS_LABEL, reason="pH 충족")
           for position in (1, 2)]
    apply_policy_test_guards(evs, [result])
    assert [e.final_status for e in evs] == [PASS_LABEL, FAIL_LABEL]
    assert "2026-12-30" in evs[1].reason and "pH 충족" not in evs[1].reason


def test_missing_stage_names_tests_and_does_not_invent_first_page_evidence():
    rule = next(r for r in RuleBook().rules if r.id == "R29")
    result = required_tests(rule, ctx([]))
    assert result.status == "FAIL"
    assert all(s in result.reason for s in ["최종원액", "성상", "무균시험"])
    assert result.evidence == []


def test_missing_stage_dependency_keeps_hold_and_identifies_root_cause():
    book = RuleBook()
    book.rules = [r for r in book.rules if r.id in {"R29", "R30"}]
    results = {f.rule_id: f for f in evaluate_policies(ctx([]), book)}
    assert results["R29"].status == "FAIL"
    assert results["R30"].status == "HOLD"
    assert results["R30"].details["blocked_by"] == ["R29"]
    assert "최종원액" in results["R30"].reason


def test_product_mismatch_can_be_reviewed_with_explicit_permit(tmp_path):
    from sp_app import _version_gate, _can_review_with_linked_permit
    permit = tmp_path / "permit.pdf"
    permit.touch()
    doc = SimpleNamespace(company="회사", product="스카아코비루원멀티주", version="4.0", permit_files=(permit,))
    gate = _version_gate(doc, [])
    assert gate["status_label"] == "제품명 확인 필요"
    assert _can_review_with_linked_permit(doc, gate)
    doc.permit_files = ()
    assert not _can_review_with_linked_permit(doc, gate)


def test_permit_can_select_rule_profile_without_rewriting_submitted_product():
    from sp_pdf_judger.rule_profile import resolve_rule_product
    chunks = [SimpleNamespace(text="제품명\n스카이코비원멀티주(백신)\n", source_file="permit.pdf", page_number=1)]
    store = SimpleNamespace(enabled=True, chunks=chunks)
    product, audit = resolve_rule_product("스카아코비루원멀티주", store, RuleBook())
    assert product == "스카이코비원멀티주"
    assert audit["submitted_product"] == "스카아코비루원멀티주"
    assert audit["source"] == "linked_permit_identity"
    chunks.append(SimpleNamespace(text="제품명\n다른제품(백신)\n", source_file="other.pdf", page_number=1))
    assert resolve_rule_product("오타제품", store, RuleBook())[0] == "오타제품"


def test_product_mismatch_banner_is_not_a_version_mismatch(monkeypatch):
    import sp_app
    rendered = []
    monkeypatch.setattr(sp_app.st, "session_state", {})
    monkeypatch.setattr(sp_app.st, "markdown", lambda html, **kw: rendered.append(html))
    sp_app._render_gate_message(
        {"ok": False, "status_label": "제품명 확인 필요", "reason": "연결 허가서로 확인"},
        SimpleNamespace(),
    )
    assert "제품명 확인 필요" in rendered[0]
    assert "기준 SP 버전 불일치" not in rendered[0]


def test_all_82_user_requirements_are_retained_without_inventing_verdicts():
    book = RuleBook()
    expected = {f"{letter}.{i}" for letter, count in [("A", 23), ("B", 24), ("C", 12)] for i in range(1, count + 1)}
    expected |= {f"G{i:02}" for i in range(1, 24)}
    assert {r.id for r in book.requirements} == expected
    assert len(book.rules) == 29  # catalog entries must not inflate document verdicts
    assert next(r for r in book.requirements if r.id == "A.10").coverage == "requires_reference"
    assert all(set(rule.aliases) <= expected for rule in book.rules)
