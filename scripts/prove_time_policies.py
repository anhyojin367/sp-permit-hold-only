"""Generate separate synthetic PDFs and prove extraction -> C.3/C.10 MD decisions.

Does not modify the user's corpus, call an LLM, or change active policy defaults.
"""
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


RULES = ROOT / "docs" / "examples" / "time_rules"


def create_fixture(path, case, font_path):
    pdfmetrics.registerFont(TTFont("TimeProof", str(font_path)))
    canvas = Canvas(str(path), pagesize=(595.28, 841.89))
    canvas.setTitle(f"Synthetic time-policy fixture: {case}")
    canvas.setFont("TimeProof", 10)
    canvas.drawString(48, 795, f"SP synthetic test / {case} / not a submitted document")
    rows = [
        ("1 배양 공정", 16),
        ("시작일시: 2026.09.25 23:45", 11),
        ("종료일시: " + ("미정" if case == "missing" else "2026.09.26 01:15"), 11),
        ("처리시간: " + ("89분" if case == "mismatch" else "1시간 30분"), 11),
        ("", 11),
        ("2 첨부용제", 16),
        ("제조년월일: 2024.01.31", 11),
        ("사용기간: " + ("미정" if case == "missing" else "1개월"), 11),
        ("유효일: " + ("2024.03.01" if case == "mismatch" else "2024.02.29"), 11),
    ]
    for index, (text, size) in enumerate(rows):
        canvas.setFont("TimeProof", size)
        canvas.drawString(48, 735 - index * 36, text)
    canvas.save()


def run_proof(output, font_path):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    book = RuleBook(RULES)
    report = {"rule_fingerprint": book.fingerprint, "llm_calls": 0, "cases": {}}
    for case, expected in [("normal", "PASS"), ("mismatch", "FAIL"), ("missing", "HOLD")]:
        pdf = output / f"{case}.pdf"
        create_fixture(pdf, case, font_path)
        records = extract_records(pdf, output / case)
        findings = evaluate_policies(PolicyContext(pdf, records), book)
        report["cases"][case] = {"expected": expected, "findings": [asdict(f) for f in findings]}
        assert len(findings) == 2 and all(f.status == expected for f in findings), report["cases"][case]
        assert all(f.evidence and f.evidence[0]["page"] == 1 for f in findings)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".local_validation" / "time_policy_proof")
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/malgun.ttf"))
    args = parser.parse_args()
    if not args.font.is_file():
        parser.error("Provide --font with an installed Korean TrueType font")
    report = run_proof(args.output, args.font)
    for case, row in report["cases"].items():
        print(case, [(f["rule_id"], f["status"]) for f in row["findings"]])
    print("No CLOVA calls. Report:", args.output / "report.json")


if __name__ == "__main__":
    main()
