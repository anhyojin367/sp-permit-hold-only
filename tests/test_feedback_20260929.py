"""User-reported D02 inconsistency and D13-D16 acceptance contracts."""
from types import SimpleNamespace

import pytest

from sp_pdf_judger.config import PASS_LABEL, FAIL_LABEL, HOLD_LABEL
from sp_pdf_judger.policy_engine import Rule, duration, mass_balance
from sp_pdf_judger.policy_summary import apply_policy_test_guards
from sp_pdf_judger.schemas import ExtractedRecord, Evaluation, TreeNode
from sp_pdf_judger.ui_html import _render_test_leaf


def context(rows):
    return SimpleNamespace(select=lambda _: rows, evidence=lambda r: {"source": "sp", "quote": r.raw_text or "fixture"})


@pytest.mark.parametrize("period,status", [("2024.10.29 ~ 2024.11.10", FAIL_LABEL),
    ("2024.10.29 ~ 2024.11.26", PASS_LABEL), ("2024.10.29 ~", HOLD_LABEL)])
def test_duration_changes_only_the_matched_individual_test(period, status):
    row = ExtractedRecord(order_idx=7, test_name="성숙마우스접종시험", test_period=period)
    rule = Rule(id="CUSTOM", title="기간", operation="duration", instruction="28일 이상",
                params={"minimum_days": 28})
    finding = duration(rule, context([row]))
    evaluations = [Evaluation(order_idx=i, test_name=row.test_name, final_status=PASS_LABEL,
                   reason="생존율 85% 충족", comparison_completed=True) for i in (7, 8)]
    audit = apply_policy_test_guards(evaluations, [finding])
    assert evaluations[0].final_status == status
    assert evaluations[1].final_status == PASS_LABEL  # same name, different stage/record
    if status != PASS_LABEL:
        assert audit[0]["previous_status"] == PASS_LABEL
        assert "CUSTOM" in evaluations[0].reason
        assert "판정 이유" in _render_test_leaf(TreeNode("t", row.test_name, 1, evaluation=evaluations[0]), 0)


def test_duration_does_not_turn_an_existing_failure_into_hold():
    row = ExtractedRecord(order_idx=1, test_name="시험", test_period="미정")
    rule = Rule(id="T", title="기간", operation="duration", instruction="기간", params={"minimum_days": 28})
    ev = Evaluation(order_idx=1, final_status=FAIL_LABEL, reason="이미 확인된 실패")
    apply_policy_test_guards([ev], [duration(rule, context([row]))])
    assert ev.final_status == FAIL_LABEL and ev.reason == "이미 확인된 실패"


@pytest.mark.parametrize("mode,output,expected", [("not_exceed", "270 L", "PASS"),
    ("equal", "270 L", "FAIL"), ("equal", "285 L", "PASS"), ("equal", "300 L", "FAIL"),
    ("equal", "미정", "HOLD"), ("equal", "285 mL", "HOLD")])
def test_mass_balance_equality_is_an_explicit_md_choice(mode, output, expected):
    rule = Rule(id="T", title="제조량", operation="mass_balance", instruction="직접 투입량 비교",
                params={"output_field": "제조량", "start_marker": "직접 투입", "end_marker": "하위 조성", "comparison": mode})
    row = ExtractedRecord(content=f"제조량: {output}\n직접 투입\n명칭 | 분량\n원액 | 270 L\n완충액 | 15 L\n하위 조성\n물 | 15 L")
    assert mass_balance(rule, context([row])).status == expected


def test_permit_basis_is_labelled_as_permit_not_just_normalized_sp():
    ev = Evaluation(test_name="엔도톡신시험", criteria="820 EU/mL 미만", result="760 EU/mL",
        normalized_criteria="820 EU/mg of protein 미만", normalized_result="760 EU/mL",
        final_status=HOLD_LABEL, reason="허가서 단위 환산 근거가 없습니다.", source="permit_pdf_llm_authoritative")
    rendered = _render_test_leaf(TreeNode("t", ev.test_name, 1, evaluation=ev), 0)
    assert "적용 허가서 기준" in rendered
    assert "820 EU/mL" in rendered and "820 EU/mg of protein" in rendered


@pytest.mark.parametrize("observed", ["균이 확인되지 않음", "불검출"])
def test_real_negative_observation_passes_without_llm(observed, monkeypatch):
    from sp_pdf_judger.judgement import JudgeEngine
    monkeypatch.setattr("sp_pdf_judger.llm.create_clova_client", lambda: None)
    engine = JudgeEngine(rag_store=None)
    ev = engine.judge_record(ExtractedRecord(test_name="무균시험", criteria="균이 확인되지 않아야 함", result=observed))
    assert ev.final_status == PASS_LABEL


def test_requirement_in_result_is_not_an_observation(monkeypatch):
    from sp_pdf_judger.judgement import JudgeEngine
    monkeypatch.setattr("sp_pdf_judger.llm.create_clova_client", lambda: None)
    ev = JudgeEngine(rag_store=None).judge_record(ExtractedRecord(test_name="무균시험", criteria="균이 확인되지 않아야 함", result="균이 확인되지 않아야 함"))
    assert ev.final_status == HOLD_LABEL


@pytest.mark.parametrize("result,status", [("불검출", PASS_LABEL), ("미검출", PASS_LABEL),
    ("검출되지 않음", PASS_LABEL), ("균이 확인되지 않음", PASS_LABEL),
    ("검출", FAIL_LABEL), ("양성", FAIL_LABEL), ("A 불검출, B 검출", FAIL_LABEL),
    ("A 확인되지 않음, B 확인됨", FAIL_LABEL)])
