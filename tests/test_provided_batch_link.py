"""Diagnostic pin for provided synthetic data; NOT an activated product policy."""
from pathlib import Path
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from sp_pdf_judger.extractor import extract_records
from sp_pdf_judger.policy_engine import PolicyContext, Rule, permit_required_tests, to_evaluations


def test_provided_component_a_table_links_its_ten_tests_without_header_borrowing(tmp_path):
    root = Path(__file__).resolve().parents[1]
    pdfs = list((root / "더미데이터").glob("D00_*.pdf"))
    permits = list((root / "더미데이터").glob("*허가문서*.pdf"))
    if len(pdfs) != 1 or len(permits) != 1:
        pytest.skip("Provided synthetic corpus required")
    records = extract_records(pdfs[0], tmp_path / "extract")
    ctx = PolicyContext(pdfs[0], records, permits)
    # Fixed, observed fixture hashes: never calculate and automatically approve
    # a replacement permit. This asserts presence wiring, not legal obligation.
    params = dict(
        permit_stage_path=["제조방법 및 시험기준", "Component A 중간체 원액", "Component A 중간체 원액에 대한 시험"],
        reviewed_scope_sha256="dbb226321db81bdc0db18f5bce5548864ab8f9326a957e8affc54a18581bfde7",
        reviewed_document_sha256="0c153c31ca738dfbf6a2b752ea09fa8a8ea9badbae3466d870c6a2279e77f8d2",
        requirement_policy="all_leaf_sections_required",
        sp_stage_path=["원액", "시험", "Component A 중간체원액에 대한 시험"],
        coverage_start="3.1.1 Component A 중간체 원액 정보",
        coverage_end="3.2.3 Component B 중간체원액에 대한 시험",
        batch_inventory={"type": "content", "context": ["Component A 중간체 원액 정보"]},
        batch_inventory_path=["원액", "정보", "Component A 중간체 원액 정보"],
        batch_coverage_start="3.1.1 Component A 중간체 원액 정보",
        batch_coverage_end="3.1.2 Component B 중간체 원액 정보",
        test_coverage_start="3.2.2 Component A 중간체원액에 대한 시험",
        test_coverage_end="3.2.3 Component B 중간체원액에 대한 시험",
        batch_field="제조번호", batch_field_layout="label_value_lines", batch_binding="single_batch_stage",
        name_mappings=[{"permit_test_path": [f"잔류 숙주세포 유래 {target}함량시험"],
            "sp_test_paths": [[f"잔류 숙주세포 유래 {target}시험"]],
            "reason": "합성 D00 연결 진단: 동일 중간체/측정 대상/방법. 운영 의무 검토를 대신하지 않음."}
            for target in ("단백질", "DNA")])
    policy = Rule(id="DIAGNOSTIC_A_INTERMEDIATE", title="중간체 연결 진단 (미활성)",
                  instruction="기록 존재 연결 진단이며 결과 적합이나 전체 허가 의무 검토가 아니다.",
                  operation="permit_required_tests", products=["스카이코비원멀티주"], params=params)
    finding = permit_required_tests(policy, ctx)
    assert finding.status == "PASS", finding.reason
    assert finding.details["checked"] == 10
    assert finding.details["batch_inventory_audit"]["batches"] == ["SKY-CA220705"]
    assert finding.details["batch_inventory_audit"]["raw_field_count"] == 1
    assert finding.details["test_coverage_audit"]["first"] == 15
    assert {r["batch"] for r in finding.details["requirement_checks"]} == {"SKY-CA220705"}
    assert {e["page"] for e in finding.evidence if e["source"] == "permit"} == {11, 12, 13}
    assert {e["page"] for e in finding.evidence if e["source"] == "sp"} == {13, 15, 16}
    # Use existing card/evidence adapters; the new audit needs no UI redesign.
    from sp_judgement_bridge import _policy_evidence_links
    from test_policy_evidence_links import parsed_links
    row = to_evaluations([finding], SimpleNamespace(rules=[policy]), 200)[0]
    assert row.source == "md_policy" and row.page_start == 13
    model = SimpleNamespace(pdf_path=pdfs[0], metadata={
        "permit_paths": list(map(str, permits)), "policy_audit": [asdict(finding)]})
    links = set(parsed_links(_policy_evidence_links(model, policy.id)))
    assert links == {(pdfs[0], p) for p in (13, 15, 16)} | {(permits[0], p) for p in (11, 12, 13)}
