"""Offline cold/warm benchmark for the permit index and grouped judgement."""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field, fields
import argparse
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from time import perf_counter
from types import SimpleNamespace
from typing import Iterator, Sequence
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class OfflineProviderBlocked(RuntimeError):
    """A live-provider boundary was reached while offline benchmarking."""


class BenchmarkSafetyError(RuntimeError):
    """The benchmark could not uphold its offline safety contract."""


class BenchmarkInputError(ValueError):
    """The requested benchmark inputs cannot exercise the target path."""


@dataclass
class _ProviderGuard:
    blocked_attempts: list[str] = field(default_factory=list)

    def blocker(self, boundary: str):
        def blocked(*_args, **_kwargs):
            self.blocked_attempts.append(boundary)
            raise OfflineProviderBlocked(f"offline benchmark blocked {boundary}")

        return blocked


class _OfflineClovaJudgeClient:
    def __init__(self, *_args, guard: _ProviderGuard, **_kwargs) -> None:
        self.enabled = False
        self.model = "offline-disabled"
        self.client = None
        self.call_count = 0
        self.success_count = 0
        self.last_error = ""
        self.permit_audit: list[dict] = []
        self._guard = guard

    def set_domain_detail_context(self, _context: str) -> None:
        return None

    def explain(self, *_args, **_kwargs):
        return self._guard.blocker("ClovaJudgeClient.explain")()

    def explain_permit_group_once(self, *_args, **_kwargs):
        return self._guard.blocker("ClovaJudgeClient.explain_permit_group_once")()


@contextmanager
def _offline_provider_guard() -> Iterator[_ProviderGuard]:
    """Disable every project provider seam before the pipeline is constructed."""
    from sp_pdf_judger import clova_client
    from sp_pdf_judger import llm as llm_module
    from sp_pdf_judger import manufacturing_info_validator as manufacturing_module
    from sp_pdf_judger import pipeline as pipeline_module

    guard = _ProviderGuard()

    class GuardedOfflineClient(_OfflineClovaJudgeClient):
        def __init__(self, *_args, **_kwargs) -> None:
            super().__init__(guard=guard)

    environment = {
        "CLOVA_API_KEY": "",
        "OPENAI_API_KEY": "",
        "CLOVA_BASE_URL": "offline://disabled",
    }
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, environment, clear=False))
        stack.enter_context(
            patch.object(pipeline_module, "ClovaJudgeClient", GuardedOfflineClient)
        )
        for module, name, label in (
            (
                clova_client,
                "create_clova_client",
                "sp_pdf_judger.clova_client.create_clova_client",
            ),
            (
                clova_client,
                "request_structured_response",
                "sp_pdf_judger.clova_client.request_structured_response",
            ),
            (
                llm_module,
                "create_clova_client",
                "sp_pdf_judger.llm.create_clova_client",
            ),
            (
                llm_module,
                "request_structured_response",
                "sp_pdf_judger.llm.request_structured_response",
            ),
            (
                manufacturing_module,
                "create_clova_client",
                "sp_pdf_judger.manufacturing_info_validator.create_clova_client",
            ),
            (
                manufacturing_module,
                "request_structured_response",
                "sp_pdf_judger.manufacturing_info_validator.request_structured_response",
            ),
        ):
            stack.enter_context(patch.object(module, name, guard.blocker(label)))
        stack.enter_context(
            patch.object(manufacturing_module, "_get_mfg_llm_client", lambda: None)
        )
        stack.enter_context(
            patch.object(
                socket,
                "create_connection",
                guard.blocker("socket.create_connection"),
            )
        )
        stack.enter_context(
            patch.object(
                socket.socket,
                "connect",
                guard.blocker("socket.socket.connect"),
            )
        )
        stack.enter_context(
            patch.object(
                http.client.HTTPConnection,
                "connect",
                guard.blocker("http.client.HTTPConnection.connect"),
            )
        )
        stack.enter_context(
            patch.object(
                http.client.HTTPSConnection,
                "connect",
                guard.blocker("http.client.HTTPSConnection.connect"),
            )
        )
        yield guard


def _raise_if_provider_attempted(guard: _ProviderGuard) -> None:
    if guard.blocked_attempts:
        boundaries = ", ".join(guard.blocked_attempts)
        raise BenchmarkSafetyError(
            f"offline benchmark intercepted a provider call at: {boundaries}"
        )


