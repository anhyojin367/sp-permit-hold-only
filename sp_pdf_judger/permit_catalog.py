"""Product-scoped permit catalog and authoritative policy resolution."""
from __future__ import annotations
import hashlib, json, re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DONGKOOK_UNKNOWN_SKY_M_MAPPING_ID = (
    "dongkook_unknown_sky_m_to_sky_covione"
)

@dataclass(frozen=True)
class PermitPolicy:
    policy_id: str
    authoritative: bool
    ignore_section_numbers: bool
    require_llm: bool
    ocr_mode: str
    evaluation_mode: str = "legacy"

@dataclass(frozen=True)
class ResolvedPermit:
    policy: PermitPolicy | None
    paths: tuple[Path, ...]
    fingerprint: str
    errors: tuple[str, ...]
    catalog_valid: bool | None = None
    content_hashes: tuple[tuple[Path, str], ...] = ()

def _normalize(value: str) -> str:
    return "".join(ch for ch in value.casefold() if ch.isalnum())


_SKY_COVIONE_COMPANIES = frozenset(
    _normalize(alias)
    for alias in ("SK바이오사이언스", "에스케이바이오사이언스", "SK bioscience")
)
_SKY_COVIONE_PRODUCTS = frozenset(
    _normalize(alias)
    for alias in ("스카이코비원멀티주", "스카이코비원", "SKYCovione")
)


def _is_sky_covione_target(company: str | None, product: str | None) -> bool:
    if company is None or product is None:
        return False
    return (
        _normalize(company) in _SKY_COVIONE_COMPANIES
        and _normalize(product) in _SKY_COVIONE_PRODUCTS
    )


def _safe_sky_covione_policy(*, policy_id: str) -> PermitPolicy:
    """Keep the exact target fail-closed when catalog metadata is unavailable."""
    return PermitPolicy(
        policy_id=policy_id,
        authoritative=True,
        ignore_section_numbers=True,
        require_llm=True,
        ocr_mode="auto",
        evaluation_mode="deterministic_first",
    )


def _validate_matching_catalog_entry(
    entry: dict[str, Any],
    *,
    company: str | None,
    product: str | None,
) -> dict[str, Any]:
    string_fields = ("policy_id", "path", "sha256", "ocr_mode")
    boolean_fields = (
        "authoritative",
        "ignore_section_numbers",
        "require_llm",
    )
    if any(
        not isinstance(entry.get(field), str) or not entry[field].strip()
        for field in string_fields
    ):
        raise ValueError("invalid catalog entry")
    if any(type(entry.get(field)) is not bool for field in boolean_fields):
        raise ValueError("invalid catalog entry")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", entry["sha256"]):
        raise ValueError("invalid catalog entry")
    evaluation_mode = entry.get("evaluation_mode", "legacy")
    if (
        not isinstance(evaluation_mode, str)
        or evaluation_mode not in {"legacy", "deterministic_first"}
    ):
        raise ValueError("invalid catalog entry")
    if _is_sky_covione_target(company, product) and (
        evaluation_mode != "deterministic_first"
        or not entry["authoritative"]
        or not entry["ignore_section_numbers"]
        or not entry["require_llm"]
    ):
        raise ValueError("invalid target catalog entry")
    return entry

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _matching_catalog_entry(
    root: Path,
    company: str | None,
    product: str | None,
) -> dict[str, Any] | None:
    """Return only an exact normalized company/product catalog match."""
    catalog_path = root / "permits" / "catalog.json"
    if not catalog_path.is_file() or company is None or product is None:
        return None
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or not isinstance(catalog.get("permits"), list):
        raise ValueError("invalid catalog")
    company_key, product_key = _normalize(company), _normalize(product)
    for candidate in catalog["permits"]:
        if not isinstance(candidate, dict):
            continue
        companies = candidate.get("companies")
        products = candidate.get("products")
        if not isinstance(companies, list) or not isinstance(products, list):
            continue
        if (
            company_key
            in {_normalize(alias) for alias in companies if isinstance(alias, str)}
            and product_key
            in {_normalize(alias) for alias in products if isinstance(alias, str)}
        ):
            return _validate_matching_catalog_entry(
                candidate,
                company=company,
                product=product,
            )
    return None


