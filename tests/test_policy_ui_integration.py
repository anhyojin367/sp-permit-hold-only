"""Exercise actual corpus findings through the existing UI adapters, without API."""
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

from sp_pdf_judger.pipeline import DocumentJudgePipeline
from sp_pdf_judger.permit_catalog import ResolvedPermit, resolve_submission_permits
from sp_pdf_judger.policy_engine import RuleBook, PolicyContext, evaluate_policies
from sp_pdf_judger.policy_summary import manufacturing_policy_cards, summarize_manufacturing_policies
from sp_pdf_judger.manufacturing_stage_ui import _norm_match

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("case", [f"D{i:02d}" for i in range(17)])
def test_corpus_findings_and_ui_use_identical_outcomes(case, monkeypatch, tmp_path):
    import sp_judgement_bridge as bridge
    import sp_pdf_judger.manufacturing_stage_ui as ui
    # Fresh clones must exercise the current public PDFs, not private or stale
    # historical JSON (D06's original filename no longer exists).
    pdf, = (ROOT / "더미데이터").glob(f"{case}_*.pdf")
    permits = list((ROOT / "더미데이터").glob("*허가문서*.pdf"))
    resolution = resolve_submission_permits(permits, "동국바이오사이언스", "스카이코비원멀티주")
    offline = ResolvedPermit(None, resolution.paths, resolution.fingerprint, resolution.errors)
    with patch("sp_pdf_judger.llm.create_clova_client", return_value=None):
        result = DocumentJudgePipeline(company="동국바이오사이언스", product="스카이코비원멀티주",
                                       permit_resolution=offline).run(pdf)
    records = result.extracted_records
    book = RuleBook()
    findings = evaluate_policies(PolicyContext(pdf, records, list((ROOT / "더미데이터").glob("*허가문서*.pdf")), "스카이코비원멀티주"), book)
    cards = manufacturing_policy_cards(findings, book, records=records)
    assert len(cards) == len({_norm_match(card.stage_name) for card in cards})
    if case == "D00":
        assert cards and all(card.status == "합격" for card in cards)
        assert len(cards) == 10
        assert cards[-1].stage_name == "동일 제조번호 문서 전체 정합성"
        assert not any(card.stage_name == "항원바이알" for card in cards)
        finished = next(card for card in cards if card.stage_name == "완제의약품")
        assert len(finished.test_dates) == 14 and finished.fields
        assert not any(date.test_name in {"최종원액 저장기한과 완제품 제조·시험일", "최종원액 제조량과 직접 투입량"}
                       for card in cards for date in card.test_dates)
    status, reason, meta = summarize_manufacturing_policies(findings, book, [3])
    if case == "D08":
        assert status == "검수불합격"
    result.metadata.update(manufacturing_info_cards=[asdict(card) for card in cards],
                           manufacturing_summary_meta=meta)
    result.manufacturing_summary_status = status
    result.manufacturing_summary_reason = reason
    monkeypatch.setattr(ui, "validate_manufacturing_info_consistency", lambda *a, **k: pytest.fail("Legacy re-judgement"))
    html = ui.render_manufacturing_summary_with_stage_cards(result)
    assert 'class="mfg-compact-card' in html and "제조번호" in html
    if case == "D00":
        assert "불일치 항목 있음" not in html
    csv_path = bridge._write_summary_csv_from_result(result, tmp_path / "summary.csv")
    counts = bridge._overall_from_rows(bridge._read_summary_rows_by_position(csv_path))
    assert counts == {"pass": result.summary.passed, "fail": result.summary.failed,
                      "hold": result.summary.held, "total": result.summary.total}
    if case not in {"D00", "D15", "D16"}:
        assert counts["fail"] > 0, "MD-only failures must remain visible in overall UI totals"
