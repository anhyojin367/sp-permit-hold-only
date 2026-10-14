"""Content-addressed, process-safe permit evidence snapshots.

The index stores only immutable permit evidence and parser output.  It never
stores SP submissions, provider data, credentials, or absolute source paths.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from time import monotonic
from typing import Callable

from sp_process_lock import ReviewLockTimeout, artifact_process_lock

from .permit_catalog import PermitPolicy
from .permit_conditions import CONDITION_PARSER_SCHEMA_VERSION
from .permit_models import PermitChunk, PermitCondition
from .permit_ocr import PermitPageText
from .permit_source_ownership import (
    PermitSourceDocument,
    PermitSourceSpan,
    validate_permit_ownership_snapshot,
)
from .utils import clean_text


PERMIT_INDEX_SCHEMA_VERSION = 2
OCR_IMPLEMENTATION_VERSION = "permit-ocr-v2"
PERMIT_PARSER_SCHEMA_VERSION = "permit-chunks-v2"
_VALID_PAGE_SOURCES = {"native", "windows_ocr", "tesseract", "unreadable"}
_SAFE_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def permit_evaluation_fingerprint() -> str:
    """Invalidate aggregate judgements when immutable permit semantics change."""
    payload = {
        "PERMIT_INDEX_SCHEMA_VERSION": PERMIT_INDEX_SCHEMA_VERSION,
        "OCR_IMPLEMENTATION_VERSION": OCR_IMPLEMENTATION_VERSION,
        "PERMIT_PARSER_SCHEMA_VERSION": PERMIT_PARSER_SCHEMA_VERSION,
        "CONDITION_PARSER_SCHEMA_VERSION": CONDITION_PARSER_SCHEMA_VERSION,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _SourceChangedDuringBuild(RuntimeError):
    pass


class _SourceUnreadableDuringBuild(RuntimeError):
    pass


@dataclass(frozen=True)
class PermitIndexConfig:
    ocr_mode: str = "auto"
    ocr_language: str = "kor+eng"
    render_dpi: int = 180
    ocr_implementation_version: str = OCR_IMPLEMENTATION_VERSION
    chunk_parser_version: str = PERMIT_PARSER_SCHEMA_VERSION
    condition_parser_version: str = CONDITION_PARSER_SCHEMA_VERSION
    structure_policy: tuple[tuple[str, str | bool | int], ...] = ()

    def __post_init__(self) -> None:
        if not self.ocr_mode or not self.ocr_language:
            raise ValueError("OCR mode and language are required")
        if type(self.render_dpi) is not int or self.render_dpi <= 0:
            raise ValueError("render DPI must be a positive integer")
        if not self.ocr_implementation_version or not self.chunk_parser_version or not self.condition_parser_version:
            raise ValueError("parser and OCR versions are required")
        keys = [key for key, _ in self.structure_policy]
        if any(not isinstance(key, str) or not key for key in keys) or len(keys) != len(set(keys)):
            raise ValueError("structure policy keys must be unique non-empty strings")
        if any(type(value) not in {str, bool, int} for _, value in self.structure_policy):
            raise ValueError("structure policy values must be JSON scalar values")


@dataclass(frozen=True)
class PermitIndexKey:
    value: str
    pdf_sha256: str


PermitSearchIndexEntry = tuple[str, str, tuple[str, ...]]
PermitConditionManifestEntry = tuple[str, tuple[str, ...]]
PermitPageError = tuple[int, str]
PermitDiagnostic = tuple[str, str | bool | int]


@dataclass(frozen=True)
class PermitIndexSnapshot:
    pages: tuple[PermitPageText, ...]
    chunks: tuple[PermitChunk, ...]
    source_documents: tuple[PermitSourceDocument, ...]
    conditions: tuple[PermitCondition, ...]
    extraction_errors: tuple[str, ...]
    page_errors: tuple[PermitPageError, ...]
    search_index: tuple[PermitSearchIndexEntry, ...]
    build_diagnostics: tuple[PermitDiagnostic, ...]
    condition_manifest: tuple[PermitConditionManifestEntry, ...] = ()


@dataclass(frozen=True)
class PermitIndexLoad:
    snapshot: PermitIndexSnapshot | None
    cache_hit: bool
    key: str
    schema_version: int
    elapsed_ms: int
    ocr_page_count: int
    failure_code: str | None = None


def permit_index_root() -> Path:
    override = os.environ.get("SP_PERMIT_INDEX_DIR")
    if override:
        return Path(override).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "SPValidator" / "permit_index"
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return Path(xdg_cache).expanduser() / "SPValidator" / "permit_index"
    return Path.home() / ".cache" / "SPValidator" / "permit_index"


def permit_index_config(policy: PermitPolicy | None) -> PermitIndexConfig:
    projection: tuple[tuple[str, str | bool | int], ...] = ()
    if policy is not None:
        # These snapshots contain raw evidence structure. Runtime judgement,
        # scoring, and section-number matching flags do not change that data.
        projection = (("policy_id", policy.policy_id),)
    return PermitIndexConfig(
        ocr_mode=policy.ocr_mode if policy is not None else "auto",
        structure_policy=projection,
    )


def _config_payload(config: PermitIndexConfig) -> dict[str, object]:
    return {
        "ocr_mode": config.ocr_mode,
        "ocr_language": config.ocr_language,
        "render_dpi": config.render_dpi,
        "ocr_implementation_version": config.ocr_implementation_version,
        "chunk_parser_version": config.chunk_parser_version,
        "condition_parser_version": config.condition_parser_version,
        "structure_policy": dict(sorted(config.structure_policy)),
    }


def build_permit_index_key(
    *,
    pdf_sha256: str,
    config: PermitIndexConfig,
) -> PermitIndexKey:
    digest = str(pdf_sha256).casefold()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError("pdf_sha256 must be a lowercase-compatible SHA-256 digest")
    payload = {
        "schema_version": PERMIT_INDEX_SCHEMA_VERSION,
        "pdf_sha256": digest,
        "config": _config_payload(config),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return PermitIndexKey(hashlib.sha256(encoded).hexdigest(), digest)


def _raw_key(value: str) -> str:
    return clean_text(value).casefold()


def build_permit_search_index(chunks: tuple[PermitChunk, ...] | list[PermitChunk]) -> tuple[PermitSearchIndexEntry, ...]:
    grouped: dict[tuple[str, str], list[str]] = {}
    for chunk in chunks:
        key = (_raw_key(chunk.normalized_stage_path), _raw_key(chunk.normalized_test_name or chunk.title))
        grouped.setdefault(key, []).append(chunk.evidence_id)
    return tuple((stage, test, tuple(ids)) for (stage, test), ids in grouped.items())


def build_condition_manifest(
    chunks: tuple[PermitChunk, ...] | list[PermitChunk],
    conditions: tuple[PermitCondition, ...] | list[PermitCondition],
) -> tuple[PermitConditionManifestEntry, ...]:
    """Freeze exact ordered condition coverage, including empty chunk sets."""
    by_evidence: dict[str, list[str]] = {chunk.evidence_id: [] for chunk in chunks}
    for condition in conditions:
        if condition.evidence_id in by_evidence:
            by_evidence[condition.evidence_id].append(condition.condition_id)
    return tuple(
        (chunk.evidence_id, tuple(by_evidence[chunk.evidence_id]))
        for chunk in chunks
    )


def _canonical_decimal(value: Decimal) -> str:
    return format(value, "f")


def _condition_digest(condition: PermitCondition, version: str) -> str:
    values = asdict(condition)
    values["condition_id"] = ""
    # v1 parser hashes only fields explicitly supplied to the dataclass; the
    # additive mandatory=True default is enforced below but was not part of
    # the original digest payload.
    values.pop("mandatory", None)

    def encode(value: object) -> str:
        if isinstance(value, Decimal):
            text = format(value, "f")
            return text.rstrip("0").rstrip(".") if "." in text else text
        raise TypeError(type(value).__name__)

    data = json.dumps(
        [version, values],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=encode,
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _safe_pairs(values: tuple[tuple[object, object], ...]) -> bool:
    keys: set[str] = set()
    for key, value in values:
        if not isinstance(key, str) or not _SAFE_CODE_RE.fullmatch(key) or key in keys:
            return False
        if type(value) not in {str, bool, int}:
            return False
        if isinstance(value, str) and ("/" in value or "\\" in value or len(value) > 256):
            return False
        keys.add(key)
    return True


def _safe_error_codes(values: tuple[str, ...]) -> bool:
    return len(values) == len(set(values)) and all(
        isinstance(value, str) and _SAFE_CODE_RE.fullmatch(value) for value in values
    )


def _validate_snapshot(
    snapshot: PermitIndexSnapshot,
    *,
    pdf_sha256: str,
    condition_parser_version: str,
) -> bool:
    if not isinstance(snapshot, PermitIndexSnapshot):
        return False
    pages = snapshot.pages
    if not pages or tuple(page.page_number for page in pages) != tuple(range(1, len(pages) + 1)):
        return False
    for page in pages:
        if type(page.page_number) is not int or not isinstance(page.text, str) or page.source not in _VALID_PAGE_SOURCES:
            return False
        if (page.source == "unreadable") != (not clean_text(page.text)):
            return False
    if len(snapshot.source_documents) != 1:
        return False
    document = snapshot.source_documents[0]
    if document.pdf_sha256.casefold() != pdf_sha256.casefold():
        return False
    if document.pages != tuple((page.page_number, clean_text(page.text)) for page in pages):
        return False
    if Path(document.source_file).name != document.source_file or not document.source_file:
        return False
    if any(Path(chunk.source_file).name != chunk.source_file for chunk in snapshot.chunks):
        return False
    unreadable = {page.page_number for page in pages if page.source == "unreadable"}
    if (
        len(snapshot.page_errors) != len(set(snapshot.page_errors))
        or set(snapshot.page_errors) != {(number, "unreadable_page") for number in unreadable}
    ):
        return False
    if not _safe_error_codes(snapshot.extraction_errors) or not _safe_pairs(snapshot.build_diagnostics):
        return False
    ownership_errors = validate_permit_ownership_snapshot(
        snapshot.source_documents,
        snapshot.chunks,
        allow_empty_page_numbers=unreadable,
    )
    if ownership_errors:
        return False
    chunk_ids = {chunk.evidence_id for chunk in snapshot.chunks}
    conditions = snapshot.conditions
    condition_ids = {condition.condition_id for condition in conditions}
    if len(condition_ids) != len(conditions):
        return False
    by_condition = {condition.condition_id: condition for condition in conditions}
    for condition in conditions:
        if (
            not _SHA256_RE.fullmatch(condition.condition_id)
            or condition.evidence_id not in chunk_ids
            or type(condition.clause_index) is not int
            or condition.clause_index < 0
            or condition.mandatory is not True
            or condition.condition_id != _condition_digest(condition, condition_parser_version)
            or any(child not in condition_ids or child == condition.condition_id for child in condition.children)
            or (condition.kind == "and") != bool(condition.children)
        ):
            return False
        # A child graph must remain local to the same immutable evidence node.
        if any(by_condition[child].evidence_id != condition.evidence_id for child in condition.children):
            return False
    expected_manifest = build_condition_manifest(snapshot.chunks, snapshot.conditions)
    if snapshot.condition_manifest != expected_manifest:
        return False
    if tuple(
        condition_id
        for _, condition_ids in snapshot.condition_manifest
        for condition_id in condition_ids
    ) != tuple(condition.condition_id for condition in snapshot.conditions):
        return False
    expected_search = build_permit_search_index(snapshot.chunks)
    if snapshot.search_index != expected_search:
        return False
    if any(
        not isinstance(stage, str)
        or not isinstance(test, str)
        or not ids
        or len(ids) != len(set(ids))
        or any(evidence_id not in chunk_ids for evidence_id in ids)
        for stage, test, ids in snapshot.search_index
    ):
        return False
    return True


def _page_to_json(page: PermitPageText) -> dict[str, object]:
    return {"page_number": page.page_number, "text": page.text, "source": page.source}


def _span_to_json(span: PermitSourceSpan) -> dict[str, object]:
    return asdict(span)


def _chunk_to_json(chunk: PermitChunk) -> dict[str, object]:
    values = asdict(chunk)
    values["section_path_titles"] = list(chunk.section_path_titles)
    values["owned_spans"] = [_span_to_json(span) for span in chunk.owned_spans]
    return values


def _condition_to_json(condition: PermitCondition) -> dict[str, object]:
    values = asdict(condition)
    for field in ("value", "lower", "upper"):
        value = values[field]
        values[field] = _canonical_decimal(value) if isinstance(value, Decimal) else None
    values["children"] = list(condition.children)
    return values


def _snapshot_payload(
    snapshot: PermitIndexSnapshot,
    *,
    key: PermitIndexKey,
    config: PermitIndexConfig,
) -> dict[str, object]:
    return {
        "schema_version": PERMIT_INDEX_SCHEMA_VERSION,
        "key": key.value,
        "pdf_sha256": key.pdf_sha256,
        "page_count": len(snapshot.pages),
        "config": _config_payload(config),
        "pages": [_page_to_json(page) for page in snapshot.pages],
        "documents": [
            {
                "source_file": document.source_file,
                "pages": [{"page_number": number, "text": text} for number, text in document.pages],
                "pdf_sha256": document.pdf_sha256,
                "evidence_id": document.evidence_id,
            }
            for document in snapshot.source_documents
        ],
        "chunks": [_chunk_to_json(chunk) for chunk in snapshot.chunks],
        "conditions": [_condition_to_json(condition) for condition in snapshot.conditions],
        "extraction_errors": list(snapshot.extraction_errors),
        "page_errors": [
            {"page_number": page_number, "code": code}
            for page_number, code in snapshot.page_errors
        ],
        "search_index": [
            {"stage_key": stage, "test_key": test, "evidence_ids": list(ids)}
            for stage, test, ids in snapshot.search_index
        ],
        "build_diagnostics": [
            {"key": diag_key, "value": value}
            for diag_key, value in snapshot.build_diagnostics
        ],
        "condition_manifest": [
            {"evidence_id": evidence_id, "condition_ids": list(condition_ids)}
            for evidence_id, condition_ids in snapshot.condition_manifest
        ],
    }


def _require_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


def _require_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("list required")
    return value


def _deserialize_snapshot(
    payload: object,
    *,
    key: PermitIndexKey,
    config: PermitIndexConfig,
) -> PermitIndexSnapshot:
    raw = _require_dict(payload)
    if (
        raw.get("schema_version") != PERMIT_INDEX_SCHEMA_VERSION
        or raw.get("key") != key.value
        or raw.get("pdf_sha256") != key.pdf_sha256
        or raw.get("config") != _config_payload(config)
    ):
        raise ValueError("index header mismatch")
    pages = tuple(
        PermitPageText(
            page_number=_require_dict(item)["page_number"],  # type: ignore[arg-type]
            text=_require_dict(item)["text"],  # type: ignore[arg-type]
            source=_require_dict(item)["source"],  # type: ignore[arg-type]
        )
        for item in _require_list(raw.get("pages"))
    )
    if raw.get("page_count") != len(pages):
        raise ValueError("page count mismatch")
    documents: list[PermitSourceDocument] = []
    for item in _require_list(raw.get("documents")):
        values = _require_dict(item)
        document = PermitSourceDocument(
            source_file=values["source_file"],  # type: ignore[arg-type]
            pages=tuple(
                (
                    _require_dict(page)["page_number"],  # type: ignore[arg-type]
                    _require_dict(page)["text"],  # type: ignore[arg-type]
                )
                for page in _require_list(values.get("pages"))
            ),
            pdf_sha256=values["pdf_sha256"],  # type: ignore[arg-type]
        )
        if values.get("evidence_id") != document.evidence_id:
            raise ValueError("document evidence mismatch")
        documents.append(document)
    chunks: list[PermitChunk] = []
    for item in _require_list(raw.get("chunks")):
        values = _require_dict(item)
        spans = tuple(
            PermitSourceSpan(**_require_dict(span))
            for span in _require_list(values.get("owned_spans"))
        )
        chunks.append(
            PermitChunk(
                source_file=values["source_file"],  # type: ignore[arg-type]
                page_number=values["page_number"],  # type: ignore[arg-type]
                section_number=values["section_number"],  # type: ignore[arg-type]
                title=values["title"],  # type: ignore[arg-type]
                text=values["text"],  # type: ignore[arg-type]
                page_start=values.get("page_start"),  # type: ignore[arg-type]
                page_end=values.get("page_end"),  # type: ignore[arg-type]
                section_path_titles=tuple(_require_list(values.get("section_path_titles"))),  # type: ignore[arg-type]
                normalized_test_name=values.get("normalized_test_name", ""),  # type: ignore[arg-type]
                normalized_stage_path=values.get("normalized_stage_path", ""),  # type: ignore[arg-type]
                owned_spans=spans,
                evidence_id=values.get("evidence_id", ""),  # type: ignore[arg-type]
                parent_evidence_id=values.get("parent_evidence_id", ""),  # type: ignore[arg-type]
            )
        )
    conditions: list[PermitCondition] = []
    for item in _require_list(raw.get("conditions")):
        values = _require_dict(item)

        def decimal(name: str) -> Decimal | None:
            value = values.get(name)
            return None if value is None else Decimal(value)  # type: ignore[arg-type]

        conditions.append(
            PermitCondition(
                condition_id=values["condition_id"],  # type: ignore[arg-type]
                evidence_id=values["evidence_id"],  # type: ignore[arg-type]
                clause_index=values["clause_index"],  # type: ignore[arg-type]
                kind=values["kind"],  # type: ignore[arg-type]
                operator=values.get("operator"),  # type: ignore[arg-type]
                value=decimal("value"),
                unit=values.get("unit"),  # type: ignore[arg-type]
                lower=decimal("lower"),
                upper=decimal("upper"),
                lower_inclusive=values["lower_inclusive"],  # type: ignore[arg-type]
                upper_inclusive=values["upper_inclusive"],  # type: ignore[arg-type]
                children=tuple(_require_list(values.get("children"))),  # type: ignore[arg-type]
                quote=values["quote"],  # type: ignore[arg-type]
                stage_key=values["stage_key"],  # type: ignore[arg-type]
                test_key=values["test_key"],  # type: ignore[arg-type]
                metric_key=values["metric_key"],  # type: ignore[arg-type]
                mandatory=values.get("mandatory", True),  # type: ignore[arg-type]
                method_key=values.get("method_key"),  # type: ignore[arg-type]
                scope_text=values.get("scope_text", ""),  # type: ignore[arg-type]
            )
        )
    snapshot = PermitIndexSnapshot(
        pages=pages,
        chunks=tuple(chunks),
        source_documents=tuple(documents),
        conditions=tuple(conditions),
        extraction_errors=tuple(_require_list(raw.get("extraction_errors"))),  # type: ignore[arg-type]
        page_errors=tuple(
            (
                _require_dict(item)["page_number"],  # type: ignore[arg-type]
                _require_dict(item)["code"],  # type: ignore[arg-type]
            )
            for item in _require_list(raw.get("page_errors"))
        ),
        search_index=tuple(
            (
                _require_dict(item)["stage_key"],  # type: ignore[arg-type]
                _require_dict(item)["test_key"],  # type: ignore[arg-type]
                tuple(_require_list(_require_dict(item).get("evidence_ids"))),  # type: ignore[arg-type]
            )
            for item in _require_list(raw.get("search_index"))
        ),
        build_diagnostics=tuple(
            (
                _require_dict(item)["key"],  # type: ignore[arg-type]
                _require_dict(item)["value"],  # type: ignore[arg-type]
            )
            for item in _require_list(raw.get("build_diagnostics"))
        ),
        condition_manifest=tuple(
            (
                _require_dict(item)["evidence_id"],  # type: ignore[arg-type]
                tuple(_require_list(_require_dict(item).get("condition_ids"))),  # type: ignore[arg-type]
            )
            for item in _require_list(raw.get("condition_manifest"))
        ),
    )
    if not _validate_snapshot(
        snapshot,
        pdf_sha256=key.pdf_sha256,
        condition_parser_version=config.condition_parser_version,
    ):
        raise ValueError("invalid permit snapshot")
    return snapshot


def _read_valid_entry(
    entry: Path,
    *,
    key: PermitIndexKey,
    config: PermitIndexConfig,
) -> PermitIndexSnapshot | None:
    try:
        payload = json.loads(entry.read_text(encoding="utf-8"))
        return _deserialize_snapshot(payload, key=key, config=config)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError, InvalidOperation):
        return None


def _write_entry_atomically(
    entry: Path,
    snapshot: PermitIndexSnapshot,
    *,
    key: PermitIndexKey,
    config: PermitIndexConfig,
    source_path: Path,
) -> None:
    payload = _snapshot_payload(snapshot, key=key, config=config)
    entry.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=entry.parent,
            # Randomness already makes the temporary name unique. Repeating
            # the full digest made it longer than the final entry on Windows.
            prefix=".permit-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Validate the exact bytes that will be published, not the in-memory object.
        if _read_valid_entry(temporary_path, key=key, config=config) is None:
            raise ValueError("serialized permit index failed validation")
        # This is deliberately the final operation before publication.  It is
        # separate from the resolver's content hash and closes the build TOCTOU
        # window without making warm reads rehash the permit.
        try:
            current_hash = _file_sha256(source_path)
        except OSError as exc:
            raise _SourceUnreadableDuringBuild from exc
        if current_hash.casefold() != key.pdf_sha256:
            raise _SourceChangedDuringBuild
        os.replace(temporary_path, entry)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rebind_display_name(snapshot: PermitIndexSnapshot, display_name: str) -> PermitIndexSnapshot:
    documents = tuple(replace(document, source_file=display_name) for document in snapshot.source_documents)
    chunks = tuple(replace(chunk, source_file=display_name) for chunk in snapshot.chunks)
    return replace(snapshot, source_documents=documents, chunks=chunks)


def _load_result(
    *,
    snapshot: PermitIndexSnapshot | None,
    cache_hit: bool,
    key: PermitIndexKey,
    started: float,
    failure_code: str | None = None,
    ocr_page_count: int = 0,
) -> PermitIndexLoad:
    return PermitIndexLoad(
        snapshot=snapshot,
        cache_hit=cache_hit,
        key=key.value,
        schema_version=PERMIT_INDEX_SCHEMA_VERSION,
        elapsed_ms=max(0, int((monotonic() - started) * 1000)),
        ocr_page_count=ocr_page_count,
        failure_code=failure_code,
    )


def load_or_build_permit_index(
    *,
    pdf_path: Path,
    pdf_sha256: str,
    policy: PermitPolicy | None,
    builder: Callable[[], PermitIndexSnapshot],
    config: PermitIndexConfig | None = None,
) -> PermitIndexLoad:
    """Load a trusted immutable snapshot or build and atomically publish one."""
    started = monotonic()
    pdf_path = Path(pdf_path)
    try:
        active_config = config or permit_index_config(policy)
        key = build_permit_index_key(pdf_sha256=pdf_sha256, config=active_config)
    except (TypeError, ValueError):
        fallback = PermitIndexKey("", str(pdf_sha256))
        return _load_result(
            snapshot=None, cache_hit=False, key=fallback, started=started,
            failure_code="invalid_config",
        )
    root = permit_index_root()
    entry = root / "entries" / f"{key.value}.json"
    lock_path = root / ".locks" / f"{key.value}.lock"

    loaded = _read_valid_entry(entry, key=key, config=active_config)
    if loaded is not None:
        rebound = _rebind_display_name(loaded, pdf_path.name)
        return _load_result(snapshot=rebound, cache_hit=True, key=key, started=started)
    try:
        with artifact_process_lock(lock_path):
            loaded = _read_valid_entry(entry, key=key, config=active_config)
            if loaded is not None:
                rebound = _rebind_display_name(loaded, pdf_path.name)
                return _load_result(snapshot=rebound, cache_hit=True, key=key, started=started)
            try:
                snapshot = builder()
            except Exception:
                return _load_result(
                    snapshot=None, cache_hit=False, key=key, started=started,
                    failure_code="build_failed",
                )
            try:
                valid_snapshot = _validate_snapshot(
                    snapshot,
                    pdf_sha256=key.pdf_sha256,
                    condition_parser_version=active_config.condition_parser_version,
                )
            except Exception:
                valid_snapshot = False
            if not valid_snapshot:
                return _load_result(
                    snapshot=None, cache_hit=False, key=key, started=started,
                    failure_code="validation_failed",
                )
            try:
                _write_entry_atomically(
                    entry,
                    snapshot,
                    key=key,
                    config=active_config,
                    source_path=pdf_path,
                )
            except _SourceChangedDuringBuild:
                return _load_result(
                    snapshot=None, cache_hit=False, key=key, started=started,
                    failure_code="source_changed",
                )
            except _SourceUnreadableDuringBuild:
                return _load_result(
                    snapshot=None, cache_hit=False, key=key, started=started,
                    failure_code="source_unreadable",
                )
            except (OSError, TypeError, ValueError):
                return _load_result(
                    snapshot=None, cache_hit=False, key=key, started=started,
                    failure_code="publish_failed",
                )
            rebound = _rebind_display_name(snapshot, pdf_path.name)
            ocr_pages = sum(page.source in {"windows_ocr", "tesseract"} for page in snapshot.pages)
            return _load_result(
                snapshot=rebound,
                cache_hit=False,
                key=key,
                started=started,
                ocr_page_count=ocr_pages,
            )
    except ReviewLockTimeout:
        return _load_result(
            snapshot=None, cache_hit=False, key=key, started=started,
            failure_code="lock_timeout",
        )
    except OSError:
        return _load_result(
            snapshot=None, cache_hit=False, key=key, started=started,
            failure_code="index_io_failed",
        )