def permit_hash_map(resolution: ResolvedPermit) -> dict[Path, str]:
    """Return canonical permit paths keyed to their validated content digests."""
    hashes: dict[Path, str] = {}
    for path, digest in resolution.content_hashes:
        canonical_path = Path(path).resolve()
        existing = hashes.get(canonical_path)
        if existing is not None and existing != digest:
            raise ValueError(f"conflicting content hashes for {canonical_path.name}")
        hashes[canonical_path] = digest
    return hashes

def _fingerprint(entry: dict[str, Any] | None, paths: tuple[Path, ...], hashes: tuple[str, ...], catalog_valid: bool | None, catalog_hash: str | None) -> str:
    payload = {"entry": entry, "paths": [str(path) for path in paths], "hashes": list(hashes), "catalog_valid": catalog_valid, "catalog_hash": catalog_hash}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()

def resolve_permits(explicit_paths: Iterable[Path], company: str | None, product: str | None, package_dir: Path | None = None) -> ResolvedPermit:
    explicit: list[Path] = []
    seen: set[Path] = set()
    errors: list[str] = []
    for raw_path in explicit_paths:
        path = Path(raw_path).resolve()
        if path in seen:
            continue
        seen.add(path)
        if path.is_file():
            explicit.append(path)
        else:
            errors.append(f"explicit permit missing: {path.name}")
    root = (Path(package_dir) if package_dir is not None else Path(__file__).parent).resolve()
    entry: dict[str, Any] | None = None
    policy: PermitPolicy | None = None
    try:
        entry = _matching_catalog_entry(root, company, product)
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError, KeyError, ValueError) as exc:
        errors.append(f"catalog error: {type(exc).__name__}")
    resolved = list(explicit)
    catalog_valid: bool | None = None
    catalog_hash: str | None = None
    known_hashes: dict[Path, str] = {}
    if entry is not None:
        policy = PermitPolicy(
            policy_id=str(entry["policy_id"]),
            authoritative=bool(entry["authoritative"]),
            ignore_section_numbers=bool(entry["ignore_section_numbers"]),
            require_llm=bool(entry["require_llm"]),
            ocr_mode=str(entry["ocr_mode"]),
            evaluation_mode=str(entry.get("evaluation_mode", "legacy")),
        )
        catalog_file = (root / str(entry["path"])).resolve()
        catalog_valid = False
        if not catalog_file.is_file():
            errors.append(f"catalog file missing: {catalog_file.name}")
        else:
            try:
                actual_hash = _sha256(catalog_file)
            except OSError:
                errors.append(f"catalog file unreadable: {catalog_file.name}")
            else:
                catalog_hash = actual_hash
                known_hashes[catalog_file] = actual_hash
                if actual_hash.casefold() != str(entry.get("sha256", "")).casefold():
                    errors.append(f"catalog sha256 mismatch for {catalog_file.name}")
                else:
                    catalog_valid = True
                    if catalog_file not in seen:
                        resolved.append(catalog_file)
        if not catalog_valid:
            resolved = [path for path in resolved if path != catalog_file]
    elif _is_sky_covione_target(company, product):
        policy = _safe_sky_covione_policy(
            policy_id="sk_bioscience_sky_covione_multidose"
        )
        catalog_valid = False
        if not errors:
            errors.append("catalog target unavailable")
    valid_paths: list[Path] = []
    content_hashes: list[tuple[Path, str]] = []
    for path in resolved:
        digest = known_hashes.get(path)
        if digest is None:
            try:
                digest = _sha256(path)
            except OSError:
                errors.append(f"permit file unreadable: {path.name}")
                continue
        valid_paths.append(path)
        content_hashes.append((path, digest))
    paths = tuple(valid_paths)
    hashes = tuple(digest for _, digest in content_hashes)
    return ResolvedPermit(
        policy=policy,
        paths=paths,
        fingerprint=_fingerprint(entry, paths, hashes, catalog_valid, catalog_hash),
        errors=tuple(errors),
        catalog_valid=catalog_valid,
        content_hashes=tuple(content_hashes),
    )


