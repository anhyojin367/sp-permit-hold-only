from sp_pdf_judger.schemas import Evaluation, TreeNode
from sp_pdf_judger.ui_html import _render_test_leaf


def test_incomplete_hold_has_visible_reason_and_status():
    ev = Evaluation(test_name="무균시험", criteria="균이 확인되지 않아야 함",
                    result="균이 확인되지 않아야 함", final_status="검수보류",
                    reason="실제 관측 결과 대신 요구 문장이 반복되었습니다.",
                    source="extraction_quality", comparison_completed=False)
    html = _render_test_leaf(TreeNode("t", "무균시험", 1, evaluation=ev), 0)
    assert "판정 이유" in html and ev.reason in html
    assert "#f59e0b" in html  # Existing HOLD dot, no layout redesign.


def test_unjudged_record_does_not_get_a_fake_status():
    ev = Evaluation(test_name="무균시험", result="음성")
    html = _render_test_leaf(TreeNode("t", "무균시험", 1, evaluation=ev), 0)
    assert "판정 이유" not in html and "#f59e0b" not in html