@dataclass
class _PermitWorkDiagnostics:
    extractor_calls: int = 0
    extracted_pages: int = 0
    page_sources: Counter[str] = field(default_factory=Counter)
    extraction_errors: int = 0
    chunk_parser_calls: int = 0
    chunks_parsed: int = 0
    condition_parser_calls: int = 0
    conditions_parsed: int = 0

    def as_dict(self) -> dict[str, dict[str, object]]:
        return {
            "extractor": {
                "calls": self.extractor_calls,
                "page_count": self.extracted_pages,
                "page_sources": dict(sorted(self.page_sources.items())),
                "error_count": self.extraction_errors,
            },
            "parser": {
                "chunk_parser_calls": self.chunk_parser_calls,
                "chunks_parsed": self.chunks_parsed,
                "condition_parser_calls": self.condition_parser_calls,
                "conditions_parsed": self.conditions_parsed,
            },
        }


@contextmanager
def _permit_work_instrumentation() -> Iterator[_PermitWorkDiagnostics]:
    """Count real permit extraction/parser work without changing its behavior."""
    from sp_pdf_judger import permit_pdf_store as store_module
    from sp_pdf_judger import pipeline as pipeline_module

    diagnostics = _PermitWorkDiagnostics()
    real_store_class = store_module.PermitPdfStore
    real_extractor = store_module.extract_permit_page_texts
    real_chunk_parser = real_store_class._parse_page_texts
    real_condition_parser = store_module.parse_permit_conditions

    def counted_extractor(*args, **kwargs):
        diagnostics.extractor_calls += 1
        pages, errors = real_extractor(*args, **kwargs)
        diagnostics.extracted_pages += len(pages)
        diagnostics.extraction_errors += len(errors)
        diagnostics.page_sources.update(
            str(getattr(page, "source", "unknown")) for page in pages
        )
        return pages, errors

    def counted_chunk_parser(self, *args, **kwargs):
        diagnostics.chunk_parser_calls += 1
        chunks = real_chunk_parser(self, *args, **kwargs)
        diagnostics.chunks_parsed += len(chunks)
        return chunks

    def counted_condition_parser(*args, **kwargs):
        diagnostics.condition_parser_calls += 1
        conditions = real_condition_parser(*args, **kwargs)
        diagnostics.conditions_parsed += len(conditions)
        return conditions

    def store_factory(paths=None, policy=None, **kwargs):
        kwargs.setdefault("page_text_extractor", counted_extractor)
        return real_store_class(paths, policy=policy, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(real_store_class, "_parse_page_texts", counted_chunk_parser)
        )
        stack.enter_context(
            patch.object(store_module, "parse_permit_conditions", counted_condition_parser)
        )
        stack.enter_context(
            patch.object(pipeline_module, "PermitPdfStore", store_factory)
        )
        yield diagnostics


@contextmanager
def _benchmark_workspace(cache_dir: Path | None) -> Iterator[tuple[Path, bool]]:
    """Create an isolated cache so the first phase is a guaranteed cold load."""
    if cache_dir is not None:
        parent = Path(cache_dir).expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=True)
        workspace = Path(tempfile.mkdtemp(prefix="permit-benchmark-", dir=parent))
        yield workspace, True
        return

    # Keep the default temporary directory beneath the caller's writable cwd.
    # Some locked-down Windows hosts expose a system TEMP that cannot be listed.
    with tempfile.TemporaryDirectory(
        prefix="permit-benchmark-",
        dir=Path.cwd(),
    ) as raw_workspace:
        yield Path(raw_workspace), False


def _validated_pdf(path: Path, label: str) -> Path:
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        raise BenchmarkInputError(f"{label} PDF does not exist: {candidate}")
    if candidate.suffix.casefold() != ".pdf":
        raise BenchmarkInputError(f"{label} input must be a PDF: {candidate.name}")
    return candidate


def _extract_and_expand_records(sp_pdf: Path, output_dir: Path):
    """Run the normal SP extractor once, then mirror the pipeline table expansion."""
    from sp_pdf_judger import extractor as extractor_module
    from sp_pdf_judger.sp_table_result_generalizer import (
        expand_table_records_for_judgement,
    )

    records = extractor_module.extract_records(sp_pdf, output_dir)
    if not records:
        return []
    record_type = type(records[0])
    allowed_fields = {item.name for item in fields(record_type)}
    expanded = expand_table_records_for_judgement(
        [dict(vars(record)) for record in records],
        has_permit=True,
        keep_original_table_record=False,
    )
    return [
        record_type(
            **{
                key: value
                for key, value in row.items()
                if key in allowed_fields
            }
        )
        for row in expanded
    ]


