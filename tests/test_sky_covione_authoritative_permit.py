from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

import sp_judgement_bridge as judgement_bridge
import sp_pdf_judger.pipeline as pipeline_module
import sp_pdf_judger.llm as llm_module
from sp_pdf_judger.config import FAIL_LABEL, HOLD_LABEL, PASS_LABEL
from sp_pdf_judger.judgement import JudgeEngine
from sp_pdf_judger.llm import ClovaJudgeClient, JudgeResponse
from sp_pdf_judger.permit_catalog import PermitPolicy, ResolvedPermit
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.schemas import ExtractedRecord


AUTHORITATIVE_POLICY = PermitPolicy("sky", True, True, True, "native")
NON_AUTHORITATIVE_POLICY = PermitPolicy("other", False, False, False, "native")

FULL_EGG_CHUNK = """3.2.1 유정란접종시험
요막강 내 접종
Allantoic eggs: 10~11일령 SPF 유정란 최소 10개에 시험검체를 접종한다.
36 ± 2 ℃에서 3일 배양하고 1차 생존율은 80% 이상이어야 한다.
계대 후 36 ± 2 ℃에서 3일 추가 배양하고 2차 생존율도 80% 이상이어야 한다.
기니피그, 닭, 사람 O형 적혈구에 대한 혈구응집반응이 없어야 한다.
난황낭 접종
Yolk-sac eggs: 6~7일령 SPF 유정란 최소 10개를 36 ± 2 ℃에서 9일 배양한다.
1차 생존율은 80% 이상이어야 하고 계대 후 9일 추가 배양하며 2차 생존율도 80% 이상이어야 한다.
"""


