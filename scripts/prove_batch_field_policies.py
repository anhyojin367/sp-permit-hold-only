"""Isolated adjacent-label/value PDF fixtures; never edits provided documents."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.prove_required_test_policies import draw_pdf
from scripts.reviewed_test_fixture import copy_reviewed_permit
from scripts.validate_corpus import engine_fingerprint
from sp_pdf_judger.extractor import extract_records
from sp_pdf_judger.policy_engine import PolicyContext, RuleBook, evaluate_policies


RULES = ROOT / "docs/examples/batch_field_rules"
CASES = {"normal": "PASS", "missing_test": "FAIL", "missing_scope": "HOLD",
         "related_field_only": "HOLD", "conflicting_lot": "HOLD",
         "multi_batch_complete": "PASS", "multi_batch_missing": "FAIL",
         "unbound_multi_batch": "HOLD"}


def run_proof(output, font):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    permit = output / "permit.pdf"
    copy_reviewed_permit(permit)
    report = {"engine_fingerprint": engine_fingerprint(), "llm_calls": 0, "cases": {}}
    for case, expected in CASES.items():
        multi = case.startswith("multi_batch") or case == "unbound_multi_batch"
        rows = ["제조번호: FIN-9", "1 원액", "1.1 배치목록",
                ("제조용 세포주 제조번호" if case == "related_field_only" else "제조번호") + "     L1"]
        if multi:
            rows += ["1.2 배치목록", "제조번호     L2"]
        rows += ["1.3 시험"]
        for lot in (["L1", "L2"] if multi else ["L1"]):
            for name in ("무균시험", "함량시험"):
                if name == "함량시험" and (case == "missing_test" or case == "multi_batch_missing" and lot == "L2"):
                    continue
                rows += [f"시험명: {name}", "시험결과: " + ("불검출" if name == "무균시험" else "95")]
                if multi and case != "unbound_multi_batch" or case == "conflicting_lot":
                    bound = "L2" if case == "conflicting_lot" else lot
                    rows += [f"비고: 제조번호 {bound}"]
        if case != "missing_scope":
            rows += ["2 다음단계", "제조번호: OTHER-8"]
        pdf = output / f"{case}.pdf"
        draw_pdf(pdf, rows, font)
        records = extract_records(pdf, output / case)
        book = RuleBook(RULES / ("multi" if multi else "single"))
        ctx = PolicyContext(pdf, records, [permit], product="합성시험제품")
        findings = evaluate_policies(ctx, book)
        report["cases"][case] = {"expected": expected, "rule_fingerprint": book.fingerprint,
            "record_count": len(records), "findings": [asdict(f) for f in findings]}
    report["engine_changed_during_run"] = engine_fingerprint() != report["engine_fingerprint"]
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not report["engine_changed_during_run"]
    for case, row in report["cases"].items():
        assert len(row["findings"]) == 1 and row["findings"][0]["status"] == row["expected"], (case, row)
        assert not row["findings"][0]["details"].get("execution_error"), (case, row)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".local_validation/batch_field_proof")
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/malgun.ttf"))
    args = parser.parse_args()
    report = run_proof(args.output, args.font)
    for case, row in report["cases"].items(): print(case, row["findings"][0]["status"])
    print("CLOVA calls: 0. Report:", args.output / "report.json")
