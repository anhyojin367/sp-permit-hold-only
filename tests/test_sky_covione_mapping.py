from pathlib import Path

import pytest

import sp_app as app
import sp_pdf_judger.permit_catalog as permit_catalog
from sp_app import (
    InboxDocument,
    ReferenceDocument,
    _map_unknown_sky_covione_product,
    _version_gate,
)
from sp_pdf_judger.permit_catalog import resolve_permits


@pytest.mark.parametrize(
    ("company", "product", "number"),
    [
        ("다른회사", "제품명 미확인", "SKY-M260001"),
        ("동국바이오사이언스", "다른제품", "SKY-M260001"),
        ("동국바이오사이언스", "제품명 미확인", "OTHER-260001"),
    ],
)
def test_unknown_product_mapping_does_not_change_other_cases(company, product, number):
    assert _map_unknown_sky_covione_product(company, product, number) == product


def test_unknown_dongkook_sky_m_product_maps_to_sky_covione():
    assert (
        _map_unknown_sky_covione_product(
            "동국바이오사이언스", "제품명 미확인", "SKY-M260001"
        )
        == "스카이코비원멀티주"
    )


def test_exact_dongkook_unknown_sky_m_mapping_records_provenance():
    assert app._resolve_product_mapping(
        "동국바이오사이언스", "제품명 미확인", "SKY-M260001"
    ) == (
        "스카이코비원멀티주",
        permit_catalog.DONGKOOK_UNKNOWN_SKY_M_MAPPING_ID,
    )


@pytest.mark.parametrize(
    ("company", "product", "number"),
    [
        ("다른회사", "제품명 미확인", "SKY-M260001"),
        ("동국바이오사이언스", "기존제품", "SKY-M260001"),
        ("동국바이오사이언스", "제품명 미확인", "OTHER-260001"),
    ],
)
def test_product_mapping_provenance_is_empty_for_other_cases(
    company, product, number
):
    assert app._resolve_product_mapping(company, product, number) == (product, "")


def test_unknown_dongkook_sky_m_mapping_does_not_change_company_policy_scope():
    product = _map_unknown_sky_covione_product(
        "동국바이오사이언스",
        "제품명 미확인",
        "SKY-M260001",
    )

    resolution = resolve_permits([], "동국바이오사이언스", product)

    assert resolution.policy is None
    assert resolution.paths == ()


def _document(*, company="동국바이오사이언스", product="스카이코비원멀티주", version="v1.2"):
    return InboxDocument(
        path=Path("submission.pdf"),
        company=company,
        product=product,
        title="제출 SP",
        product_number="SKY-M260001",
        version=version,
        received_date="2026.08.26",
        subject="",
    )


def test_inbox_document_old_constructor_defaults_mapping_id_to_empty():
    assert _document().product_mapping_id == ""


def _reference(*, company="동국바이오사이언스", product="스카이코비원멀티주", version="v8.0"):
    return ReferenceDocument(
        company=company,
        product=product,
        title="기준 SP",
        doc_id="REF-1",
        version=version,
        effective_date="2026.05.10",
        status="적용중",
        pages=1,
        checks=1,
        owner="백신검정과",
    )


def test_non_4_0_version_is_flagged_but_preserves_submission_and_reference():
    doc = _document(version="v1.2")
    ref = _reference(version="v8.0")

    result = _version_gate(doc, [ref])

    assert result["ok"] is False
    assert result["matched"] is ref
    assert doc.version == "v1.2"
    assert "불일치" in result["reason"]


def test_sky_covione_exception_does_not_cross_company_boundary():
    result = _version_gate(
        _document(company="SK바이오사이언스"),
        [_reference(company="SK바이오사이언스")],
    )
    assert result["ok"] is False


def test_other_products_also_use_temporary_4_0_baseline():
    doc = _document(product="다른제품", version="v4.0")
    ref = _reference(product="다른제품", version="v8.0")

    result = _version_gate(doc, [ref])

    assert result["ok"] is True
    assert result["matched"] is ref


def test_missing_version_is_flagged_without_losing_matching_reference():
    doc = _document(version="")
    ref = _reference()

    result = _version_gate(doc, [ref])

    assert result["ok"] is False
    assert result["matched"] is ref
    assert "검수는 계속 진행" in result["reason"]
