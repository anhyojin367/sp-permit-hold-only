"""Synthetic real-extractor proof of scoped numeric MD; never activates a product."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml

from scripts.prove_permit_applicability import draw_pages, fixture_contract, POLICY
from scripts.validate_corpus import engine_fingerprint
from sp_pdf_judger.extractor import extract_records
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.permit_applicability_md import evaluate_numeric_pdf

CASES = {
    "normal": ("100 mL", "2", "20 mg/mL", ["required", "required"]),
    "missing_own_field": (None, "2", "20 mg/mL", ["unknown", "unknown"]),
    "wrong_unit": ("100 L", "2", "20 mg/mL", ["unknown", "unknown"]),
    "duplicate_field": ("100 mL", "2", "20 mg/mL", ["unknown", "unknown"]),
    "round_one": ("100 mL", "1", "20 mg/mL", ["not_required", "required"]),
    "procedure_false": ("100 mL", "2", "5 mg/mL", ["required", "required"]),
    "wrong_batch": ("100 mL", "2", "20 mg/mL", "reject"),
    "multiple_batches": ("100 mL", "2", "20 mg/mL", "reject"),
}


def source_scopes():
    info = dict(record_path=["원액", "정보", "A 정보"], record_type="content", start="1.1.1 A 정보", end="1.1.2 B 정보")
    test = dict(record_path=["원액", "시험", "A 시험"], record_type="test", test_name="농도확인시험",
                start="시험명 농도확인시험", end="시험명 다음시험")
    return [dict(sp_stage="원액", batch_field="제조번호", batch_source=info, fields=[
        dict(name="용기규격", label="용기규격", source=info),
        dict(name="시험차수", label="시험차수", source=info),
        dict(name="농도", label="시험결과", source=test),
    ])]


def run_proof(output, font, rules_md=None):
    output, font = Path(output), Path(font)
    output.mkdir(parents=True, exist_ok=True)
    permit = output / "permit.pdf"
    draw_pages(permit, [["1. 제조방법", "1.1. 원액", "용기규격 100 mL인 경우에만 다음 시험을 수행한다.",
        "1.1.1. 무균시험", "시험차수 2 이상인 경우에만 수행한다.", "1.1.2. 함량시험",
        "농도 10 mg/mL 이상이면 희석 절차를 생략한다."]], font)
    store = PermitPdfStore([permit], policy=POLICY)
    contract = fixture_contract(store)
    config = dict(schema_version=1, id="SYNTHETIC_SCOPED_NUMERIC", company="합성제조사", product="합성시험제품",
        review_note="이 스크립트가 만든 고정 합성 허가/표에 대한 시험 전용 설정. 실제 제품 자동 승인이 아니다.",
        input_layout="single_batch_scoped_records_v1", source_scopes=source_scopes(),
        reviewed_document_id=store.source_documents[0].evidence_id,
        reviewed_node_ids=[n["evidence_id"] for n in contract.nodes],
        reviewed_condition_ids=[c.source_span_id for c in contract.conditions],
        stages=[dict(source_node_id=contract.requirements[0].stage_node_id, sp_stage="원액")],
        fields=[dict(name=c.field, unit=c.unit) for c in contract.conditions],
        requirements=[r.model_dump() for r in contract.requirements],
        conditions=[c.model_dump() for c in contract.conditions])
    # Repeated scope dictionaries must serialize independently (MD rejects aliases).
    config = json.loads(json.dumps(config, ensure_ascii=False))
    rules = Path(rules_md) if rules_md is not None else output / "conditions.md"
    if rules_md is None:
        rules.write_text("---\n" + yaml.safe_dump({"applicability": config}, allow_unicode=True, sort_keys=False)
                         + "---\n\n# 합성 표 연결 검증 전용\n", encoding="utf-8")
    report = dict(engine_fingerprint=engine_fingerprint(), rules_md_sha256=hashlib.sha256(rules.read_bytes()).hexdigest(),
                  logical_calls=0, cases={})
    for name, (size, round_, concentration, expected) in CASES.items():
        lines = ["1. 원액", "1.1. 정보", "1.1.1 A 정보", "제조번호", "A-01"]
        if name == "multiple_batches": lines += ["제조번호 A-02"]
        if size is not None: lines += ["용기규격", size]
        if name == "duplicate_field": lines += ["용기규격 50 mL"]
        lines += ["시험차수 " + round_, "1.1.2 B 정보", "제조번호 B-99", "용기규격 50 mL", "1.2. 시험", "1.2.1 A 시험",
                  "시험명 농도확인시험", "시험방법 기록", "시험기준 기록", "시험기간 2025.01.01", "시험결과 " + concentration]
        if name == "wrong_batch": lines += ["비고", "제조번호 B-99"]
        lines += ["시험명 다음시험", "시험방법 기록", "시험기준 기록", "시험기간 2025.01.01", "시험결과 1", "2. 다음 단계"]
        pdf = output / f"{name}.pdf"
        draw_pages(pdf, [lines], font)
        records = extract_records(pdf, output / (name + "_extract"))
        row = dict(expected=expected, record_count=len(records))
        try:
            row["result"] = evaluate_numeric_pdf(rules, store, pdf, records, company="합성제조사", product="합성시험제품")
        except ValueError as error:
            row["rejected"] = str(error)
        actual = "reject" if "rejected" in row else [d["applicability"] for d in row["result"]["decisions"]]
        row["matches_expected"] = actual == expected
        if expected == "reject":
            required_reason = "다른 배치" if name == "wrong_batch" else "단일 배치"
            row["matches_expected"] = row["matches_expected"] and required_reason in row.get("rejected", "")
        report["cases"][name] = row
        print(name, actual, "matches", row["matches_expected"], row.get("rejected", ""), flush=True)
    report["engine_changed_during_run"] = report["engine_fingerprint"] != engine_fingerprint()
    report["rules_changed_during_run"] = report["rules_md_sha256"] != hashlib.sha256(rules.read_bytes()).hexdigest()
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".local_validation/scoped_facts_proof")
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/malgun.ttf"))
    parser.add_argument("--rules-md", type=Path, help="Use explicit MD without rewriting it (synthetic source only)")
    args = parser.parse_args()
    report = run_proof(args.output, args.font, args.rules_md)
    raise SystemExit(0 if not report["engine_changed_during_run"] and not report["rules_changed_during_run"]
                     and all(r["matches_expected"] for r in report["cases"].values()) else 1)
