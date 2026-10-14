from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from sp_pdf_judger.permit_catalog import PermitPolicy
from sp_pdf_judger.permit_conditions import parse_permit_conditions
from sp_pdf_judger.permit_index import (
    CONDITION_PARSER_SCHEMA_VERSION,
    OCR_IMPLEMENTATION_VERSION,
    PERMIT_INDEX_SCHEMA_VERSION,
    PERMIT_PARSER_SCHEMA_VERSION,
    PermitIndexConfig,
    PermitIndexSnapshot,
    build_condition_manifest,
    build_permit_index_key,
    permit_index_config,
    build_permit_search_index,
    load_or_build_permit_index,
    permit_index_root,
)
from sp_pdf_judger.permit_ocr import PermitPageText
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.schemas import ExtractedRecord


POLICY = PermitPolicy("index-test", True, True, True, "native")
PAGES = (
    PermitPageText(1, "1. 원액\n1.1. 확인시험\n최소 10개이어야 한다.", "native"),
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(pdf_sha256: str, source_file: str = "permit.pdf") -> PermitIndexSnapshot:
    store = PermitPdfStore([], policy=POLICY)
    chunks = tuple(
        store._parse_page_texts(
            PAGES,
            source_file,
            pdf_sha256=pdf_sha256,
        )
    )
    conditions = tuple(
        condition
        for chunk in chunks
        for condition in parse_permit_conditions(chunk)
    )
    return PermitIndexSnapshot(
        pages=PAGES,
        chunks=chunks,
        source_documents=tuple(store.source_documents),
        conditions=conditions,
        extraction_errors=(),
        page_errors=(),
        search_index=build_permit_search_index(chunks),
        build_diagnostics=(("page_count", len(PAGES)),),
        condition_manifest=build_condition_manifest(chunks, conditions),
    )


def _config(**changes) -> PermitIndexConfig:
    values = dict(
        ocr_mode="native",
        ocr_language="kor+eng",
        render_dpi=180,
        ocr_implementation_version=OCR_IMPLEMENTATION_VERSION,
        chunk_parser_version=PERMIT_PARSER_SCHEMA_VERSION,
        condition_parser_version=CONDITION_PARSER_SCHEMA_VERSION,
        structure_policy=(("policy_id", "index-test"), ("authoritative", True)),
    )
    values.update(changes)
    return PermitIndexConfig(**values)


def test_atomic_publish_temporary_name_is_no_longer_than_final_entry(monkeypatch, tmp_path):
    import sp_pdf_judger.permit_index as module
    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(tmp_path / "cache"))
    pdf = tmp_path / "permit.pdf"
    pdf.write_bytes(b"fixture")
    digest = _sha(pdf)
    real = module.tempfile.NamedTemporaryFile
    def windows_length_checked(**kwargs):
        # On Windows an otherwise legal final entry near MAX_PATH must not
        # fail because the atomic temporary filename is longer than it.
        assert len(kwargs["prefix"]) + 8 + len(kwargs["suffix"]) <= 69
        return real(**kwargs)
    monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", windows_length_checked)
    first = load_or_build_permit_index(pdf_path=pdf, pdf_sha256=digest, policy=POLICY,
                                      builder=lambda: _snapshot(digest), config=_config())
    assert first.snapshot is not None and first.failure_code is None
    second = load_or_build_permit_index(pdf_path=pdf, pdf_sha256=digest, policy=POLICY,
                                       builder=lambda: pytest.fail("warm rebuilt"), config=_config())
    assert second.cache_hit is True


def test_root_override_and_production_default_are_not_repo_or_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "explicit-index"
    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(override))
    assert permit_index_root() == override

    monkeypatch.delenv("SP_PERMIT_INDEX_DIR")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    root = permit_index_root()
    assert root == tmp_path / "local-app-data" / "SPValidator" / "permit_index"
    assert root != Path.cwd() and not root.is_relative_to(tmp_path / "temp")


