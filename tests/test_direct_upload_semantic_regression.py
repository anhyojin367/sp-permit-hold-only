"""Exercise upload policy -> real pipeline -> grounded response, not a forced policy."""
import json
from types import SimpleNamespace

import fitz
import pytest

from sp_pdf_judger.config import PASS_LABEL
from sp_pdf_judger.permit_catalog import resolve_submission_permits
from sp_pdf_judger.permit_conditions import parse_permit_conditions
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.permit_ocr import PermitPageText
from sp_pdf_judger.pipeline import DocumentJudgePipeline
from sp_pdf_judger.schemas import ExtractedRecord


@pytest.mark.parametrize("test_name,body,result,period,clauses", [
    (
        "외래성인자부정시험(in vitro)",
        "14일 간 배양하고 추가로 14일간 배양하면서 세포병변을 관찰한다. 검사결과가 모두 음성이어야 한다.",
        "음성", "2024.10.29 ~ 2024.11.26",
        [
            {"clause_id": 1, "kind": "procedure", "status": "N/A", "reason": "14일씩 두 단계의 절차"},
            {"clause_id": 2, "kind": "acceptance", "status": "PASS", "reason": "음성 결과가 일치"},
        ],
    ),
    (
        "불용성이물시험",
        "대한민국약전의 불용성이물시험법 제1법에 따르며 이에 적합하여야 한다.",
        "이물이 없음", "2025.07.18",
        [{"clause_id": 1, "kind": "acceptance", "status": "PASS", "reason": "이물이 없어 적합"}],
    ),
])
def test_ordinary_upload_does_not_replace_semantic_permit_review_with_unsafe_atoms(
    tmp_path, monkeypatch, test_name, body, result, period, clauses,
):
    # Only I/O boundaries are replaced. Catalog resolution, parsing, branching,
    # judgement, source-ID validation and final result construction remain real.
    pdf = tmp_path / "submission.pdf"
    permit = tmp_path / "permit.pdf"
    for path in (pdf, permit):
        with fitz.open() as document:
            document.new_page()
            document.save(path)
    resolution = resolve_submission_permits([permit], "동국바이오사이언스", "스카이코비원멀티주")
    stage = "CHO 마스터 세포주"
    pages = [PermitPageText(1, f"2.1. {stage}\n2.1.1. {test_name}\n{body}", "native")]
    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(tmp_path / "index"))
    def store_factory(paths, policy, **kwargs):
        store = PermitPdfStore.from_page_texts(pages, policy=policy, source_file=permit.name)
        store.permit_pdf_paths = list(paths)
        store.conditions = [condition for chunk in store.chunks for condition in parse_permit_conditions(chunk)]
        return store
    monkeypatch.setattr("sp_pdf_judger.pipeline.PermitPdfStore", store_factory)
    monkeypatch.delenv("CLOVA_RESPONSE_CACHE_DIR", raising=False)
    monkeypatch.setattr("sp_pdf_judger.clova_transport._GATES", {})
    def create(**kwargs):
        title = kwargs["response_format"]["json_schema"]["schema"].get("title")
        if title == "GroundedPermitResponse":
            response = {"permit_match_status": "matched", "candidate_id": 1,
                        "reason": "허가서 조건과 일치", "clauses": clauses}
        else:
            response = {"status": PASS_LABEL, "reason": "시험결과가 일치하여 검수합격입니다."}
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(response)), finish_reason="stop")])
    client = SimpleNamespace(base_url="https://example.invalid",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr("sp_pdf_judger.llm.create_clova_client", lambda *a, **k: client)
    record = ExtractedRecord(record_type="test", order_idx=1, section_number="2.1.1",
        section_title=stage, test_name=test_name, criteria="허가서에 적합하여야 한다.",
        result=result, method="자가기시", test_period=period, page_start=1, page_end=1,
        raw_text=f"{test_name}\n시험결과 {result}", batch_id="CHO-MCB-211012",
        batch_evidence={"source_kind": "explicit_table_column", "label": "제조번호", "quote": "제조번호 CHO-MCB-211012"})
    pipeline = DocumentJudgePipeline(company="동국바이오사이언스", product="스카이코비원멀티주",
        permit_resolution=resolution, llm_execution_settings=("test-only-key", "test", ""))
    reviewed = pipeline.run(pdf, extracted_records=[record])
    evaluation, = [e for e in reviewed.evaluations if e.record_type == "test"]
    assert evaluation.final_status == PASS_LABEL, evaluation.reason
    assert evaluation.source == "permit_pdf_llm_authoritative"
    assert evaluation.result == result
    assert not evaluation.permit_group_id
