"""Oct 7 Word: every expanded permit excerpt, including MD evidence, is readable.

Screenshot 1 is the R07 heading; 2 is its quoted evidence. Screenshots 3-21
are the expanded per-test excerpts, not new requests to alter verdicts.
"""
from copy import deepcopy
from dataclasses import asdict
from html.parser import HTMLParser
import html
import re
from types import SimpleNamespace

import pytest

from sp_judgement_bridge import _policy_evidence_links
from sp_pdf_judger.schemas import Evaluation
from sp_pdf_judger.ui_html import _render_reason_box
from test_feedback_20261006 import corpus_view
from test_policy_evidence_links import parsed_links


SCREENSHOTS = [
    (3,4,"2.1.2.1.8"),(4,5,"2.1.2.1.9"),(5,5,"2.1.2.1.11"),
    (6,6,"2.1.2.1.13"),(7,6,"2.1.2.1.14"),(8,6,"2.1.2.1.16"),
    (9,8,"2.1.5.1.5"),(10,7,"2.1.3.1.6"),(11,11,"2.2.2.3"),
    (12,12,"2.2.2.9"),(13,13,"2.3.1.5"),(14,14,"2.3.1.10"),
    (15,15,"2.4.1.5"),(16,16,"2.4.1.9"),(17,17,"2.4.1.12"),
    (18,19,"2.2.1.7"),(19,20,"2.2.1.9"),(20,20,"2.2.1.10"),
    (21,21,"2.2.1.14"),
]


class Excerpts(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.depth = 0
        self.quotes = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag == "blockquote":
            self.depth += 1
            self.quotes.append("")

    def handle_endtag(self, tag):
        if tag == "blockquote":
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.quotes[-1] += data


def body(store, page, section):
    chunk, = [c for c in store.chunks if c.page_number == page and c.section_number == section]
    return "\n".join(chunk.text.splitlines()[1:]).strip()


def significant_body(raw):
    raw = re.sub(r"(?m)^[ \t]*(?:문서\s*확인\s*번호\s*[:：][^\n]*|\d+/\d+|-\s*\d+\s*-)[ \t]*$", "", raw)
    return re.sub(r"\s+", "", raw.translate(str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")))


@pytest.mark.parametrize("image,page,section", SCREENSHOTS, ids=[f"word-image-{i}" for i,_,_ in SCREENSHOTS])
def test_expanded_permit_quotes_do_not_reintroduce_pdf_layout(corpus_view, image, page, section):
    view, store, _ = corpus_view
    raw = body(store, page, section)
    ev = Evaluation(record_type="test", source="permit_pdf_llm_authoritative",
        comparison_completed=True, final_status="검수합격", normalized_criteria=raw,
        normalized_result="관측값", reason=f"허가서 기준 '{raw}'을 확인했습니다.")
    before = deepcopy(asdict(ev))
    markup = _render_reason_box(ev, permit_view=view)
    assert "문서확인번호" not in markup and "HCGT-8NTS" not in markup
    assert "허가서 원문 추출 보기" not in markup
    assert 'class="permit-quote"' in markup
    quote, = Excerpts(markup).quotes
    assert quote == view.format_quote(raw)
    assert quote.count("\n") <= 1
    assert significant_body(quote) == significant_body(raw)
    assert not re.search(r"(?m)^\s*(?:\d+/\d+|-\s*\d+\s*-)\s*$", quote)
    assert asdict(ev) == before  # Original criterion/results/status are untouched.


def test_r07_permit_evidence_uses_same_display_and_preserves_pdf_target(corpus_view):
    view, store, meta = corpus_view
    from pathlib import Path
    permit = Path(meta["permit_paths"][0])
    raw = body(store, 14, "2.3.1.10")  # Word screenshots 1/2.
    evidence = {"source":"permit", "file":str(permit), "page":14, "quote":raw}
    result = SimpleNamespace(pdf_path=Path("sp.pdf"), metadata={**deepcopy(meta),
        "policy_audit":[{"rule_id":"R07", "evidence":[evidence]}]})
    before = deepcopy(result)
    markup = _policy_evidence_links(result, "R07")
    assert "문서확인번호" not in markup and "HCGT-8NTS" not in markup
    assert Excerpts(markup).quotes == [view.format_quote(raw)]
    assert "15 ng/mg of protein 미만" in markup
    assert parsed_links(markup) == [(permit.resolve(),14)]
    assert result == before


def test_sp_evidence_is_not_cleaned_as_permit_text(corpus_view):
    _, _, meta = corpus_view
    raw = "관측 비율:\n19/60\n106 cells/mL\n문서확인번호: SP-ORIGINAL"
    result = SimpleNamespace(pdf_path="sp.pdf", metadata={**deepcopy(meta),
        "policy_audit":[{"rule_id":"R07", "evidence":[
            {"source":"sp", "page":1, "quote":raw}]}]})
    assert Excerpts(_policy_evidence_links(result, "R07")).quotes == [raw]


def test_clean_quote_preserves_structured_rows_ratios_and_escapes_html():
    from sp_pdf_judger.permit_text_view import EMPTY_VIEW
    raw = "조건:\nA | 3/4\nB | 106\n<script>내용</script> \n다음"
    ev = Evaluation(source="permit_pdf_llm_authoritative", comparison_completed=True,
        final_status="검수보류", normalized_criteria=raw, normalized_result="3/4",
        reason="근거 확인 필요")
    before = deepcopy(asdict(ev))
    markup = _render_reason_box(ev)
    assert "<script>" not in markup and "&lt;script&gt;" in markup
    assert Excerpts(markup).quotes == [EMPTY_VIEW.format_quote(raw)]
    assert "A | 3/4\nB | 106" in html.unescape(markup)
    assert asdict(ev) == before