@pytest.mark.parametrize(
    "field,value",
    [
        ("ocr_mode", "auto"),
        ("ocr_language", "kor"),
        ("render_dpi", 300),
        ("ocr_implementation_version", "next-ocr"),
        ("chunk_parser_version", "next-parser"),
        ("condition_parser_version", "next-condition"),
        ("structure_policy", (("policy_id", "other"),)),
    ],
)
def test_key_changes_for_each_structure_input(field: str, value: object) -> None:
    baseline = build_permit_index_key(pdf_sha256="a" * 64, config=_config())
    changed = build_permit_index_key(
        pdf_sha256="a" * 64,
        config=_config(**{field: value}),
    )
    assert changed != baseline
    assert build_permit_index_key(pdf_sha256="b" * 64, config=_config()) != baseline


def test_key_has_no_path_filename_mtime_alias_or_verdict_policy_input(tmp_path: Path) -> None:
    # The public key contract intentionally has no path, alias, MD-rule, API, or SP argument.
    first = tmp_path / "first.pdf"
    second = tmp_path / "renamed.pdf"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    before = build_permit_index_key(pdf_sha256=_sha(first), config=_config())
    os.utime(first, (1, 1))
    after = build_permit_index_key(pdf_sha256=_sha(second), config=_config())
    assert before == after


def test_runtime_policy_flags_do_not_duplicate_raw_evidence_index() -> None:
    baseline = PermitPolicy("same-policy", True, True, True, "auto", "legacy")
    runtime_only_changes = PermitPolicy(
        "same-policy", False, False, False, "auto", "deterministic_first"
    )
    assert permit_index_config(runtime_only_changes) == permit_index_config(baseline)


def test_mapped_and_legacy_submission_policy_share_identical_permit_index_key() -> None:
    pdf_sha256 = "c" * 64
    legacy = PermitPolicy(
        "submission-linked", True, True, True, "auto", "legacy"
    )
    mapped = PermitPolicy(
        "submission-linked", True, True, True, "auto", "deterministic_first"
    )

    assert build_permit_index_key(
        pdf_sha256=pdf_sha256,
        config=permit_index_config(legacy),
    ) == build_permit_index_key(
        pdf_sha256=pdf_sha256,
        config=permit_index_config(mapped),
    )


def test_first_build_then_warm_load_round_trips_all_typed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "permit.pdf"
    pdf.write_bytes(b"stable permit bytes")
    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(tmp_path / "index"))
    calls = 0

    def builder() -> PermitIndexSnapshot:
        nonlocal calls
        calls += 1
        return _snapshot(_sha(pdf), pdf.name)

    first = load_or_build_permit_index(
        pdf_path=pdf,
        pdf_sha256=_sha(pdf),
        policy=POLICY,
        builder=builder,
        config=_config(),
    )
    second = load_or_build_permit_index(
        pdf_path=pdf,
        pdf_sha256=_sha(pdf),
        policy=POLICY,
        builder=lambda: pytest.fail("warm load called builder"),
        config=_config(),
    )

    assert calls == 1
    assert first.failure_code is None and second.failure_code is None
    assert first.cache_hit is False and second.cache_hit is True
    assert first.ocr_page_count == 0 and second.ocr_page_count == 0
    assert first.snapshot == second.snapshot == _snapshot(_sha(pdf), pdf.name)
    assert first.key == second.key
    assert first.schema_version == second.schema_version == PERMIT_INDEX_SCHEMA_VERSION


