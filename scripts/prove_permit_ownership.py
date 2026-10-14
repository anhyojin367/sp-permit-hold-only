"""Real PDF extraction proof of exclusive evidence ownership; no LLM/verdict."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from sp_pdf_judger.permit_catalog import PermitPolicy
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.permit_source_ownership import build_permit_ownership_inventory
from scripts.validate_corpus import engine_fingerprint


POLICY = PermitPolicy(policy_id="ownership-proof", authoritative=True,
                      ignore_section_numbers=True, require_llm=True, ocr_mode="auto")


def run_proof(output, font):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    pdf = output / "permit_ownership.pdf"
    pages = [
        ["SYNTHETIC QA / SOURCE OWNERSHIP / PAGE 1", "변경 및 처분사항 등",
         "2024.06.19 허가조건", "2024.11.25 제조방법", "1. 제조방법",
         "이 상위 문장은 하위 문장과 별도로 보존한다.", "1.1. 원액",
         "각 배치의 근거를 개별 확인한다.", "1.1.1. 확인시험", "반복되는 문장",
         "1.1.1.1. 추가시험", "반복되는 문장"],
        ["앞 페이지 추가시험의 이어지는 문장이다.", "1.1.2. 함량시험",
         "농도가 지정 범위인 경우 희석하지 않는다.", "희석 생략은 시험 면제 확정이 아니다.",
         "2. 다음 단계", "2.1. 확인시험", "다른 단계의 확인시험을 구분한다."],
        ["제품명", "소유범위 검증용 합성제품", "제조원", "시험용 가상제조원",
         "이 문서는 QA 전용이며 원본 제출 문서가 아니다."],
    ]
    pdfmetrics.registerFont(TTFont("OwnershipProof", str(font)))
    canvas = Canvas(str(pdf), pagesize=(595.28, 841.89), invariant=1)
    canvas.setTitle("Synthetic permit source ownership proof")
    for rows in pages:
        canvas.setFont("OwnershipProof", 11)
        for index, text in enumerate(rows):
            canvas.drawString(42, 791 - index * 25, text)
        canvas.showPage()
    canvas.save()
    store = PermitPdfStore([pdf], policy=POLICY)
    manifest = build_permit_ownership_inventory(store)
    assert not manifest["errors"], manifest["errors"]
    child = next(n for n in manifest["nodes"] if n["title"] == "추가시험")
    assert len(child["ancestor_ids"]) == 3
    assert {s["page_number"] for s in child["owned_spans"]} == {1, 2}
    assert sum(n["owned_text"].count("반복되는 문장") for n in manifest["nodes"]) == 2
    assert len([n for n in manifest["nodes"] if n["source_kind"] == "dated_entry"]) == 2
    assert sum("희석" in n["owned_text"] for n in manifest["nodes"]) == 1
    report = {"engine_fingerprint": engine_fingerprint(), "llm_calls": 0,
              "scope": "source ownership only; not conditional applicability", "manifest": manifest}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".local_validation/permit_ownership_proof")
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/malgun.ttf"))
    args = parser.parse_args()
    result = run_proof(args.output, args.font)
    print("Source nodes:", len(result["manifest"]["nodes"]), "Errors:", result["manifest"]["errors"])
    print("CLOVA calls: 0. Report:", args.output / "report.json")
