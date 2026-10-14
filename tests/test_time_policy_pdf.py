"""Separate synthetic PDF fixtures, never edits to the provided D00-D12 corpus."""
from pathlib import Path

import pytest


def test_pdf_extraction_and_md_decisions_end_to_end(tmp_path):
    pytest.importorskip("reportlab")
    font = Path("C:/Windows/Fonts/malgun.ttf")
    if not font.is_file():
        pytest.skip("Korean TrueType font required; run prove_time_policies.py --font on this host")
    from scripts.prove_time_policies import run_proof
    report = run_proof(tmp_path, font)
    assert report["llm_calls"] == 0
    assert [row["expected"] for row in report["cases"].values()] == ["PASS", "FAIL", "HOLD"]
