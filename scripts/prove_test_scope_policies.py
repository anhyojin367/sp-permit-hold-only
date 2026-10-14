"""Separate source blocks, through real PDF extraction and opt-in MD. No API."""
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


CASES = {"complete": "PASS", "missing_target": "FAIL", "mention_in_target": "HOLD",
         "missing_boundary": "HOLD", "duplicate_target": "HOLD",
         "result_missing": "HOLD", "only_other_stage": "FAIL"}


def run_proof(output, font):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    permit = output / "permit.pdf"
    copy_reviewed_permit(permit)
    report = {"engine_fingerprint": engine_fingerprint(), "llm_calls": 0, "cases": {}}
    book = RuleBook(ROOT / "docs/examples/test_scope_rules")
    for case, expected in CASES.items():
        rows = ["1 원액", "1.1 배치목록", "제조번호 L1", "1.2 다른단계",
                "시험명: 함량시험", "시험결과: 95",
                "1.3 임의시험" if case == "missing_boundary" else "1.3 대상시험"]
        if case != "only_other_stage":
            rows += ["시험명: 무균시험", "시험결과: 불검출"]
        else:
            rows += ["대상 구간의 시험 기록 없음"]
        if case in {"complete", "missing_boundary", "duplicate_target", "result_missing"}:
            rows += ["시험명: 함량시험"]
            if case != "result_missing": rows += ["시험결과: 95"]
        if case == "mention_in_target": rows += ["참고: 함량시험은 별도 확인이 필요함"]
        if case == "duplicate_target": rows += ["시험명: 함량시험", "시험결과: 94"]
        rows += ["1.4 다음시험", "다음 절의 안내", "2 다음단계"]
        pdf = output / f"{case}.pdf"
        draw_pdf(pdf, rows, font)
        records = extract_records(pdf, output / case)
        findings = evaluate_policies(PolicyContext(pdf, records, [permit], product="합성시험제품"), book)
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
    parser.add_argument("--output", type=Path, default=ROOT / ".local_validation/test_scope_proof")
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/malgun.ttf"))
    args = parser.parse_args()
    report = run_proof(args.output, args.font)
    for case, row in report["cases"].items(): print(case, row["findings"][0]["status"])
    print("CLOVA calls: 0. Report:", args.output / "report.json")