def _grouped_report(metadata: dict) -> dict[str, object]:
    path_counts = metadata.get("permit_group_path_counts") or {}
    return {
        "evaluation_mode": metadata.get("permit_evaluation_mode"),
        "raw_test_count": int(metadata.get("permit_group_raw_test_count", 0) or 0),
        "group_count": int(metadata.get("permit_group_count", 0) or 0),
        "path_counts": {
            "deterministic": int(path_counts.get("deterministic", 0) or 0),
            "ambiguous_llm": int(path_counts.get("ambiguous_llm", 0) or 0),
            "hold_without_llm": int(path_counts.get("hold_without_llm", 0) or 0),
            "primary_fallback": int(path_counts.get("primary_fallback", 0) or 0),
        },
        "provider_calls": int(metadata.get("permit_group_provider_calls", 0) or 0),
        "planned_batch_count": metadata.get("permit_group_planned_batch_count", 0),
        "planned_batch_sizes": metadata.get("permit_group_planned_batch_sizes", []),
        "planned_prompt_chars": metadata.get("permit_group_planned_prompt_chars", []),
        "compacted_candidate_count": metadata.get("permit_group_compacted_candidate_count", 0),
        "oversized_count": metadata.get("permit_group_oversized_count", 0),
        "transport": metadata.get("permit_group_transport", {}),
        "estimated_saved_calls": int(
            metadata.get("permit_group_estimated_saved_calls", 0) or 0
        ),
    }


def _phase_report(result, work: _PermitWorkDiagnostics, elapsed_ms: float) -> dict:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    report = {
        "elapsed_ms": round(elapsed_ms, 3),
        "cache_hit": metadata.get("permit_index_cache_hit"),
        "permit_index_elapsed_ms": int(
            metadata.get("permit_index_elapsed_ms", 0) or 0
        ),
        "permit_index_key": metadata.get("permit_index_key"),
        "permit_index_schema": metadata.get("permit_index_schema"),
        "permit_index_diagnostics": metadata.get("permit_index_diagnostics") or [],
        "ocr": {
            "page_count": int(metadata.get("permit_ocr_page_count", 0) or 0),
            "page_sources": work.as_dict()["extractor"]["page_sources"],
        },
        **work.as_dict(),
        "grouped_judgement": _grouped_report(metadata),
        "summary": asdict(result.summary),
        "llm_enabled": bool(metadata.get("llm_enabled")),
        "llm_call_count": int(metadata.get("llm_call_count", 0) or 0),
        "llm_success_count": int(metadata.get("llm_success_count", 0) or 0),
        "permit_extraction_errors": list(
            metadata.get("permit_extraction_errors") or []
        ),
    }
    return report


def _run_phase(
    *,
    sp_pdf: Path,
    resolution,
    records: Sequence,
    static_result,
    company: str | None,
    product: str | None,
) -> dict:
    from sp_pdf_judger import pipeline as pipeline_module

    started = perf_counter()
    with _permit_work_instrumentation() as work:
        pipeline = pipeline_module.DocumentJudgePipeline(
            company=company,
            product=product,
            permit_resolution=resolution,
        )
        result = pipeline.run(
            sp_pdf,
            extracted_records=list(records),
            static_result=static_result,
        )
    return _phase_report(result, work, (perf_counter() - started) * 1000)


def _validate_phase_pair(cold: dict, warm: dict) -> None:
    if cold.get("cache_hit") is not False:
        raise BenchmarkSafetyError(
            "cold phase did not build a fresh permit index: "
            f"{cold.get('permit_index_diagnostics') or []}"
        )
    if warm.get("cache_hit") is not True:
        raise BenchmarkSafetyError(
            "warm phase did not reuse the permit index: "
            f"{warm.get('permit_index_diagnostics') or []}"
        )
    warm_extractor = warm["extractor"]
    warm_parser = warm["parser"]
    if any(
        int(value or 0)
        for value in (
            warm_extractor.get("calls"),
            warm_parser.get("chunk_parser_calls"),
            warm_parser.get("condition_parser_calls"),
        )
    ):
        raise BenchmarkSafetyError(
            "warm phase unexpectedly reran permit extraction or parsing"
        )
    for name, phase in (("cold", cold), ("warm", warm)):
        grouped = phase["grouped_judgement"]
        if grouped.get("evaluation_mode") != "deterministic_first":
            raise BenchmarkSafetyError(
                f"{name} phase did not enter deterministic-first grouped judgement"
            )
        if phase.get("llm_enabled") or phase.get("llm_call_count"):
            raise BenchmarkSafetyError(
                f"{name} phase enabled or attempted the live LLM provider"
            )
        if grouped.get("provider_calls"):
            raise BenchmarkSafetyError(
                f"{name} phase reported grouped provider calls in offline mode"
            )