class FakePermitLLM:
    def __init__(self, response: JudgeResponse | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.enabled = True
        self.calls: list[dict] = []

    def explain(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class EmptyRagStore:
    def search(self, query: str, top_k: int = 9):
        return []


def _store(policy: PermitPolicy = AUTHORITATIVE_POLICY, text: str = FULL_EGG_CHUNK):
    return PermitPdfStore.from_page_texts([(1, text)], policy=policy, source_file="permit.pdf")


def _record(*, test_name: str = "유정란접종시험", criteria: str = "생존율 80% 이상", result: str = "생존율 90%"):
    return ExtractedRecord(
        record_type="test",
        section_number="9.9.9",
        section_title="바이러스 시험",
        test_name=test_name,
        criteria=criteria,
        result=result,
        method="SP 시험법",
        test_period="3일",
        remarks="원자료 비고",
        raw_text=f"{test_name}\n기준: {criteria}\n결과: {result}",
    )


def _engine(client, policy=AUTHORITATIVE_POLICY):
    return JudgeEngine(
        EmptyRagStore(),
        llm_client=client,
        permit_store=_store(policy),
        permit_policy=policy,
    )


def test_authoritative_hold_preserves_grounding_failure_diagnostic():
    response = JudgeResponse(status=HOLD_LABEL, reason="not a trusted diagnostic",
        permit_match_status="ambiguous", matched_permit_test="", permit_basis="")
    response._permit_grounding_errors = ["clause_id=1: procedure의 PASS는 허용되지 않습니다."]
    client = FakePermitLLM(response)
    result = _engine(client).judge_record(_record())
    assert result.final_status == HOLD_LABEL
    assert "clause_id=1" in result.reason and "procedure" in result.reason


def test_model_cannot_inject_private_grounding_diagnostic():
    response = JudgeResponse.model_validate({"status": HOLD_LABEL, "reason": "api/429 private",
        "permit_match_status": "ambiguous", "_permit_grounding_errors": ["api/429 private"]})
    result = _engine(FakePermitLLM(response)).judge_record(_record())
    assert response._permit_grounding_errors == []
    assert "429" not in result.reason
    assert "_permit_grounding_errors" not in JudgeResponse.model_json_schema()["properties"]


class _CapturingPermitStore:
    def __init__(self, permit_pdf_paths=None, policy=None, *, pdf_sha256_by_path=None):
        self.permit_pdf_paths = list(permit_pdf_paths or [])
        self.pdf_sha256_by_path = pdf_sha256_by_path
        self.policy = policy
        self.chunks = []
        self.extraction_errors = []
        self.extraction_diagnostics = []

    @property
    def enabled(self):
        return False


class _PipelineRag:
    docs = []
    loaded_sources = []

    def search(self, query, top_k=9):
        return []


class _PipelineLlm:
    enabled = False
    model = "test"
    call_count = 0
    success_count = 0
    last_error = None

    def __init__(self, **_kwargs):
        pass

    def set_domain_detail_context(self, _context):
        pass


def test_pipeline_scopes_authoritative_policy_to_exact_company_and_product(monkeypatch, tmp_path):
    """Catch accidental catalog activation outside the company/product pair."""
    monkeypatch.setattr(pipeline_module, "PermitPdfStore", _CapturingPermitStore)
    monkeypatch.setattr(pipeline_module, "UcumRagStore", _PipelineRag)
    monkeypatch.setattr(pipeline_module, "ClovaJudgeClient", _PipelineLlm)

    target = pipeline_module.DocumentJudgePipeline(
        company="SK bioscience",
        product="SKYCovione",
    )
    wrong_company = pipeline_module.DocumentJudgePipeline(
        company="다른 회사",
        product="SKYCovione",
    )
    wrong_product = pipeline_module.DocumentJudgePipeline(
        company="SK bioscience",
        product="다른 제품",
    )
    explicit = tmp_path / "linked-non-target.pdf"
    explicit.write_bytes(b"not a catalog permit")
    linked_non_target = pipeline_module.DocumentJudgePipeline(
        permit_pdf_paths=[explicit],
        company="다른 회사",
        product="다른 제품",
    )

    assert target.judge_engine.permit_policy is not None
    assert target.judge_engine.permit_policy.authoritative is True
    assert target.judge_engine.permit_policy.evaluation_mode == "deterministic_first"
    assert [path.name for path in target.permit_store.permit_pdf_paths] == [
        "sky_covione_multidose.pdf"
    ]
    assert wrong_company.judge_engine.permit_policy is None
    assert wrong_company.permit_store.permit_pdf_paths == []
    assert wrong_company.permit_store.pdf_sha256_by_path == {}
    assert wrong_product.judge_engine.permit_policy is None
    assert wrong_product.permit_store.permit_pdf_paths == []
    assert wrong_product.permit_store.pdf_sha256_by_path == {}
    assert linked_non_target.permit_store.permit_pdf_paths == [explicit.resolve()]
    assert linked_non_target.judge_engine.permit_policy is None
    assert set(linked_non_target.permit_store.pdf_sha256_by_path) == {explicit.resolve()}


def test_bridge_cache_key_includes_resolved_permit_fingerprint(tmp_path):
    """Catch stale after-artifacts when a catalog decision changes without UI paths."""
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(b"source")

    before = judgement_bridge._artifact_key(
        pdf_path, [], None, "SK bioscience", "SKYCovione", "detail", "permit-v1"
    )
    after = judgement_bridge._artifact_key(
        pdf_path, [], None, "SK bioscience", "SKYCovione", "detail", "permit-v2"
    )

    assert before != after


def test_bridge_resolves_once_and_reuses_one_resolution_for_before_and_after(monkeypatch, tmp_path):
    """Catch divergent catalog reads between cache construction and pipeline phases."""
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(b"source")
    explicit = tmp_path / "linked.pdf"
    explicit.write_bytes(b"permit")
    policy = PermitPolicy("sky", True, True, True, "auto")
    resolution = ResolvedPermit(
        policy, (explicit.resolve(),), "resolved-fingerprint", ("catalog diagnostic",)
    )
    resolve_calls = []
    pipeline_calls = []

    def resolve_once(paths, company, product, *, product_mapping_id=""):
        resolve_calls.append((list(paths), company, product, product_mapping_id))
        return resolution

    class Pipeline:
        def __init__(self, **kwargs):
            pipeline_calls.append(kwargs)

        def run(self, _pdf_path, **_kwargs):
            return SimpleNamespace(
                extracted_records=[],
                metadata={"permit_extraction_diagnostics": [{"path": str(explicit)}]},
            )

    monkeypatch.setattr(judgement_bridge, "resolve_permits", resolve_once)
    monkeypatch.setattr(judgement_bridge, "DocumentJudgePipeline", Pipeline)
    monkeypatch.setattr(judgement_bridge, "_write_summary_csv_from_result", lambda *_args: None)
    monkeypatch.setattr(judgement_bridge, "_attach_rule_regression_log", lambda artifacts: artifacts)
    monkeypatch.setattr(judgement_bridge, "_remember_judgement_artifacts", lambda _artifacts: None)

    artifacts = judgement_bridge.ensure_judgement_artifacts(
        pdf_path=pdf_path,
        permit_paths=[explicit],
        original_csv_dir=None,
        company="SK bioscience",
        product="SKYCovione",
        product_mapping_id="dongkook_unknown_sky_m_to_sky_covione",
    )

    assert len(resolve_calls) == 1
    assert resolve_calls[0][3] == "dongkook_unknown_sky_m_to_sky_covione"
    assert len(pipeline_calls) == 2
    assert all(call["permit_resolution"] is resolution for call in pipeline_calls)
    assert pipeline_calls[0]["permit_enabled"] is False
    assert pipeline_calls[1]["permit_pdf_paths"] == [explicit.resolve()]
    assert artifacts["permit_fingerprint"] == "resolved-fingerprint"
    assert artifacts["permit_policy_id"] == "sky"
    assert artifacts["product_mapping_id"] == "dongkook_unknown_sky_m_to_sky_covione"
    assert artifacts["permit_resolution_errors"] == ["catalog diagnostic"]


def test_completed_artifact_metadata_contains_only_safe_execution_fingerprint(monkeypatch, tmp_path):
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(b"source")
    secret = "never-persist-this-api-key"
    endpoint = "https://private-clova.example.test/secret-path"
    monkeypatch.setattr(llm_module, "dotenv_values", lambda *args, **kwargs: {})
    monkeypatch.setattr(llm_module, "CLOVA_API_KEY", "")
    monkeypatch.setenv("CLOVA_API_KEY", secret)
    monkeypatch.setenv("CLOVA_MODEL", "HCX-007")
    monkeypatch.setenv("CLOVA_BASE_URL", endpoint)
    monkeypatch.setattr(judgement_bridge.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(judgement_bridge, "JUDGEMENT_STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(
        judgement_bridge,
        "JUDGEMENT_STATUS_INDEX",
        tmp_path / "status" / "status_index.json",
    )
    monkeypatch.setattr(
        judgement_bridge,
        "resolve_permits",
        lambda *_args, **_kwargs: ResolvedPermit(None, (), "no-permit", ()),
    )
    monkeypatch.setattr(
        judgement_bridge,
        "resolve_domain_detail_profile",
        lambda *_args: SimpleNamespace(fingerprint="detail", sources=[]),
    )

    class Pipeline:
        def __init__(self, **_kwargs):
            pass

        def run(self, _pdf_path, **_kwargs):
            return SimpleNamespace(extracted_records=[], metadata={})

    monkeypatch.setattr(judgement_bridge, "DocumentJudgePipeline", Pipeline)
    monkeypatch.setattr(judgement_bridge, "_write_summary_csv_from_result", lambda *_args: None)
    monkeypatch.setattr(judgement_bridge, "_attach_rule_regression_log", lambda artifacts: artifacts)

    artifacts = judgement_bridge.ensure_judgement_artifacts(
        pdf_path=pdf_path,
        permit_paths=[],
        original_csv_dir=None,
        company="회사",
        product="제품",
        _background=True,
    )
    fingerprint = llm_module.llm_execution_fingerprint()
    status = judgement_bridge._read_status_index()["documents"]
    status_entry = next(iter(status.values()))
    serialized = repr({"artifacts": artifacts, "status": status_entry})

    assert artifacts["llm_execution_fingerprint"] == fingerprint
    assert status_entry["llm_execution_fingerprint"] == fingerprint
    assert secret not in serialized
    assert endpoint not in serialized


@pytest.mark.parametrize("problem", ["missing", "sha256 mismatch"])
def test_bridge_runs_after_phase_for_invalid_target_catalog(monkeypatch, tmp_path, problem):
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(b"source")
    resolution = ResolvedPermit(AUTHORITATIVE_POLICY, (), f"invalid-{problem}", (problem,), False)
    pipeline_calls = []

    class Pipeline:
        def __init__(self, **kwargs):
            pipeline_calls.append(kwargs)

        def run(self, _pdf_path, **_kwargs):
            return SimpleNamespace(extracted_records=[], metadata={})

    monkeypatch.setattr(
        judgement_bridge,
        "resolve_permits",
        lambda *_args, **_kwargs: resolution,
    )
    monkeypatch.setattr(judgement_bridge, "DocumentJudgePipeline", Pipeline)
    monkeypatch.setattr(judgement_bridge, "_write_summary_csv_from_result", lambda *_args: None)
    monkeypatch.setattr(judgement_bridge, "_attach_rule_regression_log", lambda artifacts: artifacts)
    monkeypatch.setattr(judgement_bridge, "_remember_judgement_artifacts", lambda _artifacts: None)

    artifacts = judgement_bridge.ensure_judgement_artifacts(
        pdf_path=pdf_path, permit_paths=[], original_csv_dir=None,
        company="SK bioscience", product="SKYCovione",
    )

    assert len(pipeline_calls) == 2
    assert pipeline_calls[0]["permit_enabled"] is False
    assert pipeline_calls[1]["permit_resolution"] is resolution
    assert artifacts["after_result"] is not artifacts["before_result"]
    assert artifacts["permit_resolution_errors"] == [problem]


@pytest.mark.parametrize("problem", ["missing", "sha256 mismatch"])
def test_direct_pipeline_holds_sp_pass_for_invalid_target_catalog(monkeypatch, tmp_path, problem):
    pdf_path = tmp_path / "source.pdf"
    document = fitz.open()
    document.new_page()
    document.save(pdf_path)
    document.close()
    resolution = ResolvedPermit(AUTHORITATIVE_POLICY, (), f"invalid-{problem}", (problem,), False)
    monkeypatch.setattr(pipeline_module, "resolve_permits", lambda *_args: resolution)
    monkeypatch.setattr(pipeline_module, "UcumRagStore", _PipelineRag)
    monkeypatch.setattr(pipeline_module, "ClovaJudgeClient", _PipelineLlm)

    result = pipeline_module.DocumentJudgePipeline(
        company="SK bioscience", product="SKYCovione",
    ).run(pdf_path, extracted_records=[_record()])

    assert result.evaluations[0].final_status == HOLD_LABEL
    assert result.evaluations[0].source == "permit_pdf_llm_authoritative"
    assert result.metadata["permit_paths"] == []
    assert result.metadata["permit_resolution_errors"] == [problem]


def test_pipeline_metadata_includes_permit_resolution_and_extraction_diagnostics(monkeypatch, tmp_path):
    """Catch loss of resolution observability while retaining legacy metadata."""
    pdf_path = tmp_path / "source.pdf"
    document = fitz.open()
    document.new_page()
    document.save(pdf_path)
    document.close()
    explicit_permit = tmp_path / "linked-non-target.pdf"
    explicit_permit.write_bytes(b"not a pdf")
    monkeypatch.setattr(pipeline_module, "UcumRagStore", _PipelineRag)
    monkeypatch.setattr(pipeline_module, "ClovaJudgeClient", _PipelineLlm)

    result = pipeline_module.DocumentJudgePipeline(
        permit_pdf_paths=[explicit_permit], company="다른 회사", product="다른 제품"
    ).run(pdf_path, extracted_records=[ExtractedRecord(record_type="content", content="문서 정보", page_start=1)])

    assert result.metadata["permit_paths"] == [str(explicit_permit.resolve())]
    assert result.metadata["permit_policy_id"] is None
    assert result.metadata["permit_extraction_errors"]
    assert result.metadata["permit_extraction_diagnostics"]
    assert "llm_enabled" in result.metadata
    assert "record_count" in result.metadata


def test_mapped_permit_fail_overrides_sp_pass_and_composes_reason():
    client = FakePermitLLM(
        JudgeResponse(
            status=FAIL_LABEL,
            reason="허가서 조건 위반으로 검수불합격입니다.",
            permit_match_status="matched",
            matched_permit_test="유정란접종시험",
            permit_basis="혈구응집반응이 없어야",
            failed_requirements=["혈구응집반응이 없어야"],
        )
    )

    evaluation = _engine(client).judge_record(
        _record(result="생존율 90%, 혈구응집반응 확인")
    )

    assert evaluation.final_status == FAIL_LABEL
    assert evaluation.source == "permit_pdf_llm_authoritative"
    assert "혈구응집반응이 없어야" in evaluation.reason
    assert "혈구응집반응 확인" in evaluation.reason


def test_authoritative_call_has_full_record_and_full_complex_permit_context():
    client = FakePermitLLM(
        JudgeResponse(
            status=PASS_LABEL,
            reason="모든 허가서 조건을 만족하여 검수합격입니다.",
            permit_match_status="matched",
            matched_permit_test="유정란접종시험",
            permit_basis="36 ± 2 ℃에서 3일 배양",
        )
    )

    _engine(client).judge_record(_record())

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["authoritative_permit"] is True
    assert "유정란접종시험" in call["record_context"]
    assert "SP 시험법" in call["record_context"]
    assert "원자료 비고" in call["record_context"]
    context = "\n".join(call["rag_contexts"])
    start = context.index("요막강 내 접종")
    split = context.index("난황낭 접종")
    allantoic = context[start:split]
    yolk_sac = context[split:]
    for required in ["10~11일령", "최소 10개", "36 ± 2 ℃", "3일", "1차 생존율은 80%", "계대", "2차 생존율도 80%", "기니피그", "닭", "사람 O형", "혈구응집반응이 없어야"]:
        assert required in allantoic
    assert allantoic.count("3일") >= 2
    assert allantoic.count("80% 이상") >= 2
    for required in ["6~7일령", "최소 10개", "36 ± 2 ℃", "9일", "1차 생존율은 80%", "계대", "2차 생존율도 80%"]:
        assert required in yolk_sac
    assert yolk_sac.count("9일") >= 2
    assert yolk_sac.count("80% 이상") >= 2
    for required in ["10~11일령", "10개", "36 ± 2 ℃", "3일", "9일", "계대", "80%", "혈구응집반응이 없어야"]:
        assert required in context
    for required in ["Allantoic eggs", "최소 10개", "기니피그", "닭", "사람 O형", "Yolk-sac eggs", "6~7일령"]:
        assert required in context


def test_short_cho_master_cell_result_is_mapped_permit_fail():
    client = FakePermitLLM(
        JudgeResponse(
            status=FAIL_LABEL,
            reason="허가서 최소 기간 미달로 검수불합격입니다.",
            permit_match_status="matched",
            matched_permit_test="돼지유래바이러스부정시험",
            permit_basis="최소 21일",
            failed_requirements=["최소 21일"],
        )
    )
    record = _record(
        test_name="돼지유래바이러스부정시험",
        criteria="음성이어야 함",
        result="14일 배양 후 음성",
    )
    store = _store(text="""3.2.2 돼지유래바이러스부정시험
CHO 마스터 세포주를 최소 21일 배양해야 한다.
""")
    engine = JudgeEngine(EmptyRagStore(), client, store, AUTHORITATIVE_POLICY)

    evaluation = engine.judge_record(record)

    assert evaluation.final_status == FAIL_LABEL
    assert "최소 21일" in evaluation.reason
    assert "14일" in evaluation.reason


def test_not_found_preserves_primary_verdict():
    client = FakePermitLLM(
        JudgeResponse(status=PASS_LABEL, reason="허가서 매칭 없음", permit_match_status="not_found")
    )

    evaluation = _engine(client).judge_record(_record())

    assert evaluation.final_status == PASS_LABEL
    assert evaluation.source == "rule_before"


def test_ambiguous_yields_hold():
    client = FakePermitLLM(
        JudgeResponse(status=PASS_LABEL, reason="매칭이 불명확하여 검수보류", permit_match_status="ambiguous")
    )

    evaluation = _engine(client).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL
    assert evaluation.source == "permit_pdf_llm_authoritative"


def test_ambiguous_diagnostic_fields_do_not_leak():
    client = FakePermitLLM(
        JudgeResponse(
            status=PASS_LABEL,
            reason="api/429 secret reason",
            permit_match_status="ambiguous",
            permit_basis="api/429 secret basis",
            normalized_result="api/429 secret result",
        )
    )
    evaluation = _engine(client).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL
    assert "api" not in (evaluation.reason or "").lower()
    assert "429" not in (evaluation.reason or "")
    assert "api" not in (evaluation.normalized_criteria or "").lower()
    assert "api" not in (evaluation.normalized_result or "").lower()


@pytest.mark.parametrize(
    "client",
    [
        None,
        FakePermitLLM(None),
        FakePermitLLM(error=RuntimeError("429 secret api error")),
    ],
)
def test_required_authoritative_llm_unavailable_or_invalid_is_safe_hold(client):
    evaluation = _engine(client).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL
    assert "429" not in (evaluation.reason or "")
    assert "api" not in (evaluation.reason or "").lower()


def test_non_authoritative_policy_retains_hold_only_permit_behavior():
    client = FakePermitLLM(
        JudgeResponse(
            status=FAIL_LABEL,
            reason="비권위 허가서 결과",
            permit_match_status="matched",
        )
    )
    evaluation = _engine(client, NON_AUTHORITATIVE_POLICY).judge_record(_record())

    assert evaluation.final_status == PASS_LABEL
    assert client.calls == []


def test_clova_authoritative_prompt_requires_number_independent_every_condition_permit_match(monkeypatch):
    client = ClovaJudgeClient(api_key="fake")
    client.enabled = True
    client.client = object()
    captured = {}

    def fake_request_structured_response(**kwargs):
        captured.update(kwargs)
        return JudgeResponse(status=PASS_LABEL, reason="검수합격", permit_match_status="matched")

    monkeypatch.setattr("sp_pdf_judger.llm.request_structured_response", fake_request_structured_response)
    client.explain(
        test_name="유정란접종시험",
        criteria="기준",
        result="결과",
        rag_contexts=[FULL_EGG_CHUNK],
        authoritative_permit=True,
        record_context="full record context",
    )

    prompt = captured["prompt"]
    assert "matched" in prompt and "ambiguous" in prompt and "not_found" in prompt
    assert "section number" in prompt.lower() or "section-number" in prompt.lower() or "번호" in prompt
    assert "every" in prompt.lower() or "모든" in prompt
    assert "authoritative" in prompt.lower() or "우선" in prompt
    assert "full record context" in prompt


def test_result_only_primary_pass_is_overridden_by_authoritative_fail():
    client = FakePermitLLM(
        JudgeResponse(
            status=FAIL_LABEL,
            reason="검수불합격",
            permit_match_status="matched",
            matched_permit_test="유정란접종시험",
            permit_basis="혈구응집반응이 없어야",
            failed_requirements=["혈구응집반응이 없어야"],
        )
    )
    evaluation = _engine(client).judge_record(
        _record(criteria=None, result="적합, 혈구응집반응 확인")
    )

    assert evaluation.final_status == FAIL_LABEL
    assert evaluation.source == "permit_pdf_llm_authoritative"
    assert len(client.calls) == 1


def test_determined_loq_table_pass_is_overridden_once_without_losing_rows():
    client = FakePermitLLM(
        JudgeResponse(
            status=FAIL_LABEL,
            reason="검수불합격",
            permit_match_status="matched",
            matched_permit_test="잔류 숙주세포 DNA 시험",
            permit_basis="유전자 중 1종 이상이 정량한계 미만이어야 함",
            failed_requirements=["유전자 중 1종 이상이 정량한계 미만이어야 함"],
        )
    )
    record = _record(
        test_name="잔류 숙주세포 DNA 시험",
        criteria="유전자 중 1종 이상이 정량한계 미만이어야 함",
        result=(
            "유전자 | 시험결과\n"
            "A 유전자 | 정량한계 이상\n"
            "B 유전자 | 정량한계 이상\n"
            "C 유전자 | 정량한계 미만"
        ),
    )
    store = _store(text="""3.2.2 잔류 숙주세포 DNA 시험
유전자 중 1종 이상이 정량한계 미만이어야 함.
""")
    evaluation = JudgeEngine(EmptyRagStore(), client, store, AUTHORITATIVE_POLICY).judge_record(record)

    assert evaluation.final_status == FAIL_LABEL
    assert len(evaluation.lot_judgements) == 3
    assert all(row["status"] == FAIL_LABEL for row in evaluation.lot_judgements)
    assert all(row["source"] == "permit_pdf_llm_authoritative" for row in evaluation.lot_judgements)
    assert [row["item_value"] for row in evaluation.lot_judgements] == ["A 유전자", "B 유전자", "C 유전자"]
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        JudgeResponse(status=PASS_LABEL, reason="검수합격", permit_match_status="matched", permit_basis="혈구응집반응이 없어야"),
        JudgeResponse(status=PASS_LABEL, reason="검수합격", permit_match_status="matched", matched_permit_test="다른 시험", permit_basis="혈구응집반응이 없어야"),
        JudgeResponse(status=PASS_LABEL, reason="검수합격", permit_match_status="matched", matched_permit_test="유정란접종시험", permit_basis="허가서 기준"),
        JudgeResponse(status=PASS_LABEL, reason="검수합격", permit_match_status="matched", matched_permit_test="3.2.1", permit_basis="혈구응집반응이 없어야"),
    ],
)
def test_matched_requires_grounded_test_name_and_concrete_basis(response):
    client = FakePermitLLM(response)
    evaluation = _engine(client).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL
    assert evaluation.source == "permit_pdf_llm_authoritative"


@pytest.mark.parametrize("matched_test", ["시험", "확인시험", "성상", "무균시험", "일", "3.2.1"])
def test_generic_or_one_character_matched_test_is_not_grounded(matched_test):
    response = JudgeResponse(
        status=PASS_LABEL,
        reason="검수합격",
        permit_match_status="matched",
        matched_permit_test=matched_test,
        permit_basis="혈구응집반응이 없어야",
    )
    evaluation = _engine(FakePermitLLM(response)).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL


@pytest.mark.parametrize(
    ("matched_test", "basis"),
    [
        ("허가서", "혈구응집반응이 없어야"),
        ("permit", "혈구응집반응이 없어야"),
        ("pdf", "혈구응집반응이 없어야"),
        ("PDF 판정 후보 문단", "혈구응집반응이 없어야"),
        ("유정란접종시험", "이상"),
        ("유정란접종시험", "최소"),
        ("유정란접종시험", "기준"),
    ],
)
def test_wrapper_and_vague_permit_fragments_are_not_grounded(matched_test, basis):
    response = JudgeResponse(
        status=PASS_LABEL,
        reason="검수합격",
        permit_match_status="matched",
        matched_permit_test=matched_test,
        permit_basis=basis,
    )
    evaluation = _engine(FakePermitLLM(response)).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL


@pytest.mark.parametrize("fragment", ["허가서 PDF 판정 후보 문단", "pdf", "permit", "시험", "확인"])
def test_generated_wrapper_and_generic_identity_fragments_are_not_grounded(fragment):
    response = JudgeResponse(
        status=PASS_LABEL,
        reason="검수합격",
        permit_match_status="matched",
        matched_permit_test=fragment,
        permit_basis="혈구응집반응이 없어야",
    )
    evaluation = _engine(FakePermitLLM(response)).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL


@pytest.mark.parametrize("basis", ["허가서 PDF 판정 후보 문단", "이상", "최소"])
def test_generated_wrapper_and_generic_basis_fragments_are_not_grounded(basis):
    response = JudgeResponse(
        status=PASS_LABEL,
        reason="검수합격",
        permit_match_status="matched",
        matched_permit_test="유정란접종시험",
        permit_basis=basis,
    )
    evaluation = _engine(FakePermitLLM(response)).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL


@pytest.mark.parametrize("basis", ["만족해야 한다", "충족해야 한다", "conditions", "requirements"])
def test_satisfaction_only_basis_is_not_concrete_even_when_verbatim_in_permit_context(basis):
    """Catch boilerplate that a model could quote without naming a permit condition."""
    response = JudgeResponse(
        status=PASS_LABEL,
        reason="검수합격",
        permit_match_status="matched",
        matched_permit_test="유정란접종시험",
        permit_basis=basis,
    )
    store = _store(text=f"3.2.1 유정란접종시험\n{basis}\n")

    evaluation = JudgeEngine(
        EmptyRagStore(), FakePermitLLM(response), store, AUTHORITATIVE_POLICY
    ).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL


@pytest.mark.parametrize("basis", ["이에 적합하여야 한다", "이에 적합해야 한다", "위 기준에 적합하여야 한다"])
def test_deictic_compliance_sentence_cannot_authorize_permit_override(basis):
    store = _store(text=f"3.2.1 유정란접종시험\n{basis}\n")
    response = JudgeResponse(
        status=PASS_LABEL, reason="검수합격", permit_match_status="matched",
        matched_permit_test="유정란접종시험", permit_basis=basis,
    )

    evaluation = JudgeEngine(EmptyRagStore(), FakePermitLLM(response), store, AUTHORITATIVE_POLICY).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL
    assert evaluation.source == "permit_pdf_llm_authoritative"


def test_failed_requirement_must_itself_name_a_concrete_permit_condition():
    store = _store(text="3.2.1 유정란접종시험\n혈구응집반응이 없어야 한다. 이에 적합하여야 한다.")
    response = JudgeResponse(
        status=FAIL_LABEL, reason="검수불합격", permit_match_status="matched",
        matched_permit_test="유정란접종시험", permit_basis="혈구응집반응이 없어야 한다",
        failed_requirements=["이에 적합하여야 한다"],
    )

    evaluation = JudgeEngine(EmptyRagStore(), FakePermitLLM(response), store, AUTHORITATIVE_POLICY).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL


def test_explicitly_named_standard_is_concrete_permit_basis():
    basis = "대한민국약전 무균시험법에 적합하여야 한다"
    store = _store(text=f"3.2.1 유정란접종시험\n{basis}")
    response = JudgeResponse(
        status=FAIL_LABEL, reason="검수불합격", permit_match_status="matched",
        matched_permit_test="유정란접종시험", permit_basis=basis,
        failed_requirements=[basis],
    )

    evaluation = JudgeEngine(EmptyRagStore(), FakePermitLLM(response), store, AUTHORITATIVE_POLICY).judge_record(_record(result="무균시험 양성"))

    assert evaluation.final_status == FAIL_LABEL
    assert basis in evaluation.reason
    assert "무균시험 양성" in evaluation.reason


@pytest.mark.parametrize(
    "basis",
    ["이상이어야", "이상이어야 한다", "최소이어야", "minimum", "days", "at least", "must be", "없어야 한다", "21"],
)
def test_vague_criterion_grammar_or_bare_number_is_not_concrete(basis):
    text = f"3.2.1 유정란접종시험\n{basis}\n"
    response = JudgeResponse(
        status=PASS_LABEL,
        reason="검수합격",
        permit_match_status="matched",
        matched_permit_test="유정란접종시험",
        permit_basis=basis,
    )
    store = _store(text=text)
    evaluation = JudgeEngine(EmptyRagStore(), FakePermitLLM(response), store, AUTHORITATIVE_POLICY).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL


@pytest.mark.parametrize(
    ("basis", "text"),
    [
        ("80% 이상", "3.2.1 유정란접종시험\n80% 이상이어야 한다"),
        ("최소 21일", "3.2.1 유정란접종시험\n최소 21일 배양해야 한다"),
        ("36 ± 2 ℃", "3.2.1 유정란접종시험\n36 ± 2 ℃에서 배양해야 한다"),
        ("혈구응집반응이 없어야 한다", "3.2.1 유정란접종시험\n혈구응집반응이 없어야 한다"),
        ("발색된 점이 확인되어야 한다", "2.3.1.7 Component B 확인시험\n반응 후 발색된 점이 확인되어야 한다"),
        ("음성이어야 한다", "3.2.1 유정란접종시험\n음성이어야 한다"),
    ],
)
def test_concrete_numeric_and_textual_basis_remains_grounded(basis, text):
    response = JudgeResponse(
        status=PASS_LABEL,
        reason="검수합격",
        permit_match_status="matched",
        matched_permit_test="유정란접종시험",
        permit_basis=basis,
    )
    store = _store(text=text)
    evaluation = JudgeEngine(EmptyRagStore(), FakePermitLLM(response), store, AUTHORITATIVE_POLICY).judge_record(_record())

    assert evaluation.final_status == PASS_LABEL


def test_authoritative_evidence_keeps_source_lines_that_look_like_metadata():
    body = """3.2.1 유정란접종시험
- 내용: 혈구응집반응이 없어야 한다
- 파일: source-stated condition
- 페이지: source-stated condition
"""
    response = JudgeResponse(
        status=PASS_LABEL,
        reason="검수합격",
        permit_match_status="matched",
        matched_permit_test="유정란접종시험",
        permit_basis="혈구응집반응이 없어야 한다",
    )
    store = _store(text=body)
    client = FakePermitLLM(response)
    evaluation = JudgeEngine(EmptyRagStore(), client, store, AUTHORITATIVE_POLICY).judge_record(_record())

    assert evaluation.final_status == PASS_LABEL
    assert "- 내용: 혈구응집반응이 없어야 한다" in client.calls[0]["rag_contexts"][0]
    assert "- 파일: source-stated condition" in client.calls[0]["rag_contexts"][0]
    assert "- 페이지: source-stated condition" in client.calls[0]["rag_contexts"][0]


def test_exact_generated_wrapper_cannot_ground_either_identity_or_basis():
    wrapper = "허가서 PDF 판정 후보 문단"
    response = JudgeResponse(
        status=PASS_LABEL,
        reason="검수합격",
        permit_match_status="matched",
        matched_permit_test=wrapper,
        permit_basis=wrapper,
    )
    evaluation = _engine(FakePermitLLM(response)).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL


def test_stage_qualified_generic_test_can_be_grounded_from_section_path():
    store = _store(
        text="""3.2 바이러스 시험
3.2.1 확인시험
80% 이상이어야 함.
"""
    )
    response = JudgeResponse(
        status=PASS_LABEL,
        reason="검수합격",
        permit_match_status="matched",
        matched_permit_test="바이러스 > 확인시험",
        permit_basis="80% 이상",
    )
    record = _record(test_name="확인시험", result="80% 이상", criteria="80% 이상")
    evaluation = JudgeEngine(EmptyRagStore(), FakePermitLLM(response), store, AUTHORITATIVE_POLICY).judge_record(record)

    assert evaluation.final_status == PASS_LABEL


def test_invalid_status_is_hold_before_not_found_fallback():
    response = JudgeResponse(status="provider-error", reason="api/429 secret", permit_match_status="not_found")
    evaluation = _engine(FakePermitLLM(response)).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL
    assert "api" not in (evaluation.reason or "").lower()


def test_provider_diagnostic_in_structured_permit_fields_is_rejected_without_leak():
    response = JudgeResponse(
        status=FAIL_LABEL,
        reason="검수불합격",
        permit_match_status="matched",
        matched_permit_test="유정란접종시험",
        permit_basis="api/429 secret",
        failed_requirements=["api/429 secret"],
    )
    evaluation = _engine(FakePermitLLM(response)).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL
    assert "api" not in (evaluation.reason or "").lower()
    assert "429" not in (evaluation.reason or "")


@pytest.mark.parametrize("status", [PASS_LABEL, FAIL_LABEL])
def test_authoritative_pass_fail_reason_and_normalized_result_ignore_model_diagnostics(status):
    response = JudgeResponse(
        status=status,
        reason="api/429 secret provider detail",
        permit_match_status="matched",
        matched_permit_test="유정란접종시험",
        permit_basis="혈구응집반응이 없어야",
        failed_requirements=["혈구응집반응이 없어야"],
        normalized_result="api/429 secret normalized result",
    )
    evaluation = _engine(FakePermitLLM(response)).judge_record(
        _record(result="생존율 90%, 혈구응집반응 확인")
    )

    assert evaluation.final_status == status
    assert "api" not in (evaluation.reason or "").lower()
    assert "429" not in (evaluation.reason or "")
    assert "api" not in (evaluation.normalized_result or "").lower()
    assert "429" not in (evaluation.normalized_result or "")


def test_fail_reason_uses_actual_sp_result_not_failed_requirement():
    client = FakePermitLLM(
        JudgeResponse(
            status=FAIL_LABEL,
            reason="검수불합격",
            permit_match_status="matched",
            matched_permit_test="유정란접종시험",
            permit_basis="혈구응집반응이 없어야",
            failed_requirements=["혈구응집반응이 없어야"],
            normalized_result="혈구응집반응 확인",
        )
    )
    evaluation = _engine(client).judge_record(_record(result="생존율 90%, 혈구응집반응 확인"))

    assert evaluation.final_status == FAIL_LABEL
    assert "혈구응집반응 확인" in evaluation.reason


@pytest.mark.parametrize("enabled", [False, 0, None, ""])
def test_false_like_disabled_client_is_safe_hold(enabled):
    client = FakePermitLLM(
        JudgeResponse(status=FAIL_LABEL, reason="api/429 secret", permit_match_status="matched"),
    )
    client.enabled = enabled
    evaluation = _engine(client).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL
    assert "api" not in (evaluation.reason or "").lower()


def test_authoritative_context_keeps_long_chunk_and_all_retrieved_chunks():
    tail = " SENTINEL_AT_END "
    long_text = "3.2.1 유정란접종시험\n" + ("허가서 조건 보충 " * 500) + tail
    second = "3.2.2 유정란접종시험 추가조건\n두 번째 조건도 확인해야 함"
    client = FakePermitLLM(
        JudgeResponse(
            status=PASS_LABEL,
            reason="검수합격",
            permit_match_status="matched",
            matched_permit_test="유정란접종시험",
            permit_basis="허가서 조건 보충",
        )
    )
    store = _store(text=long_text + "\n" + second)
    evaluation = JudgeEngine(EmptyRagStore(), client, store, AUTHORITATIVE_POLICY).judge_record(_record())

    assert evaluation.final_status == PASS_LABEL
    context = "\n".join(client.calls[0]["rag_contexts"])
    assert "SENTINEL_AT_END" in context
    assert "두 번째 조건도 확인해야 함" in context


def test_valid_store_without_search_candidate_preserves_primary_without_llm_call():
    client = FakePermitLLM(None)
    store = _store(text="3.2.1 전혀 다른 허가시험\n다른 기준")
    evaluation = JudgeEngine(EmptyRagStore(), client, store, AUTHORITATIVE_POLICY).judge_record(_record())

    assert evaluation.final_status == PASS_LABEL
    assert client.calls == []


@pytest.mark.parametrize("test_name", ["성상", "무균시험"])
def test_missing_stage_for_duplicate_generic_permit_heading_holds(test_name):
    text = "\n".join(
        f"2.{index}. 제조단계 {index}\n2.{index}.1. {test_name}\n판정기준 {index}"
        for index in range(1, 8)
    )
    store = _store(text=text)
    client = FakePermitLLM(None)
    record = ExtractedRecord(test_name=test_name, criteria="적합", result="적합")

    evaluation = JudgeEngine(EmptyRagStore(), client, store, AUTHORITATIVE_POLICY).judge_record(record)

    assert evaluation.final_status == HOLD_LABEL
    assert evaluation.source == "permit_pdf_llm_authoritative"
    assert client.calls == []


def test_authoritative_store_with_extraction_error_is_safe_hold():
    client = FakePermitLLM(None)
    store = _store()
    store.chunks = []
    store.extraction_errors.append("Permit OCR page 7 is unreadable")
    evaluation = JudgeEngine(EmptyRagStore(), client, store, AUTHORITATIVE_POLICY).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL
    assert client.calls == []


def test_authoritative_store_with_partial_extraction_error_is_safe_hold():
    client = FakePermitLLM(None)
    store = _store()
    store.extraction_errors.append("Permit OCR page 2 is unreadable")
    evaluation = JudgeEngine(EmptyRagStore(), client, store, AUTHORITATIVE_POLICY).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL
    assert client.calls == []


def test_fatal_page_unreadable_error_is_hold_even_with_enabled_chunks():
    client = FakePermitLLM(
        JudgeResponse(
            status=PASS_LABEL,
            reason="검수합격",
            permit_match_status="matched",
            matched_permit_test="유정란접종시험",
            permit_basis="혈구응집반응이 없어야",
        )
    )
    store = _store()
    store.extraction_errors.append("Permit OCR page 7 is unreadable")
    evaluation = JudgeEngine(EmptyRagStore(), client, store, AUTHORITATIVE_POLICY).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL
    assert client.calls == []


@pytest.mark.parametrize(
    "message",
    [
        "Permit OCR page 7 is unreadable",
        "Could not read permit PDF: corrupted document",
        "Could not extract permit document.pdf",
        "Could not render permit OCR pages: renderer failed",
    ],
)
def test_exact_fatal_content_error_is_hold_with_enabled_chunks(message):
    client = FakePermitLLM(None)
    store = _store()
    store.extraction_errors.append(message)
    evaluation = JudgeEngine(EmptyRagStore(), client, store, AUTHORITATIVE_POLICY).judge_record(_record())

    assert evaluation.final_status == HOLD_LABEL
    assert client.calls == []


def test_non_content_cache_warning_allows_mapped_evaluation():
    client = FakePermitLLM(
        JudgeResponse(
            status=PASS_LABEL,
            reason="검수합격",
            permit_match_status="matched",
            matched_permit_test="유정란접종시험",
            permit_basis="혈구응집반응이 없어야",
        )
    )
    store = _store()
    store.extraction_errors.append("Could not write permit OCR cache")
    evaluation = JudgeEngine(EmptyRagStore(), client, store, AUTHORITATIVE_POLICY).judge_record(_record())

    assert evaluation.final_status == PASS_LABEL


@pytest.mark.parametrize(
    "message",
    [
        "Could not write permit OCR cache",
        "Windows OCR failed for page 7; recovered usable text",
        "Permit extraction warning: recovered usable text",
    ],
)
def test_operational_warning_allows_mapped_evaluation_with_usable_chunks(message):
    client = FakePermitLLM(
        JudgeResponse(
            status=PASS_LABEL,
            reason="검수합격",
            permit_match_status="matched",
            matched_permit_test="유정란접종시험",
            permit_basis="혈구응집반응이 없어야",
        )
    )
    store = _store()
    store.extraction_errors.append(message)
    evaluation = JudgeEngine(EmptyRagStore(), client, store, AUTHORITATIVE_POLICY).judge_record(_record())

    assert evaluation.final_status == PASS_LABEL


def test_criteria_only_record_enters_authoritative_hold_without_external_call():
    client = FakePermitLLM(None)
    evaluation = _engine(client).judge_record(_record(result=None))

    assert evaluation.final_status == HOLD_LABEL
    assert evaluation.source == "permit_pdf_llm_authoritative"
    assert client.calls == []


def test_authoritative_prompt_has_full_context_once_and_criteria_only_sp_text(monkeypatch):
    client = FakePermitLLM(
        JudgeResponse(
            status=PASS_LABEL,
            reason="검수합격",
            permit_match_status="matched",
            matched_permit_test="유정란접종시험",
            permit_basis="혈구응집반응이 없어야",
        )
    )
    unique_tail = "UNIQUE_PERMIT_SENTINEL_9f7c"
    store = _store(text=FULL_EGG_CHUNK + "\n" + unique_tail)
    evaluation = JudgeEngine(EmptyRagStore(), client, store, AUTHORITATIVE_POLICY).judge_record(_record())

    assert evaluation.final_status == PASS_LABEL
    call = client.calls[0]
    rendered = "\n".join(
        [
            call["criteria"],
            call["result"] or "",
            call["record_context"],
            *call["rag_contexts"],
            call["deterministic_reason"],
        ]
    )
    assert rendered.count(unique_tail) == 1
    assert "[허가서 전체 검색 근거]" not in call["criteria"]