def test_same_bytes_new_name_reuses_entry_but_rebinds_display_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "original.pdf"
    renamed = tmp_path / "renamed.pdf"
    first.write_bytes(b"same permit")
    renamed.write_bytes(first.read_bytes())
    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(tmp_path / "index"))
    digest = _sha(first)
    cold = load_or_build_permit_index(
        pdf_path=first,
        pdf_sha256=digest,
        policy=POLICY,
        builder=lambda: _snapshot(digest, first.name),
        config=_config(),
    )
    warm = load_or_build_permit_index(
        pdf_path=renamed,
        pdf_sha256=digest,
        policy=POLICY,
        builder=lambda: pytest.fail("renamed identical bytes must reuse"),
        config=_config(),
    )

    assert warm.cache_hit is True
    assert warm.snapshot is not None and cold.snapshot is not None
    assert {d.source_file for d in warm.snapshot.source_documents} == {renamed.name}
    assert {c.source_file for c in warm.snapshot.chunks} == {renamed.name}
    assert [c.evidence_id for c in warm.snapshot.chunks] == [c.evidence_id for c in cold.snapshot.chunks]
    assert [c.parent_evidence_id for c in warm.snapshot.chunks] == [c.parent_evidence_id for c in cold.snapshot.chunks]


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda p: p.update(schema_version=999),
        lambda p: p.update(key="0" * 64),
        lambda p: p.update(pdf_sha256="0" * 64),
        lambda p: p.update(page_count=99),
        lambda p: p["pages"][0].update(source="unknown"),
        lambda p: p["pages"][0].update(page_number=2),
        lambda p: p["documents"][0].update(evidence_id="bad"),
        lambda p: p["documents"].append(dict(p["documents"][0])),
        lambda p: p["chunks"][0].update(evidence_id="bad"),
        lambda p: p["chunks"].append(dict(p["chunks"][0])),
        lambda p: p["chunks"][-1].update(parent_evidence_id="missing"),
        lambda p: p["chunks"][-1]["owned_spans"].append(
            dict(p["chunks"][-1]["owned_spans"][0])
        ),
        lambda p: p["chunks"][-1]["owned_spans"][0].update(char_end=99999),
        lambda p: p["chunks"][-1]["owned_spans"][0].update(text="tampered"),
        lambda p: p["conditions"][0].update(evidence_id="missing"),
        lambda p: p["conditions"].append(dict(p["conditions"][0])),
        lambda p: p["conditions"][0].update(children=["missing"]),
        lambda p: p["search_index"][0].update(evidence_ids=["missing"]),
    ],
)
def test_semantically_corrupt_json_is_rebuilt_inside_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrupt
) -> None:
    pdf = tmp_path / "permit.pdf"
    pdf.write_bytes(b"corruption fixture")
    root = tmp_path / "index"
    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(root))
    digest = _sha(pdf)
    first = load_or_build_permit_index(
        pdf_path=pdf,
        pdf_sha256=digest,
        policy=POLICY,
        builder=lambda: _snapshot(digest),
        config=_config(),
    )
    entry = root / "entries" / f"{first.key}.json"
    payload = json.loads(entry.read_text(encoding="utf-8"))
    corrupt(payload)
    entry.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    rebuilds = 0

    def rebuild() -> PermitIndexSnapshot:
        nonlocal rebuilds
        rebuilds += 1
        return _snapshot(digest)

    loaded = load_or_build_permit_index(
        pdf_path=pdf,
        pdf_sha256=digest,
        policy=POLICY,
        builder=rebuild,
        config=_config(),
    )
    assert rebuilds == 1
    assert loaded.cache_hit is False and loaded.failure_code is None
    assert loaded.snapshot == _snapshot(digest)


def test_truncated_json_is_rebuilt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "permit.pdf"
    pdf.write_bytes(b"truncated fixture")
    root = tmp_path / "index"
    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(root))
    digest = _sha(pdf)
    built = load_or_build_permit_index(
        pdf_path=pdf, pdf_sha256=digest, policy=POLICY,
        builder=lambda: _snapshot(digest), config=_config(),
    )
    (root / "entries" / f"{built.key}.json").write_text("{", encoding="utf-8")
    calls = 0

    def builder() -> PermitIndexSnapshot:
        nonlocal calls
        calls += 1
        return _snapshot(digest)

    loaded = load_or_build_permit_index(
        pdf_path=pdf, pdf_sha256=digest, policy=POLICY,
        builder=builder, config=_config(),
    )
    assert calls == 1 and loaded.failure_code is None and loaded.cache_hit is False


def test_removed_condition_array_is_rejected_and_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "permit.pdf"
    pdf.write_bytes(b"condition deletion fixture")
    root = tmp_path / "index"
    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(root))
    digest = _sha(pdf)
    built = load_or_build_permit_index(
        pdf_path=pdf, pdf_sha256=digest, policy=POLICY,
        builder=lambda: _snapshot(digest), config=_config(),
    )
    entry = root / "entries" / f"{built.key}.json"
    payload = json.loads(entry.read_text(encoding="utf-8"))
    assert payload["conditions"]
    payload["conditions"] = []
    entry.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    rebuilds = 0

    def rebuild() -> PermitIndexSnapshot:
        nonlocal rebuilds
        rebuilds += 1
        return _snapshot(digest)

    loaded = load_or_build_permit_index(
        pdf_path=pdf, pdf_sha256=digest, policy=POLICY,
        builder=rebuild, config=_config(),
    )
    assert rebuilds == 1
    assert loaded.cache_hit is False and loaded.failure_code is None
    assert loaded.snapshot is not None and loaded.snapshot.conditions


