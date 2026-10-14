from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .permit_catalog import PermitPolicy
from .permit_conditions import parse_permit_conditions
from .permit_index import (
    PermitIndexSnapshot,
    build_condition_manifest,
    build_permit_search_index,
    load_or_build_permit_index,
)
from .permit_models import PermitChunk
from .permit_ocr import PermitPageText, extract_permit_page_texts
from .permit_source_ownership import PermitSourceDocument, make_span, node_id
from .schemas import ExtractedRecord
from .utils import clean_text
from .extraction_runtime import file_sha256


_HEADING_RE = re.compile(r"(?m)^\s*(?P<section>\d+(?:\.\d+)+(?:\.)?|\d+\.)\s+(?P<title>[^\n]+?)\s*$")
_LEADING_SECTION_RE = re.compile(r"^\s*\d+(?:\.\d+)+(?:\.)?\s*")
_NON_IDENTITY_RE = re.compile(r"[^0-9a-zA-Z가-힣]+")
_GENERIC_TEST_NAMES = {"확인시험", "성상", "무균시험", "시험"}
_GENERIC_STAGE_TOKENS = {"시험", "기준", "대한", "및", "test"}


def _is_product_metadata_page(text: str) -> bool:
    """Recognize independent permit form pages, not a test's continuation.

    Require multiple standalone form labels. A lone '성상' or a word inside a
    test sentence must never cut off the following acceptance conditions.
    The complete page is retained as a separate source chunk.
    """
    lines = [re.sub(r"\s+", "", line) for line in text.splitlines() if line.strip()]
    lines = [line for line in lines if not re.fullmatch(r"문서확인번호:.*|-\d+-|\d+/\d+", line)]
    if not lines:
        return False
    fields = {"성상", "포장단위", "제품명", "제조원", "제조국", "제조원소재지", "수행공정"}
    return lines[0] in fields and len(fields.intersection(lines)) >= 2


@lru_cache(maxsize=8192)
def _specific_stage_identity(text: str) -> str:
    value = _compact_identity(text, strip_number=True)
    value = re.sub(r"(?:에대한시험|대한시험|시험)$", "", value)
    if value in {"", "재료", "원액", "시험기준", "제조방법", "완제의약품", "세포은행"}:
        return ""
    return value if len(value) >= 4 else ""


def _is_real_heading(match: re.Match) -> bool:
    title = unicodedata.normalize("NFKC", match.group("title")).strip()
    if any(int(part) == 0 for part in match.group("section").rstrip(".").split(".")):
        return False
    # A line-wrapped decimal such as "8.5 이어야 한다." is not section 8.5.
    # Treating it as a heading destroys all subsequent manufacturing-stage paths.
    if re.search(r"이어야|되어야|않아야|하여야|해야|한다\.?$", title):
        return False
    if re.match(r"(?:[xX×]\s*10|[%℃~±]|(?:mL|mg|ng|μg|µg|μm|mm|nm|cm|mM|cells|mOsm)(?:/|\b))", title):
        return False
    return True


def normalize_permit_heading(text: str) -> str:
    """Return a heading's semantic title without its dotted section number."""
    return clean_text(_LEADING_SECTION_RE.sub("", text or ""))


@lru_cache(maxsize=8192)
def _compact_identity(text: str, *, strip_number: bool) -> str:
    value = normalize_permit_heading(text) if strip_number else clean_text(text)
    return _NON_IDENTITY_RE.sub("", value.casefold())


def _section_depth(section_number: str) -> int:
    return len([part for part in section_number.rstrip(".").split(".") if part])


def _record_path_values(record: ExtractedRecord) -> list[str]:
    values: list[str] = []
    for item in record.section_path or []:
        if isinstance(item, str):
            values.append(item)
        elif isinstance(item, dict):
            values.extend(
                str(value)
                for key, value in item.items()
                if key in {"title", "section_title", "name", "label"} and value
            )
    return values


