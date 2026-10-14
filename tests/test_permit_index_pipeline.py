from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

import sp_pdf_judger.permit_index as index_module
import sp_pdf_judger.permit_pdf_store as store_module
import sp_pdf_judger.pipeline as pipeline_module
import sp_pdf_judger.policy_engine as policy_module
from sp_pdf_judger.permit_catalog import PermitPolicy, ResolvedPermit, permit_hash_map
from sp_pdf_judger.permit_ocr import PermitPageText
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.permit_source_ownership import PermitSourceDocument
from sp_pdf_judger.schemas import Evaluation, ExtractedRecord


POLICY = PermitPolicy("sky", True, True, True, "auto", "deterministic_first")
PAGES = (
    PermitPageText(1, "1. 원액\n1.1. 확인시험\n최소 10개이어야 한다.", "native"),
    PermitPageText(2, "생존율은 80% 이상이어야 한다.", "windows_ocr"),
)


def _resolution(paths, policy=POLICY):
    paths = tuple(path.resolve() for path in paths)
    return ResolvedPermit(
        policy, paths, "resolved-fingerprint", (), True,
        tuple((path, hashlib.sha256(path.read_bytes()).hexdigest()) for path in paths),
    )


@pytest.fixture
def pipeline_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(tmp_path / "index"))
    # RAG and provider construction are external dependencies; permit extraction,
    # parsing, persistence, policy context, and pipeline orchestration stay real.
    monkeypatch.setattr(pipeline_module, "UcumRagStore", lambda: SimpleNamespace(
        docs=[], loaded_sources=[], search=lambda *_args, **_kwargs: []))
    monkeypatch.setattr(pipeline_module, "ClovaJudgeClient", lambda **_kwargs: SimpleNamespace(
        enabled=False, model="test", call_count=0, success_count=0, last_error=None,
        set_domain_detail_context=lambda _context: None))
    permit = tmp_path / "permit.pdf"
    permit.write_bytes(b"synthetic permit bytes")
    sp_pdf = tmp_path / "sp.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "SP source text")
        document.save(sp_pdf)
    resolution = _resolution([permit])
    extraction_calls = []

    def extract(path, **kwargs):
        extraction_calls.append((path, kwargs))
        return list(PAGES), []

    def make_store(paths, policy=None, **kwargs):
        return PermitPdfStore(paths, policy=policy, page_text_extractor=extract, **kwargs)

    monkeypatch.setattr(pipeline_module, "PermitPdfStore", make_store)
    return SimpleNamespace(permit=permit, sp_pdf=sp_pdf, resolution=resolution,
                           extraction_calls=extraction_calls)


def _pipeline(env, **kwargs):
    return pipeline_module.DocumentJudgePipeline(
        company="SK bioscience", product="SKYCovione",
        permit_resolution=env.resolution, **kwargs)


def _run(pipeline, env):
    static = SimpleNamespace(pdf_path=env.sp_pdf, metadata={}, preview_image_path=None,
                             manufacturing_summary_page_numbers=[],
                             manufacturing_summary_image_paths=[])
    fixture_record = ExtractedRecord(
        order_idx=1,
        record_type="content",
        section_title="인덱스 파이프라인 픽스처",
        content="인덱스 메타데이터 검증용 레코드",
    )
    return pipeline.run(
        env.sp_pdf,
        extracted_records=[fixture_record],
        static_result=static,
    )


