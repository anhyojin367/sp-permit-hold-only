from pathlib import Path

import pytest


def test_independent_test_scope_from_pdf_through_md(tmp_path):
    pytest.importorskip("reportlab")
    font = Path("C:/Windows/Fonts/malgun.ttf")
    if not font.is_file(): pytest.skip("Korean font required")
    from scripts.prove_test_scope_policies import run_proof
    report = run_proof(tmp_path, font)
    assert report["llm_calls"] == 0 and len(report["cases"]) == 7
    missing = report["cases"]["missing_target"]["findings"][0]
    assert [(r["test_path"], r["status"]) for r in missing["details"]["requirement_checks"]] == [
        (["무균시험"], "PASS"), (["함량시험"], "FAIL")]
    empty = report["cases"]["only_other_stage"]["findings"][0]
    assert [r["status"] for r in empty["details"]["requirement_checks"]] == ["FAIL", "FAIL"]
