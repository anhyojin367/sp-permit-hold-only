# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader

from sp_gmail_ingest import INDEX_FILE_NAME


@dataclass(frozen=True)
class DocumentMeta:
    company: str
    title: str
    product_number: str
    version: str
    received_date: str
    subject: str


def get_document_meta(pdf_path: str | Path) -> DocumentMeta:
    path = Path(pdf_path)
    index_record = _load_index_record(path)
    text = _extract_front_text(path)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    compact = _compact(text)

    return DocumentMeta(
        company=_extract_company(lines, compact),
        title=_extract_title(path, lines, compact, index_record),
        product_number=_extract_number(compact),
        version=_extract_version(compact),
        received_date=_extract_received_date(path, index_record),
        subject=str(index_record.get("subject", "")),
    )


def _extract_front_text(path: Path, max_pages: int = 4) -> str:
    try:
        reader = PdfReader(str(path))
        chunks: list[str] = []
        for page in reader.pages[:max_pages]:
            chunks.append(page.extract_text() or "")
        return "\n".join(chunks)
    except Exception:
        return ""


def _load_index_record(path: Path) -> dict:
    index_path = path.parent / INDEX_FILE_NAME
    if not index_path.exists():
        return {}
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return index.get("files", {}).get(path.name, {})


def _extract_company(lines: list[str], compact: str) -> str:
    patterns = [
        r"회사명\s*[:：]?\s*([가-힣A-Za-z0-9().,\s·㈜주식회사-]{2,40})",
        r"제\s*조\s*원\s*[:：]?\s*([가-힣A-Za-z0-9().,\s·㈜주식회사-]{2,40})",
        r"제조사\s*명칭\s*([가-힣A-Za-z0-9().,\s·㈜주식회사-]{2,40})",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            return _clean_value(match.group(1), stop_words=("주소", "제조년월일", "제조번호", "부서명"))

    for line in lines[:12]:
        if "Summary Protocol" in line:
            before = line.split("Summary Protocol", 1)[0].strip()
            if before and len(before) <= 40:
                return _clean_value(before)
        if any(token in line for token in ("주식회사", "(주)", "㈜")) and len(line) <= 50:
            return _clean_value(line)

    return "제조사 미확인"


def _extract_title(path: Path, lines: list[str], compact: str, index_record: dict) -> str:
    product_name = _extract_product_name(lines, compact)
    document_type = _extract_document_type(compact)
    if product_name != "미확인":
        if document_type:
            return f"{product_name} {document_type}"[:60]
        return f"{product_name} 요약서"[:60]

    title_patterns = [
        r"([가-힣A-Za-z0-9().,\s·-]{0,35}제조\s*및\s*품질(?:관리)?요약서)",
        r"([가-힣A-Za-z0-9().,\s·-]{0,35}품질(?:관리)?요약서)",
    ]
    for pattern in title_patterns:
        match = re.search(pattern, compact)
        if match:
            return _clean_value(match.group(1), stop_words=("제조번호", "부서명", "회사명"))[:42]

    for line in lines[:16]:
        if "요약서" in line or "Summary Protocol" in line:
            return _clean_value(line)[:42]

    subject = str(index_record.get("subject", "")).strip()
    if subject:
        cleaned = re.sub(r"^\s*\[[^\]]+\]\s*", "", subject).strip()
        if cleaned:
            return cleaned[:42]

    return re.sub(r"^\d{8}_\d{6}_", "", path.stem)[:42]


def _extract_document_type(compact: str) -> str:
    patterns = [
        r"제조\s*및\s*품질\s*관리\s*요약서",
        r"제조\s*및\s*품질\s*요약서",
        r"품질\s*관리\s*요약서",
        r"품질\s*요약서",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            matched = re.sub(r"\s+", "", match.group(0))
            if matched == "제조및품질관리요약서":
                return "제조 및 품질관리요약서"
            if matched == "제조및품질요약서":
                return "제조 및 품질요약서"
            if matched == "품질관리요약서":
                return "품질관리요약서"
            if matched == "품질요약서":
                return "품질요약서"
    return ""


def _extract_product_name(lines: list[str], compact: str) -> str:
    patterns = [
        r"제품명\s*([가-힣A-Za-z0-9().,\s·-]{2,40})",
        r"-\s*\d+\s*-\s*([가-힣A-Za-z0-9().,\s·-]{2,40})\s*(?:\(|1\.)",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            value = _clean_value(match.group(1), stop_words=("생기수재명", "포장단위", "제조번호"))
            return value.split("(", 1)[0].strip() or value

    for line in lines[:20]:
        if 2 <= len(line) <= 40 and not any(skip in line for skip in ("Summary Protocol", "제조번호", "페이지")):
            if re.search(r"[가-힣]", line) and not re.fullmatch(r"[-\d/\s]+", line):
                value = _clean_value(line)
                return value.split("(", 1)[0].strip() or value
    return "미확인"


def _extract_number(compact: str) -> str:
    patterns = [
        r"제품번호\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9._/\-\s]{2,80})",
        r"제조번호\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9._/\-\s]{2,80})",
        r"Lot\s*(?:No\.?|Number)?\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9._/\-\s]{2,80})",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact, re.IGNORECASE)
        if match:
            return _clean_identifier(match.group(1))
    return "미확인"


def _clean_identifier(value: str) -> str:
    value = re.sub(r"\s*-\s*", "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .-/")
    parts = value.split()
    if not parts:
        return "미확인"

    first = parts[0]
    if len(parts) > 1 and re.fullmatch(r"\d+/\d+", parts[1]):
        return first
    return first


def _extract_version(compact: str) -> str:
    patterns = [
        r"버전\s*[:：]?\s*(v?\d+(?:\.\d+)*)",
        r"\bver(?:sion)?\.?\s*[:：]?\s*(v?\d+(?:\.\d+)*)",
        r"\bv\s*[:：]?\s*(\d+(?:\.\d+)*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            return value if value.lower().startswith("v") else f"v{value}"
    return ""


def _extract_received_date(path: Path, index_record: dict) -> str:
    raw = str(index_record.get("received_at", "")).strip()
    if raw:
        try:
            return datetime.fromisoformat(raw).strftime("%Y.%m.%d")
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y.%m.%d")


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clean_value(value: str, stop_words: tuple[str, ...] = ()) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip(" :：,")
    for word in stop_words:
        if word in cleaned:
            cleaned = cleaned.split(word, 1)[0].strip(" :：,")
    return cleaned or "미확인"