def test_fresh_pipeline_warm_load_skips_all_extraction_and_parsing(pipeline_env, monkeypatch):
    calls = {"chunks": 0, "conditions": 0}
    original_chunks = PermitPdfStore._parse_page_texts
    original_conditions = store_module.parse_permit_conditions

    def chunks(self, *args, **kwargs):
        calls["chunks"] += 1
        return original_chunks(self, *args, **kwargs)

    def conditions(chunk):
        calls["conditions"] += 1
        return original_conditions(chunk)

    monkeypatch.setattr(PermitPdfStore, "_parse_page_texts", chunks)
    monkeypatch.setattr(store_module, "parse_permit_conditions", conditions)
    first = _pipeline(pipeline_env)
    assert calls["chunks"] == 1
    assert calls["conditions"] > 0
    cold_calls = dict(calls)
    second = _pipeline(pipeline_env)

    assert calls == cold_calls
    assert len(pipeline_env.extraction_calls) == 1
    assert pipeline_env.extraction_calls[0][1] == {
        "ocr_mode": "auto", "pdf_sha256": pipeline_env.resolution.content_hashes[0][1],
        "use_page_cache": False,
    }
    assert first.permit_store.chunks == second.permit_store.chunks
    assert first.permit_store.conditions == second.permit_store.conditions
    assert first.permit_store.source_documents == second.permit_store.source_documents
    assert first.permit_store.raw_search_index == second.permit_store.raw_search_index
    assert first.permit_store.index_diagnostics[0]["cache_hit"] is False
    assert second.permit_store.index_diagnostics[0]["cache_hit"] is True
    record = ExtractedRecord(test_name="확인시험", section_title="원액")
    assert first.permit_store.search(record) == second.permit_store.search(record)


@pytest.mark.parametrize("enabled", [True, False])
def test_pipeline_uses_only_resolver_authorized_hashes(pipeline_env, monkeypatch, enabled):
    supplied = []
    real_factory = pipeline_module.PermitPdfStore

    def capture(paths, **kwargs):
        supplied.append((paths, kwargs))
        return real_factory(paths, **kwargs)

    monkeypatch.setattr(pipeline_module, "PermitPdfStore", capture)
    pipeline = _pipeline(pipeline_env, permit_enabled=enabled)
    expected = permit_hash_map(pipeline_env.resolution) if enabled else {}
    assert supplied[0][1].get("pdf_sha256_by_path") == expected
    assert supplied[0][0] == ([pipeline_env.permit.resolve()] if enabled else [])
    assert pipeline.permit_store.pdf_sha256_by_path == expected
    if not enabled:
        assert pipeline_env.extraction_calls == []


def test_pipeline_rejects_permit_changed_after_resolution_before_run(pipeline_env):
    pipeline = _pipeline(pipeline_env)
    pipeline_env.permit.write_bytes(b"permit changed after resolver validation")

    with pytest.raises(RuntimeError, match="허가서.*변경"):
        _run(pipeline, pipeline_env)