def resolve_submission_permits(
    explicit_paths: Iterable[Path],
    company: str | None,
    product: str | None,
    package_dir: Path | None = None,
    *,
    product_mapping_id: str = "",
) -> ResolvedPermit:
    """An explicitly linked permit replaces catalog defaults, never mixes with them."""
    paths = tuple(dict.fromkeys(Path(p).resolve() for p in explicit_paths))
    if not paths:
        return resolve_permits([], company, product, package_dir)
    root = (Path(package_dir) if package_dir is not None else Path(__file__).parent).resolve()
    catalog_errors: list[str] = []
    path_errors: list[str] = []
    target_entry: dict[str, Any] | None = None
    try:
        target_entry = _matching_catalog_entry(root, company, product)
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError, KeyError, ValueError) as exc:
        catalog_errors.append(f"catalog error: {type(exc).__name__}")
    product_mapping_id = str(product_mapping_id or "").strip()
    mapped_target = product_mapping_id == DONGKOOK_UNKNOWN_SKY_M_MAPPING_ID
    target_fallback = target_entry is None and _is_sky_covione_target(
        company, product
    )
    content_hashes: list[tuple[Path, str]] = []
    for path in paths:
        if not path.is_file():
            path_errors.append(f"linked permit missing: {path.name}")
            continue
        try:
            content_hashes.append((path, _sha256(path)))
        except OSError:
            path_errors.append(f"linked permit unreadable: {path.name}")
    valid = tuple(path for path, _ in content_hashes)
    policy = (
        _safe_sky_covione_policy(policy_id="submission-linked")
        if target_fallback or mapped_target
        else PermitPolicy(
            policy_id="submission-linked",
            authoritative=True,
            ignore_section_numbers=bool(
                target_entry.get("ignore_section_numbers", True)
                if target_entry is not None
                else True
            ),
            require_llm=bool(
                target_entry.get("require_llm", True)
                if target_entry is not None
                else True
            ),
            ocr_mode=str(
                target_entry.get("ocr_mode", "auto")
                if target_entry is not None
                else "auto"
            ),
            evaluation_mode=str(
                target_entry.get("evaluation_mode", "legacy")
                if target_entry is not None
                # Linking a PDF does not opt an existing/unseen product into
                # experimental condition execution. Preserve authoritative
                # semantic review unless an exact policy selected grouping.
                else "legacy"
            ),
        )
    )
    entry = {
        "policy_id": policy.policy_id,
        "authoritative": policy.authoritative,
        "ignore_section_numbers": policy.ignore_section_numbers,
        "require_llm": policy.require_llm,
        "ocr_mode": policy.ocr_mode,
        "evaluation_mode": policy.evaluation_mode,
        "company": company,
        "product": product,
        "product_mapping_id": product_mapping_id,
    }
    hashes_by_path = dict(content_hashes)
    effective_catalog_valid = False if path_errors or target_fallback else None
    fingerprint = _fingerprint(
        entry,
        paths,
        tuple(hashes_by_path.get(path, "missing") for path in paths),
        effective_catalog_valid,
        None,
    )
    return ResolvedPermit(
        policy=policy,
        paths=valid,
        fingerprint=fingerprint,
        errors=tuple([*catalog_errors, *path_errors]),
        catalog_valid=effective_catalog_valid,
        content_hashes=tuple(content_hashes),
    )
