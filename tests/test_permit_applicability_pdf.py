from pathlib import Path

import pytest


def test_applicability_contract_through_separate_real_pdfs(tmp_path):
    from scripts.prove_permit_applicability import run_proof
    font = Path("C:/Windows/Fonts/malgun.ttf")
    if not font.is_file(): pytest.skip("Korean font required")
    report = run_proof(tmp_path, font)
    assert report["logical_calls"] == 0 and not report["engine_changed_during_run"]
    assert len(report["cases"]) == 5 and all(r["matches_expected"] for r in report["cases"].values())
