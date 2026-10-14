"""Separate requests must never overwrite the extraction evidence of another."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, current_thread
from types import SimpleNamespace

import pytest

import sp_pdf_judger.pipeline as module
from sp_pdf_judger.schemas import ExtractedRecord


@pytest.fixture
def isolated_pipeline(monkeypatch, tmp_path):
    """Run real orchestration with no PDF, LLM, or domain-specific judgement."""
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(module, "RuleBook", lambda *a: SimpleNamespace(
        fingerprint="rules", rules=[], requirements=[], search_alias_groups=lambda _: []))
    monkeypatch.setattr(module, "PolicyContext", lambda *a, **kw: None)
    monkeypatch.setattr(module, "evaluate_policies", lambda *a: [])
    monkeypatch.setattr(module, "summarize_manufacturing_policies", lambda *a: ("", "", {}))
    monkeypatch.setattr(module, "manufacturing_policy_cards", lambda *a, **kw: [])
    monkeypatch.setattr(module, "_find_manufacturing_summary_pages", lambda _: [1])
    monkeypatch.setattr(module, "render_first_page", lambda pdf, path: path.write_text(
        current_thread().name, encoding="utf-8"))

    def render_page(*, output_dir, **kwargs):
        path = output_dir / "summary.png"
        path.write_text(current_thread().name, encoding="utf-8")
        return path

    monkeypatch.setattr(module, "_render_pdf_page", render_page)

    def factory():
        obj = module.DocumentJudgePipeline.__new__(module.DocumentJudgePipeline)
        obj.rule_dir = None
        obj.company = ""  # Same empty default as the real constructor.
        obj.product = "test-product"
        obj.permit_store = SimpleNamespace(configure_search_aliases=lambda _: None,
            enabled=False, permit_pdf_paths=[], chunks=[], extraction_errors=[])
        obj.permit_resolution = SimpleNamespace(paths=[], policy=None, fingerprint="", errors=[])
        obj.judge_engine = SimpleNamespace(comparison_bases={}, permit_policy=None)
        obj.rag_store = SimpleNamespace(docs=[], loaded_sources=[])
        obj.llm_client = SimpleNamespace(enabled=False, model="", call_count=0,
            success_count=0, last_error="")
        profile = SimpleNamespace(requested_company="", requested_product="",
            matched_company="", matched_product="", sources=[], fingerprint="",
            render_for_llm=lambda: "")
        obj._activate_detail_profile = lambda _: profile
        return obj

    def extract(pdf, directory):
        records = [ExtractedRecord(record_type="content", content=current_thread().name)]
        (directory / "05_records.json").write_text(
            json.dumps([asdict(r) for r in records]), encoding="utf-8")
        return records

    monkeypatch.setattr(module, "extract_records", extract)
    return factory


def test_submission_company_is_forwarded_to_policy_context(isolated_pipeline, monkeypatch, tmp_path):
    captured = {}
    def context(*args, **kwargs):
        captured.update(kwargs)
    monkeypatch.setattr(module, "PolicyContext", context)
    pipeline = isolated_pipeline()
    pipeline.company = "synthetic-company"
    pipeline.run(tmp_path / "same.pdf")
    assert captured["company"] == "synthetic-company"


def test_unit_provenance_survives_final_md_veto(isolated_pipeline, monkeypatch, tmp_path):
    from sp_pdf_judger.policy_engine import RuleBook
    from sp_pdf_judger.schemas import Evaluation
    monkeypatch.setattr(module, "RuleBook", RuleBook)
    obj = isolated_pipeline()
    obj.product = "스카이코비원멀티주"
    row = ExtractedRecord(order_idx=1, test_name="엔도톡신시험",
        criteria="820 EU/mg of protein 미만", result="760 IU/mg of protein")
    obj.judge_engine.judge_record = lambda r: Evaluation(order_idx=1, test_name=r.test_name,
        criteria=r.criteria, result=r.result, final_status="검수합격", reason="수치 충족", comparison_completed=True)
    def veto(evaluations, findings):
        evaluations[0].final_status = "검수불합격"
        evaluations[0].reason = "R10: 기간 미달"
        return []
    monkeypatch.setattr(module, "apply_policy_test_guards", veto)
    result = obj.run(tmp_path / "fake.pdf", extracted_records=[row])
    ev = result.evaluations[0]
    assert ev.final_status == "검수불합격" and ev.result == row.result
    assert "기간 미달" in ev.reason and "MD 단위 표기 대응" in ev.reason
    assert "수치 충족" not in ev.reason
    assert result.metadata["unit_normalizations"][0]["original"] == row.result


def test_changing_pdf_during_extraction_cannot_receive_stale_receipt(isolated_pipeline, monkeypatch, tmp_path):
    pdf = tmp_path / "changing.pdf"
    pdf.write_bytes(b"original")
    original = module.extract_records
    def mutate(path, directory):
        records = original(path, directory)
        path.write_bytes(b"different")
        return records
    monkeypatch.setattr(module, "extract_records", mutate)
    with pytest.raises(RuntimeError, match="SP 파일이 변경"):
        isolated_pipeline().run(pdf)


def test_parallel_requests_keep_separate_raw_evidence(isolated_pipeline, monkeypatch, tmp_path):
    barrier = Barrier(2)
    captured = {}

    def overlapping_extract(pdf, directory):
        token = current_thread().name
        raw = directory / "05_records.json"
        raw.write_text(json.dumps([{"record_type": "content", "content": token}]), encoding="utf-8")
        captured[token] = directory
        barrier.wait(timeout=10)
        loaded = json.loads(raw.read_text(encoding="utf-8"))
        return [ExtractedRecord(**loaded[0])]

    monkeypatch.setattr(module, "extract_records", overlapping_extract)

    def run():
        return current_thread().name, isolated_pipeline().run(tmp_path / "same.pdf")

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = [f.result(timeout=20) for f in (pool.submit(run), pool.submit(run))]
    for token, result in (a, b):
        assert result.extracted_records[0].content == token
        assert result.preview_image_path.read_text(encoding="utf-8") == token
        assert result.manufacturing_summary_image_paths[0].read_text(encoding="utf-8") == token
    assert len(set(captured.values())) == 2


def test_later_run_does_not_rewrite_earlier_artifacts(isolated_pipeline, tmp_path):
    first = isolated_pipeline().run(tmp_path / "same.pdf")
    paths = [first.preview_image_path, *first.manufacturing_summary_image_paths,
             Path(first.metadata["extract_dir"]) / "05_records.json"]
    for path in paths:
        path.write_text("retained-original-evidence", encoding="utf-8")
    second = isolated_pipeline().run(tmp_path / "same.pdf")
    assert first.metadata["extract_dir"] != second.metadata["extract_dir"]
    assert all(p.read_text(encoding="utf-8") == "retained-original-evidence" for p in paths)


def test_rejudgement_reuses_actual_extraction_source(isolated_pipeline, monkeypatch, tmp_path):
    pdf = tmp_path / "same.pdf"
    first = isolated_pipeline().run(pdf)
    raw = Path(first.metadata["extract_dir"]) / "05_records.json"
    before = raw.read_bytes()
    monkeypatch.setattr(module, "extract_records", lambda *a: pytest.fail("Must reuse extraction"))
    second = isolated_pipeline().run(pdf, extracted_records=first.extracted_records, static_result=first)
    assert second.metadata["extract_dir"] == first.metadata["extract_dir"]
    assert raw.read_bytes() == before
    assert second.preview_image_path == first.preview_image_path
    assert second.manufacturing_summary_image_paths == first.manufacturing_summary_image_paths


def test_new_extraction_with_static_preview_has_new_evidence(isolated_pipeline, tmp_path):
    pdf = tmp_path / "same.pdf"
    first = isolated_pipeline().run(pdf)
    second = isolated_pipeline().run(pdf, static_result=first)
    assert second.metadata["extract_dir"] != first.metadata["extract_dir"]
    assert (Path(second.metadata["extract_dir"]) / "05_records.json").is_file()
    assert second.preview_image_path == first.preview_image_path


def test_failure_cannot_corrupt_previous_success(isolated_pipeline, monkeypatch, tmp_path):
    pdf = tmp_path / "same.pdf"
    first = isolated_pipeline().run(pdf)
    raw = Path(first.metadata["extract_dir"]) / "05_records.json"
    before = raw.read_bytes()

    def fail(pdf, directory):
        (directory / "05_records.json").write_text("partial-write", encoding="utf-8")
        raise RuntimeError("interrupted extraction")

    monkeypatch.setattr(module, "extract_records", fail)
    with pytest.raises(RuntimeError, match="interrupted extraction"):
        isolated_pipeline().run(pdf)
    assert raw.read_bytes() == before


def test_static_result_for_another_pdf_is_rejected(isolated_pipeline, tmp_path):
    first = isolated_pipeline().run(tmp_path / "one.pdf")
    with pytest.raises(ValueError, match="PDF"):
        isolated_pipeline().run(tmp_path / "two.pdf", extracted_records=first.extracted_records,
                                static_result=first)


def test_records_without_static_source_do_not_borrow_old_files(isolated_pipeline, tmp_path):
    pdf = tmp_path / "same.pdf"
    first = isolated_pipeline().run(pdf)
    second = isolated_pipeline().run(pdf, extracted_records=[ExtractedRecord(
        record_type="content", content="independent records")])
    assert second.metadata["extract_dir"] != first.metadata["extract_dir"]
    assert not (Path(second.metadata["extract_dir"]) / "05_records.json").exists()
    assert second.extracted_records[0].content == "independent records"
    assert second.metadata["extraction_reused"] is False


def test_workspaces_remain_short_for_long_source_names(isolated_pipeline, tmp_path):
    result = isolated_pipeline().run(tmp_path / ("very-long-name-" * 12 + ".pdf"))
    workspace = Path(result.metadata["run_workspace"])
    assert workspace.parent == tmp_path / "sp_pdf_judger_preview"
    assert len(workspace.name) < 40
    assert result.preview_image_path.name == "page1.png"


def test_two_processes_extract_actual_same_pdf_without_shared_artifacts(tmp_path):
    """Use separate processes: PyMuPDF itself is not made thread-safe here."""
    root = Path(__file__).resolve().parents[1]
    sources = sorted((root / "더미데이터").glob("D02_*.pdf"))
    if len(sources) != 1:
        pytest.skip("Private D02 fixture required")
    code = '''
import hashlib, json, sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from sp_pdf_judger.pipeline import DocumentJudgePipeline
from sp_pdf_judger.ui_html import _load_records_json_for_ui
with patch("sp_pdf_judger.llm.create_clova_client", return_value=None):
    first = DocumentJudgePipeline(company="동국바이오사이언스", product="스카이코비원멀티주",
                                  permit_enabled=False).run(Path(sys.argv[1]))
    second = DocumentJudgePipeline(company="동국바이오사이언스", product="스카이코비원멀티주",
                                   permit_enabled=False).run(Path(sys.argv[1]),
        extracted_records=first.extracted_records, static_result=first)
raw = Path(first.metadata["extract_dir"]) / "05_records.json"
assert raw.is_file() and _load_records_json_for_ui(second) == json.loads(raw.read_text(encoding="utf-8"))
assert second.metadata["extract_dir"] == first.metadata["extract_dir"]
assert second.metadata["extraction_reused"] is True
assert first.metadata["run_workspace"] != second.metadata["run_workspace"]
assert first.metadata["llm_call_count"] == second.metadata["llm_call_count"] == 0
assert asdict(first.summary) == asdict(second.summary)
print("WORKSPACE_AUDIT=" + json.dumps({"extract_dir": first.metadata["extract_dir"],
    "preview": str(first.preview_image_path),
    "manufacturing": [str(p) for p in first.manufacturing_summary_image_paths],
    "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
    "records": len(first.extracted_records), "summary": asdict(first.summary)}))
'''
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "TEMP": str(tmp_path), "TMP": str(tmp_path)}
    processes = [subprocess.Popen([sys.executable, "-c", code, str(sources[0])], cwd=root,
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8") for _ in range(2)]
    reports = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=180)
            assert process.returncode == 0, stderr[-5000:]
            reports.append(json.loads(next(line.removeprefix("WORKSPACE_AUDIT=")
                for line in stdout.splitlines() if line.startswith("WORKSPACE_AUDIT="))))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
    a, b = reports
    assert a["extract_dir"] != b["extract_dir"]
    assert a["preview"] != b["preview"]
    assert set(a["manufacturing"]).isdisjoint(b["manufacturing"])
    assert a["raw_sha256"] == b["raw_sha256"] and a["records"] == b["records"] > 0
    assert a["summary"] == b["summary"]
    for report in reports:
        for name in [report["preview"], *report["manufacturing"]]:
            assert Path(name).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
