"""Second review: preserve outcomes while making evidence and counts auditable."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from sp_pdf_judger.policy_engine import RuleBook, normalize_record_units
from sp_pdf_judger.review_presentation import (unit_audit_reason, render_reason,
    render_review_context, related_failure_groups, review_receipt, source_fingerprint)
from sp_pdf_judger.schemas import ExtractedRecord, Evaluation


def test_unit_audit_preserves_original_and_has_precise_scope():
    record = ExtractedRecord(order_idx=12, test_name="엔도톡신시험",
        criteria="820 EU/mg of protein 미만", result="760 IU/mg of protein")
    audit = []
    compared = normalize_record_units(record, RuleBook(), "스카이코비원멀티주", audit=audit)
    assert record.result == "760 IU/mg of protein"
    assert compared.result == "760 EU/mg of protein"
    assert len(audit) == 1 and audit[0]["field"] == "result"
    assert audit[0]["rule_id"] == "R13" and audit[0]["order_idx"] == 12
    reason = unit_audit_reason(audit[0])
    assert all(v in reason for v in [record.result, compared.result, "스카이코비원", "엔도톡신", "원문은 보존"])
    outside = []
    normalize_record_units(record, RuleBook(), "다른 제품", audit=outside)
    assert outside == []


def test_absent_units_do_not_create_audit_and_denominators_are_not_converted():
    for criteria, result in [(None, None), ("820 EU/mg of protein 미만", "760 IU/mL")]:
        audit = []
        row = normalize_record_units(ExtractedRecord(test_name="엔도톡신시험", criteria=criteria, result=result),
            RuleBook(), "스카이코비원멀티주", audit=audit)
        if result is None:
            assert audit == []
        else:
            assert row.result == "760 EU/mL"  # denominator is NEVER invented
            assert audit[0]["to"] == "EU/mL"


def test_long_reason_keeps_full_text_and_normalization_always_visible():
    text = "숫자 조건 불충족. " * 70 + "<script>bad()</script>"
    note = "MD 단위 표기 대응: IU → EU (해당 시험만)"
    markup = render_reason(text + "\n" + note)
    assert "판정 설명 전체 보기" in markup and "(설명 일부)" in markup
    assert "&lt;script&gt;bad()&lt;/script&gt;" in markup
    assert markup.index("</details>") < markup.index(note)
    assert "<script>" not in markup


def test_table_reason_also_shows_unit_audit():
    from sp_pdf_judger.ui_html import _render_reason_box
    ev = Evaluation(comparison_completed=True, reason="MD 단위 표기 대응: 원문 IU → EU",
        lot_judgements=[{"item_value": "A", "reason": "충족"}])
    assert "MD 단위 표기 대응" in _render_reason_box(ev)


def test_md_card_shows_partial_aliases_and_single_conclusion():
    from sp_judgement_bridge import _render_structural_validation_cards
    ev = Evaluation(record_type="structural_validation", section_number="R29", source="md_policy",
        test_name="필수 공정", final_status="검수불합격", reason="최종원액 없음", result="최종원액 없음")
    result = SimpleNamespace(evaluations=[ev], metadata={"policy_audit":[
        {"rule_id":"R29", "aliases":["A.3", "C.1"], "reason":"최종원액 없음"}],
        "requirement_catalog":[{"id":"C.1", "coverage":"partial"}]})
    markup = _render_structural_validation_cards(result)
    assert "A.3 · C.1 (일부 구현)" in markup
    assert markup.count("최종원액 없음") == 1


def test_related_checks_require_identical_failed_source_not_shared_page_only():
    item = {"source":"sp", "file":"a.pdf", "page":21, "quote":"제조량 270 L"}
    rows = [{"rule_id":rid, "status":"FAIL", "evidence":[deepcopy(item)]} for rid in ["R05","R30"]]
    before = deepcopy(rows)
    assert related_failure_groups(rows) == [{"page":21,"rules":["R05","R30"]}]
    assert rows == before
    rows[1]["evidence"][0]["quote"] = "다른 제조량"
    assert related_failure_groups(rows) == []
    rows[1]["evidence"][0] = deepcopy(item)
    rows[1]["status"] = "PASS"
    assert related_failure_groups(rows) == []


def test_receipt_hashes_content_not_filename_or_absolute_path(tmp_path):
    pdf, permit = tmp_path / "same.pdf", tmp_path / "permit.pdf"
    pdf.write_bytes(b"first")
    permit.write_bytes(b"permit")
    first = review_receipt(pdf, [permit], "rule-sha")
    pdf.write_bytes(b"other")
    second = review_receipt(pdf, [permit], "rule-sha")
    assert first["sp"]["sha256"] != second["sp"]["sha256"]
    assert first["permits"] == second["permits"]
    markup = render_review_context(SimpleNamespace(metadata={"review_receipt":first}))
    assert str(tmp_path) not in markup and first["sp"]["sha256"] in markup
    assert "표의 대상별 결과는 각각 1건" in markup


def test_engine_fingerprint_is_line_ending_and_root_independent(tmp_path):
    for name, content in [("a", b"x\r\ny\r\n"), ("b", b"x\ny\n")]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "json추출.py").write_bytes(content)
    assert source_fingerprint(tmp_path / "a") == source_fingerprint(tmp_path / "b")


def test_dashboard_light_palette_does_not_depend_on_browser_theme(monkeypatch):
    import sp_app
    captured = []
    monkeypatch.setattr(sp_app.st, "markdown", lambda text, **kw: captured.append(text))
    sp_app._inject_styles()
    css = "\n".join(captured)
    assert "color-scheme:light" in css
    assert '.gate-banner.error { background:#fff1f2' in css
    assert 'button:not([kind="primary"])' in css
    config = (Path(__file__).resolve().parents[1] / ".streamlit" / "config.toml").read_text()
    assert 'base = "light"' in config


def test_home_count_help_does_not_change_any_counts():
    from sp_app import _review_summary_html
    markup = _review_summary_html({"state":"completed", "summary":{"pass":129,"fail":2,"hold":0,"total":131}})
    assert "129건" in markup and "2건" in markup and "131건" in markup
    assert "수정할 곳의 수가 아니라" in markup
