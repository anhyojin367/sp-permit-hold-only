"""Exhaustive positional permit evidence, deliberately without applicability decisions.

Offsets address clean_text-normalized extracted physical pages, NOT PDF byte
offsets or bounding boxes. Search chunks may repeat descendants; owned spans
must instead partition every non-whitespace source character exactly once.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date

from .utils import clean_text


def _id(kind, *values):
    payload = json.dumps(["permit-ownership-v1", kind, *values], ensure_ascii=False,
                         separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PermitSourceDocument:
    source_file: str
    pages: tuple[tuple[int, str], ...]
    pdf_sha256: str = ""

    @property
    def evidence_id(self):
        # A rename cannot change immutable PDF evidence. Legacy synthetic inputs
        # still need their filename namespace because no content digest exists.
        return _id("document", "" if self.pdf_sha256 else self.source_file,
                   self.pdf_sha256, self.pages)


@dataclass(frozen=True)
class PermitSourceSpan:
    document_id: str
    page_number: int
    char_start: int
    char_end: int
    text: str
    kind: str
    evidence_id: str


def make_span(document, page_number, text, start, end, kind):
    quote = text[start:end]
    return PermitSourceSpan(document.evidence_id, page_number, start, end, quote, kind,
                            _id("span", document.evidence_id, page_number, start, end, kind, quote))


def node_id(first_span, section_number, path_titles):
    return _id("node", first_span.evidence_id, section_number, path_titles)


def _is_date_entry(number):
    if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", number):
        return False
    try:
        date(*map(int, number.split(".")))
    except ValueError:
        return False
    return True


def validate_permit_ownership_snapshot(
    documents,
    chunks,
    *,
    allow_empty_page_numbers=frozenset(),
) -> tuple[str, ...]:
    """Validate immutable source ownership without accessing any PDF path.

    An index may retain explicitly unreadable physical pages as empty source
    slots.  Direct callers remain strict; the index must identify each allowed
    page separately before opting into this narrow exception.
    """
    documents, chunks = tuple(documents), tuple(chunks)
    allow_empty_page_numbers = frozenset(allow_empty_page_numbers)
    errors = []
    if len(documents) != 1:
        errors.append("단일 허가 원문이 필요합니다. 복수 파일의 개정 관계는 아직 지원하지 않습니다.")
    docs = {doc.evidence_id: doc for doc in documents}
    if len(docs) != len(documents):
        errors.append("중복 허가 원문이 연결되어 있습니다.")
    pages, coverage = {}, {}
    for doc in documents:
        numbers = [n for n, _ in doc.pages]
        if not numbers or numbers != list(range(numbers[0], numbers[-1] + 1)):
            errors.append("원문 페이지가 비어 있거나 중복·역순·누락되어 있습니다.")
        for number, text in doc.pages:
            if not text and number not in allow_empty_page_numbers:
                errors.append(f"원문 {number}쪽에 읽을 수 있는 텍스트가 없습니다.")
            pages[(doc.evidence_id, number)] = text
            coverage[(doc.evidence_id, number)] = [0] * len(text)
    by_id = {c.evidence_id: c for c in chunks}
    if len(by_id) != len(chunks) or not chunks:
        errors.append("문단 ID가 중복되거나 문단이 없습니다.")
    span_ids = []
    seen = set()
    last_source_positions = {}
    for chunk in chunks:
        spans = chunk.owned_spans
        if not spans or not chunk.evidence_id:
            errors.append("위치 근거가 없는 구형 문단은 전체 근거로 사용할 수 없습니다.")
            continue
        if chunk.evidence_id != node_id(spans[0], chunk.section_number, chunk.section_path_titles):
            errors.append("문단 ID와 원문 위치가 일치하지 않습니다.")
        doc = docs.get(spans[0].document_id)
        if doc is None or doc.source_file != chunk.source_file:
            errors.append("문단의 허가 원문 파일이 일치하지 않습니다.")
        doc_id = spans[0].document_id
        first_position = (spans[0].page_number, spans[0].char_start)
        if doc_id in last_source_positions and first_position < last_source_positions[doc_id]:
            errors.append("문단 목록의 원문 순서가 잘못되었습니다.")
        last_source_positions[doc_id] = (spans[-1].page_number, spans[-1].char_end)
        last_position = None
        for span in spans:
            span_ids.append(span.evidence_id)
            key = (span.document_id, span.page_number)
            text = pages.get(key)
            if (text is None or span.document_id != spans[0].document_id
                    or type(span.char_start) is not int or type(span.char_end) is not int
                    or not 0 <= span.char_start < span.char_end <= len(text)
                    or span.kind not in {"heading", "body"}):
                errors.append("원문 구간의 문서·페이지·문자 범위가 잘못되었습니다.")
                continue
            if (text[span.char_start:span.char_end] != span.text or span.evidence_id !=
                    _id("span", span.document_id, span.page_number, span.char_start,
                        span.char_end, span.kind, span.text)):
                errors.append("원문 인용 또는 구간 ID가 일치하지 않습니다.")
            position = (span.page_number, span.char_start)
            if last_position is not None and position < last_position:
                errors.append("문단 원문 구간의 순서가 잘못되었습니다.")
            last_position = (span.page_number, span.char_end)
            for offset in range(span.char_start, span.char_end):
                coverage[key][offset] += 1
        parent_id = chunk.parent_evidence_id
        parent = by_id.get(parent_id)
        parts = chunk.section_number.split(".") if chunk.section_number else []
        dated = _is_date_entry(chunk.section_number)
        if parent_id:
            if (parent is None or parent_id not in seen or parent.source_file != chunk.source_file
                    or dated or _is_date_entry(parent.section_number)
                    or parent.section_number.split(".") != parts[:-1]
                    or parent.section_path_titles != chunk.section_path_titles[:-1]
                    or not parent.owned_spans
                    or parent.owned_spans[0].document_id != spans[0].document_id):
                errors.append("상위 문단의 번호·계층·원문 연결이 불명확합니다.")
        elif len(parts) > 1 and not dated:
            errors.append("하위 문단의 상위 근거가 없습니다.")
        # An ancestor chain is a reference, never a duplicate of its text.
        visited = {chunk.evidence_id}
        current = parent_id
        while current:
            if current in visited or current not in by_id:
                errors.append("상위 근거 연결에 순환 또는 누락이 있습니다.")
                break
            visited.add(current)
            current = by_id[current].parent_evidence_id
        seen.add(chunk.evidence_id)
    if any(count > 1 for count in Counter(span_ids).values()):
        errors.append("같은 원문 구간이 여러 문단에 소유되어 있습니다.")
    for key, hits in coverage.items():
        if any(count > 1 or (not char.isspace() and count != 1)
               for char, count in zip(pages[key], hits)):
            errors.append(f"{key[1]}쪽 원문 소유 범위에 누락 또는 중복이 있습니다.")
    return tuple(dict.fromkeys(errors))


def build_permit_ownership_inventory(store):
    """Diagnostic graph with snapshot validation plus live extraction/path checks.

    Nonempty errors prohibit downstream decisions. This inventory does not
    infer mandatory tests, applicability, exemptions, or amendment precedence.
    """
    documents = list(getattr(store, "source_documents", []))
    chunks = store.chunks
    errors = []
    if store.extraction_errors:
        errors.append("허가서 추출 오류가 있습니다.")
    if store.policy is None or not store.policy.authoritative:
        errors.append("전체 계층을 보존하는 권위 허가 파서가 필요합니다.")
    errors.extend(validate_permit_ownership_snapshot(documents, chunks))
    for doc in documents:
        paths = [p for p in store.permit_pdf_paths if p.name == doc.source_file]
        if doc.pdf_sha256:
            if len(paths) != 1 or not paths[0].is_file():
                errors.append("허가 PDF의 연결을 유일하게 확인할 수 없습니다.")
            else:
                try:
                    current_hash = hashlib.sha256(paths[0].read_bytes()).hexdigest()
                except OSError:
                    current_hash = ""
                if current_hash != doc.pdf_sha256:
                    errors.append("추출 후 허가 PDF가 변경되었거나 읽을 수 없습니다.")
    if any(not any(d.source_file == p.name for d in documents) for p in store.permit_pdf_paths):
        errors.append("연결된 허가 PDF의 전체 추출 원문이 없습니다.")
    nodes, unscoped = [], []
    by_id = {c.evidence_id: c for c in chunks}
    for chunk in chunks:
        spans = chunk.owned_spans
        if not spans or not chunk.evidence_id:
            continue
        dated = _is_date_entry(chunk.section_number)
        if not chunk.section_number or dated:
            unscoped.append(chunk.evidence_id)
        ancestors, visited = [], {chunk.evidence_id}
        current = chunk.parent_evidence_id
        while current and current not in visited and current in by_id:
            visited.add(current)
            ancestors.append(current)
            current = by_id[current].parent_evidence_id
        nodes.append({"evidence_id": chunk.evidence_id, "source_file": chunk.source_file,
                      "document_id": spans[0].document_id, "section_number": chunk.section_number,
                      "title": chunk.title, "path_titles": list(chunk.section_path_titles),
                      "source_kind": "dated_entry" if dated else "section" if chunk.section_number else "unscoped",
                      "parent_evidence_id": chunk.parent_evidence_id, "ancestor_ids": list(reversed(ancestors)),
                      "leaf": not any(c.parent_evidence_id == chunk.evidence_id for c in chunks),
                      "owned_text": chunk.owned_text, "owned_spans": [asdict(s) for s in spans]})
    return {"schema": "permit-ownership-v1", "errors": list(dict.fromkeys(errors)),
            "offset_basis": "clean_text_normalized_physical_page",
            "documents": [{"evidence_id": d.evidence_id, "source_file": d.source_file,
                           "pdf_sha256": d.pdf_sha256, "page_count": len(d.pages)} for d in documents],
            "nodes": nodes, "unscoped_ids": unscoped}
