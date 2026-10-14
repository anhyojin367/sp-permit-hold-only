from types import SimpleNamespace

from sp_pdf_judger.judgement import JudgeEngine
from sp_pdf_judger.llm import ClovaJudgeClient, JudgeResponse
from sp_pdf_judger.permit_catalog import PermitPolicy
from sp_pdf_judger.permit_llm_protocol import GroundedPermitResponse, ClauseDecision, parse_candidates
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.policy_engine import Rule, numeric_compare
from sp_pdf_judger.schemas import ExtractedRecord


def setup(acceptance="함량은 10 ng/mL 이하이어야 한다.", *, kind="acceptance"):
    policy = PermitPolicy("fixture", True, True, True, "native")
    store = PermitPdfStore.from_page_texts([(4, "2.1. Component A 중간체 원액\n2.1.1. 함량시험\n" + acceptance)],
                                           policy=policy, source_file="fixture-permit.pdf")
    local_client = SimpleNamespace(permit_audit=[])
    def explain(**kwargs):
        candidates = parse_candidates(kwargs["rag_contexts"][0])
        answer = GroundedPermitResponse(permit_match_status="matched", candidate_id=1, reason="근거 확인",
            clauses=[ClauseDecision(clause_id=i + 1, kind=kind,
                     status="PASS" if kind == "acceptance" else "N/A", reason="원문 확인")
                     for i, _ in enumerate(candidates[0].clauses)])
        return ClovaJudgeClient._ground_permit_result(local_client, answer, candidates, "fixture")
    client = SimpleNamespace(enabled=True, explain=explain)
    engine = JudgeEngine(SimpleNamespace(search=lambda *a, **k: []), client, store, policy)
    record = ExtractedRecord(order_idx=7, test_name="함량시험", section_title="Component A 중간체 원액",
                             criteria="5 ng/mL 이하", result="8 ng/mL")
    return engine, record


def test_grounded_source_is_captured_and_shared_with_md_without_an_extra_api_call():
    engine, record = setup()
    assert engine.judge_record(record).final_status == "검수합격"
    basis = engine.comparison_bases[7][-1]
    assert basis["source"] == "permit" and "10 ng/mL" in basis["criteria"]
    assert basis["evidence"][0]["file"] == "fixture-permit.pdf"
    assert basis["evidence"][0]["page"] == 4
    ctx = SimpleNamespace(select=lambda _: [record], comparison_bases=engine.comparison_bases, permit_authoritative=True,
                          evidence=lambda _: {"source": "sp", "page": 2, "quote": record.criteria})
    rule = Rule(id="T", title="수치", operation="numeric_compare", instruction="허가 기준 적용", criterion_source="permit_first")
    assert numeric_compare(rule, ctx).status == "PASS"


def test_method_only_is_an_explicit_sp_fallback_not_an_unresolved_permit():
    engine, record = setup("자가 방법으로 측정한다.", kind="procedure")
    engine.judge_record(record)
    assert engine.comparison_bases[7][-1]["source"] == "sp"


def test_failed_grounding_and_reused_record_id_cannot_reuse_a_previous_basis():
    engine, record = setup()
    engine.judge_record(record)
    engine.llm_client.explain = lambda **_: JudgeResponse(status="검수합격", reason="근거 없는 합격",
        permit_match_status="matched", matched_permit_test="다른 시험", permit_basis="1 ng/mL 이하")
    assert engine.judge_record(record).final_status == "검수보류"
    assert engine.comparison_bases[7] == [{"source": "unresolved", "reason": "허가서 적용 기준을 확정하지 못했습니다."}]


def test_missing_result_cannot_leave_a_stale_permit_basis():
    engine, record = setup()
    engine.judge_record(record)
    record.result = None
    assert engine.judge_record(record).final_status == "검수보류"
    assert 7 not in engine.comparison_bases
