"""Real PDF/extractor tests for numeric facts; not product obligation approval."""
import hashlib
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

import scripts.prove_scoped_condition_facts as proof
from scripts.prove_permit_applicability import POLICY
from sp_pdf_judger.extractor import extract_records
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.permit_applicability_md import evaluate_numeric_pdf
from sp_pdf_judger.scoped_condition_facts import ScopedStage, read_scoped_targets

FONT = Path("C:/Windows/Fonts/malgun.ttf")


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    if not FONT.is_file(): pytest.skip("Korean QA font required")
    root = tmp_path_factory.mktemp("scoped-pdf")
    report = proof.run_proof(root, FONT)
    return root, report


def test_real_extractor_md_and_numeric_integration(generated):
    root, report = generated
    assert len(report["cases"]) == 8
    assert all(row["matches_expected"] for row in report["cases"].values())
    assert not report["engine_changed_during_run"] and report["logical_calls"] == 0
    for name in ("normal", "missing_own_field", "wrong_unit", "duplicate_field", "round_one", "procedure_false"):
        result = report["cases"][name]["result"]
        assert result["source_page_count"] == 1
        assert result["source_pdf_sha256"] == hashlib.sha256((root / f"{name}.pdf").read_bytes()).hexdigest()
        assert result["targets"][0]["batch"] == "A-01"
        assert result["model_used"] is False


def test_pdf_entry_reads_entire_file_not_a_callers_partial_pages(generated, tmp_path):
    root, report = generated
    pdf = tmp_path / "normal.pdf"
    with fitz.open(root / "normal.pdf") as document:
        document.new_page().insert_text((42, 72), "Synthetic appendix: source snapshot includes this page.")
        document.save(pdf)
    store = PermitPdfStore([root / "permit.pdf"], policy=POLICY)
    records = extract_records(pdf, tmp_path / "extract")
    result = evaluate_numeric_pdf(root / "conditions.md", store, pdf, records, company="합성제조사", product="합성시험제품")
    assert result["source_page_count"] == 2
    assert [r["applicability"] for r in result["decisions"]] == ["required", "required"]
    assert result["evaluation_id"] != report["cases"]["normal"]["result"]["evaluation_id"]


def test_pdf_changed_during_evaluation_is_rejected(generated, tmp_path, monkeypatch):
    root, _ = generated
    pdf = tmp_path / "normal.pdf"
    pdf.write_bytes((root / "normal.pdf").read_bytes())
    store = PermitPdfStore([root / "permit.pdf"], policy=POLICY)
    records = extract_records(pdf, tmp_path / "extract")
    import sp_pdf_judger.permit_applicability_md as module
    original = module.read_scoped_targets
    def change(*args, **kwargs):
        result = original(*args, **kwargs)
        pdf.write_bytes(pdf.read_bytes() + b"\n% concurrent-change\n")
        return result
    monkeypatch.setattr(module, "read_scoped_targets", change)
    with pytest.raises(ValueError, match="변경"):
        evaluate_numeric_pdf(root / "conditions.md", store, pdf, records, company="합성제조사", product="합성시험제품")


def test_only_md_operator_correction_changes_scoped_pdf_decision(generated, tmp_path):
    root, _ = generated
    pdf = root / "normal.pdf"
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    rules = tmp_path / "draft.md"
    original = (root / "conditions.md").read_text(encoding="utf-8")
    # Deliberate authoring error, not an alternative approved interpretation.
    rules.write_text(original.replace("operator: eq", "operator: ne"), encoding="utf-8")
    store = PermitPdfStore([root / "permit.pdf"], policy=POLICY)
    records = extract_records(pdf, tmp_path / "extract")
    wrong = evaluate_numeric_pdf(rules, store, pdf, records, company="합성제조사", product="합성시험제품")
    rules.write_text(original, encoding="utf-8")
    corrected = evaluate_numeric_pdf(rules, store, pdf, records, company="합성제조사", product="합성시험제품")
    assert [r["applicability"] for r in wrong["decisions"]] == ["not_required", "not_required"]
    assert [r["applicability"] for r in corrected["decisions"]] == ["required", "required"]
    assert wrong["evaluation_id"] != corrected["evaluation_id"]
    assert digest == hashlib.sha256(pdf.read_bytes()).hexdigest()


def test_provided_d00_amount_fact_is_only_from_its_own_information_table(tmp_path):
    root = Path(__file__).resolve().parents[1]
    files = list((root / "더미데이터").glob("D00_*.pdf"))
    if len(files) != 1: pytest.skip("Provided synthetic D00 required")
    pdf = files[0]
    records = extract_records(pdf, tmp_path / "actual-extract")
    with fitz.open(pdf) as document:
        pages = [(i + 1, page.get_text()) for i, page in enumerate(document)]
    info = dict(record_path=["원액", "정보", "Component A 중간체 원액 정보"], record_type="content",
        start="3.1.1 Component A 중간체 원액 정보", end="3.1.2 Component B 중간체 원액 정보")
    scope = ScopedStage.model_validate(dict(sp_stage="Component A 중간체 원액", batch_field="제조번호",
        batch_source=info, fields=[dict(name="제조량", label="제조량", source=info)]))
    targets, audits = read_scoped_targets(SimpleNamespace(pages=pages, records=records), [scope],
        source_file=pdf.name, pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest())
    assert len(targets) == 1 and targets[0].batch == "SKY-CA220705"
    assert len(targets[0].facts) == 1
    fact = targets[0].facts[0]
    assert fact.raw_value == "130 L" and fact.page == 13
    import unicodedata
    assert fact.quote.split() == ["제조량", "130", "L"]
    page = unicodedata.normalize("NFKC", pages[12][1])
    assert page[fact.char_start:fact.char_end] == fact.quote
    assert audits[0]["batch_page"] == 13
    # Extraction wiring only. No invented applicability threshold on the real permit.
