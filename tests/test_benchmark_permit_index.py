from __future__ import annotations

import hashlib
from pathlib import Path
import socket
from types import SimpleNamespace

import fitz
import pytest

from scripts import benchmark_permit_index as benchmark
from sp_pdf_judger import clova_client
from sp_pdf_judger import extractor as extractor_module
from sp_pdf_judger import permit_pdf_store as store_module
from sp_pdf_judger import pipeline as pipeline_module
from sp_pdf_judger.permit_catalog import PermitPolicy
from sp_pdf_judger.permit_ocr import PermitPageText
from sp_pdf_judger.schemas import ExtractedRecord


def test_offline_guard_blocks_the_real_provider_request_boundary():
    with benchmark._offline_provider_guard() as guard:
        client = pipeline_module.ClovaJudgeClient()

        assert client.enabled is False
        assert client.call_count == 0
        with pytest.raises(benchmark.OfflineProviderBlocked):
            clova_client.request_structured_response(client=object())
        with socket.socket() as connection:
            with pytest.raises(benchmark.OfflineProviderBlocked):
                connection.connect(("127.0.0.1", 9))

    assert guard.blocked_attempts == [
        "sp_pdf_judger.clova_client.request_structured_response",
        "socket.socket.connect",
    ]
    with pytest.raises(benchmark.BenchmarkSafetyError, match="provider call"):
        benchmark._raise_if_provider_attempted(guard)


def test_permit_work_instrumentation_observes_cold_then_zero_work_warm(
    monkeypatch,
    tmp_path: Path,
):
    permit = tmp_path / "permit.pdf"
    permit.write_bytes(b"synthetic permit bytes")
    digest = hashlib.sha256(permit.read_bytes()).hexdigest()
    policy = PermitPolicy(
        "benchmark",
        authoritative=True,
        ignore_section_numbers=True,
        require_llm=True,
        ocr_mode="auto",
        evaluation_mode="deterministic_first",
    )
    pages = [
        PermitPageText(
            1,
            "1. 원액\n1.1. 확인시험\n생존율은 80% 이상이어야 한다.",
            "native",
        )
    ]

    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setattr(
        store_module,
        "extract_permit_page_texts",
        lambda *_args, **_kwargs: (pages, []),
    )

    with benchmark._permit_work_instrumentation() as cold:
        first = pipeline_module.PermitPdfStore(
            [permit],
            policy=policy,
            pdf_sha256_by_path={permit.resolve(): digest},
        )

    cold_report = cold.as_dict()
    assert first.index_diagnostics[0]["cache_hit"] is False
    assert cold_report["extractor"] == {
        "calls": 1,
        "page_count": 1,
        "page_sources": {"native": 1},
        "error_count": 0,
    }
    assert cold_report["parser"]["chunk_parser_calls"] == 1
    assert cold_report["parser"]["chunks_parsed"] > 0
    assert cold_report["parser"]["condition_parser_calls"] > 0
    assert cold_report["parser"]["conditions_parsed"] > 0

    with benchmark._permit_work_instrumentation() as warm:
        second = pipeline_module.PermitPdfStore(
            [permit],
            policy=policy,
            pdf_sha256_by_path={permit.resolve(): digest},
        )

    assert second.index_diagnostics[0]["cache_hit"] is True
    assert warm.as_dict() == {
        "extractor": {
            "calls": 0,
            "page_count": 0,
            "page_sources": {},
            "error_count": 0,
        },
        "parser": {
            "chunk_parser_calls": 0,
            "chunks_parsed": 0,
            "condition_parser_calls": 0,
            "conditions_parsed": 0,
        },
    }


def test_run_benchmark_reports_cold_warm_grouping_and_zero_provider_calls(
    monkeypatch,
    tmp_path: Path,
):
    sp_pdf = tmp_path / "sp.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "SP source")
        document.save(sp_pdf)
    permit = tmp_path / "permit.pdf"
    permit.write_bytes(b"synthetic permit bytes")
    pages = [
        PermitPageText(
            1,
            "1. 원액\n1.1. 확인시험\n생존율은 80% 이상이어야 한다.",
            "native",
        )
    ]
    record = ExtractedRecord(
        record_type="test",
        order_idx=1,
        section_number="1.1",
        section_title="원액",
        test_name="확인시험",
        criteria="80% 이상",
        result="생존율 85%",
        method="명시 시험법",
        page_start=1,
        page_end=1,
        raw_text="제조번호 LOT-A 생존율 85%",
        batch_id="LOT-A",
        batch_evidence={
            "source_kind": "explicit_table_column",
            "label": "제조번호",
            "quote": "제조번호 LOT-A",
        },
    )

    monkeypatch.setattr(
        extractor_module,
        "extract_records",
        lambda *_args, **_kwargs: [record],
    )
    monkeypatch.setattr(
        store_module,
        "extract_permit_page_texts",
        lambda *_args, **_kwargs: (pages, []),
    )
    monkeypatch.setattr(
        pipeline_module,
        "UcumRagStore",
        lambda: SimpleNamespace(
            docs=[],
            loaded_sources=[],
            search=lambda *_args, **_kwargs: [],
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "evaluate_policies",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        pipeline_module,
        "summarize_manufacturing_policies",
        lambda *_args, **_kwargs: (None, None, {}),
    )
    monkeypatch.setattr(
        pipeline_module,
        "manufacturing_policy_cards",
        lambda *_args, **_kwargs: [],
    )

    report = benchmark.run_benchmark(
        sp_pdf=sp_pdf,
        permit_pdf=permit,
        # The grouping benchmark must use an explicit grouped catalog policy.
        # An ordinary upload is not allowed to opt into grouping implicitly.
        company="SK bioscience",
        product="스카이코비원멀티주",
    )

    assert report["offline"] == {
        "clova_enabled": False,
        "network_enabled": False,
        "api_requests_sent": 0,
    }
    assert report["sp_extraction"]["record_count"] == 1
    assert report["sp_extraction"]["test_record_count"] == 1
    assert report["cold"]["cache_hit"] is False
    assert report["warm"]["cache_hit"] is True
    assert report["cold"]["extractor"]["calls"] == 1
    assert report["cold"]["parser"]["chunk_parser_calls"] == 1
    assert report["warm"]["extractor"]["calls"] == 0
    assert report["warm"]["parser"]["chunk_parser_calls"] == 0
    for phase in (report["cold"], report["warm"]):
        grouped = phase["grouped_judgement"]
        assert grouped["evaluation_mode"] == "deterministic_first"
        assert grouped["raw_test_count"] == 1
        assert grouped["group_count"] == 1
        assert sum(grouped["path_counts"].values()) == 1
        assert grouped["provider_calls"] == 0
        assert grouped["estimated_saved_calls"] == 1


def test_group_benchmark_rejects_ordinary_upload_instead_of_overriding_policy(tmp_path):
    sp_pdf = tmp_path / "sp.pdf"
    permit = tmp_path / "permit.pdf"
    sp_pdf.write_bytes(b"synthetic SP bytes")
    permit.write_bytes(b"synthetic permit bytes")

    with pytest.raises(benchmark.BenchmarkInputError, match="did not select grouped evaluation"):
        benchmark.run_benchmark(
            sp_pdf=sp_pdf,
            permit_pdf=permit,
            company="Benchmark Company",
            product="Benchmark Product",
        )
