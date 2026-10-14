"""Independent section/status oracle for the private D00-D12 presentation corpus.

This replays saved decisions, with no CLOVA request or re-judgement. Full corpus
judgement runs remain separately versioned in .local_validation.
"""
import csv
import json
from collections import Counter
from pathlib import Path

import pytest

from sp_pdf_judger.config import PASS_LABEL, FAIL_LABEL, HOLD_LABEL
from sp_pdf_judger.extractor import _record_from_dict
from sp_pdf_judger.schemas import Evaluation, ProcessingResult, Summary
from sp_pdf_judger.stage_csv_exporter import export_stage_csvs, _write_export_summary_csv
from sp_pdf_judger.manufacturing_stage_ui import build_stage_info_cards

ROOT = Path(__file__).resolve().parents[1]
# The fixture's actual section-to-stage relationship, not the UI matching code.
SECTIONS = {
    "2.1.2.1.1": "CHO 마스터 세포주", "2.1.2.1.2": "E.coli 마스터 세포주",
    "2.1.2.2.1": "CHO 제조용 세포주", "2.1.2.2.2": "E.coli 제조용 세포주",
    "3.2.2": "Component A 중간체원액", "3.2.3": "Component B 중간체원액",
    "3.2.4": "나노파티클원액", "4.2": "최종원액", "5.2": "완제의약품",
}
LABELS = (PASS_LABEL, FAIL_LABEL, HOLD_LABEL)


def norm(value):
    return "".join(value.split()).casefold()


def authoritative_counts(evaluations):
    counts = Counter()
    for ev in evaluations:
        statuses = [r["status"] for r in ev.lot_judgements] if ev.lot_judgements else [ev.final_status]
        assert all(status in LABELS for status in statuses)
        counts.update(statuses)
    return tuple(counts[label] for label in LABELS)


def load_result(path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ProcessingResult(Path(raw["pdf_path"]), Path(raw["preview_image_path"]),
        [_record_from_dict(row, i) for i, row in enumerate(raw["extracted_records"])],
        [Evaluation(**row) for row in raw["evaluations"]], [], Summary(**raw["summary"]), raw["metadata"])


@pytest.mark.parametrize("mode", ["integrated_offline", "live"])
@pytest.mark.parametrize("case", [f"D{i:02}" for i in range(13)])
def test_all_stages_and_csv_follow_saved_authoritative_decisions(mode, case, tmp_path):
    import sp_judgement_bridge as bridge
    path = ROOT / ".local_validation" / mode / f"{case}.json"
    if not path.exists():
        pytest.skip("Private corpus required")
    result = load_result(path)
    expected = {
        norm(name): authoritative_counts([e for e in result.evaluations
            if e.section_number == section and e.source != "md_policy"])
        for section, name in SECTIONS.items()
    }
    cards = build_stage_info_cards(result)
    assert {norm(card.display_name) for card in cards} == set(expected)
    for card in cards:
        assert (card.passed, card.failed, card.held) == expected[norm(card.display_name)]

    summary = bridge._write_summary_csv_from_result(result, tmp_path / "simulation.csv")
    rows = list(csv.DictReader(summary.open(encoding="utf-8-sig")))
    for row in rows:
        if norm(row["제조명"]) in expected:
            assert tuple(int(row[label]) for label in LABELS) == expected[norm(row["제조명"])]
    assert tuple(int(rows[-1][label]) for label in LABELS) == (
        result.summary.passed, result.summary.failed, result.summary.held)

    exports = export_stage_csvs(result, tmp_path / "stages")
    assert exports
    for exported in exports:
        rows = list(csv.DictReader(exported.csv_path.open(encoding="utf-8-sig")))
        expected_statuses = [status for ev in exported.evaluations for status in (
            [r["status"] for r in ev.lot_judgements] if ev.lot_judgements else [ev.final_status])]
        assert [row["검수 결과"] for row in rows] == expected_statuses
    export_summary = _write_export_summary_csv(exports, tmp_path / "stages")
    rows = list(csv.DictReader(export_summary.open(encoding="utf-8-sig")))
    for exported, row in zip(exports, rows):
        assert tuple(int(row[label]) for label in LABELS) == authoritative_counts(exported.evaluations)