@lru_cache(maxsize=8192)
def _stage_match_score(record_stage: str, chunk_stage: str) -> float:
    """Score actual stage overlap, favoring a specific normalized path."""
    def meaningful_tokens(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z]+|[가-힣]+", normalize_permit_heading(value).casefold())
            if token not in _GENERIC_STAGE_TOKENS
        }

    record_tokens = meaningful_tokens(record_stage)
    chunk_tokens = meaningful_tokens(chunk_stage)
    record_identity = "".join(
        token
        for token in re.findall(r"[a-z]+|[가-힣]+", normalize_permit_heading(record_stage).casefold())
        if token not in _GENERIC_STAGE_TOKENS
    )
    chunk_identity = "".join(
        token
        for token in re.findall(r"[a-z]+|[가-힣]+", normalize_permit_heading(chunk_stage).casefold())
        if token not in _GENERIC_STAGE_TOKENS
    )
    if not record_identity or not chunk_identity:
        return 0.0
    if record_identity == chunk_identity:
        return 100.0
    if record_identity in chunk_identity:
        return 80.0
    if chunk_identity in record_identity:
        return 20.0 + (30.0 * len(chunk_identity) / len(record_identity))

    overlap = record_tokens & chunk_tokens
    if not overlap:
        return 0.0
    return float(len(overlap) * 5)