def test_builder_source_change_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "permit.pdf"
    pdf.write_bytes(b"before")
    root = tmp_path / "index"
    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(root))
    digest = _sha(pdf)

    def builder() -> PermitIndexSnapshot:
        snapshot = _snapshot(digest)
        pdf.write_bytes(b"after")
        return snapshot

    loaded = load_or_build_permit_index(
        pdf_path=pdf, pdf_sha256=digest, policy=POLICY,
        builder=builder, config=_config(),
    )
    assert loaded.snapshot is None and loaded.failure_code == "source_changed"
    assert not list((root / "entries").glob("*.json"))


def test_failed_builder_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "permit.pdf"
    pdf.write_bytes(b"builder failure")
    root = tmp_path / "index"
    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(root))

    def builder() -> PermitIndexSnapshot:
        raise RuntimeError("secret path or provider text must not be persisted")

    loaded = load_or_build_permit_index(
        pdf_path=pdf, pdf_sha256=_sha(pdf), policy=POLICY,
        builder=builder, config=_config(),
    )
    assert loaded.snapshot is None and loaded.failure_code == "build_failed"
    assert not list((root / "entries").glob("*.json"))


def test_atomic_publication_exposes_only_a_complete_valid_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sp_pdf_judger.permit_index as permit_index

    pdf = tmp_path / "permit.pdf"
    pdf.write_bytes(b"atomic publication")
    root = tmp_path / "index"
    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(root))
    digest = _sha(pdf)
    config = _config()
    key = build_permit_index_key(pdf_sha256=digest, config=config)
    destination = root / "entries" / f"{key.value}.json"
    real_replace = permit_index.os.replace
    observed = []

    def inspect_replace(source, target):
        source, target = Path(source), Path(target)
        assert target == destination and not target.exists()
        payload = json.loads(source.read_text(encoding="utf-8"))
        assert payload["key"] == key.value and payload["page_count"] == 1
        observed.append(source.name)
        real_replace(source, target)

    monkeypatch.setattr(permit_index.os, "replace", inspect_replace)
    loaded = load_or_build_permit_index(
        pdf_path=pdf,
        pdf_sha256=digest,
        policy=POLICY,
        builder=lambda: _snapshot(digest),
        config=config,
    )

    assert loaded.failure_code is None and observed
    assert destination.is_file()
    assert not list(destination.parent.glob("*.tmp"))


def test_store_cold_then_warm_skips_extractor_chunk_and_condition_parsers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sp_pdf_judger.permit_pdf_store as store_module

    pdf = tmp_path / "permit.pdf"
    pdf.write_bytes(b"store integration")
    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(tmp_path / "index"))
    digest = _sha(pdf)
    calls = {"extract": 0, "chunk": 0, "condition": 0}
    original_chunk = PermitPdfStore._parse_page_texts
    original_condition = store_module.parse_permit_conditions

    def extractor(path: Path, ocr_mode: str, **kwargs):
        calls["extract"] += 1
        assert path == pdf and ocr_mode == "native"
        assert kwargs == {"pdf_sha256": digest, "use_page_cache": False}
        return list(PAGES), []

    def parse_chunks(self, *args, **kwargs):
        calls["chunk"] += 1
        return original_chunk(self, *args, **kwargs)

    def parse_conditions(chunk):
        calls["condition"] += 1
        return original_condition(chunk)

    monkeypatch.setattr(PermitPdfStore, "_parse_page_texts", parse_chunks)
    monkeypatch.setattr(store_module, "parse_permit_conditions", parse_conditions)
    cold = PermitPdfStore(
        [pdf], policy=POLICY, page_text_extractor=extractor,
        pdf_sha256_by_path={pdf: digest},
    )
    assert calls["extract"] == 1 and calls["chunk"] == 1
    assert calls["condition"] == len(cold.chunks)
    cold_counts = dict(calls)

    warm = PermitPdfStore(
        [pdf], policy=POLICY,
        page_text_extractor=lambda *_a, **_k: pytest.fail("warm extractor called"),
        pdf_sha256_by_path={pdf: digest},
    )
    assert calls == cold_counts
    assert warm.chunks == cold.chunks
    assert warm.source_documents == cold.source_documents
    assert warm.conditions == cold.conditions
    assert warm.raw_search_index == cold.raw_search_index
    assert warm.index_diagnostics[0]["cache_hit"] is True

    from sp_pdf_judger.permit_source_ownership import validate_permit_ownership_snapshot
    assert validate_permit_ownership_snapshot(warm.source_documents, warm.chunks) == ()

    record = ExtractedRecord(section_title="원액", test_name="확인시험")
    assert [c.evidence_id for c in warm.search(record)] == [c.evidence_id for c in cold.search(record)]
    assert [c.owned_text for c in warm.search(record)] == [c.owned_text for c in cold.search(record)]


