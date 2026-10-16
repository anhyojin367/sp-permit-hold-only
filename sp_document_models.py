"""Stable import identities for Streamlit caches and dialog fragments."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InboxDocument:
    path: Path
    company: str
    product: str
    title: str
    product_number: str
    version: str
    received_date: str
    subject: str
    source: str = "Gmail"
    permit_files: tuple[Path, ...] = ()
    product_mapping_id: str = ""
    permit_link_error: str = ""


@dataclass(frozen=True)
class ReferenceDocument:
    company: str
    product: str
    title: str
    doc_id: str
    version: str
    effective_date: str
    status: str
    pages: int
    checks: int
    owner: str
