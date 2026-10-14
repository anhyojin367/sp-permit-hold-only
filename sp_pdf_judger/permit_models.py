"""Immutable permit evidence and condition IR; no storage or verdict behavior."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .permit_source_ownership import PermitSourceSpan
from .utils import clean_text


@dataclass(frozen=True)
class PermitChunk:
    source_file: str
    page_number: int
    section_number: str
    title: str
    text: str
    page_start: int | None = None
    page_end: int | None = None
    section_path_titles: tuple[str, ...] = ()
    normalized_test_name: str = ""
    normalized_stage_path: str = ""
    # Additive provenance: .text retains legacy descendant-expanded retrieval.
    owned_spans: tuple[PermitSourceSpan, ...] = ()
    evidence_id: str = ""
    parent_evidence_id: str = ""

    @property
    def owned_text(self) -> str:
        return clean_text("\n".join(span.text for span in self.owned_spans))


@dataclass(frozen=True)
class PermitCondition:
    condition_id: str
    evidence_id: str
    clause_index: int
    kind: str
    operator: str | None
    value: Decimal | None
    unit: str | None
    lower: Decimal | None
    upper: Decimal | None
    lower_inclusive: bool
    upper_inclusive: bool
    children: tuple[str, ...]
    quote: str
    stage_key: str
    test_key: str
    metric_key: str
    mandatory: bool = True
    method_key: str | None = None
    scope_text: str = ""