def test_store_renamed_warm_load_keeps_evidence_and_current_display_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.pdf"
    renamed = tmp_path / "renamed.pdf"
    first.write_bytes(b"rename integration")
    renamed.write_bytes(first.read_bytes())
    digest = _sha(first)
    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(tmp_path / "index"))

    def extractor(*_args, **_kwargs):
        return list(PAGES), []

    cold = PermitPdfStore([first], policy=POLICY, page_text_extractor=extractor,
                          pdf_sha256_by_path={first: digest})
    warm = PermitPdfStore(
        [renamed], policy=POLICY,
        page_text_extractor=lambda *_a, **_k: pytest.fail("renamed warm extractor called"),
        pdf_sha256_by_path={renamed: digest},
    )
    assert [c.evidence_id for c in warm.chunks] == [c.evidence_id for c in cold.chunks]
    assert {c.source_file for c in warm.chunks} == {renamed.name}
    assert {d.source_file for d in warm.source_documents} == {renamed.name}


def test_two_processes_build_same_key_once(tmp_path: Path) -> None:
    pdf = tmp_path / "permit.pdf"
    pdf.write_bytes(b"process concurrency")
    root = tmp_path / "index"
    worker = Path(__file__).parent / "helpers" / "permit_index_worker.py"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    children = [
        subprocess.Popen(
            [sys._base_executable if os.name == "nt" else sys.executable,
             str(worker), str(root), str(pdf), str(tmp_path / f"result-{index}.json")],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    results = []
    for index, child in enumerate(children):
        _, stderr = child.communicate(timeout=30)
        assert child.returncode == 0, stderr
        results.append(json.loads((tmp_path / f"result-{index}.json").read_text(encoding="utf-8")))

    assert (tmp_path / "builder-count.txt").read_text(encoding="utf-8").splitlines() == ["built"]
    assert sorted(result["cache_hit"] for result in results) == [False, True]
    assert all(result["failure_code"] is None for result in results)


SOURCE_PERMIT_PDF = (
    Path(__file__).resolve().parents[1]
    / "sp_pdf_judger/permits/documents/sky_covione_multidose.pdf"
)


@pytest.mark.skipif(not SOURCE_PERMIT_PDF.exists(), reason="bundled permit PDF is absent")
def test_bundled_permit_second_fresh_store_skips_all_build_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sp_pdf_judger.permit_pdf_store as store_module

    monkeypatch.setenv("SP_PERMIT_INDEX_DIR", str(tmp_path / "index"))
    digest = _sha(SOURCE_PERMIT_PDF)
    first = PermitPdfStore(
        [SOURCE_PERMIT_PDF], policy=POLICY,
        pdf_sha256_by_path={SOURCE_PERMIT_PDF: digest},
    )
    assert first.chunks and first.conditions

    def forbidden(*_args, **_kwargs):
        raise AssertionError("warm bundled permit reparsed")

    monkeypatch.setattr(store_module, "parse_permit_conditions", forbidden)
    monkeypatch.setattr(PermitPdfStore, "_parse_page_texts", forbidden)
    second = PermitPdfStore(
        [SOURCE_PERMIT_PDF], policy=POLICY,
        page_text_extractor=forbidden,
        pdf_sha256_by_path={SOURCE_PERMIT_PDF: digest},
    )
    assert second.chunks == first.chunks
    assert second.conditions == first.conditions
    assert second.index_diagnostics[0]["cache_hit"] is True
