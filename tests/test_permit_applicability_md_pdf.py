"""Real synthetic PDF extraction to explicitly selected MD, never product activation."""
import hashlib
from pathlib import Path

import pytest

import scripts.prove_permit_applicability as proof

RULES = Path(__file__).resolve().parents[1] / "docs/examples/applicability_rules/conditions.md"
FONT = Path("C:/Windows/Fonts/malgun.ttf")


def require_font():
    if not FONT.is_file(): pytest.skip("Korean QA font required")


def test_real_pdf_proof_reads_md_without_fixture_fallback(tmp_path, monkeypatch):
    require_font()
    def forbidden(*args): raise AssertionError("Python fixture must not define the MD run")
    monkeypatch.setattr(proof, "fixture_contract", forbidden)
    report = proof.run_proof(tmp_path, FONT, rules_md=RULES, isolate_targets=True)
    assert all(r["matches_expected"] for r in report["cases"].values())
    assert report["logical_calls"] == 0
    assert not report["rules_changed_during_run"] and not report["engine_changed_during_run"]
    assert report["rules_md_sha256"] == hashlib.sha256(RULES.read_bytes()).hexdigest()


@pytest.mark.parametrize("invalid", ["wrong-document", "missing-condition"])
def test_invalid_md_fails_before_paid_client_creation(tmp_path, monkeypatch, invalid):
    require_font()
    text = RULES.read_text(encoding="utf-8")
    if invalid == "wrong-document":
        text = text.replace("bb75de604a47cca29298efffc1def2abdde3ca27501f29de9cbb8a14a735f078", "f" * 64)
    else:
        text = text.replace("      purpose: procedure\n", "")
    path = tmp_path / "invalid.md"
    path.write_text(text, encoding="utf-8")
    import sp_pdf_judger.llm as llm
    def forbidden(): raise AssertionError("paid client must not be constructed")
    monkeypatch.setattr(llm, "ClovaJudgeClient", forbidden)
    with pytest.raises(ValueError): proof.run_proof(tmp_path / "proof", FONT, live=True, rules_md=path)


def test_md_only_operator_correction_changes_real_pdf_decision_and_pin(tmp_path):
    require_font()
    # Deliberately wrong authoring followed by a correction, not two approved
    # interpretations of the same permit. Source semantics still need review.
    path = tmp_path / "draft.md"
    original = RULES.read_text(encoding="utf-8")
    path.write_text(original.replace("      operator: eq", "      operator: ne"), encoding="utf-8")
    wrong = proof.run_proof(tmp_path / "proof", FONT, rules_md=path)
    path.write_text(original, encoding="utf-8")
    corrected = proof.run_proof(tmp_path / "proof", FONT, rules_md=path)
    assert wrong["cases"]["required"]["validation"]["decisions"][0]["applicability"] == "not_required"
    assert corrected["cases"]["required"]["validation"]["decisions"][0]["applicability"] == "required"
    assert wrong["cases"]["required"]["contract_id"] != corrected["cases"]["required"]["contract_id"]
    assert not wrong["cases"]["required"]["matches_expected"]
    assert all(r["matches_expected"] for r in corrected["cases"].values())


def test_numeric_pdf_mode_never_fabricates_or_validates_model_response(tmp_path, monkeypatch):
    require_font()
    def forbidden(*args, **kwargs): raise AssertionError("Numeric mode must not use the LLM protocol")
    for name in ("expected_response", "validate_evidence", "fixture_contract", "request_checked_evidence"):
        monkeypatch.setattr(proof, name, forbidden)
    import sp_pdf_judger.llm as llm
    monkeypatch.setattr(llm, "ClovaJudgeClient", forbidden)
    report = proof.run_proof(tmp_path, FONT, rules_md=RULES, execution_mode="reviewed_numeric_v1")
    assert report["mode"] == "numeric" and report["logical_calls"] == 0
    assert not report["rules_changed_during_run"] and not report["engine_changed_during_run"]
    assert all(row["matches_expected"] for row in report["cases"].values())
    assert report["cases"]["missing_fact"]["validation"]["evaluation_status"] == "held"
    assert report["cases"]["required"]["validation"]["evaluation_status"] == "complete"
    assert all("response" not in row and "attempts" not in row for row in report["cases"].values())
    assert (tmp_path / "numeric-report.json").is_file()
    assert not (tmp_path / "live-report.json").exists()


@pytest.mark.parametrize("options", [
    {"execution_mode": "auto"}, {"execution_mode": "reviewed_numeric_v1"},
    {"execution_mode": "reviewed_numeric_v1", "rules_md": RULES, "live": True},
    {"execution_mode": "reviewed_numeric_v1", "rules_md": RULES, "isolate_targets": True},
])
def test_ambiguous_numeric_mode_is_rejected_before_any_artifact_or_api(tmp_path, options):
    output = tmp_path / "not-created"
    with pytest.raises(ValueError): proof.run_proof(output, FONT, **options)
    assert not output.exists()
