import hashlib
import json
from pathlib import Path

import pytest

import sp_pdf_judger.permit_catalog as permit_catalog
from sp_pdf_judger.permit_catalog import (
    PermitPolicy,
    ResolvedPermit,
    permit_hash_map,
    resolve_permits,
    resolve_submission_permits,
)


def _catalog(
    tmp_path: Path,
    *,
    sha256: str,
    companies: list[str] | None = None,
    products: list[str] | None = None,
    evaluation_mode: str = "deterministic_first",
) -> Path:
    package = tmp_path / "package"
    (package / "permits" / "documents").mkdir(parents=True)
    (package / "permits" / "documents" / "fixture.pdf").write_bytes(b"permit")
    (package / "permits" / "catalog.json").write_text(
        json.dumps(
            {"permits": [{"policy_id": "fixture_policy", "companies": companies or ["SK바이오사이언스", "에스케이바이오사이언스", "SK bioscience"], "products": products or ["스카이코비원멀티주", "스카이코비원", "SKYCovione"], "path": "permits/documents/fixture.pdf", "sha256": sha256, "authoritative": True, "ignore_section_numbers": True, "require_llm": True, "ocr_mode": "auto", "evaluation_mode": evaluation_mode}]},
            ensure_ascii=False,
        ), encoding="utf-8"
    )
    return package


def test_aliases_resolve_authoritative_catalog_file(tmp_path):
    package = _catalog(tmp_path, sha256="85441a43c7cb9f242a5c20c6648026689c795ad4e8321e215bf68d48fc9a4b1c")
    result = resolve_permits([], " sk-바이오사이언스 ", "스카이코비원 멀티주", package)
    assert result.policy.policy_id == "fixture_policy"
    assert result.policy.authoritative is True
    assert result.paths == (package / "permits" / "documents" / "fixture.pdf",)
    assert result.errors == ()
    assert result.fingerprint


def test_literal_fixture_hash_is_the_expected_sha256():
    assert hashlib.sha256(b"permit").hexdigest() == "85441a43c7cb9f242a5c20c6648026689c795ad4e8321e215bf68d48fc9a4b1c"


def test_company_alias_boundary_does_not_resolve_catalog(tmp_path):
    package = _catalog(tmp_path, sha256="85441a43c7cb9f242a5c20c6648026689c795ad4e8321e215bf68d48fc9a4b1c")
    result = resolve_permits([], "동국바이오사이언스", "스카이코비원멀티주", package)
    assert result.policy is None
    assert result.paths == ()


def test_product_alias_boundary_does_not_resolve_catalog(tmp_path):
    package = _catalog(tmp_path, sha256="85441a43c7cb9f242a5c20c6648026689c795ad4e8321e215bf68d48fc9a4b1c")
    result = resolve_permits([], "SK바이오사이언스", "다른제품", package)
    assert result.policy is None
    assert result.paths == ()


def test_hash_mismatch_excludes_catalog_file_but_retains_policy(tmp_path):
    package = _catalog(tmp_path, sha256="0" * 64)
    result = resolve_permits([], "SK bioscience", "SKYCovione", package)
    assert result.policy is not None and result.policy.authoritative
    assert result.catalog_valid is False
    assert result.paths == ()
    assert any("sha256" in error for error in result.errors)


def test_missing_catalog_file_excludes_path_but_retains_policy(tmp_path):
    package = _catalog(tmp_path, sha256="85441a43c7cb9f242a5c20c6648026689c795ad4e8321e215bf68d48fc9a4b1c")
    (package / "permits" / "documents" / "fixture.pdf").unlink()

    result = resolve_permits([], "SK bioscience", "SKYCovione", package)

    assert result.policy is not None and result.policy.authoritative
    assert result.catalog_valid is False
    assert result.paths == ()
    assert any("missing" in error for error in result.errors)


def test_invalid_catalog_file_cannot_reenter_through_explicit_path(tmp_path):
    package = _catalog(tmp_path, sha256="0" * 64)
    catalog_file = package / "permits" / "documents" / "fixture.pdf"

    result = resolve_permits([catalog_file], "SK bioscience", "SKYCovione", package)

    assert result.paths == ()
    assert result.catalog_valid is False


