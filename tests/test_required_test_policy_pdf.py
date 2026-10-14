"""New isolated fixtures exercise PDF -> records/permit inventory -> MD result."""
from pathlib import Path

import pytest


def test_required_test_pdf_extraction_and_md_decisions(tmp_path):
    pytest.importorskip("reportlab")
    font = Path("C:/Windows/Fonts/malgun.ttf")
    if not font.is_file():
        pytest.skip("Korean font required for the synthetic extraction proof")
    from scripts.prove_required_test_policies import run_proof
    report = run_proof(tmp_path, font)
    assert report["llm_calls"] == 0
    assert len(report["cases"]) == 7
    multi = report["cases"]["multi_batch_missing"]["findings"][0]
    failures = [r for r in multi["details"]["requirement_checks"] if r["status"] == "FAIL"]
    assert [(r["batch"], r["test_path"]) for r in failures] == [("L2", ["함량시험"])]
