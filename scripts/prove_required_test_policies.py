"""Separate synthetic PDF proof of opt-in permit/batch presence rules. No API calls."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from sp_pdf_judger.extractor import extract_records
from sp_pdf_judger.policy_engine import PolicyContext, RuleBook, evaluate_policies
from scripts.validate_corpus import engine_fingerprint
from scripts.reviewed_test_fixture import copy_reviewed_permit


RULES = ROOT / "docs" / "examples" / "required_test_rules"
CASES = {"normal": "PASS", "missing_test": "FAIL", "missing_scope": "HOLD",
         "multi_batch_complete": "PASS", "multi_batch_missing": "FAIL",
         "unknown_batch": "HOLD", "changed_permit": "HOLD"}


def draw_pdf(path, rows, font_path):
    pdfmetrics.registerFont(TTFont("RequiredProof", str(font_path)))
    # Stable bytes make the reviewed fixture document pin reproducible across
    # repeated local runs; no creation-time or random document ID variation.
    canvas = Canvas(str(path), pagesize=(595.28, 841.89), invariant=1)
    canvas.setTitle("Synthetic mandatory-test proof / " + path.stem)
    canvas.setFont("RequiredProof", 10)
    canvas.drawString(42, 809, "SYNTHETIC QA / " + path.stem + " / not a submitted document")
    for index, text in enumerate(rows):
        canvas.setFont("RequiredProof", 11)
        canvas.drawString(42, 771 - index * 21, text)
    canvas.setFont("RequiredProof", 9)
    canvas.drawString(42, 30, "Test fixture only. Source user PDFs are not modified.")
    canvas.save()


def create_sp(path, case, font_path):
    multi = case in {"multi_batch_complete", "multi_batch_missing", "unknown_batch"}
    rows = ["1 원액", "1.1 배치목록", "제조번호: L1"]
    if multi:
        rows += ["1.2 배치목록 2", "제조번호: L2"]
    rows += ["1.3 시험"]
    if case != "missing_scope":
        for lot in (["L1", "L2"] if multi else ["L1"]):
            for name in ("무균시험", "함량시험"):
                if name == "함량시험" and (case == "missing_test" or case == "multi_batch_missing" and lot == "L2"):
                    continue
                bound = "미정" if case == "unknown_batch" and lot == "L2" else lot
                rows += [f"시험명: {name}", "시험방법: 자사 시험법으로 측정한다.",
                         "시험기준: " + ("균이 없어야 한다." if name == "무균시험" else "90 이상"),
                         "시험결과: " + ("불검출" if name == "무균시험" else "95"),
                         f"비고: 제조번호: {bound}"]
    if case != "missing_scope":
        rows += ["2 다음단계"]
    draw_pdf(path, rows, font_path)


def run_proof(output, font_path):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    permit_rows = ["1. 제조방법 및 시험기준", "1.1. 원액", "1.1.1. 무균시험",
                   "균이 없어야 한다.", "1.1.2. 함량시험", "90 이상이어야 한다.", "2. 안내"]
    permit = output / "permit.pdf"
    revised = output / "permit_conditional.pdf"
    # The MD pins reviewed PDF bytes, not just its visible text. Regenerating it
    # with a different Python/zlib version legitimately invalidates that pin.
    copy_reviewed_permit(permit)
    draw_pdf(revised, [row.replace("균이 없어야 한다.", "재시험인 경우에만 실시한다. 균이 없어야 한다.")
                       for row in permit_rows], font_path)
    book = RuleBook(RULES)
    report = {"rule_fingerprint": book.fingerprint, "engine_fingerprint": engine_fingerprint(),
              "llm_calls": 0, "cases": {}}
    for case, expected in CASES.items():
        pdf = output / f"{case}.pdf"
        create_sp(pdf, case, font_path)
        records = extract_records(pdf, output / case)
        ctx = PolicyContext(pdf, records, [revised if case == "changed_permit" else permit], product="합성시험제품")
        findings = evaluate_policies(ctx, book)
        report["cases"][case] = {"expected": expected, "findings": [asdict(f) for f in findings]}
    report["engine_changed_during_run"] = engine_fingerprint() != report["engine_fingerprint"]
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not report["engine_changed_during_run"], "Engine changed during proof"
    for case, row in report["cases"].items():
        assert len(row["findings"]) == 1 and row["findings"][0]["status"] == row["expected"], (case, row)
        assert not row["findings"][0]["details"].get("execution_error"), (case, row)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".local_validation" / "required_test_proof")
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/malgun.ttf"))
    args = parser.parse_args()
    if not args.font.is_file():
        parser.error("Provide --font with an installed Korean TrueType font")
    report = run_proof(args.output, args.font)
    for case, row in report["cases"].items():
        print(case, row["findings"][0]["status"])
    print("No CLOVA calls. Report:", args.output / "report.json")


if __name__ == "__main__":
    main()