def test_catalog_validity_changes_fingerprint(tmp_path):
    package = _catalog(tmp_path, sha256="0" * 64)
    invalid = resolve_permits([], "SK bioscience", "SKYCovione", package)
    catalog_file = package / "permits" / "documents" / "fixture.pdf"
    catalog_file.write_bytes(b"changed invalid content")
    changed_invalid = resolve_permits([], "SK bioscience", "SKYCovione", package)

    assert invalid.fingerprint != changed_invalid.fingerprint


def test_explicit_paths_are_deduplicated_before_catalog_path(tmp_path):
    package = _catalog(tmp_path, sha256="85441a43c7cb9f242a5c20c6648026689c795ad4e8321e215bf68d48fc9a4b1c")
    first = tmp_path / "explicit.pdf"
    first.write_bytes(b"explicit")
    result = resolve_permits([first, first], "SK바이오사이언스", "스카이코비원", package)
    assert result.paths == (first, package / "permits" / "documents" / "fixture.pdf")


def test_equivalent_explicit_path_spellings_deduplicate_to_one_resolved_path(tmp_path):
    first = tmp_path / "nested" / "explicit.pdf"
    first.parent.mkdir()
    first.write_bytes(b"explicit")
    equivalent = first.parent / ".." / "nested" / first.name

    result = resolve_permits([equivalent, first], None, None, tmp_path)

    assert result.paths == (first.resolve(),)


def test_explicit_catalog_document_still_produces_authoritative_policy(tmp_path):
    package = _catalog(tmp_path, sha256="85441a43c7cb9f242a5c20c6648026689c795ad4e8321e215bf68d48fc9a4b1c")
    catalog_file = package / "permits" / "documents" / "fixture.pdf"

    result = resolve_permits([catalog_file], "SK바이오사이언스", "스카이코비원", package)

    assert result.policy is not None
    assert result.policy.authoritative is True
    assert result.paths == (catalog_file.resolve(),)


def test_five_argument_policy_defaults_to_legacy_evaluation_mode():
    """Catch new policy modes breaking callers that use the original constructor."""
    policy = PermitPolicy("fixture", True, True, True, "auto")

    assert policy.evaluation_mode == "legacy"


def test_non_target_catalog_policies_remain_legacy(tmp_path):
    """Catch an accidental default-mode flip while activating SkyCovione only."""
    catalog_path = Path(permit_catalog.__file__).parent / "permits" / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    target_policy_id = "sk_bioscience_sky_covione_multidose"

    other_policies = [
        entry
        for entry in catalog["permits"]
        if entry["policy_id"] != target_policy_id
    ]

    assert all(
        resolve_permits([], entry["companies"][0], entry["products"][0]).policy.evaluation_mode
        == "legacy"
        for entry in other_policies
    )
    # The shipped catalog currently contains only the target. Also exercise a
    # valid non-target entry so this default-mode check is never vacuous.
    package = _catalog(
        tmp_path,
        sha256="85441a43c7cb9f242a5c20c6648026689c795ad4e8321e215bf68d48fc9a4b1c",
        companies=["Other Biopharma"],
        products=["Other Vaccine"],
        evaluation_mode="legacy",
    )
    result = resolve_permits([], "Other Biopharma", "Other Vaccine", package)
    assert result.catalog_valid is True
    assert result.policy.policy_id == "fixture_policy"
    assert result.policy.evaluation_mode == "legacy"


def test_non_target_catalog_without_evaluation_mode_keeps_legacy_default(tmp_path):
    package = _catalog(
        tmp_path,
        sha256="85441a43c7cb9f242a5c20c6648026689c795ad4e8321e215bf68d48fc9a4b1c",
        companies=["Existing Biopharma"],
        products=["Existing Product"],
        evaluation_mode="legacy",
    )
    catalog_path = package / "permits" / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["permits"][0].pop("evaluation_mode")
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    result = resolve_permits(
        [],
        "Existing Biopharma",
        "Existing Product",
        package,
    )

    assert result.policy is not None
    assert result.policy.policy_id == "fixture_policy"
    assert result.policy.evaluation_mode == "legacy"
    assert result.catalog_valid is True


def test_shipped_dongkook_sky_covione_does_not_widen_sk_policy_scope():
    """Product mapping must not turn a different company into the SK target."""
    result = resolve_permits(
        [],
        "동국바이오사이언스",
        "스카이코비원멀티주",
    )

    assert result.policy is None
    assert result.paths == ()


