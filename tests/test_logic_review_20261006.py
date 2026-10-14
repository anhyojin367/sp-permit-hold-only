"""R24 long explanations use the available column without changing verdicts."""
from copy import deepcopy
import html
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from sp_judgement_bridge import _render_structural_validation_cards
from sp_pdf_judger.schemas import Evaluation


class LayoutTree(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.fields = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"div", "details"}:
            assert not any(t == "span" for t, _ in self.stack), "block nested in inline span"
        if attrs.get("class") == "structural-field-value":
            assert self.stack[-1][1].get("class") == "structural-field"
            self.fields.append(attrs)
        if tag not in {"br", "hr", "img", "meta", "link", "input"}:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1][0] == tag
        self.stack.pop()


@pytest.mark.parametrize("kind", ["structural_validation", "numeric_precision_validation", "temporal_sequence_validation"])
@pytest.mark.parametrize("status", ["검수합격", "검수불합격", "검수보류"])
def test_card_reason_grid_is_scoped_and_preserves_full_reason(kind, status):
    reason = " / ".join(f"R24: 시험{i} 시험일 2025-07-18, 해당 제조일 2027-07-18 이후 조건 위반." for i in range(12))
    reason += " <기준> <script>alert(1)</script>"
    ev = Evaluation(record_type=kind, source="md_policy", section_number="R24",
                    test_name="시험대상의 제조일과 시험일 순서", criteria="해당 제조일보다 빠를 수 없다.",
                    reason=reason, final_status=status)
    result = SimpleNamespace(evaluations=[ev], metadata={"policy_audit":[
        {"rule_id":"R24", "reason":reason, "aliases":["C.3","C.4"], "evidence":[]}]})
    before = deepcopy(result)
    markup = _render_structural_validation_cards(result, record_type=kind)
    assert result == before
    assert '.structural-card-body div {' not in markup
    assert '.structural-card-body > .structural-field {' in markup
    assert 'class="reason-preview"' in markup and 'class="reason-full-text"' in markup
    assert html.escape(reason) in markup and '<script>' not in markup
    assert 'data-rule-id="R24"' in markup
    tree = LayoutTree()
    tree.feed(markup)
    assert len(tree.fields) == 2 and not tree.stack


def test_short_reason_is_not_truncated_or_duplicated():
    ev = Evaluation(record_type="temporal_sequence_validation", section_number="R24",
                    reason="시험일이 제조일보다 빠릅니다.", final_status="검수불합격")
    markup = _render_structural_validation_cards(SimpleNamespace(evaluations=[ev], metadata={}),
                                                record_type="temporal_sequence_validation")
    assert markup.count(ev.reason) == 1
    assert 'class="reason-preview"' not in markup
