from types import SimpleNamespace

from sp_pdf_judger.policy_engine import Finding
from sp_pdf_judger.policy_summary import summarize_manufacturing_policies


def book(*ids):
    return SimpleNamespace(fingerprint="rules-v1", rules=[
        SimpleNamespace(id=rule_id, report_group="manufacturing_dates") for rule_id in ids])


def finding(rule_id, status):
    return Finding(rule_id, "제조일", status, f"{rule_id} 원문 근거 판정")


def test_summary_uses_md_failure_not_independent_legacy_pass():
    status, reason, meta = summarize_manufacturing_policies([finding("R18", "FAIL")], book("R18"), [3])
    assert status == "검수불합격"
    assert "R18 원문 근거 판정" in reason
    assert meta["source"] == "md_policy"
    assert meta["rule_fingerprint"] == "rules-v1"


def test_md_rule_ids_can_change_without_code_change():
    status, reason, meta = summarize_manufacturing_policies([finding("CUSTOM-DATE", "PASS")], book("CUSTOM-DATE"), [3])
    assert status == "검수합격"
    assert meta["rule_ids"] == ["CUSTOM-DATE"]


def test_no_rules_no_applicable_result_or_no_page_is_not_a_pass():
    for findings, rules, pages in [([], book(), [3]), ([], book("R18"), [3]),
                                   ([finding("R18", "N/A")], book("R18"), [3]),
                                   ([finding("R18", "PASS")], book("R18"), [])]:
        assert summarize_manufacturing_policies(findings, rules, pages)[0] == "검수보류"


def test_missing_result_and_partial_hold_prevent_summary_pass():
    assert summarize_manufacturing_policies([finding("A", "PASS")], book("A", "B"), [3])[0] == "검수보류"
    assert summarize_manufacturing_policies([finding("A", "PASS"), finding("B", "HOLD")], book("A", "B"), [3])[0] == "검수보류"


def test_proven_failure_is_not_hidden_by_another_hold():
    assert summarize_manufacturing_policies([finding("A", "FAIL"), finding("B", "HOLD")], book("A", "B"), [3])[0] == "검수불합격"


def test_manufacturing_cards_only_project_md_comparisons():
    from sp_pdf_judger.policy_summary import manufacturing_policy_cards
    b = SimpleNamespace(rules=[SimpleNamespace(id="CUSTOM", report_group="manufacturing_info")])
    f = finding("CUSTOM", "FAIL")
    f.details["manufacturing_checks"] = [{"stage_name": "중간체", "kind": "test_date", "test_name": "시험A",
        "date_text": "2026.01.01", "status": "불합격", "reason": "CUSTOM: 날짜 위반"}]
    cards = manufacturing_policy_cards([f, finding("UNRELATED", "FAIL")], b)
    assert len(cards) == 1 and cards[0].stage_name == "중간체"
    assert cards[0].status == "불합격"
    assert cards[0].test_dates[0].reason == "CUSTOM: 날짜 위반"
    f.details["manufacturing_checks"].append({"stage_name": "중 간 체", "kind": "field", "field_name": "제조번호",
        "summary_value": "A", "document_value": "A", "status": "합격", "reason": "일치"})
    cards = manufacturing_policy_cards([f], b)
    assert len(cards) == 1 and len(cards[0].fields) == 1


def test_md_renderer_does_not_call_legacy_judgement(monkeypatch):
    import sp_pdf_judger.manufacturing_stage_ui as ui
    import sp_pdf_judger.manufacturing_info_validator as legacy
    def forbidden(*a, **k):
        raise AssertionError("Rendering cannot re-judge document rules")
    monkeypatch.setattr(ui, "validate_manufacturing_info_consistency", forbidden)
    monkeypatch.setattr(legacy, "validate_manufacturing_info_consistency", forbidden)
    result = SimpleNamespace(metadata={"manufacturing_info_cards": []}, pdf_path=None, manufacturing_summary_image_paths=[])
    ui.render_manufacturing_summary_with_stage_cards(result)