def test_linked_upload_preserves_an_explicit_existing_legacy_catalog_policy(tmp_path):
    package = _catalog(tmp_path, sha256=hashlib.sha256(b"permit").hexdigest(),
                       companies=["Existing"], products=["Existing"], evaluation_mode="legacy")
    uploaded = tmp_path / "uploaded.pdf"
    uploaded.write_bytes(b"new linked permit")
    result = resolve_submission_permits([uploaded], "Existing", "Existing", package)
    assert result.policy.evaluation_mode == "legacy"
    assert result.paths == (uploaded.resolve(),)


def test_named_product_upload_keeps_existing_authoritative_legacy_review(tmp_path):
    """Ordinary upload must not silently opt into experimental condition execution."""
    linked = tmp_path / "uploaded-permit.pdf"
    linked.write_bytes(b"linked permit")

    result = resolve_submission_permits(
        [linked],
        "동국바이오사이언스",
        "스카이코비원멀티주",
    )

    assert result.paths == (linked.resolve(),)
    assert result.policy is not None
    assert result.policy.policy_id == "submission-linked"
    assert result.policy.evaluation_mode == "legacy"
    assert resolve_permits([], "동국바이오사이언스", "스카이코비원멀티주").policy is None


def test_mapped_submission_link_uses_safe_deterministic_policy_only_with_exact_id(
    tmp_path,
):
    linked = tmp_path / "mapped-permit.pdf"
    linked.write_bytes(b"mapped linked permit")

    result = resolve_submission_permits(
        [linked],
        "동국바이오사이언스",
        "스카이코비원멀티주",
        product_mapping_id=permit_catalog.DONGKOOK_UNKNOWN_SKY_M_MAPPING_ID,
    )

    assert result.paths == (linked.resolve(),)
    assert result.policy == PermitPolicy(
        policy_id="submission-linked",
        authoritative=True,
        ignore_section_numbers=True,
        require_llm=True,
        ocr_mode="auto",
        evaluation_mode="deterministic_first",
    )


@pytest.mark.parametrize("mapping_id", ["", "unknown_mapping"])
def test_unknown_mapping_id_keeps_dongkook_submission_legacy(tmp_path, mapping_id):
    linked = tmp_path / f"legacy-{mapping_id or 'empty'}.pdf"
    linked.write_bytes(b"legacy linked permit")

    result = resolve_submission_permits(
        [linked],
        "동국바이오사이언스",
        "스카이코비원멀티주",
        product_mapping_id=mapping_id,
    )

    assert result.policy is not None
    assert result.policy.evaluation_mode == "legacy"


def test_submission_fingerprint_covers_effective_policy_and_mapping_id(tmp_path):
    linked = tmp_path / "same-permit.pdf"
    linked.write_bytes(b"same linked permit")

    legacy = resolve_submission_permits(
        [linked], "동국바이오사이언스", "스카이코비원멀티주"
    )
    mapped = resolve_submission_permits(
        [linked],
        "동국바이오사이언스",
        "스카이코비원멀티주",
        product_mapping_id=permit_catalog.DONGKOOK_UNKNOWN_SKY_M_MAPPING_ID,
    )

    assert legacy.fingerprint != mapped.fingerprint
    assert mapped.fingerprint == resolve_submission_permits(
        [linked],
        "동국바이오사이언스",
        "스카이코비원멀티주",
        product_mapping_id=permit_catalog.DONGKOOK_UNKNOWN_SKY_M_MAPPING_ID,
    ).fingerprint


def test_submission_linked_sk_sky_covione_inherits_grouped_mode(tmp_path):
    linked = tmp_path / "uploaded-target-permit.pdf"
    linked.write_bytes(b"linked target permit")

    result = resolve_submission_permits(
        [linked],
        "SK바이오사이언스",
        "스카이코비원멀티주",
    )

    assert result.paths == (linked.resolve(),)
    assert result.policy is not None
    assert result.policy.evaluation_mode == "deterministic_first"


@pytest.mark.parametrize("company,product", [("처음보는회사", "처음보는제품"), (None, None)])
def test_submission_linked_unseen_product_keeps_authoritative_legacy(tmp_path, company, product):
    linked = tmp_path / "other-permit.pdf"
    linked.write_bytes(b"linked permit")

    result = resolve_submission_permits(
        [linked],
        company,
        product,
    )

    assert result.policy is not None
    assert result.policy.evaluation_mode == "legacy"
    assert result.policy.authoritative is True