def test_general_method_distinguishes_negated_detection_from_detection(result, status):
    from sp_pdf_judger.pipeline import _general_method_priority_evaluation
    ev, _ = _general_method_priority_evaluation(ExtractedRecord(test_name="무균시험", method="유럽약전",
        criteria="균이 확인되지 않아야 함", result=result, test_period="2025.04.23 ~ 2025.05.07"))
    assert ev is not None and ev.final_status == status


@pytest.mark.parametrize("criterion", ["균이 확인되지 않아야 함", "허가서 기준에 따름"])
def test_unfamiliar_sterility_notation_does_not_crash_general_method(criterion):
    from sp_pdf_judger.pipeline import _general_method_priority_evaluation
    _general_method_priority_evaluation(ExtractedRecord(test_name="무균시험", method="유럽약전",
        criteria=criterion, result="x", test_period="2025.04.23 ~ 2025.05.07"))


def test_per_record_md_failure_does_not_affect_other_records():
    from sp_pdf_judger.policy_engine import evaluate_policies
    rule = Rule(id="T", title="자릿수", operation="precision", instruction="자릿수 확인", selector={"type": "test"})
    rows = [ExtractedRecord(order_idx=i, test_name="시험", criteria="3.0 CFU/mL 이상", result=v)
            for i, v in enumerate(("4.0 CFU/mL", "4.00 CFU/mL"))]
    ctx = context(rows)
    ctx.product = "P"
    result = evaluate_policies(ctx, SimpleNamespace(rules=[rule]))
    evs = [Evaluation(order_idx=i, final_status=PASS_LABEL) for i in range(2)]
    apply_policy_test_guards(evs, result)
    assert [e.final_status for e in evs] == [PASS_LABEL, FAIL_LABEL]


def test_inbox_shows_original_filename_between_received_date_and_permit(monkeypatch, tmp_path):
    import sp_app as app
    from contextlib import nullcontext
    doc = SimpleNamespace(path=tmp_path / "sp_uuid_stored.pdf", company="회사", product="제품",
        title="공통 SP 제목", product_number="B1", version="4.0", received_date="2026.09.29",
        subject="직접 업로드", permit_files=())
    rendered = []
    monkeypatch.setattr(app, "_index_record", lambda _: {"original_name": "D02_<시험>.pdf"})
    monkeypatch.setattr(app, "_judgement_status_for_doc", lambda _: {})
    monkeypatch.setattr(app, "_company_logo_html", lambda *a: "")
    monkeypatch.setattr(app.st, "markdown", lambda text, **kw: rendered.append(text))
    monkeypatch.setattr(app.st, "columns", lambda *a, **kw: [nullcontext() for _ in range(4)])
    monkeypatch.setattr(app.st, "button", lambda *a, **kw: False)
    app._render_inbox_card(doc, False, {"ok": True}, 0)
    card = rendered[0]
    assert card.index("접수일") < card.index("문서 제목") < card.index("허가서")
    assert "D02_&lt;시험&gt;.pdf" in card and "sp_uuid_stored.pdf" not in card


@pytest.mark.parametrize("product,test,criteria,value,changed", [
    ("스카이코비원멀티주", "무균시험", "균이 확인되지 않아야 함", "x", True),
    ("스카이코비원멀티주", "무균시험", "균이 확인되지 않아야 함", "X", True),
    ("다른 제품", "무균시험", "균이 확인되지 않아야 함", "x", False),
    ("스카이코비원멀티주", "성상", "균이 확인되지 않아야 함", "x", False),
    ("스카이코비원멀티주", "무균시험", "균이 확인되어야 함", "x", False),
    ("스카이코비원멀티주", "무균시험", "균이 확인되지 않아야 함", "x / 양성", False),
    ("스카이코비원멀티주", "무균시험", "균이 확인되지 않아야 함", "균이 확인되지 않아야 함", False),
])
def test_md_result_alias_is_exact_and_preserves_original(product, test, criteria, value, changed):
    from sp_pdf_judger.policy_engine import RuleBook, normalize_record_result
    row = ExtractedRecord(test_name=test, criteria=criteria, result=value)
    normalized, audit = normalize_record_result(row, RuleBook(), product)
    assert row.result == value
    assert bool(audit) is changed
    assert normalized.result == ("균이 확인되지 않음" if changed else value)


def test_conflicting_result_aliases_fail_closed(tmp_path):
    from sp_pdf_judger.policy_engine import RuleBook
    text = """---
rules:
  - {id: T, title: T, operation: duration, instruction: T, params: {minimum_days: 28}}
result_aliases:
  - {products: [P], test: T, criteria: C, values: [x], normalized_result: N, reason: R}
  - {products: [P], test: T, criteria: C, values: [X], normalized_result: N, reason: R}
---
"""
    (tmp_path / "rule.md").write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="Overlapping result aliases"):
        RuleBook(tmp_path)


def test_d02_rule_detection_alone_is_not_a_passed_regression():
    from scripts.validate_corpus import assess_result
    result = SimpleNamespace(evaluations=[], metadata={"llm_success_count": 1,
        "policy_audit": [{"rule_id": r, "status": "FAIL"} for r in ("R02", "R10")]})
    audit = assess_result("D02", result, True)
    assert not audit["missing_rules"] and audit["missing_test_failures"]


@pytest.mark.parametrize("key", ["D15", "D16"])
def test_negative_variant_regression_requires_the_actual_individual_pass(key):
    from scripts.validate_corpus import assess_result
    result = SimpleNamespace(evaluations=[], metadata={"llm_success_count": 1, "policy_audit": []})
    assert assess_result(key, result, True)["missing_test_passes"]