def test_partial_missing_process_date_is_not_a_pass_and_md_controls_equality():
    from sp_pdf_judger.policy_engine import process_order, Rule
    nodes = [{"node_id": key, "name": key, "fields": {"제조년월일": "2026.01.01"}} for key in ("a", "b", "c")]
    ctx = SimpleNamespace(records=[SimpleNamespace(diagram_data={"nodes": nodes, "edges": [
        {"from": "a", "to": "b"}, {"from": "b", "to": "c"}]})], evidence=lambda _: {"source": "sp"})
    rule = Rule(id="T", title="공정", operation="process_order", instruction="공정 확인")
    assert process_order(rule, ctx).status == "PASS"
    rule.params["allow_same_day"] = False
    assert process_order(rule, ctx).status == "FAIL"
    rule.params["allow_same_day"] = True
    nodes[-1]["fields"] = {}
    assert process_order(rule, ctx).status == "HOLD"


def test_explicit_md_test_guard_blocks_llm_pass_and_preserves_previous_audit():
    from sp_pdf_judger.schemas import Evaluation
    from sp_pdf_judger.policy_summary import apply_policy_test_guards
    ev = Evaluation(order_idx=4, final_status="검수합격", reason="모델 합격", source="permit_pdf_llm_authoritative")
    f = finding("CUSTOM", "HOLD")
    f.details["test_guards"] = [{"order_idx": 4, "status": "검수보류", "reason": "두번째 대상 결과 누락"}]
    audit = apply_policy_test_guards([ev], [f])
    assert ev.final_status == "검수보류" and ev.source == "md_policy_guard"
    assert audit[0]["previous_reason"] == "모델 합격"
    ev.final_status = "검수불합격"
    assert apply_policy_test_guards([ev], [f]) == [] and ev.final_status == "검수불합격"


def test_policy_guard_targets_position_and_synchronizes_grouped_lot_rows():
    from sp_pdf_judger.schemas import Evaluation
    from sp_pdf_judger.policy_summary import apply_policy_test_guards

    first = Evaluation(
        order_idx=4,
        record_position=0,
        record_type="test",
        final_status="검수합격",
        reason="허가 그룹 합격",
        source="permit_group_deterministic",
        comparison_completed=True,
        lot_judgements=[{
            "item_value": "row-a",
            "status": "검수합격",
            "reason": "허가 그룹 합격",
            "source": "permit_group_deterministic",
        }],
    )
    second = Evaluation(
        order_idx=4,
        record_position=1,
        record_type="test",
        final_status="검수합격",
        reason="둘째 행 합격",
        source="permit_group_deterministic",
        comparison_completed=True,
    )
    guarded = finding("CUSTOM", "FAIL")
    guarded.details["test_guards"] = [{
        "order_idx": 4,
        "record_position": 0,
        "status": "검수불합격",
        "reason": "첫 행만 위반",
    }]

    audit = apply_policy_test_guards([first, second], [guarded])

    assert first.final_status == "검수불합격"
    assert first.lot_judgements[0]["status"] == "검수불합격"
    assert first.lot_judgements[0]["reason"] == first.reason
    assert first.lot_judgements[0]["source"] == "md_policy_guard"
    assert second.final_status == "검수합격"
    assert audit[0]["record_position"] == 0


def test_position_guard_falls_back_to_order_for_legacy_evaluations():
    """Other products still use evaluations created before position fan-out."""
    from sp_pdf_judger.schemas import Evaluation
    from sp_pdf_judger.policy_summary import apply_policy_test_guards

    legacy = Evaluation(
        order_idx=4,
        record_type="test",
        final_status="검수합격",
        reason="기존 판정",
        source="rule",
    )
    guarded = finding("CUSTOM", "FAIL")
    guarded.details["test_guards"] = [{
        "order_idx": 4,
        "record_position": 0,
        "status": "검수불합격",
        "reason": "기존 정책 위반",
    }]

    audit = apply_policy_test_guards([legacy], [guarded])

    assert legacy.final_status == "검수불합격"
    assert len(audit) == 1