def test_submission_linked_non_target_catalog_parse_error_does_not_fail_closed(
    tmp_path,
):
    package = tmp_path / "package"
    (package / "permits").mkdir(parents=True)
    (package / "permits" / "catalog.json").write_text(
        "{invalid json",
        encoding="utf-8",
    )
    linked = tmp_path / "linked.pdf"
    linked.write_bytes(b"linked permit")

    result = resolve_submission_permits(
        [linked],
        "다른회사",
        "다른제품",
        package,
    )

    assert result.policy is not None
    assert result.policy.evaluation_mode == "legacy"
    assert result.paths == (linked.resolve(),)
    assert result.catalog_valid is None


def test_target_catalog_parse_error_keeps_authoritative_grouped_policy_fail_closed(
    tmp_path,
):
    package = tmp_path / "package"
    (package / "permits").mkdir(parents=True)
    (package / "permits" / "catalog.json").write_text(
        "{invalid json",
        encoding="utf-8",
    )

    result = resolve_permits(
        [],
        "SK바이오사이언스",
        "스카이코비원멀티주",
        package,
    )

    assert result.policy is not None
    assert result.policy.authoritative is True
    assert result.policy.evaluation_mode == "deterministic_first"
    assert result.catalog_valid is False
    assert result.paths == ()
    assert any("catalog error" in error for error in result.errors)


def test_submission_linked_target_catalog_parse_error_keeps_grouped_policy_fail_closed(
    tmp_path,
):
    package = tmp_path / "package"
    (package / "permits").mkdir(parents=True)
    (package / "permits" / "catalog.json").write_text(
        "{invalid json",
        encoding="utf-8",
    )
    linked = tmp_path / "linked.pdf"
    linked.write_bytes(b"linked permit")

    result = resolve_submission_permits(
        [linked],
        "SK바이오사이언스",
        "스카이코비원멀티주",
        package,
    )

    assert result.policy is not None
    assert result.policy.authoritative is True
    assert result.policy.evaluation_mode == "deterministic_first"
    assert result.catalog_valid is False
    assert result.paths == (linked.resolve(),)
    assert any("catalog error" in error for error in result.errors)


def test_submission_target_catalog_failure_changes_resolution_fingerprint(tmp_path):
    package = _catalog(
        tmp_path,
        sha256="85441a43c7cb9f242a5c20c6648026689c795ad4e8321e215bf68d48fc9a4b1c",
    )
    linked = tmp_path / "linked.pdf"
    linked.write_bytes(b"linked permit")
    trusted = resolve_submission_permits(
        [linked],
        "SK바이오사이언스",
        "스카이코비원멀티주",
        package,
    )
    (package / "permits" / "catalog.json").write_text(
        "{invalid json",
        encoding="utf-8",
    )

    rejected = resolve_submission_permits(
        [linked],
        "SK바이오사이언스",
        "스카이코비원멀티주",
        package,
    )

    assert trusted.catalog_valid is None
    assert rejected.catalog_valid is False
    assert rejected.fingerprint != trusted.fingerprint


