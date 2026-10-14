"""Source-aware presentation of permit quotations; never used for verdicts.

Raw quotes, clause IDs, offsets, and numeric inputs stay untouched. PDF layout
is used only when the file still matches the review receipt. In particular,
plain ``106`` is NOT guessed to mean ``10**6``.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .utils import clean_text

_SUPERSCRIPT = str.maketrans("0123456789+-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻")
_DOCUMENT_NUMBER = re.compile(r"문서\s*확인\s*번호\s*[:：]\s*\S.*")
_PAGE_NUMBER = re.compile(r"(?:\d+\s*/\s*\d+|[-–]\s*\d+\s*[-–])")
_STRUCTURED = re.compile(r"(?:[•●▪]|\d+(?:\.\d+)*[.)]\s|[①-⑳])")
_GROUNDED_GROUP_SOURCES = frozenset(
    {"permit_group_deterministic", "permit_group_llm_grounded"}
)


def is_permit_basis_source(source: str | None) -> bool:
    value = str(source or "")
    return value.startswith("permit_pdf") or value in _GROUNDED_GROUP_SOURCES


def _structured(line: str) -> bool:
    return "|" in line or bool(_STRUCTURED.match(line.lstrip()))


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def reflow_permit_prose(text: str) -> str:
    """Join soft wraps using the source's trailing spaces, not a word dictionary.

    ``확\n인`` -> ``확인`` but ``확인 \n결과`` -> ``확인 결과``. Table rows,
    lists and paragraph boundaries are retained. Repeated lines are never
    deduplicated: a repeated word can be part of a different condition.
    """
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    paragraphs: list[str] = []
    previous = ""
    for line in lines:
        if not line.strip():
            if paragraphs and paragraphs[-1]:
                paragraphs.append("")
            previous = ""
            continue
        if not previous or _structured(line) or _structured(previous):
            paragraphs.append(line.strip())
        else:
            # A PDF wrap can split a Korean/Latin word. Explicit source spaces
            # distinguish that from a word boundary; sentence ends need space.
            space = bool(previous[-1:].isspace() or line[:1].isspace()
                         or re.search(r"[.!?。:]$", previous.rstrip()))
            paragraphs[-1] += (" " if space else "") + line.strip()
        previous = line
    return "\n".join(paragraphs).strip()


@dataclass(frozen=True)
class PermitTextView:
    furniture: frozenset[str] = frozenset()
    line_edits: tuple[tuple[str, str], ...] = ()
    guarded_permit_orders: frozenset[int] = frozenset()
    guarded_permit_positions: frozenset[int] = frozenset()

    def has_permit_basis(self, evaluation) -> bool:
        source = getattr(evaluation, "source", "") or ""
        # MD guards replace the verdict/source but retain normalized_criteria.
        # Recover its provenance only from the saved guard audit, never from
        # the test name or a criterion that happens to resemble a permit.
        if is_permit_basis_source(source):
            return True
        if source != "md_policy_guard":
            return False
        position = getattr(evaluation, "record_position", None)
        if isinstance(position, int):
            return position in self.guarded_permit_positions
        return getattr(evaluation, "order_idx", None) in self.guarded_permit_orders

    def format_quote(self, text: str | None) -> str:
        raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = raw.split("\n")
        # When a historic source PDF is unavailable, only an explicitly labelled
        # document-number cluster is recognizable. A lone 3/4 remains a result.
        headers = {i for i, line in enumerate(lines) if _DOCUMENT_NUMBER.fullmatch(line.strip())}
        last = max((i for i, line in enumerate(lines) if line.strip()), default=-1)
        removed = set(headers)
        for i, line in enumerate(lines):
            key = _key(line)
            nearby_header = any(abs(i - h) <= 2 for h in headers)
            previous = next((p.rstrip() for p in reversed(lines[:i]) if p.strip()), "")
            verified_trailer = (key in self.furniture and i == last
                                and previous.endswith((".", "。")))
            if _PAGE_NUMBER.fullmatch(key) and (nearby_header or verified_trailer):
                removed.add(i)
        # Blank lines belonging to a removed page header/footer are layout too,
        # not an intentional paragraph break inside the acceptance sentence.
        for _ in range(2):
            removed.update(i for i, line in enumerate(lines) if not line.strip()
                           and (i - 1 in removed or i + 1 in removed))
        edits = dict(self.line_edits)
        kept = []
        for i, line in enumerate(lines):
            key = _key(line)
            if i in removed:
                continue
            if key in edits:
                trailing = " " if line[-1:].isspace() else ""
                line = edits[key] + trailing
            kept.append(line)
        return reflow_permit_prose("\n".join(kept))


EMPTY_VIEW = PermitTextView()


@lru_cache(maxsize=16)
def _pdf_view(path: str, expected_sha: str, size: int, mtime_ns: int) -> PermitTextView:
    import fitz

    file = Path(path)
    # stat keys avoid repeatedly opening/hashing a long PDF during Streamlit
    # reruns. Hash binding prevents using a replaced permit for an old verdict.
    digest = hashlib.sha256()
    with file.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    actual = digest.hexdigest()
    if actual != expected_sha:
        return EMPTY_VIEW
    furniture: set[str] = set()
    variants: dict[str, set[str]] = {}
    with fitz.open(file) as document:
        for page in document:
            # Typography needs text spans only, not embedded image byte arrays.
            flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
            for block in page.get_text("dict", flags=flags)["blocks"]:
                for line in block.get("lines", []):
                    spans = line["spans"]
                    raw = "".join(span["text"] for span in spans)
                    key = _key(raw)
                    edge = line["bbox"][1] < page.rect.height * .14 or line["bbox"][3] > page.rect.height * .85
                    if edge and (_DOCUMENT_NUMBER.fullmatch(key) or _PAGE_NUMBER.fullmatch(key)):
                        furniture.add(key)
                    rendered = ""
                    for span in spans:
                        value = span["text"]
                        # Font flag 1 is superscript in PyMuPDF. Never infer an
                        # exponent from the digits/threshold/test name alone.
                        if span["flags"] & 1 and re.fullmatch(r"[+\-]?\d+\s*", value):
                            value = value.translate(_SUPERSCRIPT)
                        rendered += value
                    variants.setdefault(key, set()).add(_key(rendered))
    # Ambiguous identical lines with different typography are not rewritten.
    edits = tuple((key, next(iter(values))) for key, values in variants.items()
                  if len(values) == 1 and key not in values)
    return PermitTextView(frozenset(furniture), edits)


def permit_view_for_result(result) -> PermitTextView:
    meta = getattr(result, "metadata", {}) or {}
    receipts = meta.get("review_receipt", {}).get("permits", [])
    views = []
    permit_paths = (
        getattr(result, "runtime_permit_paths", ())
        or meta.get("permit_paths", [])
    )
    for path in permit_paths:
        file = Path(path)
        hashes = {r.get("sha256") for r in receipts if r.get("name") == file.name and r.get("sha256")}
        if len(hashes) != 1:
            continue
        try:
            stat = file.stat()
            views.append(_pdf_view(str(file.resolve()), hashes.pop(), stat.st_size, stat.st_mtime_ns))
        except (OSError, ValueError, RuntimeError):
            # An unavailable original must not break display of a saved review.
            continue
    variants: dict[str, set[str]] = {}
    for view in views:
        for key, value in view.line_edits:
            variants.setdefault(key, set()).add(value)
    # Without a per-quote document binding, multiple permit files may contain
    # visually identical digit strings with different meanings. Do not guess.
    edits = tuple((k, next(iter(v))) for k, v in variants.items() if len(v) == 1) if len(views) == 1 else ()
    guard_audit = [
        row for row in meta.get("policy_test_guard_audit", [])
        if isinstance(row, dict)
        and is_permit_basis_source(row.get("previous_source"))
    ]
    guarded_positions = frozenset(
        row["record_position"] for row in guard_audit
        if type(row.get("record_position")) is int
    )
    guarded_orders = frozenset(
        row["order_idx"] for row in guard_audit
        if type(row.get("order_idx")) is int
        and type(row.get("record_position")) is not int
    )
    return PermitTextView(
        frozenset().union(*(v.furniture for v in views)),
        edits,
        guarded_orders,
        guarded_positions,
    )


def permit_reason_view(reason: str | None, criteria: str | None, view: PermitTextView) -> str:
    """Only reformat the permit quotation, never the SP result or MD reason."""
    text = str(reason or "")
    basis = clean_text(criteria)
    if basis and basis in text:
        return text.replace(basis, view.format_quote(basis))
    # A clause-specific/table-row reason may quote a subset of the main basis.
    formatted = re.sub(
        r"(허가서 기준(?:은)? ')([\s\S]*?)('(?:을|이고|이나|이))",
        lambda match: match[1] + view.format_quote(match[2]) + match[3],
        text,
    )
    formatted = re.sub(
        r"(허가서에\s*')([\s\S]*?)('\s*기준(?:이|은|으로)?)",
        lambda match: match[1] + view.format_quote(match[2]) + match[3],
        formatted,
    )
    return re.sub(
        r"(허가\s*기준:\s*)([\s\S]*?)(?=\s+SP\s*결과:|$)",
        lambda match: match[1] + view.format_quote(match[2]),
        formatted,
    )