def run_benchmark(
    *,
    sp_pdf: Path,
    permit_pdf: Path,
    company: str | None = None,
    product: str | None = None,
    cache_dir: Path | None = None,
) -> dict[str, object]:
    """Run a cold and warm pipeline pass while all network providers are blocked."""
    from sp_pdf_judger.permit_catalog import resolve_submission_permits

    sp_path = _validated_pdf(sp_pdf, "SP")
    permit_path = _validated_pdf(permit_pdf, "permit")

    with _benchmark_workspace(cache_dir) as (workspace, retained):
        index_root = workspace / "permit_index"
        extract_root = workspace / "sp_extract"
        with patch.dict(
            os.environ,
            {"SP_PERMIT_INDEX_DIR": str(index_root)},
            clear=False,
        ):
            with _offline_provider_guard() as provider_guard:
                resolution = resolve_submission_permits(
                    [permit_path],
                    company,
                    product,
                )
                if resolution.errors or resolution.paths != (permit_path,):
                    details = "; ".join(resolution.errors) or "permit was not resolved"
                    raise BenchmarkInputError(details)
                if resolution.policy is None:
                    raise BenchmarkInputError("linked permit policy is unavailable")
                if resolution.policy.evaluation_mode != "deterministic_first":
                    raise BenchmarkInputError("linked permit did not select grouped evaluation")

                extraction_started = perf_counter()
                records = _extract_and_expand_records(sp_path, extract_root)
                extraction_elapsed_ms = (
                    perf_counter() - extraction_started
                ) * 1000
                test_count = sum(
                    getattr(record, "record_type", "") == "test"
                    for record in records
                )
                if test_count == 0:
                    raise BenchmarkInputError(
                        "SP extraction produced no test records for grouped judgement"
                    )
                static_result = SimpleNamespace(
                    pdf_path=sp_path,
                    metadata={"extract_dir": str(extract_root)},
                    preview_image_path=None,
                    manufacturing_summary_page_numbers=[],
                    manufacturing_summary_image_paths=[],
                )
                cold = _run_phase(
                    sp_pdf=sp_path,
                    resolution=resolution,
                    records=records,
                    static_result=static_result,
                    company=company,
                    product=product,
                )
                warm = _run_phase(
                    sp_pdf=sp_path,
                    resolution=resolution,
                    records=records,
                    static_result=static_result,
                    company=company,
                    product=product,
                )
                _raise_if_provider_attempted(provider_guard)
                _validate_phase_pair(cold, warm)

        return {
            "offline": {
                "clova_enabled": False,
                "network_enabled": False,
                "api_requests_sent": 0,
            },
            "inputs": {
                "sp": sp_path.name,
                "permit": permit_path.name,
                "company": company or "",
                "product": product or "",
            },
            "cache": {
                "index_root": str(index_root),
                "retained": retained,
                "cold_isolated": True,
            },
            "timing_scope": (
                "Each cold/warm elapsed_ms covers pipeline construction, permit "
                "index load/build, grouped judgement, and document policies; SP "
                "extraction is measured once outside both phases."
            ),
            "sp_extraction": {
                "elapsed_ms": round(extraction_elapsed_ms, 3),
                "record_count": len(records),
                "test_record_count": test_count,
            },
            "cold": cold,
            "warm": warm,
        }


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark a cold and warm persistent permit index plus grouped "
            "judgement with CLOVA and network provider calls forcibly disabled."
        )
    )
    parser.add_argument("--sp", required=True, type=Path, help="SP PDF to judge")
    parser.add_argument("--output", type=Path, help="Optional JSON measurement report path")
    parser.add_argument(
        "--permit",
        required=True,
        type=Path,
        help="Explicit permit PDF used for both benchmark phases",
    )
    parser.add_argument(
        "--company",
        help="Optional company name used for domain context",
    )
    parser.add_argument(
        "--product",
        help="Optional product name used for domain context",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help=(
            "Optional parent directory for a retained, isolated benchmark cache; "
            "existing cache entries are never deleted or reused"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        report = run_benchmark(
            sp_pdf=args.sp,
            permit_pdf=args.permit,
            company=args.company,
            product=args.product,
            cache_dir=args.cache_dir,
        )
    except (BenchmarkInputError, BenchmarkSafetyError, OfflineProviderBlocked) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 2
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
