from __future__ import annotations

import re
from collections import defaultdict
from typing import Mapping

from .utils import clean_text


CID_MARKER_RE = re.compile(r"\(cid:\d+\)", re.IGNORECASE)
_CORRUPT_MARKER_RE = re.compile(r"\(cid:\d+\)|\uFFFD", re.IGNORECASE)
_SCIENTIFIC_CID_RE = re.compile(
    r"(?P<mantissa>\d+(?:[.,]\d+)?)\s*[xX×]\s*10"
    r"(?P<cid>\(cid:\d+\))\s*"
    r"(?P<unit>[A-Za-zµμ]+(?:\s*/\s*[A-Za-z]+)?)",
    re.IGNORECASE,
)
_SUPERSCRIPT_TO_DIGIT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def has_corrupt_marker(text: str | None) -> bool:
    return bool(_CORRUPT_MARKER_RE.search(str(text or "")))


def infer_scientific_cid_replacements(
    native_page_text: str,
    ocr_page_text: str,
) -> dict[str, str]:
    """Ground CID exponent replacements in matching OCR mantissa/unit context."""
    native = re.sub(r"\s+", " ", clean_text(native_page_text))
    ocr = re.sub(r"\s+", " ", clean_text(ocr_page_text).translate(_SUPERSCRIPT_TO_DIGIT))
    if not native or not ocr:
        return {}

    candidates: dict[str, set[str]] = defaultdict(set)
    for match in _SCIENTIFIC_CID_RE.finditer(native):
        mantissa = match.group("mantissa")
        cid = match.group("cid")
        unit = re.sub(r"\s+", "", match.group("unit"))
        unit_pattern = r"\s*/\s*".join(re.escape(part) for part in unit.split("/"))
        context_pattern = re.compile(
            rf"(?<![\d.]){re.escape(mantissa)}\s*[xX×]\s*10\s*\^?\s*"
            rf"(?P<exponent>\d{{1,2}})(?!\d)\s*{unit_pattern}(?![A-Za-z])",
            re.IGNORECASE,
        )
        candidates[cid].update(
            found.group("exponent") for found in context_pattern.finditer(ocr)
        )

    return {
        cid: f"^{next(iter(exponents))}"
        for cid, exponents in candidates.items()
        if len(exponents) == 1
    }


def apply_cid_replacements(text: str | None, replacements: Mapping[str, str]) -> str:
    recovered = str(text or "")
    for cid, value in replacements.items():
        recovered = re.sub(re.escape(cid), lambda _match, replacement=value: replacement, recovered, flags=re.IGNORECASE)
    return recovered
