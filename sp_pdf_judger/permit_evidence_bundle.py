"""Complete source-linked evidence for authoritative semantic requests."""
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping
from .permit_llm_protocol import PermitCandidate, _without_page_furniture
from .permit_source_ownership import PermitSourceSpan, make_span, node_id
from .permit_semantic_settings import BUNDLE_VERSION

_REFERENCE = re.compile(r'(?<![\d.])(\d+(?:\.\d+)+)\.?\s*(?:항|절|에\s*따른|에\s*따라|에\s*준|참조)')
_RELATIVE_REFERENCE = re.compile(r'(?:위|상기|전술한|앞의)\s*(?:항|시험|방법)|전항|전절')


@dataclass(frozen=True)
class EvidenceBundle:
    bundle_id: str
    candidates: tuple[PermitCandidate, ...]
    spans: tuple[PermitSourceSpan, ...]
    complete: bool
    errors: tuple[str, ...]
    clause_span_ids: Mapping[tuple[int, int], tuple[str, ...]]


def build_evidence_bundle(selection, *, all_chunks, documents, company, product):
    errors = []
    if not selection.complete:
        errors.append('retrieval_incomplete')
    documents = tuple(documents)
    docs = {doc.evidence_id: doc for doc in documents}
    nodes = {}
    children = {}
    numbers = {}
    for chunk in all_chunks:
        if chunk.evidence_id in nodes:
            errors.append('duplicate_node')
        nodes[chunk.evidence_id] = chunk
        children.setdefault(chunk.parent_evidence_id, []).append(chunk)
        if chunk.owned_spans:
            numbers.setdefault((chunk.owned_spans[0].document_id, chunk.section_number), []).append(chunk)
    all_spans, candidates, clause_spans, relations = {}, [], {}, []

    def document_id(chunk):
        return chunk.owned_spans[0].document_id if chunk.owned_spans else ''

    for selected in selection.chunks:
        doc_id = document_id(selected)
        included = {}
        visiting = set()
        expanded = set()

        def include(chunk, *, descendants=False):
            key = chunk.evidence_id
            if key in visiting:
                errors.append('relationship_cycle')
                return
            if not key or nodes.get(key) != chunk or document_id(chunk) != doc_id or doc_id not in docs:
                errors.append('invalid_source_node')
                return
            if key in included and (not descendants or key in expanded):
                return
            visiting.add(key)
            included[key] = chunk
            if descendants:
                expanded.add(key)
            # Ancestors contribute ONLY their owned spans, never expanded text.
            parent_id = chunk.parent_evidence_id
            if parent_id:
                parent = nodes.get(parent_id)
                if parent is None or document_id(parent) != doc_id:
                    errors.append('broken_parent')
                elif (parent.section_number.split('.') != chunk.section_number.split('.')[:-1]
                      or parent.section_path_titles != chunk.section_path_titles[:-1]):
                    errors.append('invalid_parent_path')
                elif parent_id not in included:
                    include(parent)
                elif parent_id == key:
                    errors.append('relationship_cycle')
            reference_body = '\n'.join(s.text for s in chunk.owned_spans if s.kind == 'body')
            if _RELATIVE_REFERENCE.search(reference_body):
                errors.append('unresolved_relative_reference')
            for match in _REFERENCE.finditer(reference_body):
                targets = numbers.get((doc_id, match[1]), [])
                if len(targets) != 1:
                    errors.append('unresolved_reference')
                elif targets[0].evidence_id in visiting:
                    errors.append('reference_cycle')
                else:
                    relations.append((key, targets[0].evidence_id))
                    include(targets[0], descendants=True)
            if descendants:
                for child in children.get(key, ()):
                    if child.evidence_id in visiting:
                        errors.append('relationship_cycle')
                    else:
                        include(child, descendants=True)
            visiting.remove(key)

        include(selected, descendants=True)
        spans = {}
        for chunk in included.values():
            if not chunk.owned_spans or chunk.evidence_id != node_id(chunk.owned_spans[0], chunk.section_number, chunk.section_path_titles):
                errors.append('invalid_node_id')
            relations.append((chunk.evidence_id, chunk.parent_evidence_id))
            for span in chunk.owned_spans:
                doc = docs.get(span.document_id)
                page = dict(doc.pages).get(span.page_number) if doc else None
                if (span.document_id != doc_id or doc is None or doc.source_file != chunk.source_file
                    or page is None or not 0 <= span.char_start < span.char_end <= len(page)
                    or make_span(doc, span.page_number, page, span.char_start, span.char_end, span.kind) != span):
                    errors.append('invalid_source_span')
                    continue
                spans[span.evidence_id] = span
        ordered = tuple(sorted(spans.values(), key=lambda s: (s.page_number, s.char_start)))
        all_spans.update(spans)
        # Keep literal headings: they preserve the target of composite clauses.
        # No generated preamble may become part of a source quotation.
        body = '\n'.join(s.text for s in ordered).strip()
        clauses = tuple(p.strip() for p in re.split(r'(?<=다\.)\s+|(?<=함\.)\s+', _without_page_furniture(body)) if p.strip())
        if not ordered or not clauses:
            errors.append('empty_source')
            continue
        candidate_id = len(candidates) + 1
        candidates.append(PermitCandidate(candidate_id, selected.title, ' > '.join(selected.section_path_titles[:-1]),
                                          body, clauses, selected.source_file, selected.page_number))
        # Map each sentence to its exact contributing spans (including page breaks).
        ranges, offset = [], 0
        for span in ordered:
            ranges.append((offset, offset + len(span.text), span.evidence_id))
            offset += len(span.text) + 1
        cursor = 0
        for clause_id, clause in enumerate(clauses, 1):
            # Furniture removal can make a clause non-contiguous; link all literal
            # parts, never invent an LLM quote or lose its source page.
            ids = []
            for part in clause.splitlines():
                start = body.find(part, cursor)
                if start < 0:
                    errors.append('clause_source_missing')
                    continue
                end = start + len(part)
                ids.extend(sid for a, b, sid in ranges if a < end and b > start)
                cursor = end
            clause_spans[(candidate_id, clause_id)] = tuple(dict.fromkeys(ids))
            if not ids:
                errors.append('clause_without_source')
    payload = [BUNDLE_VERSION, company, product, [vars(c) for c in candidates],
               sorted(all_spans), sorted(relations)]
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    if not candidates:
        errors.append('empty_selection')
    return EvidenceBundle(digest, tuple(candidates), tuple(all_spans.values()), not errors,
                          tuple(dict.fromkeys(errors)), MappingProxyType(clause_spans))


def render_bundle_context(bundle):
    return '\n\n'.join(f'[허가서 근거 {c.candidate_id}]\n- 파일: {c.source_file}\n- 페이지: {c.page_number}\n'
                       f'- 섹션: {c.title}\n- 경로: {c.stage}\n- 내용:\n{c.text}' for c in bundle.candidates)