@pytest.mark.parametrize("linked", [False, True])
def test_malformed_matching_target_entry_keeps_grouped_policy_fail_closed(
    tmp_path,
    linked,
):
    package = tmp_path / "package"
    (package / "permits").mkdir(parents=True)
    (package / "permits" / "catalog.json").write_text(
        json.dumps(
            {
                "permits": [
                    {
                        "companies": ["SK바이오사이언스"],
                        "products": ["스카이코비원멀티주"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    uploaded = tmp_path / "linked.pdf"
    uploaded.write_bytes(b"linked permit")
    resolver = resolve_submission_permits if linked else resolve_permits

    result = resolver(
        [uploaded] if linked else [],
        "SK바이오사이언스",
        "스카이코비원멀티주",
        package,
    )

    assert result.policy is not None
    assert result.policy.authoritative is True
    assert result.policy.evaluation_mode == "deterministic_first"
    assert result.catalog_valid is False
    assert result.paths == ((uploaded.resolve(),) if linked else ())
    assert any("catalog error" in error for error in result.errors)


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("evaluation_mode", "__missing__"),
        ("evaluation_mode", "legacy"),
        ("evaluation_mode", 123),
        ("authoritative", "false"),
    ],
)
def test_malformed_shipped_target_policy_cannot_weaken_grouped_mode(
    tmp_path,
    linked,
    field,
    bad_value,
):
    package = tmp_path / "package"
    documents = package / "permits" / "documents"
    documents.mkdir(parents=True)
    catalog_pdf = documents / "fixture.pdf"
    catalog_pdf.write_bytes(b"permit")
    entry = {
        "policy_id": "sk_bioscience_sky_covione_multidose",
        "companies": ["SK바이오사이언스"],
        "products": ["스카이코비원멀티주"],
        "path": "permits/documents/fixture.pdf",
        "sha256": hashlib.sha256(b"permit").hexdigest(),
        "authoritative": True,
        "ignore_section_numbers": True,
        "require_llm": True,
        "ocr_mode": "auto",
        "evaluation_mode": "deterministic_first",
    }
    if bad_value == "__missing__":
        entry.pop(field)
    else:
        entry[field] = bad_value
    (package / "permits" / "catalog.json").write_text(
        json.dumps({"permits": [entry]}, ensure_ascii=False),
        encoding="utf-8",
    )
    uploaded = tmp_path / "linked.pdf"
    uploaded.write_bytes(b"linked permit")
    resolver = resolve_submission_permits if linked else resolve_permits

    result = resolver(
        [uploaded] if linked else [],
        "SK바이오사이언스",
        "스카이코비원멀티주",
        package,
    )

    assert result.policy is not None
    assert result.policy.authoritative is True
    assert result.policy.evaluation_mode == "deterministic_first"
    assert result.catalog_valid is False
    assert result.paths == ((uploaded.resolve(),) if linked else ())


def test_resolution_exposes_one_precomputed_hash_per_canonical_valid_path_in_order(tmp_path):
    """Catch path/hash drift or duplicate hashes after canonical resolution."""
    package = _catalog(tmp_path, sha256="85441a43c7cb9f242a5c20c6648026689c795ad4e8321e215bf68d48fc9a4b1c")
    explicit = tmp_path / "explicit.pdf"
    explicit.write_bytes(b"explicit")
    catalog_file = package / "permits" / "documents" / "fixture.pdf"

    result = resolve_permits([explicit, explicit], "SK bioscience", "SKYCovione", package)

    expected_paths = (explicit.resolve(), catalog_file.resolve())
    expected_hashes = (
        hashlib.sha256(b"explicit").hexdigest(),
        hashlib.sha256(b"permit").hexdigest(),
    )
    assert result.paths == expected_paths
    assert result.content_hashes == tuple(zip(expected_paths, expected_hashes))
    assert permit_hash_map(result) == dict(zip(expected_paths, expected_hashes))


def test_resolution_hashes_each_valid_path_once(monkeypatch, tmp_path):
    """Catch re-reading the catalog permit to separately build its hash tuple."""
    package = _catalog(tmp_path, sha256="85441a43c7cb9f242a5c20c6648026689c795ad4e8321e215bf68d48fc9a4b1c")
    explicit = tmp_path / "explicit.pdf"
    explicit.write_bytes(b"explicit")
    catalog_file = (package / "permits" / "documents" / "fixture.pdf").resolve()
    calls: list[Path] = []
    real_sha256 = permit_catalog._sha256

    def count_hashes(path: Path) -> str:
        calls.append(path.resolve())
        return real_sha256(path)

    monkeypatch.setattr(permit_catalog, "_sha256", count_hashes)

    result = resolve_permits([explicit, catalog_file], "SK bioscience", "SKYCovione", package)

    assert result.paths == (explicit.resolve(), catalog_file)
    assert calls.count(explicit.resolve()) == 1
    assert calls.count(catalog_file) == 1


def test_invalid_explicit_path_is_excluded_from_hashes_with_a_safe_error(tmp_path):
    """Catch missing permits leaking into the hash contract without diagnostics."""
    valid = tmp_path / "valid.pdf"
    missing = tmp_path / "missing.pdf"
    valid.write_bytes(b"valid")

    result = resolve_permits([valid, missing], None, None, tmp_path)

    assert result.paths == (valid.resolve(),)
    assert result.content_hashes == ((valid.resolve(), hashlib.sha256(b"valid").hexdigest()),)
    assert any("missing" in error for error in result.errors)


def test_submission_resolution_uses_precomputed_hashes_and_excludes_missing_paths(tmp_path):
    """Catch linked-permit resolutions diverging from the common hash contract."""
    valid = tmp_path / "valid.pdf"
    missing = tmp_path / "missing.pdf"
    valid.write_bytes(b"valid")

    result = resolve_submission_permits([valid, missing], "company", "product", tmp_path)

    assert result.paths == (valid.resolve(),)
    assert result.content_hashes == ((valid.resolve(), hashlib.sha256(b"valid").hexdigest()),)
    assert any("missing" in error for error in result.errors)


def test_permit_hash_map_rejects_conflicting_canonical_duplicates(tmp_path):
    """Catch a later duplicate silently overwriting a previously trusted digest."""
    path = tmp_path / "permit.pdf"
    path.write_bytes(b"permit")
    equivalent = path.parent / "." / path.name
    resolution = ResolvedPermit(
        policy=None,
        paths=(),
        fingerprint="fixture",
        errors=(),
        content_hashes=((path, "a" * 64), (equivalent, "b" * 64)),
    )

    with pytest.raises(ValueError, match="conflicting content hashes"):
        permit_hash_map(resolution)


@pytest.mark.parametrize("resolver", [resolve_permits, resolve_submission_permits])
def test_unreadable_permit_is_omitted_with_safe_error(resolver, monkeypatch, tmp_path):
    """Catch exposing OS diagnostics or retaining unreadable paths as trusted."""
    valid = tmp_path / "valid.pdf"
    rejected = tmp_path / "private" / "unreadable.pdf"
    rejected.parent.mkdir()
    valid.write_bytes(b"valid")
    rejected.write_bytes(b"unreadable")
    real_open = Path.open

    def fail_rejected_read(path, *args, **kwargs):
        if path == rejected:
            raise PermissionError(f"sensitive OS diagnostic: {rejected}")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_rejected_read)
    result = resolver([valid, rejected], None, None, tmp_path)

    assert result.paths == (valid.resolve(),)
    assert result.content_hashes == ((valid.resolve(), hashlib.sha256(b"valid").hexdigest()),)
    assert len(result.errors) == 1
    assert "unreadable.pdf" in result.errors[0]
    assert str(tmp_path) not in result.errors[0]
    assert "sensitive OS diagnostic" not in result.errors[0]


@pytest.mark.parametrize("failure", ["missing", "mismatch", "unreadable"])
def test_rejected_catalog_permit_has_no_digest_and_safe_error(failure, monkeypatch, tmp_path):
    """Catch rejected catalog content entering the trusted hashes or leaking paths."""
    package = _catalog(tmp_path, sha256="0" * 64)
    rejected = package / "permits" / "documents" / "fixture.pdf"
    if failure == "missing":
        rejected.unlink()
    elif failure == "unreadable":
        real_open = Path.open

        def fail_rejected_read(path, *args, **kwargs):
            if path == rejected:
                raise PermissionError(f"sensitive OS diagnostic: {rejected}")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_rejected_read)

    result = resolve_permits([rejected], "SK bioscience", "SKYCovione", package)

    assert result.catalog_valid is False
    assert result.paths == ()
    assert result.content_hashes == ()
    assert result.errors
    assert all(str(tmp_path) not in error for error in result.errors)
    assert all("sensitive OS diagnostic" not in error for error in result.errors)


def test_submission_reads_each_canonical_path_once_in_input_order(monkeypatch, tmp_path):
    """Catch duplicate linked-permit reads or path/hash order changes."""
    first, second = tmp_path / "first.pdf", tmp_path / "second.pdf"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    equivalent = tmp_path / ".." / tmp_path.name / first.name
    reads = []
    real_open = Path.open

    def count_reads(path, *args, **kwargs):
        if args and args[0] == "rb":
            reads.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", count_reads)
    result = resolve_submission_permits([first, second, equivalent], "company", "product", tmp_path)

    assert reads == [first.resolve(), second.resolve()]
    assert result.paths == (first.resolve(), second.resolve())
    assert result.content_hashes == (
        (first.resolve(), hashlib.sha256(b"first").hexdigest()),
        (second.resolve(), hashlib.sha256(b"second").hexdigest()),
    )
    assert result.policy.evaluation_mode == "legacy"


def test_permit_hash_map_accepts_identical_canonical_duplicates(tmp_path):
    """Catch rejecting harmless duplicate spellings or returning noncanonical keys."""
    path = tmp_path / "permit.pdf"
    equivalent = tmp_path / ".." / tmp_path.name / path.name
    resolution = ResolvedPermit(None, (), "fixture", (), content_hashes=((path, "a" * 64), (equivalent, "a" * 64)))

    assert permit_hash_map(resolution) == {path.resolve(): "a" * 64}