def test_policy_context_reuses_exact_store_and_ordered_indexed_pages(pipeline_env, monkeypatch):
    sentinel = SimpleNamespace(source_documents=[
        PermitSourceDocument(str(pipeline_env.permit), ((2, "OCR second"), (1, "native first"))),
        PermitSourceDocument("other.pdf", ((1, "other first"),)),
    ])
    opened = []
    original_open = fitz.open

    def open_sp_only(path, *args, **kwargs):
        assert Path(path) == pipeline_env.sp_pdf, "PolicyContext reopened a permit PDF"
        opened.append(Path(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(policy_module.fitz, "open", open_sp_only)
    monkeypatch.setattr(policy_module, "PermitPdfStore", lambda *a, **k: pytest.fail("second store"))
    context = policy_module.PolicyContext(
        pipeline_env.sp_pdf, [], [pipeline_env.permit],
        permit_search_aliases=[["확인시험", "동일성시험"]], permit_store=sentinel)
    assert context.permit_store is sentinel
    assert context.permit_pages == [
        ("permit.pdf", 2, "OCR second"), ("permit.pdf", 1, "native first"),
        ("other.pdf", 1, "other first"),
    ]
    assert opened == [pipeline_env.sp_pdf]
    assert context.pages == [(1, "SP source text\n")]


def test_direct_policy_context_keeps_legacy_open_store_and_alias_behavior(pipeline_env, monkeypatch):
    permit = pipeline_env.permit.parent / "legacy.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "1. Identity test\nAt least 10 samples.")
        document.save(permit)
    original_open = fitz.open
    opened = []

    def tracked_open(path, *args, **kwargs):
        opened.append(Path(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(policy_module.fitz, "open", tracked_open)
    context = policy_module.PolicyContext(pipeline_env.sp_pdf, [], [permit],
        permit_search_aliases=[["Identity test", "Same test"]])
    assert opened[:2] == [pipeline_env.sp_pdf, permit]
    assert context.permit_pages == [(str(permit), 1, "1. Identity test\nAt least 10 samples.\n")]
    assert isinstance(context.permit_store, PermitPdfStore)
    assert context.permit_store.policy == PermitPolicy("linked", True, True, True, "auto")
    assert context.permit_store.search_aliases
    assert context.permit_store.index_diagnostics == []


def test_pipeline_run_passes_same_store_without_permit_reopen_or_alias_reconfiguration(
    pipeline_env, monkeypatch,
):
    pipeline = _pipeline(pipeline_env)
    original_context = policy_module.PolicyContext
    original_open = fitz.open
    contexts, aliases = [], []
    original_configure = pipeline.permit_store.configure_search_aliases

    def capture_context(*args, **kwargs):
        context = original_context(*args, **kwargs)
        contexts.append(context)
        return context

    def open_sp_only(path, *args, **kwargs):
        assert Path(path) == pipeline_env.sp_pdf, "Pipeline reopened a permit PDF"
        return original_open(path, *args, **kwargs)

    def configure(groups):
        aliases.append(groups)
        original_configure(groups)

    monkeypatch.setattr(pipeline_module, "PolicyContext", capture_context)
    monkeypatch.setattr(policy_module.fitz, "open", open_sp_only)
    monkeypatch.setattr(policy_module, "PermitPdfStore", lambda *a, **k: pytest.fail("second store"))
    monkeypatch.setattr(pipeline.permit_store, "configure_search_aliases", configure)
    result = _run(pipeline, pipeline_env)
    assert contexts[0].permit_store is pipeline.permit_store
    assert contexts[0].permit_pages == [("permit.pdf", page.page_number, page.text) for page in PAGES]
    assert aliases == [result.metadata["permit_search_aliases"]]


def test_direct_pipeline_keeps_legacy_judge_then_linked_policy_search(
    pipeline_env, monkeypatch,
):
    pipeline_env.resolution = _resolution([pipeline_env.permit], policy=None)
    pipeline = _pipeline(pipeline_env)
    record = ExtractedRecord(
        order_idx=1,
        record_type="test",
        section_number="1.1",
        section_title="원액",
        test_name="확인시험",
        criteria="생존율 80%",
        result="적합",
        page_start=1,
        page_end=1,
    )
    permit_rule = policy_module.Rule(
        id="DIRECT-COMPAT",
        title="직접 연결 허가 비교",
        instruction="허가서와 SP의 생존율을 비교한다.",
        operation="permit_test",
        selector={"type": "test", "test": "확인시험"},
        params={
            "permit_stage": ["원액"],
            "permit_pattern": r"생존율[^0-9]*(80)%",
            "sp_pattern": r"생존율[^0-9]*(80)%",
            "sp_attribute": "criteria",
        },
    )
    events = []

    def judge(row):
        candidates = pipeline.permit_store.search(row, top_k=12)
        legacy_context = SimpleNamespace(
            permit_store=pipeline.permit_store,
            select=lambda _selector: [row],
            evidence=lambda item: {"source": "sp", "page": item.page_start, "quote": item.criteria},
        )
        finding = policy_module.permit_test(permit_rule, legacy_context)
        events.append((
            "judge",
            pipeline.permit_store.policy,
            [chunk.text for chunk in candidates],
            finding.status,
        ))
        return Evaluation(
            order_idx=row.order_idx,
            record_type=row.record_type,
            section_number=row.section_number,
            section_title=row.section_title,
            test_name=row.test_name or "",
            criteria=row.criteria,
            result=row.result,
            page_start=row.page_start,
            page_end=row.page_end,
            final_status="검수합격",
            reason="legacy judge result",
            source="legacy_probe",
        )

    def evaluate(context, _book):
        candidates = context.permit_store.search(record, top_k=12)
        finding = policy_module.permit_test(permit_rule, context)
        events.append((
            "policy",
            context.permit_store.policy,
            [chunk.text for chunk in candidates],
            finding.status,
        ))
        return []

    pipeline.judge_engine.judge_record = judge
    monkeypatch.setattr(pipeline_module, "evaluate_policies", evaluate)
    monkeypatch.setattr(
        PermitPdfStore,
        "__init__",
        lambda *_args, **_kwargs: pytest.fail("pipeline run constructed a second permit store"),
    )
    static = SimpleNamespace(
        pdf_path=pipeline_env.sp_pdf,
        metadata={},
        preview_image_path=None,
        manufacturing_summary_page_numbers=[],
        manufacturing_summary_image_paths=[],
    )

    result = pipeline.run(
        pipeline_env.sp_pdf,
        extracted_records=[record],
        static_result=static,
    )

    assert [event[0] for event in events] == ["judge", "policy"]
    assert events[0][1] is None
    assert "생존율은 80% 이상" not in events[0][2][0]
    assert events[0][3] == "HOLD"
    assert events[1][1] == PermitPolicy("linked", True, True, True, "auto")
    assert "생존율은 80% 이상" in events[1][2][0]
    assert events[1][3] == "PASS"
    assert result.evaluations[0].final_status == "검수합격"
    assert result.evaluations[0].source == "legacy_probe"
    assert pipeline.permit_store.policy is None
    assert "생존율은 80% 이상" not in pipeline.permit_store.chunks[0].text


def test_direct_pipeline_restores_legacy_store_when_policy_evaluation_raises(
    pipeline_env, monkeypatch,
):
    pipeline_env.resolution = _resolution([pipeline_env.permit], policy=None)
    pipeline = _pipeline(pipeline_env)
    original_chunks = pipeline.permit_store.chunks
    monkeypatch.setattr(
        pipeline_module,
        "evaluate_policies",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("policy failed")),
    )
    static = SimpleNamespace(
        pdf_path=pipeline_env.sp_pdf,
        metadata={},
        preview_image_path=None,
        manufacturing_summary_page_numbers=[],
        manufacturing_summary_image_paths=[],
    )

    with pytest.raises(RuntimeError, match="policy failed"):
        pipeline.run(
            pipeline_env.sp_pdf,
            extracted_records=[ExtractedRecord(
                order_idx=1,
                record_type="content",
                section_title="정책 예외 픽스처",
                content="정책 컨텍스트 복구 검증",
            )],
            static_result=static,
        )

    assert pipeline.permit_store.policy is None
    assert pipeline.permit_store.chunks is original_chunks


def test_target_metadata_has_sanitized_cold_and_warm_index_audit(pipeline_env):
    first = _pipeline(pipeline_env)
    second = _pipeline(pipeline_env)
    for pipeline, hit in [(first, False), (second, True)]:
        metadata = _run(pipeline, pipeline_env).metadata
        raw = pipeline.permit_store.index_diagnostics[0]
        expected = {**raw, "ocr_mode": "auto", "chunk_count": len(pipeline.permit_store.chunks)}
        assert metadata["permit_index_diagnostics"] == [expected]
        assert metadata["permit_index_cache_hit"] is hit
        assert metadata["permit_index_key"] == raw["key"]
        assert metadata["permit_index_schema"] == index_module.PERMIT_INDEX_SCHEMA_VERSION
        assert metadata["permit_index_elapsed_ms"] == raw["elapsed_ms"]
        assert metadata["permit_ocr_page_count"] == (0 if hit else 1)
        new_metadata = {key: value for key, value in metadata.items()
                        if key.startswith("permit_index_") or key == "permit_ocr_page_count"}
        encoded = json.dumps(new_metadata, ensure_ascii=False)
        assert str(pipeline_env.permit.parent) not in encoded
        assert "source_file" in encoded and "permit.pdf" in encoded
        assert metadata["permit_paths"] == [pipeline_env.permit.name]
        assert metadata["resolved_permit_paths"] == [pipeline_env.permit.name]
        assert metadata["permit_extraction_diagnostics"][0]["path"] == pipeline_env.permit.name
        assert metadata["permit_group_batch_count"] == 0
        assert metadata["permit_group_batch_sizes"] == []


def test_new_diagnostics_whitelist_fields_and_reduce_unsafe_failure(pipeline_env):
    pipeline = _pipeline(pipeline_env)
    pipeline.permit_store.index_diagnostics = [{
        "source_file": str(pipeline_env.permit), "cache_hit": False, "key": "a" * 64,
        "schema_version": 2, "elapsed_ms": 7, "ocr_page_count": 1,
        "failure_code": "api/429 secret account=user@example.test",
        "provider_response": "private response", "path": str(pipeline_env.permit),
    }]
    metadata = _run(pipeline, pipeline_env).metadata
    audit = metadata["permit_index_diagnostics"]
    assert audit == [{"source_file": "permit.pdf", "cache_hit": False, "key": "a" * 64,
                      "schema_version": 2, "elapsed_ms": 7, "ocr_page_count": 1,
                      "failure_code": "index_failure", "ocr_mode": "auto",
                      "chunk_count": len(pipeline.permit_store.chunks)}]
    assert "api/429" not in json.dumps(audit) and "private response" not in json.dumps(audit)


@pytest.mark.parametrize(
    "hits, schemas, expected_hit, expected_schema",
    [([True, True], [2, 2], True, 2), ([True, False], [2, 3], False, None)],
)
def test_multiple_permits_keep_per_file_audit_and_aggregate_counts(
    pipeline_env, hits, schemas, expected_hit, expected_schema,
):
    pipeline = _pipeline(pipeline_env)
    pipeline.permit_store.index_diagnostics = [
        {"source_file": name, "cache_hit": hit, "key": key * 64,
         "schema_version": schema, "elapsed_ms": elapsed, "ocr_page_count": ocr,
         "failure_code": failure}
        for name, hit, key, schema, elapsed, ocr, failure in [
            ("permit.pdf", hits[0], "a", schemas[0], 3, 1, None),
            ("other.pdf", hits[1], "b", schemas[1], 5, 2, "build_failed"),
        ]
    ]
    metadata = _run(pipeline, pipeline_env).metadata
    assert metadata["permit_index_cache_hit"] is expected_hit
    assert metadata["permit_index_key"] is None
    assert metadata["permit_index_schema"] is expected_schema
    assert metadata["permit_index_elapsed_ms"] == 8
    assert metadata["permit_ocr_page_count"] == 3
    assert len(metadata["permit_index_diagnostics"]) == 2
    assert metadata["permit_index_diagnostics"][1]["failure_code"] == "build_failed"


def test_identical_multi_permits_keep_per_file_counts_with_duplicate_basenames(
    pipeline_env,
):
    first = pipeline_env.permit.parent / "one" / "permit.pdf"
    second = pipeline_env.permit.parent / "two" / "permit.pdf"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"identical permit bytes")
    second.write_bytes(b"identical permit bytes")
    pipeline_env.resolution = _resolution([first, second])

    pipeline = _pipeline(pipeline_env)
    metadata = _run(pipeline, pipeline_env).metadata
    raw = pipeline.permit_store.index_diagnostics
    per_file_chunk_count = len(pipeline.permit_store.chunks) // 2

    assert len(pipeline_env.extraction_calls) == 1
    assert len(raw) == 2
    assert raw[0]["key"] == raw[1]["key"]
    assert [entry["chunk_count"] for entry in raw] == [
        per_file_chunk_count,
        per_file_chunk_count,
    ]
    assert metadata["permit_index_key"] == raw[0]["key"]
    assert metadata["permit_index_schema"] == index_module.PERMIT_INDEX_SCHEMA_VERSION
    assert [entry["chunk_count"] for entry in metadata["permit_index_diagnostics"]] == [
        per_file_chunk_count,
        per_file_chunk_count,
    ]
    assert [entry["chunk_count"] for entry in metadata["permit_extraction_diagnostics"]] == [
        per_file_chunk_count,
        per_file_chunk_count,
    ]


def test_disabled_permit_metadata_has_empty_audit_not_vacuous_cache_hit(pipeline_env):
    metadata = _run(_pipeline(pipeline_env, permit_enabled=False), pipeline_env).metadata
    assert metadata["permit_index_diagnostics"] == []
    assert metadata["permit_index_cache_hit"] is None
    assert metadata["permit_index_key"] is None
    assert metadata["permit_index_schema"] is None
    assert metadata["permit_index_elapsed_ms"] == 0
    assert metadata["permit_ocr_page_count"] == 0
    assert metadata["permit_paths"] == []
    assert metadata["resolved_permit_paths"] == [str(pipeline_env.permit.resolve())]


def test_renamed_permit_reuses_index_and_updates_display_but_not_evidence_ids(pipeline_env):
    first = _pipeline(pipeline_env)
    renamed = pipeline_env.permit.with_name("renamed.pdf")
    renamed.write_bytes(b"synthetic permit bytes")
    pipeline_env.resolution = _resolution([renamed])
    second = _pipeline(pipeline_env)
    assert len(pipeline_env.extraction_calls) == 1
    assert [chunk.evidence_id for chunk in first.permit_store.chunks] == [
        chunk.evidence_id for chunk in second.permit_store.chunks]
    assert [condition.condition_id for condition in first.permit_store.conditions] == [
        condition.condition_id for condition in second.permit_store.conditions]
    assert {chunk.source_file for chunk in second.permit_store.chunks} == {"renamed.pdf"}
    assert [doc.source_file for doc in second.permit_store.source_documents] == ["renamed.pdf"]
    metadata = _run(second, pipeline_env).metadata
    assert metadata["permit_index_cache_hit"] is True
    assert metadata["permit_index_diagnostics"][0]["source_file"] == "renamed.pdf"


def test_permit_evaluation_fingerprint_is_canonical_and_environment_independent(monkeypatch, tmp_path):
    fingerprint = getattr(index_module, "permit_evaluation_fingerprint", None)
    assert callable(fingerprint), "Missing permit evaluation schema fingerprint"
    versions = {"PERMIT_INDEX_SCHEMA_VERSION": 17, "OCR_IMPLEMENTATION_VERSION": "ocr-test",
                "PERMIT_PARSER_SCHEMA_VERSION": "chunks-test", "CONDITION_PARSER_SCHEMA_VERSION": "conditions-test"}
    for name, value in versions.items():
        monkeypatch.setattr(index_module, name, value)
    # Canonical JSON uses sorted public version names, without whitespace.
    encoded = (b'{"CONDITION_PARSER_SCHEMA_VERSION":"conditions-test",'
               b'"OCR_IMPLEMENTATION_VERSION":"ocr-test","PERMIT_INDEX_SCHEMA_VERSION":17,'
               b'"PERMIT_PARSER_SCHEMA_VERSION":"chunks-test"}')
    expected = hashlib.sha256(encoded).hexdigest()
    assert fingerprint() == fingerprint() == expected
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(tmp_path / "different-index"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "private-user"))
    monkeypatch.setenv("CLOVA_API_KEY", "not-a-real-secret")
    assert fingerprint() == expected


@pytest.mark.parametrize("name", ["PERMIT_INDEX_SCHEMA_VERSION", "OCR_IMPLEMENTATION_VERSION",
                                   "PERMIT_PARSER_SCHEMA_VERSION", "CONDITION_PARSER_SCHEMA_VERSION"])
def test_permit_evaluation_fingerprint_invalidates_each_schema(monkeypatch, name):
    fingerprint = getattr(index_module, "permit_evaluation_fingerprint", None)
    assert callable(fingerprint), "Missing permit evaluation schema fingerprint"
    baseline = fingerprint()
    old = getattr(index_module, name)
    monkeypatch.setattr(index_module, name, old + 1 if isinstance(old, int) else old + "-next")
    assert fingerprint() != baseline
