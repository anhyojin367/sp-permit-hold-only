import hashlib

import pytest

from scripts import reviewed_test_fixture as fixture
from sp_pdf_judger.policy_engine import RuleBook


def test_reviewed_fixture_preserves_existing_md_document_pin(tmp_path):
    target = tmp_path / "permit.pdf"
    fixture.copy_reviewed_permit(target)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == fixture.REVIEWED_SHA256
    for name in ("required_test_rules", "test_scope_rules", "batch_field_rules/single", "batch_field_rules/multi"):
        book = RuleBook(fixture.ROOT / "docs/examples" / name)
        assert fixture.REVIEWED_SHA256 in (fixture.ROOT / "docs/examples" / name / "rules.md").read_text(encoding="utf-8")
        assert book.rules


def test_changed_fixture_is_not_silently_accepted(monkeypatch, tmp_path):
    changed = tmp_path / "changed.pdf"
    changed.write_bytes(fixture.REVIEWED_PERMIT.read_bytes() + b"\nchanged")
    monkeypatch.setattr(fixture, "REVIEWED_PERMIT", changed)
    with pytest.raises(ValueError, match="has changed"):
        fixture.copy_reviewed_permit(tmp_path / "copy.pdf")
    assert not (tmp_path / "copy.pdf").exists()


def test_applicability_fixture_keeps_the_reviewed_document_id(tmp_path):
    from scripts.prove_permit_applicability import POLICY
    from sp_pdf_judger.permit_pdf_store import PermitPdfStore
    from sp_pdf_judger.permit_applicability_md import load_applicability_md

    target = tmp_path / "permit.pdf"
    fixture.copy_applicability_permit(target)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == fixture.APPLICABILITY_SHA256
    md = load_applicability_md(fixture.ROOT / "docs/examples/applicability_rules/conditions.md")
    md.bind(PermitPdfStore([target], policy=POLICY), company="합성제조사", product="합성시험제품")
