"""Oct 6: all 58 reviewed screenshots, source typography and non-mutation."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
import hashlib
import html
import re

import pytest

from sp_pdf_judger.permit_text_view import (
    EMPTY_VIEW, PermitTextView, permit_reason_view, permit_view_for_result, reflow_permit_prose,
)
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.permit_catalog import resolve_submission_permits
from sp_pdf_judger.schemas import Evaluation, TreeNode
from sp_pdf_judger.ui_html import _render_reason_box, render_result_html
from sp_pdf_judger.review_presentation import render_reason

ROOT = Path(__file__).resolve().parents[1]

# Screenshot numbers in the user's Word, in its original order. These map to
# source paragraphs for display QA, NOT automatic SP/permit semantic matching.
# Repeated permit section numbers are disambiguated by physical start page.
SCREENSHOTS = [
    (1,4,"2.1.2.1.1"),(2,4,"2.1.2.1.1"),(3,4,"2.1.2.1.2"),(4,4,"2.1.2.1.3"),
    (5,4,"2.1.2.1.8"),(6,4,"2.1.2.1.8"),(7,5,"2.1.2.1.9"),
    (8,5,"2.1.2.1.10.1"),(9,5,"2.1.2.1.10.2"),(10,5,"2.1.2.1.10.3"),
    (11,5,"2.1.2.1.11"),(12,6,"2.1.2.1.12"),(13,6,"2.1.2.1.12"),
    (14,6,"2.1.2.1.13"),(15,6,"2.1.2.1.14"),(16,6,"2.1.2.1.15"),(17,6,"2.1.2.1.16"),
    (18,8,"2.1.5.1.1"),(19,8,"2.1.5.1.4"),(20,8,"2.1.5.1.5"),(21,9,"2.1.5.1.8"),
    (22,7,"2.1.3.1.1"),(23,7,"2.1.3.1.2"),(24,7,"2.1.3.1.3"),(25,7,"2.1.3.1.6"),
    (26,9,"2.1.6.1.1"),(27,10,"2.1.6.1.4"),(28,10,"2.1.6.1.8"),
    # Word labels image29 as mycoplasma, but the screenshot itself quotes the
    # same external-agent test as image30. Follow the visible evidence.
    (29,11,"2.2.1.2"),(30,11,"2.2.1.2"),(31,11,"2.2.1.3"),
    (32,11,"2.2.2.1"),(33,11,"2.2.2.3"),(34,12,"2.2.2.4"),(35,12,"2.2.2.5"),
    (36,12,"2.2.2.7"),(37,12,"2.2.2.9"),(38,13,"2.2.2.10"),
    (39,13,"2.3.1.1"),(40,13,"2.3.1.5"),(41,14,"2.3.1.6"),(42,14,"2.3.1.7"),(43,14,"2.3.1.9"),
    (44,15,"2.4.1.1"),(45,15,"2.4.1.5"),(46,16,"2.4.1.8"),(47,16,"2.4.1.9"),
    (48,17,"2.4.1.11"),(49,17,"2.4.1.12"),
    (50,19,"2.2.1.4"),(51,19,"2.2.1.7"),(52,20,"2.2.1.8"),(53,20,"2.2.1.9"),
    (54,20,"2.2.1.10"),(55,21,"2.2.1.11"),(56,21,"2.2.1.12"),
    (57,21,"2.2.1.13"),(58,21,"2.2.1.14"),
]


@pytest.fixture(scope="module")
def corpus_view():
    pdf, = (ROOT / "더미데이터").glob("*허가문서*.pdf")
    receipt = {"name": pdf.name, "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest()}
    meta = {"permit_paths": [str(pdf)], "review_receipt": {"permits": [receipt]}}
    resolution = resolve_submission_permits([pdf], "동국바이오사이언스", "스카이코비원멀티주")
    store = PermitPdfStore([pdf], resolution.policy)
    return permit_view_for_result(SimpleNamespace(metadata=meta)), store, meta


@pytest.mark.parametrize("text,expected", [
    ("확\n인되어야 한다.", "확인되어야 한다."),
    ("확인 \n결과", "확인 결과"),
    ("PCR \n시험법에 \n따르며, \nBovine \nPolyomavirus", "PCR 시험법에 따르며, Bovine Polyomavirus"),
    ("첫 문장.\n둘째 문장.", "첫 문장. 둘째 문장."),
    ("100 μg\n/mg of protein 미만", "100 μg/mg of protein 미만"),
    ("내용 \n\n다음 문단", "내용\n\n다음 문단"),
    ("항목 | 값\nA | 1\nB | 2", "항목 | 값\nA | 1\nB | 2"),
    ("① A\n② B", "① A\n② B"),
    ("희석 없이 \n0.2 μm 필터로 여과한다.", "희석 없이 0.2 μm 필터로 여과한다."),
])
def test_soft_wraps_preserve_word_boundaries_and_structured_rows(text, expected):
    assert reflow_permit_prose(text) == expected


def test_identical_lines_are_not_deleted():
    assert render_reason("A\n반복\nB\n반복").count("반복") == 2


def test_unknown_numbers_and_document_body_ratios_are_never_exponents_or_footers():
    view = PermitTextView(frozenset({"19/60"}))
    for text in ["106 cells/mL", "1.00 x 106", "관측 비율:\n19/60", "3/4"]:
        shown = view.format_quote(text)
        assert "⁶" not in shown
        if "19/60" in text:
            assert "19/60" in shown
    assert view.format_quote("기준을 만족해야 한다.\n19/60") == "기준을 만족해야 한다."


def test_footer_cluster_is_separate_from_a_continued_sentence():
    raw = "오염이 \n6/60\n문서확인번호 : DEMO-123 \n \n- 3 - \n \n확인되지 않아야 한다."
    assert EMPTY_VIEW.format_quote(raw) == "오염이 확인되지 않아야 한다."


@pytest.mark.parametrize("image,page,section", SCREENSHOTS, ids=[f"screenshot-{i:02d}" for i,_,_ in SCREENSHOTS])
def test_all_reviewed_source_paragraphs_are_readable_and_auditable(corpus_view, image, page, section):
    view, store, _ = corpus_view
    chunk, = [c for c in store.chunks if c.page_number == page and c.section_number == section]
    # The heading is not part of the quoted acceptance body.
    raw = "\n".join(chunk.text.splitlines()[1:]).strip()
    shown = view.format_quote(raw)
    assert shown and "문서확인번호" not in shown
    assert not re.search(r"(?m)^\s*(?:\d+/\d+|-\s*\d+\s*-)\s*$", shown)
    expected_body = re.sub(
        r"(?m)^[ \t]*(?:문서\s*확인\s*번호\s*[:：][^\n]*|\d+/\d+|-\s*\d+\s*-)[ \t]*$", "", raw)
    unraised = shown.translate(str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-"))
    assert re.sub(r"\s+", "", unraised) == re.sub(r"\s+", "", expected_body)
    assert shown.count("\n") <= 1, (image, shown)
    if image in {3,23}:
        assert "2.00 x 10⁶ cells/mL" in shown
    if image in {1,2,22}:
        assert "세포로 확인되어야" in shown and "세포로 확 인" not in shown
    if image in {14,15,53}:
        assert "\n" not in shown
    ev = Evaluation(record_type="test", source="permit_pdf_llm_authoritative", comparison_completed=True,
                    final_status="검수합격", normalized_criteria=raw, normalized_result="관측값",
                    reason=f"허가서 기준 '{raw}'을 확인하고 시험결과 '관측값'가 이를 만족하여 검수합격으로 판단했습니다.")
    before = deepcopy(asdict(ev))
    markup = _render_reason_box(ev, permit_view=view)
    assert asdict(ev) == before  # No mutation of thresholds, results or verdicts.
    if raw != shown:
        assert 'class="permit-quote"' in markup and html.escape(shown) in markup
        assert "문서확인번호" not in markup
        assert ev.normalized_criteria == raw  # Raw data is retained, not reprinted.
    assert html.escape(shown) in markup


def test_hash_mismatch_and_absent_receipt_disable_typography_recovery(corpus_view):
    view, _, meta = corpus_view
    line = view.line_edits[0][0]
    assert "⁶" in view.format_quote(line)
    for changed in [{}, {**meta, "review_receipt": {}},
                    {**meta, "review_receipt": {"permits": [{"name":Path(meta['permit_paths'][0]).name, "sha256":"wrong"}]}}]:
        fallback = permit_view_for_result(SimpleNamespace(metadata=changed))
        assert "⁶" not in fallback.format_quote(line)


def test_result_and_table_rows_are_not_reflowed_as_permit_conditions(corpus_view):
    view, _, _ = corpus_view
    raw = "균이 확\n인되지 않아야 한다."
    reason = f"허가서 기준 '{raw}'을 확인하고 시험결과 'A | 3/4\nB | 106'을 비교했습니다."
    shown = permit_reason_view(reason, raw, view)
    assert "균이 확인되지 않아야" in shown
    assert "A | 3/4\nB | 106" in shown


def test_display_is_escaped_and_does_not_change_sp_only_explanation():
    ev = Evaluation(source="rule", comparison_completed=True, final_status="검수보류",
                    normalized_criteria="<script>bad()</script>\n106", reason="확\n인 필요")
    markup = _render_reason_box(ev)
    assert "<script>" not in markup and "&lt;script&gt;" in markup
    assert "확<br>인 필요" in markup and "106" in markup


def test_row_reasons_use_the_same_permit_formatter(corpus_view):
    view, _, _ = corpus_view
    raw = "균이 확\n인되지 않아야 한다."
    ev = Evaluation(source="permit_pdf_llm_authoritative", comparison_completed=True,
        normalized_criteria=raw, lot_judgements=[{"item_value":"A", "normalized_criteria":raw,
            "reason":f"허가서 기준 '{raw}'을 확인했습니다.", "status":"검수보류"}])
    before = deepcopy(asdict(ev))
    markup = _render_reason_box(ev, permit_view=view)
    assert "균이 확인되지 않아야 한다." in markup
    assert asdict(ev) == before


def test_full_result_passes_source_view_to_nested_test_cards(corpus_view):
    view, _, meta = corpus_view
    raw = next(k for k, v in view.line_edits if "10⁶ cells/mL" in v)
    ev = Evaluation(source="permit_pdf_llm_authoritative", comparison_completed=True,
                    final_status="검수합격", test_name="세포성장 및 증식확인시험",
                    normalized_criteria=raw, reason=f"허가서 기준 '{raw}'을 확인했습니다.")
    tree = [TreeNode(key="2", title="시험", level=1, section_number="2", children=[
        TreeNode(key="2.1", title="세포주", level=2, section_number="2.1", children=[
            TreeNode(key="test", title=ev.test_name, level=3, node_type="test", evaluation=ev)])])]
    result = SimpleNamespace(metadata=meta, tree=tree)
    before = deepcopy(result)
    markup = render_result_html(result)
    assert "10⁶ cells/mL" in markup and 'class="permit-quote"' in markup
    assert result == before


def test_multiple_permits_do_not_apply_ambiguous_typography(corpus_view, tmp_path):
    view, _, meta = corpus_view
    duplicate = tmp_path / "second-permit.pdf"
    duplicate.write_bytes(Path(meta["permit_paths"][0]).read_bytes())
    changed = deepcopy(meta)
    changed["permit_paths"].append(str(duplicate))
    changed["review_receipt"]["permits"].append({"name":duplicate.name,
        "sha256":hashlib.sha256(duplicate.read_bytes()).hexdigest()})
    conservative = permit_view_for_result(SimpleNamespace(metadata=changed))
    assert view.line_edits and not conservative.line_edits
    assert conservative.format_quote("확\n인") == "확인"


def test_guarded_permit_keeps_typography_but_not_a_previous_pass_reason(corpus_view):
    from sp_pdf_judger.policy_summary import apply_policy_test_guards

    view, _, meta = corpus_view
    raw = next(k for k, v in view.line_edits if "10⁶ cells/mL" in v)
    ev = Evaluation(record_type="test", order_idx=24, source="permit_pdf_llm_authoritative",
        comparison_completed=True, final_status="검수합격", normalized_criteria=raw,
        normalized_result="106", reason="이전 충족 이유")
    finding = SimpleNamespace(rule_id="R14", details={"test_guards": [
        {"order_idx":24, "status":"검수불합격", "reason":"유효자릿수 불일치"}]})
    audit = apply_policy_test_guards([ev], [finding])
    # A second guard must not erase the original permit provenance either.
    second = SimpleNamespace(rule_id="R13", details={"test_guards": [
        {"order_idx":24, "status":"검수불합격", "reason":"단위 확인: 106"}]})
    audit += apply_policy_test_guards([ev], [second])
    guarded_meta = {**deepcopy(meta), "policy_test_guard_audit": audit}
    guarded_view = permit_view_for_result(SimpleNamespace(metadata=guarded_meta))
    before = deepcopy(asdict(ev))
    markup = _render_reason_box(ev, permit_view=guarded_view)
    assert "10⁶ cells/mL" in markup and "적용 허가서 기준" in markup
    assert 'class="permit-quote"' in markup and html.escape(guarded_view.format_quote(raw)) in markup
    assert ev.normalized_criteria == raw
    assert "이전 충족 이유" not in markup
    assert "R14: 유효자릿수 불일치" in markup and "R13: 단위 확인: 106" in markup
    assert "정규화 시험결과: 106" in markup
    assert asdict(ev) == before and ev.final_status == "검수불합격"


def test_md_guard_preserves_grouped_failure_permit_typography(corpus_view):
    from sp_pdf_judger.policy_summary import apply_policy_test_guards

    _view, _, meta = corpus_view
    raw = next(k for k, v in _view.line_edits if "10⁶ cells/mL" in v)
    ev = Evaluation(
        record_type="test",
        order_idx=24,
        record_position=3,
        source="permit_group_deterministic",
        comparison_completed=True,
        final_status="검수불합격",
        reason=(
            f"허가서에 '{raw}' 기준이 있으나 "
            "시험결과 10 6 cells/mL가 충족하지 않아 검수불합격입니다."
        ),
    )
    finding = SimpleNamespace(
        rule_id="R14",
        details={
            "test_guards": [
                {
                    "record_position": 3,
                    "order_idx": 24,
                    "status": "검수불합격",
                    "reason": "유효자릿수 불일치",
                }
            ]
        },
    )
    audit = apply_policy_test_guards([ev], [finding])
    guarded_view = permit_view_for_result(
        SimpleNamespace(
            metadata={**deepcopy(meta), "policy_test_guard_audit": audit}
        )
    )

    markup = html.unescape(_render_reason_box(ev, permit_view=guarded_view))

    assert guarded_view.has_permit_basis(ev)
    assert "10⁶ cells/mL" in markup
    assert "시험결과 10 6 cells/mL" in markup
    assert "R14: 유효자릿수 불일치" in markup


@pytest.mark.parametrize("audit,current_source", [
    ([], "md_policy_guard"),
    ([{"order_idx":24, "previous_source":"rule"}], "md_policy_guard"),
    ([{"order_idx":24, "previous_source":"md_policy_guard"}], "md_policy_guard"),
    ([{"order_idx":25, "previous_source":"permit_pdf_llm_authoritative"}], "md_policy_guard"),
    ([{"order_idx":"24", "previous_source":"permit_pdf_llm_authoritative"}], "md_policy_guard"),
    ([{"order_idx":24, "previous_source":"permit_pdf_llm_authoritative"}], "rule"),
])
def test_guard_without_matching_permit_provenance_keeps_raw_basis(corpus_view, audit, current_source):
    view, _, meta = corpus_view
    raw = next(k for k, v in view.line_edits if "10⁶ cells/mL" in v)
    changed = {**deepcopy(meta), "policy_test_guard_audit": audit}
    guarded_view = permit_view_for_result(SimpleNamespace(metadata=changed))
    ev = Evaluation(order_idx=24, source=current_source, comparison_completed=True,
        final_status="검수불합격", normalized_criteria=raw, reason="MD 불충족")
    before = deepcopy(asdict(ev))
    markup = _render_reason_box(ev, permit_view=guarded_view)
    assert "10⁶ cells/mL" not in markup and "적용 허가서 기준" not in markup
    assert html.escape(raw) in markup and asdict(ev) == before


def test_guarded_permit_provenance_uses_exact_record_position(corpus_view):
    _view, _store, meta = corpus_view
    changed = {**deepcopy(meta), "policy_test_guard_audit": [{
        "order_idx": 24,
        "record_position": 1,
        "previous_source": "permit_pdf_llm_authoritative",
    }]}
    guarded_view = permit_view_for_result(SimpleNamespace(metadata=changed))
    matched = Evaluation(order_idx=24, record_position=1, source="md_policy_guard")
    sibling = Evaluation(order_idx=24, record_position=2, source="md_policy_guard")

    assert guarded_view.has_permit_basis(matched)
    assert not guarded_view.has_permit_basis(sibling)


def test_runtime_permit_paths_preserve_typography_after_metadata_sanitizing(corpus_view):
    original_view, _store, meta = corpus_view
    runtime_paths = tuple(Path(path) for path in meta["permit_paths"])
    changed = deepcopy(meta)
    changed["permit_paths"] = [path.name for path in runtime_paths]

    restored = permit_view_for_result(SimpleNamespace(
        metadata=changed,
        runtime_permit_paths=runtime_paths,
    ))

    assert original_view.line_edits
    assert restored.line_edits == original_view.line_edits


@pytest.mark.parametrize(
    "source",
    ["permit_group_deterministic", "permit_group_llm_grounded"],
)
def test_grounded_group_reason_uses_permit_typography_without_rewriting_sp_result(
    source,
):
    raw_basis = "10 6 cells/mL 이상이어야 한다."
    view = PermitTextView(
        line_edits=((raw_basis, "10⁶ cells/mL 이상이어야 한다."),)
    )
    ev = Evaluation(
        source=source,
        comparison_completed=True,
        final_status="검수합격",
        reason=(
            "허가서 필수 조건과 SP 시험결과가 일치하여 검수합격입니다. "
            f"허가 기준: {raw_basis} SP 결과: 10 6 cells/mL"
        ),
    )

    markup = html.unescape(_render_reason_box(ev, permit_view=view))

    assert view.has_permit_basis(ev)
    assert "허가 기준: 10⁶ cells/mL 이상이어야 한다." in markup
    assert "SP 결과: 10 6 cells/mL" in markup


def test_deterministic_group_failure_formats_only_the_quoted_permit_basis():
    raw_basis = "10 6 cells/mL 이상이어야 한다."
    view = PermitTextView(
        line_edits=((raw_basis, "10⁶ cells/mL 이상이어야 한다."),)
    )
    ev = Evaluation(
        source="permit_group_deterministic",
        comparison_completed=True,
        final_status="검수불합격",
        reason=(
            f"허가서에 '{raw_basis}' 기준이 있으나 "
            "시험결과 10 6 cells/mL가 충족하지 않아 검수불합격입니다."
        ),
    )

    markup = html.unescape(_render_reason_box(ev, permit_view=view))

    assert "허가서에 '10⁶ cells/mL 이상이어야 한다.' 기준" in markup
    assert "시험결과 10 6 cells/mL" in markup


@pytest.mark.parametrize(
    "source",
    ["permit_group_hold", "permit_group_grounding", "rule_before"],
)
def test_ungrounded_group_sources_do_not_claim_permit_typography(source):
    raw_basis = "10 6 cells/mL 이상이어야 한다."
    view = PermitTextView(
        line_edits=((raw_basis, "10⁶ cells/mL 이상이어야 한다."),)
    )
    ev = Evaluation(
        source=source,
        comparison_completed=True,
        final_status="검수보류",
        reason=f"허가 기준: {raw_basis}",
    )

    markup = html.unescape(_render_reason_box(ev, permit_view=view))

    assert not view.has_permit_basis(ev)
    assert "10⁶ cells/mL" not in markup
    assert raw_basis in markup


@pytest.mark.parametrize(
    "previous_source, expected",
    [
        ("permit_group_deterministic", True),
        ("permit_group_llm_grounded", True),
        ("permit_group_hold", False),
        ("permit_group_grounding", False),
    ],
)
def test_md_guard_recovers_only_grounded_group_permit_provenance(
    previous_source,
    expected,
):
    view = permit_view_for_result(
        SimpleNamespace(
            metadata={
                "policy_test_guard_audit": [
                    {
                        "record_position": 3,
                        "order_idx": 24,
                        "previous_source": previous_source,
                    }
                ]
            }
        )
    )
    ev = Evaluation(
        order_idx=24,
        record_position=3,
        source="md_policy_guard",
    )

    assert view.has_permit_basis(ev) is expected