class PermitPdfStore:
    """Extract and search permit paragraphs without treating section numbers as identity."""

    def __init__(
        self,
        permit_pdf_paths: list[Path] | None = None,
        policy: PermitPolicy | None = None,
        page_text_extractor: Callable[..., tuple[list[PermitPageText], list[str]]] = extract_permit_page_texts,
        *,
        pdf_sha256_by_path: Mapping[Path, str] | None = None,
        permit_index_loader=load_or_build_permit_index,
    ) -> None:
        self.permit_pdf_paths = permit_pdf_paths or []
        self.policy = policy
        self.page_text_extractor = page_text_extractor
        self.pdf_sha256_by_path = {
            Path(path).resolve(): digest.casefold()
            for path, digest in (pdf_sha256_by_path or {}).items()
        }
        self.permit_index_loader = permit_index_loader
        self.chunks: list[PermitChunk] = []
        self.extraction_errors: list[str] = []
        self.search_aliases: dict[str, set[str]] = {}
        self.source_documents: list[PermitSourceDocument] = []
        self.conditions = []
        self.raw_search_index = []
        self.index_diagnostics: list[dict[str, object]] = []
        self._chunk_count_by_path: dict[Path, int] = {}
        for path in self.permit_pdf_paths:
            if path.exists() and path.suffix.lower() == ".pdf":
                loaded_chunks = self._load_pdf(path)
                self._chunk_count_by_path[path.resolve()] = len(loaded_chunks)
                self.chunks.extend(loaded_chunks)

    @classmethod
    def from_page_texts(
        cls,
        pages: Iterable[PermitPageText | tuple[int, str]],
        policy: PermitPolicy | None = None,
        source_file: str = "",
    ) -> "PermitPdfStore":
        """Build a deterministic store from already extracted physical page text."""
        store = cls([], policy=policy)
        store.chunks = store._parse_page_texts(pages, source_file)
        return store

    @property
    def enabled(self) -> bool:
        return bool(self.chunks)

    def _load_pdf(self, pdf_path: Path) -> list[PermitChunk]:
        pdf_sha256 = self.pdf_sha256_by_path.get(pdf_path.resolve())
        # Legacy/direct callers have no resolver-authorized digest. Keep their
        # behavior and temporary OCR cache unchanged until the pipeline passes
        # the validated content hash explicitly.
        if not pdf_sha256:
            return self._load_pdf_legacy(pdf_path)

        def build_snapshot() -> PermitIndexSnapshot:
            pages, errors = self.page_text_extractor(
                pdf_path,
                ocr_mode=self.policy.ocr_mode if self.policy is not None else "auto",
                pdf_sha256=pdf_sha256,
                use_page_cache=False,
            )
            parser = PermitPdfStore([], policy=self.policy)
            chunks = tuple(
                parser._parse_page_texts(
                    pages,
                    pdf_path.name,
                    pdf_sha256=pdf_sha256,
                )
            )
            conditions = tuple(
                condition
                for chunk in chunks
                for condition in parse_permit_conditions(chunk)
            )
            page_errors = tuple(
                (page.page_number, "unreadable_page")
                for page in pages
                if page.source == "unreadable"
            )
            unreadable_messages = {
                f"Permit OCR page {number} is unreadable" for number, _ in page_errors
            }
            extraction_errors = (
                ("permit_extraction_warning",)
                if any(error not in unreadable_messages for error in errors)
                else ()
            )
            diagnostics = (
                ("page_count", len(pages)),
                ("chunk_count", len(chunks)),
                ("condition_count", len(conditions)),
            )
            return PermitIndexSnapshot(
                pages=tuple(pages),
                chunks=chunks,
                source_documents=tuple(parser.source_documents),
                conditions=conditions,
                extraction_errors=extraction_errors,
                page_errors=page_errors,
                search_index=build_permit_search_index(chunks),
                build_diagnostics=diagnostics,
                condition_manifest=build_condition_manifest(chunks, conditions),
            )

        loaded = self.permit_index_loader(
            pdf_path=pdf_path,
            pdf_sha256=pdf_sha256,
            policy=self.policy,
            builder=build_snapshot,
        )
        snapshot = loaded.snapshot
        self.index_diagnostics.append(
            {
                "source_file": pdf_path.name,
                "cache_hit": loaded.cache_hit,
                "key": loaded.key,
                "schema_version": loaded.schema_version,
                "elapsed_ms": loaded.elapsed_ms,
                "ocr_page_count": loaded.ocr_page_count,
                "failure_code": loaded.failure_code,
                "chunk_count": len(snapshot.chunks) if snapshot is not None else 0,
            }
        )
        if snapshot is None:
            code = loaded.failure_code or "unavailable"
            self.extraction_errors.append(f"Permit index {code} for {pdf_path.name}")
            return []
        self.source_documents.extend(snapshot.source_documents)
        self.conditions.extend(snapshot.conditions)
        self.raw_search_index.extend(snapshot.search_index)
        self.extraction_errors.extend(snapshot.extraction_errors)
        self.extraction_errors.extend(
            f"Permit OCR page {page_number} is unreadable"
            for page_number, code in snapshot.page_errors
            if code == "unreadable_page"
        )
        return list(snapshot.chunks)

    def _load_pdf_legacy(self, pdf_path: Path) -> list[PermitChunk]:
        try:
            pages, errors = self.page_text_extractor(
                pdf_path,
                ocr_mode=self.policy.ocr_mode if self.policy is not None else "auto",
            )
        except Exception as exc:
            self.extraction_errors.append(f"Could not extract permit {pdf_path.name}: {exc}")
            return []
        self.extraction_errors.extend(errors)
        return self._parse_page_texts(pages, pdf_path.name,
                                      pdf_sha256=file_sha256(pdf_path))

    def chunk_count_for_path(self, pdf_path: Path) -> int:
        """Return the load-local count without conflating duplicate basenames."""
        return self._chunk_count_by_path.get(Path(pdf_path).resolve(), 0)

    def _split_page_into_sections(self, source_file: str, page_number: int, text: str) -> list[PermitChunk]:
        """Compatibility helper for callers that still split one physical page."""
        return self._parse_page_texts([(page_number, text)], source_file)

    def _parse_page_texts(
        self,
        pages: Iterable[PermitPageText | tuple[int, str]],
        source_file: str,
        *,
        pdf_sha256: str = "",
    ) -> list[PermitChunk]:
        normalized_pages = []
        for raw_page in pages:
            if isinstance(raw_page, PermitPageText):
                number, value = raw_page.page_number, raw_page.text
            elif isinstance(raw_page, tuple) and len(raw_page) == 2:
                number, value = raw_page
            else:
                raise ValueError("page texts must contain (page_number, text) tuples")
            if type(number) is not int or number <= 0 or not isinstance(value, str):
                raise ValueError("page texts require a positive non-bool int page number and str text")
            normalized_pages.append((number, clean_text(value)))
        document = PermitSourceDocument(source_file, tuple(normalized_pages), pdf_sha256)
        self.source_documents.append(document)
        chunks: list[PermitChunk] = []
        path_stack: list[tuple[int, str]] = []
        owner_stack: list[tuple[int, str]] = []
        open_section: dict[str, object] | None = None

        def own(page_number, page_text, start, end, kind="body"):
            if page_text[start:end].strip():
                return make_span(document, page_number, page_text, start, end, kind)
            return None

        def standalone(page_number, page_text, start, end, title="허가서 본문"):
            span = own(page_number, page_text, start, end)
            if span is not None:
                chunks.append(PermitChunk(source_file, page_number, "", title, clean_text(span.text),
                                          page_number, page_number, normalized_test_name=title,
                                          owned_spans=(span,), evidence_id=node_id(span, "", ())))

        def append_body(page_number, page_text, start, end):
            span = own(page_number, page_text, start, end)
            if span is not None:
                open_section["owned_spans"].append(span)

        def finish_open() -> None:
            nonlocal open_section
            if open_section is None:
                return
            text = clean_text("\n".join(open_section["parts"]))  # type: ignore[index]
            if text:
                chunks.append(
                    PermitChunk(
                        source_file=source_file,
                        page_number=open_section["page_start"],  # type: ignore[arg-type,index]
                        section_number=open_section["section_number"],  # type: ignore[arg-type,index]
                        title=open_section["title"],  # type: ignore[arg-type,index]
                        text=text,
                        page_start=open_section["page_start"],  # type: ignore[arg-type,index]
                        page_end=open_section["page_end"],  # type: ignore[arg-type,index]
                        section_path_titles=open_section["path_titles"],  # type: ignore[arg-type,index]
                        normalized_test_name=open_section["normalized_test_name"],  # type: ignore[arg-type,index]
                        normalized_stage_path=open_section["normalized_stage_path"],  # type: ignore[arg-type,index]
                        owned_spans=tuple(open_section["owned_spans"]),
                        evidence_id=open_section["evidence_id"],
                        parent_evidence_id=open_section["parent_evidence_id"],
                    )
                )
            open_section = None

        def start_section(page_number: int, section_number: str, title: str, heading_span) -> None:
            nonlocal open_section, path_stack, owner_stack
            finish_open()
            depth = _section_depth(section_number)
            path_stack = [entry for entry in path_stack if entry[0] < depth]
            path_stack.append((depth, title))
            path_titles = tuple(item_title for _, item_title in path_stack)
            owner_stack = [entry for entry in owner_stack if entry[0] < depth]
            evidence_id = node_id(heading_span, section_number.rstrip("."), path_titles)
            parent_id = owner_stack[-1][1] if owner_stack else ""
            owner_stack.append((depth, evidence_id))
            open_section = {
                "page_start": page_number,
                "page_end": page_number,
                "section_number": section_number.rstrip("."),
                "title": title,
                "parts": [f"{section_number} {title}"],
                "owned_spans": [heading_span],
                "evidence_id": evidence_id,
                "parent_evidence_id": parent_id,
                "path_titles": path_titles,
                "normalized_test_name": normalize_permit_heading(title),
                "normalized_stage_path": " ".join(
                    normalize_permit_heading(stage_title) for stage_title in path_titles[:-1]
                ),
            }

        for page_number, text in normalized_pages:
            if self.policy is None and open_section is not None:
                finish_open()
                path_stack = []
                owner_stack = []
            if not text:
                continue
            if _is_product_metadata_page(text):
                finish_open()
                path_stack = []
                owner_stack = []
                standalone(page_number, text, 0, len(text), "허가서 제품정보")
                continue
            matches = [match for match in _HEADING_RE.finditer(text) if _is_real_heading(match)]
            if not matches:
                if open_section is None:
                    standalone(page_number, text, 0, len(text))
                else:
                    open_section["parts"].append(text)  # type: ignore[index]
                    open_section["page_end"] = page_number
                    append_body(page_number, text, 0, len(text))
                continue

            prefix = clean_text(text[: matches[0].start()])
            if _section_depth(matches[0].group("section")) == 1:
                # A new top-level document section ends the preceding test.
                # Its cover/header must not be appended as a missing test clause.
                finish_open()
            if prefix:
                if open_section is not None:
                    open_section["parts"].append(prefix)  # type: ignore[index]
                    open_section["page_end"] = page_number
                    append_body(page_number, text, 0, matches[0].start())
                else:
                    standalone(page_number, text, 0, matches[0].start())

            for index, match in enumerate(matches):
                section_number = clean_text(match.group("section"))
                title = clean_text(match.group("title"))
                start_section(page_number, section_number, title,
                              own(page_number, text, match.start(), match.end(), "heading"))
                body = clean_text(text[match.end(): matches[index + 1].start() if index + 1 < len(matches) else len(text)])
                if body:
                    open_section["parts"].append(body)  # type: ignore[index]
                    append_body(page_number, text, match.end(),
                                matches[index + 1].start() if index + 1 < len(matches) else len(text))

        finish_open()
        if self.policy is not None:
            expanded: list[PermitChunk] = []
            for index, chunk in enumerate(chunks):
                if not chunk.section_number:
                    expanded.append(chunk)
                    continue
                depth = _section_depth(chunk.section_number)
                descendants: list[PermitChunk] = []
                for later in chunks[index + 1:]:
                    if not later.section_number:
                        break  # An independent form/cover page ends this hierarchy.
                    if later.section_number and _section_depth(later.section_number) <= depth:
                        break
                    if later.section_number:
                        descendants.append(later)
                if descendants:
                    chunk = replace(
                        chunk,
                        text=clean_text("\n".join([chunk.text, *(child.text for child in descendants)])),
                        page_end=descendants[-1].page_end,
                    )
                expanded.append(chunk)
            return expanded
        return chunks

    def configure_search_aliases(self, groups: Iterable[Iterable[str]]) -> None:
        # Replaced, not appended: an edited/removed MD mapping must not linger.
        self.search_aliases = {}
        for group in groups:
            names = {_compact_identity(value, strip_number=True) for value in group}
            names.discard("")
            for name in names:
                self.search_aliases.setdefault(name, set()).update(names)

    def activate_policy_context_compatibility(
        self,
    ) -> tuple[PermitPolicy | None, list[PermitChunk]] | None:
        """Temporarily reproduce the pre-reuse PolicyContext permit store.

        Before the indexed store was shared, direct explicit permits (which have
        no catalog policy) were judged with legacy search and then reopened by
        ``PolicyContext`` with the linked policy.  Reparse the already indexed
        page text instead of reopening the PDF, and let the caller restore the
        legacy state after document-wide policy evaluation.
        """
        if self.policy is not None or not self.source_documents:
            return None
        linked_policy = PermitPolicy("linked", True, True, True, "auto")
        original_policy = self.policy
        original_documents = self.source_documents
        linked_chunks: list[PermitChunk] = []
        try:
            self.policy = linked_policy
            # _parse_page_texts records the normalized source it parsed.  Keep
            # those temporary parser writes away from the indexed source list.
            self.source_documents = []
            for document in original_documents:
                linked_chunks.extend(
                    self._parse_page_texts(
                        document.pages,
                        document.source_file,
                        pdf_sha256=document.pdf_sha256,
                    )
                )
        finally:
            self.policy = original_policy
            self.source_documents = original_documents
        if not linked_chunks:
            return None
        state = (original_policy, self.chunks)
        self.policy = linked_policy
        self.chunks = linked_chunks
        return state

    def restore_policy_context_compatibility(
        self,
        state: tuple[PermitPolicy | None, list[PermitChunk]] | None,
    ) -> None:
        if state is not None:
            self.policy, self.chunks = state

    def _same_test_candidate(self, left: str, right: str) -> bool:
        return bool(left and right and (left == right or right in self.search_aliases.get(left, set())))

    def search_precise(self, record: ExtractedRecord):
        from .permit_retrieval import PermitRetrievalIndex
        import hashlib
        # Frozen chunks make this cheap tuple comparison safe even when callers
        # replace/append chunks or temporarily activate a different policy.
        signature = (tuple(self.chunks), repr(self.policy),
                     tuple(sorted((key, tuple(sorted(values))) for key, values in self.search_aliases.items())))
        if getattr(self, '_precise_signature', None) != signature:
            self._precise_index = PermitRetrievalIndex(
                self.chunks, source_fingerprint=hashlib.sha256(repr(signature).encode('utf-8')).hexdigest(),
                aliases=self.search_aliases, exact_enabled=self.policy is not None)
            self._precise_signature = signature
        return self._precise_index.select(record, fallback=self.search, top_k=12)

    def search(self, record: ExtractedRecord, top_k: int = 5) -> list[PermitChunk]:
        if not self.chunks:
            return []
        query = " ".join(
            clean_text(value)
            for value in [record.section_number, record.section_title, record.test_name, record.method,
                          record.criteria, record.result, record.raw_text]
            if clean_text(value)
        )
        query_tokens = self._tokens(query)
        if not query_tokens:
            return []
        scored: list[tuple[float, int, PermitChunk]] = []
        for index, chunk in enumerate(self.chunks):
            score = self._score_chunk(query_tokens, record, chunk)
            if score > 0:
                scored.append((score, index, chunk))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if self.policy is not None:
            # If the named test AND its specific manufacturing stage match
            # after formatting normalization or an explicit MD candidate alias,
            # exclude other stages. Aliases do not decide semantic equivalence.
            # Keep every same-stage duplicate: conflicting permits stay ambiguous.
            stage = _specific_stage_identity(record.section_title or "")
            test = _compact_identity(record.test_name or "", strip_number=True)
            # An absent test in a known stage is not permission to borrow a
            # same-named test from another manufacturing stage.
            stage_known = stage and any(
                any(_specific_stage_identity(part) == stage for part in chunk.section_path_titles)
                for chunk in self.chunks)
            if stage_known:
                scored = [item for item in scored if
                          any(_specific_stage_identity(part) == stage for part in item[2].section_path_titles[:-1])]
            exact = [item for item in scored if stage and
                     self._same_test_candidate(test, _compact_identity(item[2].normalized_test_name or item[2].title, strip_number=True)) and
                     any(_specific_stage_identity(part) == stage for part in item[2].section_path_titles[:-1])]
            if exact:
                scored = exact
        return [chunk for _, _, chunk in scored[:top_k]]

    def has_ambiguous_generic_test(self, record: ExtractedRecord) -> bool:
        """Signal duplicate generic headings when stage-free search cannot choose one."""
        if self.policy is None:
            return False
        test_name = _compact_identity(record.test_name or "", strip_number=True)
        generic_names = {_compact_identity(name, strip_number=True) for name in _GENERIC_TEST_NAMES}
        if test_name not in generic_names:
            return False
        matches = sum(
            _compact_identity(chunk.normalized_test_name or chunk.title, strip_number=True) == test_name
            for chunk in self.chunks
        )
        return matches > 1

    def _tokens(self, text: str) -> list[str]:
        value = clean_text(text).casefold().replace("e.coli", "ecoli")
        value = re.sub(r"[^0-9a-zA-Z가-힣μµ%./^]+", " ", value)
        return [
            token for token in value.split() if len(token) >= 2 and token not in
            {"시험", "시험기준", "시험결과", "시험방법", "실측치", "기준", "결과", "확인"}
        ]

    def _score_chunk(self, query_tokens: list[str], record: ExtractedRecord, chunk: PermitChunk) -> float:
        if self.policy is None:
            return self._legacy_score_chunk(query_tokens, record, chunk)
        semantic_score = self._semantic_score(record, chunk)
        if self.policy.ignore_section_numbers:
            return semantic_score
        return semantic_score + self._legacy_score_chunk(query_tokens, record, chunk)

    def _legacy_score_chunk(self, query_tokens: list[str], record: ExtractedRecord, chunk: PermitChunk) -> float:
        chunk_text = f"{chunk.section_number} {chunk.title} {chunk.text}"
        chunk_norm = chunk_text.casefold().replace("e.coli", "ecoli")
        score = 0.0
        if record.section_number and record.section_number == chunk.section_number:
            score += 80.0
        if record.section_number and chunk.section_number and (
            record.section_number.startswith(chunk.section_number) or chunk.section_number.startswith(record.section_number)
        ):
            score += 20.0
        if clean_text(record.test_name) and clean_text(record.test_name) in chunk_text:
            score += 40.0
        if clean_text(record.section_title) and clean_text(record.section_title) in chunk_text:
            score += 25.0
        if clean_text(record.method) and clean_text(record.method) in chunk_text:
            score += 20.0
        return score + sum(2.0 for token in query_tokens if token in chunk_norm)

    def _semantic_score(self, record: ExtractedRecord, chunk: PermitChunk) -> float:
        record_test = _compact_identity(record.test_name or "", strip_number=True)
        chunk_test = _compact_identity(chunk.normalized_test_name or chunk.title, strip_number=True)
        if not record_test or not chunk_test:
            return 0.0
        if self._same_test_candidate(record_test, chunk_test):
            test_score = 100.0
        elif record_test in chunk_test or chunk_test in record_test:
            test_score = 60.0
        else:
            return 0.0

        stage_values = [record.section_title or "", *_record_path_values(record)]
        record_stage_path = " ".join(value for value in stage_values if clean_text(value))
        stage_path = chunk.normalized_stage_path
        stage_score = _stage_match_score(record_stage_path, stage_path)
        if chunk_test in _GENERIC_TEST_NAMES and stage_score == 0:
            return 0.0
        return test_score + stage_score

    def format_context(self, chunks: list[PermitChunk]) -> str:
        parts: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            page_start = chunk.page_start if chunk.page_start is not None else chunk.page_number
            page_end = chunk.page_end if chunk.page_end is not None else chunk.page_number
            page_range = str(page_start) if page_start == page_end else f"{page_start}-{page_end}"
            parts.append(
                f"[허가서 근거 {index}]\n- 파일: {chunk.source_file}\n- 페이지: {page_range}\n"
                f"- 섹션: {chunk.section_number} {chunk.title}\n- 내용:\n{chunk.text}"
            )
        return "\n\n".join(parts)
