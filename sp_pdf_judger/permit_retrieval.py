"""Exact, stage-scoped lookup in front of the unchanged legacy search."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from .permit_models import PermitChunk
from .schemas import ExtractedRecord


@dataclass(frozen=True)
class RetrievalSelection:
    chunks: tuple[PermitChunk, ...]
    route: str
    complete: bool
    scored_count: int
    cache_hit: bool


class PermitRetrievalIndex:
    def __init__(self, chunks: Sequence[PermitChunk], *, source_fingerprint: str,
                 aliases: Mapping[str, Sequence[str]], exact_enabled: bool = True):
        from .permit_pdf_store import _compact_identity, _specific_stage_identity
        self.chunks = tuple(chunks)
        self.source_fingerprint = source_fingerprint
        self.exact_enabled = exact_enabled
        self.aliases = MappingProxyType({key: frozenset(values) for key, values in aliases.items()})
        lookup = {}
        for position, chunk in enumerate(self.chunks):
            test = _compact_identity(chunk.normalized_test_name or chunk.title, strip_number=True)
            stages = {_specific_stage_identity(p) for p in chunk.section_path_titles[:-1]} - {''}
            for stage in stages:
                lookup.setdefault((stage, test), []).append(position)
        self.lookup = MappingProxyType({key: tuple(values) for key, values in lookup.items()})
        self._queries = {}

    def select(self, record: ExtractedRecord, *, fallback: Callable, top_k: int = 12) -> RetrievalSelection:
        from .permit_pdf_store import _compact_identity, _specific_stage_identity
        # Include ALL record fields, not merely a convenient subset. Legacy search
        # may acquire another field later without producing stale query hits.
        key = (top_k, json.dumps(vars(record), ensure_ascii=False, sort_keys=True, default=str))
        if key in self._queries:
            return replace(self._queries[key], cache_hit=True)
        stage = _specific_stage_identity(record.section_title or '')
        test = _compact_identity(record.test_name or '', strip_number=True)
        positions = set()
        if self.exact_enabled and stage and test:
            for name in {test, *self.aliases.get(test, ())}:
                positions.update(self.lookup.get((stage, name), ()))
        if positions:
            # No top_k truncation of conflicting exact evidence.
            selection = RetrievalSelection(tuple(self.chunks[i] for i in sorted(positions)),
                                           'exact_stage_test', True, 0, False)
        else:
            selection = RetrievalSelection(tuple(fallback(record, top_k)), 'legacy_fallback',
                                           False, len(self.chunks), False)
        self._queries[key] = selection
        return selection
