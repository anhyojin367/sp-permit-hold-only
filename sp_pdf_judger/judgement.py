from __future__ import annotations

import re
import hashlib
import json
from contextvars import ContextVar
from dataclasses import asdict
from typing import TYPE_CHECKING, Mapping, Sequence

from .config import FAIL_LABEL, HOLD_LABEL, PASS_LABEL
from .criteria_parser import parse_criteria_text
from .llm import ClovaJudgeClient
from .clova_transport import ClovaServiceError
from .permit_pdf_store import PermitPdfStore
from .permit_catalog import PermitPolicy
from .rag import UcumRagStore
from .schemas import Evaluation, ExtractedRecord
from .unit_normalizer import ParsedMeasurement, parse_number_and_unit
from .utils import clean_text

if TYPE_CHECKING:
    from .permit_group_judgement import GroupVerdict, PreparedPermitGroup
    from .permit_grouping import PermitGroup
    from .permit_llm_protocol import GroundedGroupResponse
    from .permit_models import PermitCondition


PASS_REASON_WORD = "검수합격"
FAIL_REASON_WORD = "검수불합격"
HOLD_REASON_WORD = "검수보류"
_SEMANTIC_PREPARED = ContextVar('semantic_prepared_response', default=None)


def _permit_has_fatal_extraction_error(errors: object) -> bool:
    """Classify content-completeness failures separately from operational warnings."""
    if not errors:
        return False
    fatal_patterns = (
        r"^permit ocr page .+ is unreadable$",
        r"^could not read permit pdf:",
        r"^could not extract permit .+",
        r"^could not render permit ocr pages:",
    )
    return any(
        any(re.search(pattern, clean_text(error).casefold()) for pattern in fatal_patterns)
        for error in errors if clean_text(error)
    )


def _authoritative_permit_evidence(permit_context: str) -> str:
    """Keep permit chunk evidence while preserving source content verbatim."""
    evidence: list[str] = []
    in_source_content = False
    for raw_line in permit_context.splitlines():
        line = clean_text(raw_line)
        if line.startswith("[허가서 근거 "):
            in_source_content = False
            continue
        if not line or line.startswith("[허가서 PDF 판정 후보 문단]"):
            continue
        if line.startswith("아래 내용은 허가서 PDF") or line.startswith("현재 SP 시험명과"):
            continue
        if not in_source_content:
            if line == "- 내용:":
                in_source_content = True
            elif line.startswith("- 섹션:") or line.startswith("- 경로:"):
                evidence.append(line.split(":", 1)[1].strip())
            # Generated file/page labels, and malformed content labels before
            # the exact body marker, are metadata rather than evidence.
            continue
        evidence.append(line)
    return "\n".join(evidence)


def _sanitize_user_reason(text: str | None) -> str:
    text = clean_text(text)
    if not text:
        return ""

    replacements = {
        "Gemini 2차 판정": "추가 판정",
        "2차 Gemini 판정": "추가 판정",
        "2차 Gemini": "추가 판정",
        "Gemini": "추가 판정",
        "LLM": "추가 판정",
        "3차 허가서 판정": "허가서 기준 확인",
        "1차 규칙 및 2차 Gemini": "문서 기준과 참고 근거",
        "1차 규칙/JSON": "문서 기준",
        "1차 규칙": "문서 기준",
    }

    for src, dst in replacements.items():
        text = text.replace(src, dst)

    return clean_text(text)


def _format_rag_doc_context(doc: object) -> str:
    text = clean_text(getattr(doc, "text", ""))
    meta = getattr(doc, "meta", {}) or {}
    domain = clean_text(meta.get("rag_domain")) or "참고자료"
    source_file = clean_text(meta.get("rag_source_file")) or "자료"
    page = meta.get("page")
    page_text = f" · {page}쪽" if page else ""

    if len(text) > 1800:
        text = text[:1800] + " ..."

    return f"[{domain}] {source_file}{page_text}\n{text}"


def _same_or_convertible(a: ParsedMeasurement, b: ParsedMeasurement) -> bool:
    if a.unit_canonical == b.unit_canonical:
        return True

    if "scalar" in {a.unit_canonical, b.unit_canonical}:
        return True

    return False


def _compare(a: float, op: str, b: float) -> bool:
    if op == "<=":
        return a <= b
    if op == "<":
        return a < b
    if op == ">=":
        return a >= b
    if op == ">":
        return a > b
    if op == "==":
        return a == b

    raise ValueError(f"unknown op: {op}")


def _unit_mismatch_reason() -> str:
    return f"시험결과와 시험기준의 단위가 일치하지 않아 {FAIL_REASON_WORD}으로 판단했습니다."


def _compare_reason(op: str, ok: bool) -> str:
    if op == "<":
        return (
            f"시험결과가 시험기준보다 낮아 {PASS_REASON_WORD}으로 판단했습니다."
            if ok
            else f"시험결과가 시험기준보다 낮지 않아 {FAIL_REASON_WORD}으로 판단했습니다."
        )

    if op == "<=":
        return (
            f"시험결과가 시험기준 이하라 {PASS_REASON_WORD}으로 판단했습니다."
            if ok
            else f"시험결과가 시험기준을 초과해 {FAIL_REASON_WORD}으로 판단했습니다."
        )

    if op == ">":
        return (
            f"시험결과가 시험기준보다 높아 {PASS_REASON_WORD}으로 판단했습니다."
            if ok
            else f"시험결과가 시험기준보다 높지 않아 {FAIL_REASON_WORD}으로 판단했습니다."
        )

    if op == ">=":
        return (
            f"시험결과가 시험기준 이상이라 {PASS_REASON_WORD}으로 판단했습니다."
            if ok
            else f"시험결과가 시험기준보다 낮아 {FAIL_REASON_WORD}으로 판단했습니다."
        )

    if op == "==":
        return (
            f"시험결과가 시험기준과 같아 {PASS_REASON_WORD}으로 판단했습니다."
            if ok
            else f"시험결과가 시험기준과 달라 {FAIL_REASON_WORD}으로 판단했습니다."
        )

    return "시험결과와 시험기준을 비교해 판단했습니다."


def _normalize_table_text(text: str | None) -> str:
    text = clean_text(text)

    if not text:
        return ""

    text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    return text.strip()


def _status_from_text_result(result_text: str | None) -> tuple[str | None, str | None]:
    text = clean_text(result_text)

    if not text:
        return None, None

    compact = text.replace(" ", "")

    if "부적합" in compact or "불합격" in compact:
        return FAIL_LABEL, f"시험결과가 '{text}'로 기재되어 {FAIL_REASON_WORD}으로 판단했습니다."

    if "적합" in compact or "합격" in compact:
        return PASS_LABEL, f"시험결과가 '{text}'로 기재되어 {PASS_REASON_WORD}으로 판단했습니다."

    return None, None


def _normalize_inline_pipe_table(text: str) -> str:
    text = _normalize_table_text(text)

    for header in ["시험결과", "실험결과", "측정결과"]:
        text = re.sub(
            rf"({header})[ \t]+([^|\n]+?)\s*\|",
            r"\1 | \2 |",
            text,
        )

    text = re.sub(
        r"(적합|부적합|합격|불합격)[ \t]+([^|\n]+?)\s*\|",
        r"\1 | \2 |",
        text,
    )

    return text


def _split_pipe_cells(line: str) -> list[str]:
    return [clean_text(x) for x in line.split("|") if clean_text(x)]


def _parse_pipe_rows_with_wrapped_cells(lines: list[str], col_count: int) -> list[list[str]]:
    """
    Pipe table rows can be split by PDF/OCR line wrapping inside a cell.

    Example:
      Bpy V | 2021.10.12 ~
      2021.10.26 | 적합

    should become:
      [Bpy V, 2021.10.12 ~ 2021.10.26, 적합]

    This helper is used only for pipe-table parsing so ordinary text blocks are
    not affected.
    """
    parsed_rows: list[list[str]] = []
    pending: list[str] | None = None

    for line in lines:
        cells = _split_pipe_cells(line)

        if not cells:
            continue

        if pending is None:
            if (
                line.lstrip().startswith("|")
                and parsed_rows
                and len(cells) == 1
                and col_count >= 3
                and ("~" in clean_text(parsed_rows[-1][col_count - 2]))
            ):
                parsed_rows[-1][col_count - 2] = clean_text(
                    f"{parsed_rows[-1][col_count - 2]} {cells[0]}"
                )
                continue

            if len(cells) >= col_count:
                for start in range(0, len(cells), col_count):
                    row = cells[start : start + col_count]
                    if len(row) == col_count:
                        parsed_rows.append(row)
                    else:
                        pending = row
                continue

            pending = cells
            continue

        if len(pending) < col_count:
            pending[-1] = clean_text(f"{pending[-1]} {cells[0]}")
            pending.extend(cells[1:])

        if len(pending) >= col_count:
            parsed_rows.append(pending[:col_count])
            pending = pending[col_count:] or None

            while pending and len(pending) >= col_count:
                parsed_rows.append(pending[:col_count])
                pending = pending[col_count:] or None

    if pending and len(pending) == col_count:
        parsed_rows.append(pending)

    return parsed_rows


def _parse_horizontal_result_table(result: str | None) -> list[dict[str, str]]:
    text = _normalize_table_text(result)

    if not text or "|" not in text:
        return []

    lines = [clean_text(x) for x in text.splitlines() if clean_text(x) and "|" in x]

    if not lines:
        return []

    first_cells = [_split_pipe_cells(line)[0] for line in lines if _split_pipe_cells(line)]

    if not first_cells:
        return []

    has_lot = any("로트" in x for x in first_cells)
    has_result = any("시험결과" in x or "실험결과" in x or "측정결과" in x for x in first_cells)

    if not (has_lot and has_result):
        return []

    row_map: dict[str, list[str]] = {}
    row_order: list[str] = []

    for line in lines:
        parts = _split_pipe_cells(line)

        if len(parts) < 2:
            continue

        label = parts[0]
        values = parts[1:]

        if label not in row_map:
            row_map[label] = []
            row_order.append(label)

        row_map[label].extend(values)

    lot_row_key = next((k for k in row_order if "로트" in k), None)
    result_row_key = next((k for k in row_order if "시험결과" in k or "실험결과" in k or "측정결과" in k), None)

    if not lot_row_key or not result_row_key:
        return []

    date_row_key = next((k for k in row_order if "시험기간" in k or "시험일자" in k or "시험일" in k), None)

    lot_numbers = row_map.get(lot_row_key, [])
    results = row_map.get(result_row_key, [])
    dates = row_map.get(date_row_key, []) if date_row_key else []

    out: list[dict[str, str]] = []

    for idx, lot in enumerate(lot_numbers):
        lot_clean = clean_text(lot)

        if not lot_clean:
            continue

        date_value = clean_text(dates[idx]) if idx < len(dates) else ""

        if date_value == "페이지":
            date_value = ""

        out.append(
            {
                "item_label": "로트번호",
                "item_value": lot_clean,
                "lot_no": lot_clean,
                "test_date": date_value,
                "result": clean_text(results[idx]) if idx < len(results) else "",
                "display_columns": ["로트번호"]
                + (["시험기간"] if date_row_key else [])
                + ["시험결과"],
                "display_values": {
                    "로트번호": lot_clean,
                    **({"시험기간": date_value} if date_row_key else {}),
                    "시험결과": clean_text(results[idx]) if idx < len(results) else "",
                },
            }
        )

    return out


def _parse_vertical_column_table(result: str | None) -> list[dict[str, str]]:
    text = _normalize_inline_pipe_table(result)

    if not text or "|" not in text:
        return []

    lines = [clean_text(x) for x in text.splitlines() if clean_text(x)]

    header_cells: list[str] = []
    data_cells: list[str] = []
    parsed_row_cells: list[list[str]] = []

    if len(lines) >= 2 and "|" in lines[0]:
        first_line_cells = _split_pipe_cells(lines[0])

        if any("시험결과" in c or "실험결과" in c or "측정결과" in c for c in first_line_cells):
            header_cells = first_line_cells
            if lines[0].lstrip().startswith("|"):
                header_cells = ["시험구분"] + header_cells
            parsed_row_cells = _parse_pipe_rows_with_wrapped_cells(
                lines[1:],
                col_count=len(header_cells),
            )

    if not header_cells:
        cells = _split_pipe_cells(text)
        result_header_idx = next(
            (
                idx
                for idx, cell in enumerate(cells)
                if "시험결과" in cell or "실험결과" in cell or "측정결과" in cell
            ),
            None,
        )

        if result_header_idx is None:
            return []

        header_cells = cells[: result_header_idx + 1]
        data_cells = cells[result_header_idx + 1 :]

    if len(header_cells) < 2 or (not data_cells and not parsed_row_cells):
        return []

    col_count = len(header_cells)

    implicit_item_column = False

    if len(header_cells) == 2 and data_cells and len(data_cells) % 3 == 0:
        implicit_item_column = True
        header_cells = ["항목"] + header_cells
        col_count = 3

    item_idx = 0

    date_idx = next(
        (
            idx
            for idx, header in enumerate(header_cells)
            if "시험기간" in header or "시험일자" in header or "시험일" in header
        ),
        None,
    )

    result_idx = next(
        (
            idx
            for idx, header in enumerate(header_cells)
            if "시험결과" in header or "실험결과" in header or "측정결과" in header
        ),
        None,
    )

    if result_idx is None:
        return []

    item_label = "항목" if implicit_item_column else (header_cells[item_idx] or "항목")

    out: list[dict[str, str]] = []

    rows_to_parse = parsed_row_cells

    if not rows_to_parse:
        rows_to_parse = [
            data_cells[start : start + col_count]
            for start in range(0, len(data_cells), col_count)
        ]

    for row_cells in rows_to_parse:

        if len(row_cells) < col_count:
            continue

        item_value = clean_text(row_cells[item_idx])
        result_value = clean_text(row_cells[result_idx])
        date_value = clean_text(row_cells[date_idx]) if date_idx is not None and date_idx < len(row_cells) else ""

        if not item_value and not result_value:
            continue

        out.append(
            {
                "item_label": item_label,
                "item_value": item_value,
                "lot_no": item_value,
                "test_date": date_value,
                "result": result_value,
                "display_columns": header_cells,
                "display_values": {
                    header_cells[idx]: clean_text(row_cells[idx])
                    for idx in range(min(len(header_cells), len(row_cells)))
                },
            }
        )

    return out


def _parse_whitespace_table(result: str | None) -> list[dict[str, str]]:
    text = _normalize_table_text(result)

    if not text or "|" in text:
        return []

    lines = [clean_text(x) for x in text.splitlines() if clean_text(x)]

    if len(lines) < 2:
        return []

    header_idx = None

    for idx, line in enumerate(lines):
        if ("시험기간" in line or "시험일자" in line) and ("시험결과" in line or "실험결과" in line):
            header_idx = idx
            break

    if header_idx is None:
        return []

    header = lines[header_idx]
    item_label = header
    item_label = item_label.replace("시험기간", "").replace("시험일자", "").replace("시험결과", "").replace("실험결과", "")
    item_label = clean_text(item_label) or "항목"

    out: list[dict[str, str]] = []

    for line in lines[header_idx + 1:]:
        m = re.match(r"(.+?)\s+(20\d{2}[.\-/]\d{1,2})\s+(.+)$", line)

        if not m:
            continue

        out.append(
            {
                "item_label": item_label,
                "item_value": clean_text(m.group(1)),
                "lot_no": clean_text(m.group(1)),
                "test_date": clean_text(m.group(2)),
                "result": clean_text(m.group(3)),
                "display_columns": [item_label, "시험기간", "시험결과"],
                "display_values": {
                    item_label: clean_text(m.group(1)),
                    "시험기간": clean_text(m.group(2)),
                    "시험결과": clean_text(m.group(3)),
                },
            }
        )

    return out


def _parse_result_table(result: str | None) -> list[dict[str, str]]:
    horizontal = _parse_horizontal_result_table(result)

    if horizontal:
        return horizontal

    vertical = _parse_vertical_column_table(result)

    if vertical:
        return vertical

    whitespace = _parse_whitespace_table(result)

    if whitespace:
        return whitespace

    return []


def _table_obj_get(table_obj, key: str, default=None):
    if table_obj is None:
        return default

    if isinstance(table_obj, dict):
        return table_obj.get(key, default)

    return getattr(table_obj, key, default)


def _normalize_structured_table_rows(result_table) -> tuple[list[str], list[dict[str, str]]]:
    if not result_table:
        return [], []

    columns = (
        _table_obj_get(result_table, "columns", None)
        or _table_obj_get(result_table, "headers", None)
        or []
    )

    rows = _table_obj_get(result_table, "rows", None) or []

    columns = [clean_text(x) for x in columns]

    if columns and not columns[0]:
        columns[0] = "시험구분"

    normalized_rows: list[dict[str, str]] = []

    for row in rows:
        if isinstance(row, dict):
            normalized_row: dict[str, str] = {}
            for key, value in row.items():
                clean_key = clean_text(key)
                if not clean_key and columns:
                    clean_key = columns[0] or "시험구분"
                normalized_row[clean_key or "시험구분"] = clean_text(value)
            normalized_rows.append(
                normalized_row
            )
            continue

        if isinstance(row, list) and columns:
            normalized_rows.append(
                {
                    columns[idx]: clean_text(value)
                    for idx, value in enumerate(row)
                    if idx < len(columns)
                }
            )

    return columns, normalized_rows


def _parse_structured_result_table_from_record(record: ExtractedRecord) -> list[dict[str, str]]:
    """
    extractor가 result_table로 구조화해 둔 표를 판정용 row로 변환한다.

    예:
    columns: ["바이러스", "시험기간", "시험결과"]
    rows:
      {"바이러스": "Bpy V", "시험기간": "2025.01", "시험결과": "적합"}
    """
    result_table = getattr(record, "result_table", None)

    if not result_table:
        return []

    columns, rows = _normalize_structured_table_rows(result_table)

    if not rows:
        return []

    if not columns and rows:
        columns = list(rows[0].keys())

    result_col = next(
        (
            col for col in columns
            if "시험결과" in clean_text(col)
            or "실험결과" in clean_text(col)
            or "측정결과" in clean_text(col)
            or clean_text(col) in {"결과"}
        ),
        None,
    )

    date_col = next(
        (
            col for col in columns
            if "시험기간" in clean_text(col)
            or "시험일자" in clean_text(col)
            or "시험일" in clean_text(col)
            or clean_text(col) in {"기간", "일자"}
        ),
        None,
    )

    item_col = next(
        (
            col for col in columns
            if col not in {result_col, date_col}
        ),
        None,
    )

    if result_col is None:
        return []

    out: list[dict[str, str]] = []

    for row in rows:
        item_value = clean_text(row.get(item_col, "")) if item_col else ""
        result_value = clean_text(row.get(result_col, ""))
        date_value = clean_text(row.get(date_col, "")) if date_col else ""

        if not result_value:
            continue

        out.append(
            {
                "item_label": clean_text(item_col or "항목"),
                "item_value": item_value,
                "lot_no": item_value,
                "test_date": date_value,
                "result": result_value,
                "display_columns": columns,
                "display_values": {
                    col: clean_text(row.get(col, ""))
                    for col in columns
                },
            }
        )

    return out


def _looks_like_unparsed_table(result: str | None) -> bool:
    text = _normalize_table_text(result)

    if not text:
        return False

    table_markers = ["|", "시험기간", "시험일자", "시험결과", "실험결과", "로트번호", "세포", "바이러스"]
    hit = sum(1 for marker in table_markers if marker in text)

    return hit >= 2


def _compact_semantic(text: str | None) -> str:
    text = clean_text(text)
    text = text.casefold()
    text = text.replace("e.coli", "ecoli")
    text = text.replace("e.coLi".casefold(), "ecoli")
    text = text.replace("％", "%")
    text = text.replace("µ", "μ")
    text = re.sub(r"[\s\-_·.,:：/(){}\[\]]+", "", text)
    return text


def _is_concrete_permit_basis(value: str | None) -> bool:
    """Require a permit basis to name a condition, not only compliance boilerplate."""
    raw = clean_text(value)
    compact = _compact_semantic(raw)
    if not compact or re.fullmatch(r"\d+(?:\.\d+)*", compact):
        return False

    if re.search(r"\d", compact):
        numeric_signals = (
            "%", "℃", "±", "eu", "day", "일", "개", "이상", "이하", "미만", "초과",
            "최소", "최대", "atleast", "atmost", "minimum", "maximum",
        )
        if any(signal in compact for signal in numeric_signals):
            return True
        # Digits in assay names (CO1, MRC-5, etc.) do not make a qualitative
        # requirement numeric. Retain the subject/condition check below.
        if not re.search(r"되어야|없어야|않아야|이어야|해야|하여야|must|should", raw, re.I):
            return False

    subject = raw.casefold()
    grammar_patterns = (
        r"(?:이에|위\s*기준에|해당\s*기준에|그\s*기준에)\s*적합\s*(?:하여야|해야|이어야)?\s*(?:한다|함)?",
        r"(?:적합|부적합)\s*(?:하여야|해야|이어야)?\s*(?:한다|함)?",
        r"(?:이상|이하|미만|초과|최소|최대)\s*이어야\s*(?:한다)?",
        r"(?:없어야|있어야|확인되어야|이어야)\s*(?:한다)?",
        r"(?:만족|충족|준수)\s*(?:해야|하여야)?\s*(?:한다|함|된다|됩니다)?",
        r"\b(?:minimum|maximum|day|days|at least|at most|must|should|be|"
        r"conditions?|requirements?|criteria|test)\b",
    )
    for pattern in grammar_patterns:
        subject = re.sub(pattern, " ", subject)

    generic_words = (
        "허가서", "permit", "pdf", "판정", "후보", "문단", "시험", "확인", "성상", "무균시험",
        "기준", "조건", "요건", "요구사항", "requirement", "requirements",
        "condition", "conditions", "criteria", "criterion", "satisfaction", "compliance",
        "만족", "충족", "준수", "이상", "이하", "미만", "초과", "최소", "최대", "일", "개",
    )
    for word in generic_words:
        subject = subject.replace(word, " ")
    subject_compact = _compact_semantic(subject)
    return bool(subject_compact) and len(subject_compact) >= 2


def _short_permit_basis(permit_context: str | None) -> str:
    """허가서 근거 문단에서 화면에 표시할 실제 기준 문장을 짧게 추출한다.

    이전 버전처럼 "허가서 기준" 같은 포괄 문구를 반환하면 UI 판정이유가
    "허가서 기준에 따라 허가서 기준이며..."처럼 표시된다. 그래서 현재 시험에
    직접 연결되는 허가서 문장 중 실제 요구조건을 우선 반환한다.
    """
    text = clean_text(permit_context)

    if not text:
        return ""

    compact = _compact_semantic(text)

    # 자주 나오는 허가서 기준 전용 문구를 우선 추출한다.
    if "세포농도" in compact and any(x in compact for x in ["생존률", "생존율"]):
        requirements = _extract_numeric_requirements(text)
        cell_req = next((x for x in requirements if "cell" in x.casefold() or "cells" in x.casefold()), "")
        survival_req = next((x for x in requirements if "%" in x), "")
        parts = []
        if cell_req:
            parts.append(f"세포농도 {cell_req}")
        if survival_req:
            parts.append(f"생존률 {survival_req}")
        if parts:
            return " 및 ".join(parts)
        return "세포농도 및 생존률 기준을 만족해야 함"

    if any(x in compact for x in ["세포가뭉치지않아야", "뭉치지않아야", "응집되지않아야"]):
        return "세포가 뭉치지 않아야 함"

    if any(x in compact for x in ["형성이확인되지않아야", "항체의형성이확인되지않아야"]):
        return "감염성 있는 바이러스에 특이적인 항체의 형성이 확인되지 않아야 함"

    if any(x in compact for x in ["바이러스가관찰되지않아야", "관찰되지않아야"]):
        return "바이러스가 관찰되지 않아야 함"

    if any(x in compact for x in ["외래성인자가검출되지않아야", "검출되지않아야"]):
        return "검출되지 않아야 함"

    if any(x in compact for x in ["오염이탐지되지않아야", "탐지되지않아야"]):
        return "탐지되지 않아야 함"

    if any(x in compact for x in ["오염이확인되지않아야", "오염이없어야"]):
        return "오염이 확인되지 않아야 함"

    if any(x in compact for x in ["예상되는염기서열과일치", "염기서열과일치해야"]):
        return "예상되는 염기서열과 일치해야 함"

    if any(x in compact for x in ["코끼리유래세포로확인", "유래세포로확인"]):
        return "지정된 유래 세포로 확인되어야 함"

    if any(x in compact for x in ["확인되어야", "동정되어야"]):
        return "해당 항목이 확인되어야 함"

    # 위 규칙에 걸리지 않으면, 허가서 문단에서 기준처럼 보이는 짧은 문장을 직접 뽑는다.
    # 설명 머리말과 구분선은 제외한다.
    raw_lines = [clean_text(x) for x in re.split(r"[\n\r]+", text)]
    lines = []
    for line in raw_lines:
        if not line:
            continue
        if line.startswith("[허가서"):
            continue
        if "아래 내용은" in line or "2차 판정" in line or "추측하지 말고" in line:
            continue
        if set(line) <= {"-"}:
            continue
        lines.append(line)

    criterion_markers = [
        "이어야", "여야", "하여야", "해야", "않아야", "따르며", "따름",
        "이상", "이하", "미만", "초과", "일치", "확인", "검출", "관찰",
    ]

    for line in lines:
        compact_line = _compact_semantic(line)
        if any(marker in compact_line for marker in criterion_markers):
            # 시험명/번호만 있는 줄은 제외하고 실제 기준 부분만 간단히 정리한다.
            if len(line) > 180:
                line = line[:180].rstrip() + "..."
            return line

    return lines[0][:160] if lines else ""

def _semantic_tokens(text: str | None) -> list[str]:
    """시험명/기준/방법의 직접 매칭에 사용할 의미 토큰을 만든다."""
    value = clean_text(text).casefold()
    value = value.replace("e.coli", "ecoli").replace("e.coLi".casefold(), "ecoli")
    value = re.sub(r"[^0-9a-zA-Z가-힣μµ%./^]+", " ", value)

    stopwords = {
        "시험",
        "시험명",
        "시험방법",
        "시험기준",
        "시험결과",
        "기준",
        "결과",
        "방법",
        "확인",
        "분석",
        "측정",
        "판정",
        "적합",
        "부적합",
        "이상",
        "이하",
        "미만",
        "초과",
        "대한민국약전",
        "일반",
        "생기",
        "유럽약전",
        "자사기시",
    }

    tokens: list[str] = []
    for token in value.split():
        token = token.strip()
        if len(token) < 2 or token in stopwords:
            continue
        tokens.append(token)

    return tokens


def _permit_chunk_text(chunk: object) -> str:
    return " ".join(
        clean_text(getattr(chunk, name, ""))
        for name in ["section_number", "title", "text"]
        if clean_text(getattr(chunk, name, ""))
    )


def _is_restriction_record(record: ExtractedRecord) -> bool:
    target = _compact_semantic(" ".join(
        [
            clean_text(record.test_name),
            clean_text(record.section_title),
            clean_text(record.criteria),
            clean_text(record.result),
            clean_text(record.raw_text),
        ]
    ))
    markers = ["제한효소지도", "제한효소", "restriction", "enzyme", "bp", "밴드", "band"]
    return any(marker in target for marker in markers)


def _permit_anchor_candidates(record: ExtractedRecord) -> list[str]:
    """허가서 3차 판정에서 직접 근거로 인정할 시험명/섹션명 후보."""
    candidates: list[str] = []

    for value in [
        clean_text(getattr(record, "test_name", "")),
        clean_text(getattr(record, "section_title", "")),
    ]:
        compact = _compact_semantic(value)
        if compact and len(compact) >= 4 and compact not in {_compact_semantic("시험"), _compact_semantic("정보")}:
            candidates.append(value)

    # 중복 제거
    seen: set[str] = set()
    out: list[str] = []
    for value in candidates:
        key = _compact_semantic(value)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)

    return out


def _permit_chunk_has_exact_anchor(record: ExtractedRecord, chunk_text: str | None) -> bool:
    """
    허가서 chunk가 현재 시험 항목을 직접 언급하는지 확인한다.

    중요:
    - 시험기준/시험결과의 숫자나 단어가 겹친다는 이유만으로 허가서 기준을 적용하지 않는다.
    - 현재 시험명 또는 섹션명이 허가서에 직접 존재할 때만 3차 판정 근거로 인정한다.
    - 이렇게 해야 제한효소지도분석시험이 전자현미경시험/바이러스부정시험 기준으로 오판되는 문제를 막을 수 있다.
    """
    text = clean_text(chunk_text)
    if not text:
        return False

    context = _compact_semantic(text)
    anchors = _permit_anchor_candidates(record)

    if not anchors:
        return False

    for anchor in anchors:
        anchor_compact = _compact_semantic(anchor)
        if anchor_compact and anchor_compact in context:
            return True

    return False


def _permit_context_has_exact_anchor(record: ExtractedRecord, permit_context: str | None) -> bool:
    """3차 허가서 판정 직전 최종 방어막. 직접 시험명/섹션명이 없으면 허가서 판정을 적용하지 않는다."""
    return _permit_chunk_has_exact_anchor(record, permit_context)


def _extract_direct_permit_snippet(record: ExtractedRecord, chunk: object) -> str:
    """
    page/chunk 전체를 쓰지 않고 현재 시험명 주변의 허가서 문단만 잘라낸다.

    허가서 한 페이지에 여러 시험 기준이 함께 있으면, 페이지 전체를 숫자 비교에 넣는 순간
    다른 시험의 83%, 90%, 3.00 x 10^6 같은 기준이 현재 시험에 섞일 수 있다.
    따라서 시험명/섹션명이 나온 줄부터 다음 번호 섹션 전까지를 사용한다.
    """
    text = _permit_chunk_text(chunk)
    if not text:
        return ""

    anchors = [_compact_semantic(value) for value in _permit_anchor_candidates(record)]
    anchors = [value for value in anchors if value]

    if not anchors:
        return ""

    lines = [line.rstrip() for line in str(text).splitlines()]
    if not lines:
        lines = [text]

    start_idx: int | None = None

    for idx, line in enumerate(lines):
        line_compact = _compact_semantic(line)
        if any(anchor in line_compact for anchor in anchors):
            start_idx = idx
            break

    if start_idx is None:
        # 줄바꿈이 없는 chunk일 경우 compact 기준으로 직접 근거가 있는지까지만 확인하고 전체를 쓰되,
        # 이 경우에도 _permit_context_has_exact_anchor에서 한 번 더 검증한다.
        return text if _permit_chunk_has_exact_anchor(record, text) else ""

    section_heading_pattern = re.compile(r"^\s*\d+(?:\.\d+){2,}(?:\s|$)")
    end_idx = len(lines)

    for idx in range(start_idx + 1, len(lines)):
        line = lines[idx]
        if section_heading_pattern.match(line):
            end_idx = idx
            break

    snippet = "\n".join(lines[start_idx:end_idx]).strip()

    if not snippet:
        return ""

    return snippet if _permit_chunk_has_exact_anchor(record, snippet) else ""


def _permit_chunk_is_direct_for_record(record: ExtractedRecord, chunk: object) -> bool:
    """
    허가서 chunk가 현재 시험항목과 직접 연결되는지 엄격하게 검사한다.

    이전 방식처럼 시험기준/시험결과의 숫자, 단위, 일반 단어가 일부 겹치는 것만으로
    허가서 기준을 적용하면 다음 같은 오판이 발생한다.
    - 제한효소지도분석시험이 허가서의 전자현미경시험/바이러스 기준으로 불합격 처리
    - 생균수시험이 허가서의 세포성장시험 3.00 x 10^6 / 90% / 83% 기준으로 불합격 처리

    따라서 3차 허가서 판정은 현재 시험명 또는 섹션명이 허가서에 직접 존재하는 경우만 허용한다.
    """
    chunk_text = _permit_chunk_text(chunk)
    return _permit_chunk_has_exact_anchor(record, chunk_text)


def _llm_response_is_ambiguous(reason: str | None, normalized_criteria: str | None = None) -> bool:
    """LLM이 자신 있게 판정하지 못한 흔적이 있으면 허가서 단계로 넘기기 위해 보류로 처리한다."""
    text = _compact_semantic(" ".join([clean_text(reason), clean_text(normalized_criteria)]))
    if not text:
        return False

    ambiguous_markers = [
        "판단하기어려",
        "판단이어려",
        "비교가어려",
        "자동비교가어려",
        "명확하지않",
        "확정하기어려",
        "추가확인",
        "사람의판단",
        "전문가판단",
        "허가서확인",
        "허가서기준확인",
        "기준확인필요",
        "정보부족",
        "보류",
        "검수보류",
        "직접비교가어려",
    ]
    return any(marker in text for marker in ambiguous_markers)


def _result_has_negative_meaning(result: str | None) -> bool:
    r = _compact_semantic(result)

    negative_markers = [
        "확인되지않",
        "확인되지않음",
        "검출되지않",
        "검출되지않음",
        "관찰되지않",
        "관찰되지않음",
        "탐지되지않",
        "탐지되지않음",
        "형성이확인되지않",
        "형성이확인되지않음",
        "오염없",
        "오염이없",
        "오염없음",
        "오염이없음",
        "없음",
        "미검출",
        "불검출",
        "음성",
        "이상없",
        "이상없음",
        "뭉치지않",
        "뭉치지않음",
        "응집없",
        "응집없음",
    ]

    return any(marker in r for marker in negative_markers)


def _result_has_positive_detection(result: str | None) -> bool:
    r = _compact_semantic(result)

    if _result_has_negative_meaning(result):
        return False

    positive_markers = [
        "확인",
        "검출",
        "관찰",
        "탐지",
        "형성",
        "형성이확인",
        "오염",
        "양성",
        "동정",
        "일치",
        "뭉침",
        "응집",
    ]

    return any(marker in r for marker in positive_markers)


def _permit_requires_negative(permit_context: str | None) -> bool:
    c = _compact_semantic(permit_context)

    negative_requirement_markers = [
        "확인되지않아야",
        "검출되지않아야",
        "관찰되지않아야",
        "탐지되지않아야",
        "없어야",
        "오염이확인되지않아야",
        "오염이없어야",
        "형성이확인되지않아야",
        "바이러스가관찰되지않아야",
        "바이러스에의한오염이확인되지않아야",
        "뭉치지않아야",
    ]

    return any(marker in c for marker in negative_requirement_markers)


def _permit_requires_match(permit_context: str | None) -> bool:
    c = _compact_semantic(permit_context)

    match_markers = [
        "일치해야",
        "일치하여야",
        "예상되는염기서열과일치",
        "염기서열과일치",
    ]

    return any(marker in c for marker in match_markers)


def _permit_requires_confirmation(permit_context: str | None) -> bool:
    c = _compact_semantic(permit_context)

    if _permit_requires_negative(permit_context):
        return False

    confirm_markers = [
        "확인되어야",
        "확인되어야함",
        "확인하여야",
        "동정되어야",
        "로확인되어야",
    ]

    return any(marker in c for marker in confirm_markers)


def _extract_bp_values(text: str | None) -> list[int]:
    text = clean_text(text)

    if not text:
        return []

    values: list[int] = []

    for m in re.finditer(r"([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)\s*bp", text, flags=re.I):
        value = int(m.group(1).replace(",", ""))
        values.append(value)

    return values


def _judge_bp_band_pattern(
    criteria: str | None,
    result: str | None,
    tolerance: float = 0.10,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    criteria_values = _extract_bp_values(criteria)
    result_values = _extract_bp_values(result)

    if len(criteria_values) < 1 or len(result_values) < 1:
        return None, None, None, None, None

    mismatches: list[str] = []

    for c in criteria_values:
        nearest = min(result_values, key=lambda r: abs(r - c))
        diff_ratio = abs(nearest - c) / c if c else 1.0

        if diff_ratio > tolerance:
            mismatches.append(
                f"기준 {c} bp에 대응하는 결과 {nearest} bp가 허용오차 {int(tolerance * 100)}%를 초과"
            )

    if mismatches:
        return (
            FAIL_LABEL,
            "제한효소지도분석시험의 밴드 위치가 기준과 일치하지 않아 검수불합격으로 판단했습니다. "
            + " ".join(mismatches),
            "bp_pattern",
            ", ".join(f"{x} bp" for x in criteria_values),
            ", ".join(f"{x} bp" for x in result_values),
        )

    return (
        PASS_LABEL,
        "제한효소지도분석시험의 결과 밴드 위치가 기준 밴드 위치 허용범위 내에 있어 검수합격으로 판단했습니다.",
        "bp_pattern",
        ", ".join(f"{x} bp" for x in criteria_values),
        ", ".join(f"{x} bp" for x in result_values),
    )


def _judge_loq_text(
    criteria: str | None,
    result: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    c = clean_text(criteria)
    r = clean_text(result)

    if not c or not r:
        return None, None, None, None, None

    c_compact = re.sub(r"\s+", "", c)
    r_compact = re.sub(r"\s+", "", r)

    if "정량한계" not in c_compact and "LOQ" not in c_compact.upper():
        return None, None, None, None, None

    if "미만" not in c_compact and "이하" not in c_compact:
        return None, None, None, None, None

    result_satisfies = (
        "정량한계미만" in r_compact
        or "LOQ미만" in r_compact.upper()
        or "불검출" in r_compact
        or "검출되지" in r_compact
    )

    if result_satisfies:
        return (
            PASS_LABEL,
            "시험결과가 정량한계 미만으로 기재되어 시험기준을 만족하므로 검수합격으로 판단했습니다.",
            "loq_text",
            c,
            r,
        )

    if "정량한계이상" in r_compact or "LOQ이상" in r_compact.upper():
        return (
            FAIL_LABEL,
            "시험결과가 정량한계 이상으로 기재되어 시험기준을 만족하지 못하므로 검수불합격으로 판단했습니다.",
            "loq_text",
            c,
            r,
        )

    return None, None, None, None, None


def _required_loq_below_count(criteria: str | None) -> int | None:
    compact = _compact_semantic(criteria)

    if not compact:
        return None

    if "정량한계" not in compact and "loq" not in compact:
        return None

    if "유전자" not in compact and "종" not in compact:
        return None

    if "미만" not in compact:
        return None

    count_match = re.search(r"(\d+)종", compact)
    if count_match:
        return max(int(count_match.group(1)), 1)

    if any(marker in compact for marker in ["한종", "하나", "1개", "한개"]):
        return 1

    if "유전자중" in compact:
        return 1

    return None


def _loq_below_result(text: str | None) -> bool:
    compact = _compact_semantic(text)

    return any(
        marker in compact
        for marker in [
            "정량한계미만",
            "loq미만",
            "loqbelow",
            "불검출",
            "검출되지않",
            "미검출",
        ]
    ) or ("미만" in compact and "정량한계" in compact)


def _loq_above_result(text: str | None) -> bool:
    compact = _compact_semantic(text)

    return any(
        marker in compact
        for marker in [
            "정량한계이상",
            "loq이상",
            "loqabove",
        ]
    ) or ("이상" in compact and "정량한계" in compact)


def _judge_table_loq_count_requirement(
    criteria: str | None,
    table_rows: list[dict[str, str]],
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    required_count = _required_loq_below_count(criteria)

    if required_count is None or not table_rows:
        return None, None, None, None, None

    below_rows = [
        row
        for row in table_rows
        if _loq_below_result(row.get("result", ""))
    ]
    above_rows = [
        row
        for row in table_rows
        if _loq_above_result(row.get("result", ""))
    ]

    if len(below_rows) >= required_count:
        below_names = [
            clean_text(row.get("item_value", "") or row.get("lot_no", "") or row.get("item_label", ""))
            for row in below_rows
        ]
        below_names = [name for name in below_names if name]
        reason = (
            f"시험기준은 유전자 중 {required_count}종 이상이 정량한계 미만이어야 하는 조건이며, "
            f"시험결과 표에서 정량한계 미만 항목 {len(below_rows)}종"
            f"{'(' + ', '.join(below_names) + ')' if below_names else ''}이 확인되어 {PASS_REASON_WORD}으로 판단했습니다."
        )
        return (
            PASS_LABEL,
            reason,
            "loq_count_requirement",
            clean_text(criteria),
            f"정량한계 미만 {len(below_rows)}종 / 전체 {len(table_rows)}종",
        )

    if len(below_rows) + len(above_rows) == len(table_rows):
        reason = (
            f"시험기준은 유전자 중 {required_count}종 이상이 정량한계 미만이어야 하나, "
            f"시험결과 표에서 정량한계 미만 항목이 {len(below_rows)}종만 확인되어 {FAIL_REASON_WORD}으로 판단했습니다."
        )
        return (
            FAIL_LABEL,
            reason,
            "loq_count_requirement",
            clean_text(criteria),
            f"정량한계 미만 {len(below_rows)}종 / 전체 {len(table_rows)}종",
        )

    return None, None, None, None, None


def _compare_limit_expression_satisfies(
    *,
    criteria_op: str | None,
    criteria_value: float,
    result_op: str | None,
    result_value: float,
) -> bool | None:
    """Return whether a result limit expression is at least as strict as criteria."""
    upper_ops = {"<", "<="}
    lower_ops = {">", ">="}

    if criteria_op in upper_ops and result_op in upper_ops:
        if result_value < criteria_value:
            return True
        if result_value > criteria_value:
            return False
        if criteria_op == "<":
            return result_op == "<"
        return result_op in upper_ops

    if criteria_op in lower_ops and result_op in lower_ops:
        if result_value > criteria_value:
            return True
        if result_value < criteria_value:
            return False
        if criteria_op == ">":
            return result_op == ">"
        return result_op in lower_ops

    return None


def _judge_limit_expression_text(
    criteria: str | None,
    result: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """
    Handle cases where the result is itself a limit expression.

    Example:
      criteria: 20 EU/mL 미만
      result:   20 EU/mL 미만

    This means the reported result satisfies the same limit, not that the
    measured value equals 20.
    """
    c = parse_criteria_text(criteria)
    r = parse_criteria_text(result)

    if c.kind != "compare" or r.kind != "compare":
        return None, None, None, None, None

    if not c.threshold or not r.threshold:
        return None, None, None, None, None

    if not _same_or_convertible(c.threshold, r.threshold):
        return None, None, None, None, None

    ok = _compare_limit_expression_satisfies(
        criteria_op=c.comparator,
        criteria_value=c.threshold.value_canonical,
        result_op=r.comparator,
        result_value=r.threshold.value_canonical,
    )

    if ok is None:
        return None, None, None, None, None

    if ok:
        return (
            PASS_LABEL,
            f"시험결과가 시험기준과 같거나 더 엄격한 제한값 표현으로 기재되어 {PASS_REASON_WORD}으로 판단했습니다.",
            "limit_expression",
            c.threshold.pretty,
            r.threshold.pretty,
        )

    return (
        FAIL_LABEL,
        f"시험결과 제한값이 시험기준보다 완화되어 {FAIL_REASON_WORD}으로 판단했습니다.",
        "limit_expression",
        c.threshold.pretty,
        r.threshold.pretty,
    )


def _judge_qualitative_text(
    criteria: str | None,
    result: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    c = clean_text(criteria)
    r = clean_text(result)

    if not c or not r:
        return None, None, None, None, None

    c_compact = re.sub(r"\s+", "", c)
    r_compact = re.sub(r"\s+", "", r)

    has_qualitative_marker = any(
        marker in c_compact
        for marker in ["확인되어야", "확인되어야함", "오염이없어야", "없어야함", "검출되지않아야"]
    )

    if not has_qualitative_marker:
        return None, None, None, None, None

    checks: list[tuple[str, bool]] = []

    if "확인" in c_compact and "확인되지" not in c_compact:
        checks.append(("확인", "확인" in r_compact))

    if "오염" in c_compact and ("없" in c_compact or "무" in c_compact):
        checks.append(
            (
                "오염 없음",
                "오염없" in r_compact
                or "오염이없" in r_compact
                or "오염없음" in r_compact
                or "오염이없음" in r_compact
            )
        )

    if "검출" in c_compact and ("않아야" in c_compact or "없어야" in c_compact):
        checks.append(
            (
                "불검출",
                "불검출" in r_compact
                or "검출되지" in r_compact
                or "검출안" in r_compact
            )
        )

    if checks and all(ok for _, ok in checks):
        return (
            PASS_LABEL,
            "시험결과가 정성 시험기준의 필수 조건을 만족하여 검수합격으로 판단했습니다.",
            "qualitative_text",
            c,
            r,
        )

    if checks and any(not ok for _, ok in checks):
        failed = [name for name, ok in checks if not ok]

        return (
            FAIL_LABEL,
            f"시험결과에서 정성 시험기준의 필수 조건이 확인되지 않아 검수불합격으로 판단했습니다. 미충족 항목: {', '.join(failed)}",
            "qualitative_text",
            c,
            r,
        )

    return None, None, None, None, None



def _contains_any(text: str, markers: list[str]) -> bool:
    return any(marker in text for marker in markers)


def _judge_compound_qualitative_requirements(
    criteria: str,
    result: str,
    c_compact: str,
    r_compact: str,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Handle simple Korean qualitative requirements before asking the LLM.

    These are common SP phrases where a human reads multiple plain-language
    conditions joined by "and" as all required. Letting them fall through to the
    LLM makes the result sensitive to API availability and prompt conservatism.
    """

    checks: list[tuple[str, bool]] = []
    explicit_failures: list[str] = []

    def add_check(name: str, ok: bool, failed: bool = False) -> None:
        checks.append((name, ok))
        if failed:
            explicit_failures.append(name)

    if _contains_any(
        c_compact,
        [
            "확인되지않아야",
            "확인되지않을것",
            "확인되지않음",
            "검출되지않아야",
            "검출되지않을것",
            "관찰되지않아야",
            "관찰되지않을것",
            "탐지되지않아야",
            "동정되지않아야",
        ],
    ):
        ok = _contains_any(
            r_compact,
            [
                "확인되지않",
                "확인되지않음",
                "검출되지않",
                "검출되지않음",
                "관찰되지않",
                "관찰되지않음",
                "탐지되지않",
                "탐지되지않음",
                "동정되지않",
                "미검출",
                "불검출",
                "없음",
                "무균",
                "음성",
            ],
        )
        failed = (
            not ok
            and _contains_any(r_compact, ["확인", "검출", "관찰", "탐지", "동정", "성장", "양성"])
            and not _contains_any(r_compact, ["확인되지", "검출되지", "관찰되지", "탐지되지", "동정되지"])
        )
        add_check("부재/미확인 조건", ok, failed)

    if _contains_any(c_compact, ["뭉치지않", "응집되지않", "응집없", "응집이없"]):
        ok = _contains_any(
            r_compact,
            [
                "뭉치지않",
                "뭉치지않음",
                "응집되지않",
                "응집없",
                "응집이없",
                "덩어리없",
                "덩어리가없",
            ],
        )
        failed = (
            not ok
            and _contains_any(r_compact, ["뭉침", "뭉쳐", "응집", "덩어리", "군집"])
            and not _contains_any(r_compact, ["뭉치지않", "응집되지않", "응집없", "덩어리없"])
        )
        add_check("세포 응집 없음", ok, failed)

    if "구형" in c_compact:
        ok = _contains_any(r_compact, ["구형", "원형", "둥근", "둥글"])
        failed = not ok and _contains_any(r_compact, ["비구형", "구형아님", "구형이아님", "불규칙"])
        add_check("구형 형태", ok, failed)

    if not checks:
        return None, None, None, None, None

    if explicit_failures:
        return (
            FAIL_LABEL,
            f"시험결과에서 시험기준의 정성 조건을 만족하지 않는 표현이 확인되어 {FAIL_REASON_WORD}으로 판단했습니다. "
            f"미충족 항목: {', '.join(explicit_failures)}",
            "compound_qualitative_text",
            criteria,
            result,
        )

    if all(ok for _, ok in checks):
        return (
            PASS_LABEL,
            f"시험결과가 시험기준의 정성 조건({', '.join(name for name, _ in checks)})을 모두 만족하여 {PASS_REASON_WORD}으로 판단했습니다.",
            "compound_qualitative_text",
            criteria,
            result,
        )

    return None, None, None, None, None


def _judge_semantic_qualitative_text(
    criteria: str | None,
    result: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """
    1차 판정용 정성 문장 비교 규칙.

    숫자/단위 비교가 아니라 의미 비교가 필요한 항목을 규칙 기반으로 먼저 처리한다.
    예:
    - 기준: 불용성이물이 없어야 함 / 결과: 맑고 깨끗하며 불용성이물이 없음
    - 기준: 거의 무색이거나 황색, 담황색 / 결과: 담황색
    - 기준: 표시량 이상 / 결과: 표시량 이상 분출됨
    - 기준: 육안관찰 시 성상은 변함없음 / 결과: 성상 변화 없음
    """
    c = clean_text(criteria)
    r = clean_text(result)

    if not c or not r:
        return None, None, None, None, None

    c_compact = _compact_semantic(c)
    r_compact = _compact_semantic(r)

    # This appearance family contains an explicit OR, not a requirement that
    # both clarity descriptions occur in the result. Match complete choices;
    # token overlap below would also accept negation or an extra defect.
    if "또는" in c_compact and "무색투명" in c_compact and "약간탁한" in c_compact:
        # Preserve punctuation inside the text so separate conditions cannot
        # collapse into a single accepted phrase. Only layout whitespace and
        # one final sentence period are insignificant here.
        exact_criteria = re.sub(r"\s+", "", c).removesuffix(".")
        exact_result = re.sub(r"\s+", "", r).removesuffix(".")
        choices = exact_criteria.split("또는")
        if all(re.fullmatch(r"(?:무색투명|약간탁한)(?:액상)?", choice) for choice in choices):
            allowed = set(choices)
            if choices[-1].endswith("액상"):
                # In "무색 투명 또는 약간 탁한 액상", the final noun also
                # applies to the first alternative. Never drop a qualifier.
                allowed.update(choice + "액상" for choice in choices if not choice.endswith("액상"))
            if exact_result in allowed:
                return (
                    PASS_LABEL,
                    f"시험결과에 기재된 '{r}'는 시험기준 '{c}'에서 '또는'으로 허용한 성상과 "
                    f"일치하여 {PASS_REASON_WORD}으로 판단했습니다.",
                    "appearance_alternative_text",
                    c,
                    r,
                )
        # Additional requirements, a partial observation, or negation still
        # need semantic review. Do not fall through to the color-token rule.
        return None, None, None, None, None

    compound_status = _judge_compound_qualitative_requirements(c, r, c_compact, r_compact)
    if compound_status[0] is not None:
        return compound_status

    # 일치/동일/부합 조건: 염기서열 분석처럼 결과가 "일치함"으로 요약되는 정성 시험.
    match_criteria_markers = [
        "일치해야",
        "일치하여야",
        "일치해야함",
        "같아야",
        "동일해야",
        "동일하여야",
        "부합해야",
        "부합하여야",
        "예상되는",
    ]
    mismatch_result_markers = [
        "불일치",
        "일치하지않",
        "일치하지못",
        "동일하지않",
        "같지않",
        "상이",
        "다름",
        "부적합",
    ]
    match_result_markers = [
        "일치",
        "동일",
        "같음",
        "같다",
        "부합",
        "적합",
        "확인됨",
        "확인",
    ]

    if _contains_any(c_compact, match_criteria_markers):
        if _contains_any(r_compact, mismatch_result_markers):
            return (
                FAIL_LABEL,
                f"시험기준은 일치 또는 동일 조건이나 시험결과에서 불일치 의미가 나타나 {FAIL_REASON_WORD}으로 판단했습니다.",
                "semantic_match_text",
                c,
                r,
            )

        if _contains_any(r_compact, match_result_markers):
            return (
                PASS_LABEL,
                f"시험결과가 시험기준의 일치 조건을 만족하여 {PASS_REASON_WORD}으로 판단했습니다.",
                "semantic_match_text",
                c,
                r,
            )

    # "실측치"처럼 별도 수치 기준 없이 실제 확인/측정 여부를 요구하는 확인시험.
    observed_value_markers = ["실측치", "실측값", "측정치", "측정값", "관찰값"]
    negative_observed_markers = [
        "확인되지않",
        "확인불가",
        "미확인",
        "측정되지않",
        "측정불가",
        "관찰되지않",
        "부적합",
    ]
    positive_observed_markers = [
        "확인됨",
        "확인",
        "측정됨",
        "측정",
        "관찰됨",
        "관찰",
        "기재됨",
        "실측",
    ]

    if _contains_any(c_compact, observed_value_markers):
        if _contains_any(r_compact, negative_observed_markers):
            return (
                FAIL_LABEL,
                f"시험기준은 실측 또는 확인 결과를 요구하나 시험결과에서 미확인 의미가 나타나 {FAIL_REASON_WORD}으로 판단했습니다.",
                "observed_value_text",
                c,
                r,
            )

        if _contains_any(r_compact, positive_observed_markers):
            return (
                PASS_LABEL,
                f"시험결과에 실측 또는 확인 결과가 기재되어 {PASS_REASON_WORD}으로 판단했습니다.",
                "observed_value_text",
                c,
                r,
            )

    # 양성/음성처럼 기준과 결과가 같은 정성 결론인 경우
    if "음성" in c_compact:
        if "음성" in r_compact or "negative" in r_compact:
            return (
                PASS_LABEL,
                f"시험결과가 음성으로 기재되어 시험기준을 만족하므로 {PASS_REASON_WORD}으로 판단했습니다.",
                "semantic_text",
                c,
                r,
            )
        if "양성" in r_compact or "positive" in r_compact:
            return (
                FAIL_LABEL,
                f"시험기준은 음성이나 시험결과가 양성으로 기재되어 {FAIL_REASON_WORD}으로 판단했습니다.",
                "semantic_text",
                c,
                r,
            )

    if "양성" in c_compact:
        if "양성" in r_compact or "positive" in r_compact:
            return (
                PASS_LABEL,
                f"시험결과가 양성으로 기재되어 시험기준을 만족하므로 {PASS_REASON_WORD}으로 판단했습니다.",
                "semantic_text",
                c,
                r,
            )
        if "음성" in r_compact or "negative" in r_compact:
            return (
                FAIL_LABEL,
                f"시험기준은 양성이나 시험결과가 음성으로 기재되어 {FAIL_REASON_WORD}으로 판단했습니다.",
                "semantic_text",
                c,
                r,
            )

    # 부재 조건: 없어야 함 / 없음 / 미검출 / 맑고 깨끗 등
    absence_criteria_markers = [
        "없어야함",
        "없어야",
        "없을것",
        "전혀없을것",
        "확인되어서는안됨",
        "확인되면안됨",
        "검출되지않아야",
        "관찰되지않아야",
        "균이확인되어서는안됨",
        "균이확인되면안됨",
    ]
    absence_result_markers = [
        "없음",
        "없다",
        "없고",
        "없으며",
        "미검출",
        "불검출",
        "검출되지않",
        "관찰되지않",
        "확인되지않",
        "맑고깨끗",
        "깨끗",
        "무균",
        "균성장없",
        "균성장없음",
        "성장없음",
        "이물없",
        "불용성이물없",
        "이상없",
    ]
    positive_detection_markers = [
        "검출",
        "관찰",
        "확인",
        "성장",
        "있음",
        "발견",
        "양성",
    ]

    if _contains_any(c_compact, absence_criteria_markers):
        if _contains_any(r_compact, absence_result_markers):
            return (
                PASS_LABEL,
                f"시험결과가 시험기준의 부재 조건을 만족하여 {PASS_REASON_WORD}으로 판단했습니다.",
                "semantic_text",
                c,
                r,
            )

        if _contains_any(r_compact, positive_detection_markers):
            return (
                FAIL_LABEL,
                f"시험기준은 해당 항목이 없어야 하나 시험결과에서 확인 또는 검출 의미가 나타나 {FAIL_REASON_WORD}으로 판단했습니다.",
                "semantic_text",
                c,
                r,
            )

    # 무균 조건
    if "무균" in c_compact or "균성장" in c_compact:
        if _contains_any(r_compact, ["무균", "균성장없", "균성장없음", "성장없음", "균이확인되지않"]):
            return (
                PASS_LABEL,
                f"시험결과에서 균 성장이 확인되지 않아 시험기준을 만족하므로 {PASS_REASON_WORD}으로 판단했습니다.",
                "semantic_text",
                c,
                r,
            )
        if _contains_any(r_compact, ["균성장", "균확인", "성장확인", "오염"]):
            return (
                FAIL_LABEL,
                f"시험기준은 무균 조건이나 시험결과에서 균 성장 또는 오염 의미가 나타나 {FAIL_REASON_WORD}으로 판단했습니다.",
                "semantic_text",
                c,
                r,
            )

    # 성상 변화 없음 / 변함없음
    if _contains_any(c_compact, ["변함없음", "변화없음", "성상은변함없음", "성상변함없음"]):
        if _contains_any(r_compact, ["변화없음", "변함없음", "성상변화없음", "성상변함없음"]):
            return (
                PASS_LABEL,
                f"시험결과에서 성상 변화가 없어 시험기준을 만족하므로 {PASS_REASON_WORD}으로 판단했습니다.",
                "semantic_text",
                c,
                r,
            )
        if _contains_any(r_compact, ["변화있", "변함있", "변화확인"]):
            return (
                FAIL_LABEL,
                f"시험기준은 성상 변화가 없어야 하나 시험결과에서 변화 의미가 나타나 {FAIL_REASON_WORD}으로 판단했습니다.",
                "semantic_text",
                c,
                r,
            )

    # 표시량 이상 / 표시량 이상 분출됨
    if "표시량이상" in c_compact:
        if "표시량이상" in r_compact or "표시량이상분출" in r_compact:
            return (
                PASS_LABEL,
                f"시험결과가 표시량 이상으로 기재되어 시험기준을 만족하므로 {PASS_REASON_WORD}으로 판단했습니다.",
                "semantic_text",
                c,
                r,
            )

    # 색상/성상 후보 중 하나가 결과에 포함되는 경우
    color_words = [
        "무색",
        "황색",
        "담황색",
        "미황색",
        "투명",
        "백색",
        "흰색",
        "유백색",
        "맑은",
        "맑고깨끗",
    ]
    criteria_colors = [word for word in color_words if word in c_compact]
    result_colors = [word for word in color_words if word in r_compact]

    if criteria_colors and result_colors:
        # 담황색은 황색의 하위 표현처럼 겹칠 수 있으므로 양방향 포함을 허용한다.
        for rc in result_colors:
            if any(rc in cc or cc in rc for cc in criteria_colors):
                return (
                    PASS_LABEL,
                    f"시험결과의 성상이 시험기준에 포함된 허용 성상과 일치하여 {PASS_REASON_WORD}으로 판단했습니다.",
                    "semantic_text",
                    c,
                    r,
                )

    return None, None, None, None, None


def _extract_100ml_ea_per_ml_value(text: str | None) -> float | None:
    raw = clean_text(text)
    if not raw:
        return None

    patterns = [
        r"100\s*mL\s*(?:기준)?\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)\s*EA\s*/\s*mL",
        r"100\s*밀리리터\s*(?:기준)?\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)\s*EA\s*/\s*mL",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.I)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None

    return None


def _judge_albumin_100ml_particle_rule(
    criteria: str | None,
    result: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    joined = clean_text(f"{criteria or ''} {result or ''}")
    compact = _compact_semantic(joined)
    if "100ml" not in compact and "100밀리리터" not in compact:
        return None, None, None, None, None
    if "eaml" not in compact and "ea/ml" not in compact:
        return None, None, None, None, None
    if not re.search(r"(10|25)\s*(?:㎛|μm|um)", joined, flags=re.I):
        return None, None, None, None, None

    value = _extract_100ml_ea_per_ml_value(result)
    if value is None:
        return None, None, None, None, None

    if re.search(r"25\s*(?:㎛|μm|um)", joined, flags=re.I):
        limit = 2.0
        particle_label = "25 ㎛"
    elif re.search(r"10\s*(?:㎛|μm|um)", joined, flags=re.I):
        limit = 25.0
        particle_label = "10 ㎛"
    else:
        return None, None, None, None, None

    ok = value <= limit
    status = PASS_LABEL if ok else FAIL_LABEL
    reason = (
        f"100 mL 기준 불용성미립자시험({particle_label}) 결과 {value:g} EA/mL가 "
        f"기준 {limit:g} EA/mL 이하라 {PASS_REASON_WORD}으로 판단했습니다."
        if ok
        else
        f"100 mL 기준 불용성미립자시험({particle_label}) 결과 {value:g} EA/mL가 "
        f"기준 {limit:g} EA/mL를 초과해 {FAIL_REASON_WORD}으로 판단했습니다."
    )
    return (
        status,
        reason,
        "albumin_100ml_particle_rule",
        f"100 mL, {particle_label} 이상: <= {limit:g} EA/mL",
        f"100 mL 기준: {value:g} EA/mL",
    )

def _judge_special_rule(
    criteria: str | None,
    result: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    for judge_func in [
        _judge_albumin_100ml_particle_rule,
        _judge_limit_expression_text,
        _judge_bp_band_pattern,
        _judge_loq_text,
        _judge_qualitative_text,
        _judge_semantic_qualitative_text,
    ]:
        status, reason, comparator, norm_criteria, norm_result = judge_func(criteria, result)

        if status is not None:
            return status, reason, comparator, norm_criteria, norm_result

    return None, None, None, None, None


def deterministic_judge(
    criteria: str | None,
    result: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    parsed_criteria = parse_criteria_text(criteria)
    parsed_result = parse_number_and_unit(result)

    if parsed_result is None:
        return None, None, None, None, None

    if parsed_criteria.kind == "compare" and parsed_criteria.threshold is not None:
        th = parsed_criteria.threshold
        op = parsed_criteria.comparator or "=="

        if not _same_or_convertible(th, parsed_result):
            return FAIL_LABEL, _unit_mismatch_reason(), op, th.pretty, parsed_result.pretty

        ok = _compare(parsed_result.value_canonical, op, th.value_canonical)
        status = PASS_LABEL if ok else FAIL_LABEL
        reason = _compare_reason(op, ok)

        return status, reason, op, th.pretty, parsed_result.pretty

    if parsed_criteria.kind == "range" and parsed_criteria.lower and parsed_criteria.upper:
        lo = parsed_criteria.lower
        hi = parsed_criteria.upper

        if not (_same_or_convertible(lo, parsed_result) and _same_or_convertible(hi, parsed_result)):
            return FAIL_LABEL, _unit_mismatch_reason(), "between", f"{lo.pretty} ~ {hi.pretty}", parsed_result.pretty

        ok = lo.value_canonical <= parsed_result.value_canonical <= hi.value_canonical
        status = PASS_LABEL if ok else FAIL_LABEL

        if ok:
            reason = f"시험결과가 시험기준 범위 안에 포함되어 {PASS_REASON_WORD}으로 판단했습니다."
        else:
            reason = f"시험결과가 시험기준 범위를 벗어나 {FAIL_REASON_WORD}으로 판단했습니다."

        return status, reason, "between", f"{lo.pretty} ~ {hi.pretty}", parsed_result.pretty

    if parsed_criteria.kind == "exact" and parsed_criteria.threshold is not None:
        th = parsed_criteria.threshold

        if not _same_or_convertible(th, parsed_result):
            return FAIL_LABEL, _unit_mismatch_reason(), "==", th.pretty, parsed_result.pretty

        ok = parsed_result.value_canonical == th.value_canonical
        status = PASS_LABEL if ok else FAIL_LABEL
        reason = _compare_reason("==", ok)

        return status, reason, "==", th.pretty, parsed_result.pretty

    return None, None, None, None, parsed_result.pretty


def _extract_numeric_requirements(criteria: str | None) -> list[str]:
    from .unit_normalizer import normalize_measurement_text
    text = normalize_measurement_text(criteria)

    if not text:
        return []

    text = text.replace("×", "x")
    text = text.replace("％", "%")
    text = text.replace("µ", "μ")
    text = text.replace("≥", ">=")
    text = text.replace("≤", "<=")

    requirements: list[tuple[int, str]] = []

    number = r"[+-]?\d[\d,]*(?:\.\d+)?(?:\s*[xX]\s*10\s*(?:\^\s*)?[+\-]?\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)?"
    unit = r"[A-Za-zμ%/²0-9\-\.·]+(?:/[A-Za-zμ%/²0-9\-\.·]+)?(?:\s+of\s+protein)?"

    range_pattern = re.compile(
        rf"(?P<lo>{number})\s*(?P<unit1>{unit})?\s*~\s*(?P<hi>{number})\s*(?P<unit2>{unit})?"
    )

    consumed_spans: list[tuple[int, int]] = []

    for m in range_pattern.finditer(text):
        unit1 = clean_text(m.group("unit1") or "")
        unit2 = clean_text(m.group("unit2") or "")
        final_unit = unit2 or unit1
        req = f"{m.group('lo')} ~ {m.group('hi')} {final_unit}".strip()
        requirements.append((m.start(), req))
        consumed_spans.append((m.start(), m.end()))

    def is_consumed(start: int, end: int) -> bool:
        return any(not (end <= s or start >= e) for s, e in consumed_spans)

    symbol_pattern = re.compile(
        rf"(?P<op>>=|<=|>|<)\s*(?P<value>{number})\s*(?P<unit>{unit})?"
    )

    for m in symbol_pattern.finditer(text):
        if is_consumed(m.start(), m.end()):
            continue

        value = clean_text(m.group("value"))
        unit_text = clean_text(m.group("unit") or "")
        req = f"{m.group('op')} {value} {unit_text}".strip()
        requirements.append((m.start(), req))

    korean_pattern = re.compile(
        rf"(?P<value>{number})\s*(?P<unit>{unit})?\s*(?P<op>이상|이하|초과|미만)"
    )

    for m in korean_pattern.finditer(text):
        if is_consumed(m.start(), m.end()):
            continue

        value = clean_text(m.group("value"))
        unit_text = clean_text(m.group("unit") or "")
        op = clean_text(m.group("op"))

        if "." in value and not unit_text and op in {"이상", "이하"}:
            continue

        req = f"{value} {unit_text} {op}".strip()
        requirements.append((m.start(), req))

    requirements.sort(key=lambda x: x[0])

    out: list[str] = []
    seen: set[str] = set()

    for _, req in requirements:
        normalized = re.sub(r"\s+", " ", req).strip()

        if normalized and normalized not in seen:
            out.append(normalized)
            seen.add(normalized)

    return out


def _extract_numeric_results(result: str | None) -> list[str]:
    from .unit_normalizer import normalize_measurement_text
    text = normalize_measurement_text(result)

    if not text:
        return []

    text = text.replace("×", "x")
    text = text.replace("％", "%")
    text = text.replace("µ", "μ")

    number = r"[+-]?\d[\d,]*(?:\.\d+)?(?:\s*[xX]\s*10\s*(?:\^\s*)?[+\-]?\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)?"
    unit = r"[A-Za-zμ%/²0-9\-\.·]+(?:/[A-Za-zμ%/²0-9\-\.·]+)?(?:\s+of\s+protein)?"

    pattern = re.compile(rf"(?P<value>{number})\s*(?P<unit>{unit})?")

    out: list[str] = []

    for m in pattern.finditer(text):
        value = clean_text(m.group("value"))
        unit_text = clean_text(m.group("unit") or "")

        if re.fullmatch(r"20\d{2}[.\-/]\d{1,2}(?:[.\-/]\d{1,2})?", value):
            continue

        item = f"{value} {unit_text}".strip()

        if item:
            out.append(item)

    return out


def _judge_multiple_numeric_requirements(
    criteria: str | None,
    result: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    requirements = _extract_numeric_requirements(criteria)
    results = _extract_numeric_results(result)

    if len(requirements) < 2:
        return None, None, None, None, None

    if not results and ("적합" in clean_text(result) or "합격" in clean_text(result)):
        return None, None, None, None, None

    if len(results) < len(requirements):
        missing_count = len(requirements) - len(results)

        reason = (
            f"시험기준은 {len(requirements)}개이나 시험결과는 {len(results)}개만 확인되어 "
            f"{missing_count}개 결과가 누락된 것으로 판단했습니다. "
            f"누락 가능 기준: {', '.join(requirements[len(results):])}. "
            f"따라서 {FAIL_REASON_WORD}으로 판단했습니다."
        )

        return (
            FAIL_LABEL,
            reason,
            "multiple_requirements",
            " / ".join(requirements),
            " / ".join(results) if results else "",
        )

    row_statuses: list[str] = []
    row_reasons: list[str] = []
    norm_criteria_list: list[str] = []
    norm_result_list: list[str] = []

    for req, res in zip(requirements, results):
        status, reason, comparator, norm_criteria, norm_result = deterministic_judge(req, res)

        if status is None:
            return (
                HOLD_LABEL,
                f"복수 기준 중 '{req}'와 결과 '{res}'를 자동 비교하기 어려워 사람의 확인이 필요합니다.",
                "multiple_requirements",
                " / ".join(requirements),
                " / ".join(results),
            )

        row_statuses.append(status)
        row_reasons.append(f"{req} ↔ {res}: {reason}")
        norm_criteria_list.append(norm_criteria or req)
        norm_result_list.append(norm_result or res)

    final_status = FAIL_LABEL if FAIL_LABEL in row_statuses else PASS_LABEL

    return (
        final_status,
        " ".join(row_reasons),
        "multiple_requirements",
        " / ".join(norm_criteria_list),
        " / ".join(norm_result_list),
    )


def _judge_with_permit_pdf_numeric_requirements(
    permit_context: str | None,
    result: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """허가서 문단 안의 수치 기준과 시험결과를 비교한다.

    주의점:
    - 허가서 한 문단에 복수 기준(예: 세포농도 + 생존률)이 있으면 결과도 단위별로 매칭한다.
    - 단순 zip 비교만 사용하면 cells/mL 기준과 % 결과가 어긋날 수 있으므로, 먼저 단위가 맞는 결과를 찾는다.
    - 현재 함수는 _get_permit_context()에서 현재 시험명 직접 근거가 확인된 문단만 받는다는 전제에서 동작한다.
    """
    permit_text = clean_text(permit_context)
    result_text = clean_text(result)

    if not permit_text or not result_text:
        return None, None, None, None, None

    requirements = _extract_numeric_requirements(permit_text)
    results = _extract_numeric_results(result_text)

    if not requirements or not results:
        return None, None, None, None, None

    # 허가서 문단의 숫자 기준이 너무 많으면 현재 시험 기준 외의 값이 섞였을 가능성이 있으므로
    # 무리하게 판정하지 않는다. 단, 직접 추출된 짧은 문단에서 4개 이하의 기준은 허용한다.
    if len(requirements) > 4:
        return None, None, None, None, None

    used_result_indexes: set[int] = set()
    row_statuses: list[str] = []
    row_reasons: list[str] = []
    norm_criteria_list: list[str] = []
    norm_result_list: list[str] = []

    def try_compare(req: str, res: str):
        return deterministic_judge(req, res)

    for req in requirements:
        matched: tuple[int, str, str, str | None, str | None, str | None] | None = None

        # 1순위: 아직 사용하지 않은 결과 중 실제로 비교 가능한 단위/형태가 맞는 값
        for idx, res in enumerate(results):
            if idx in used_result_indexes:
                continue

            status, reason, comparator, norm_criteria, norm_result = try_compare(req, res)
            if status is not None:
                matched = (idx, status, reason or "", norm_criteria, norm_result, comparator)
                break

        # 2순위: 순서 기반 fallback. 단위가 없거나 LLM/OCR로 단위가 일부 누락된 경우를 위한 최소 fallback.
        if matched is None:
            fallback_idx = len(used_result_indexes)
            if fallback_idx < len(results):
                res = results[fallback_idx]
                status, reason, comparator, norm_criteria, norm_result = try_compare(req, res)
                if status is not None:
                    matched = (fallback_idx, status, reason or "", norm_criteria, norm_result, comparator)

        if matched is None:
            return None, None, None, None, None

        idx, status, reason, norm_criteria, norm_result, comparator = matched
        used_result_indexes.add(idx)
        res = results[idx]

        row_statuses.append(status)
        row_reasons.append(f"{req} ↔ {res}: {reason}")
        norm_criteria_list.append(norm_criteria or req)
        norm_result_list.append(norm_result or res)

    if len(used_result_indexes) < len(requirements):
        return None, None, None, None, None

    final_status = FAIL_LABEL if FAIL_LABEL in row_statuses else PASS_LABEL
    short_basis = _short_permit_basis(permit_text)

    if final_status == PASS_LABEL:
        final_reason = (
            f"허가서 기준에 따라 {short_basis}이고, 시험결과가 모든 수치 기준을 만족하여 "
            f"{PASS_REASON_WORD}으로 판단했습니다. "
            + " ".join(row_reasons)
        )
    else:
        final_reason = (
            f"허가서 기준에 따라 {short_basis}이나, 시험결과를 비교한 결과 일부 기준을 만족하지 못하여 "
            f"{FAIL_REASON_WORD}으로 판단했습니다. "
            + " ".join(row_reasons)
        )

    return (
        final_status,
        final_reason,
        "permit_pdf_numeric_requirements",
        " / ".join(norm_criteria_list),
        " / ".join(norm_result_list),
    )

def _judge_with_permit_pdf_text_requirements(
    permit_context: str | None,
    result: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """허가서 문장 기준과 시험결과를 정성 비교한다.

    예:
    - 허가서: 세포가 뭉치지 않아야 함 / 결과: 세포가 뭉치지 않음 → 합격
    - 허가서: 바이러스가 관찰되지 않아야 함 / 결과: 바이러스가 관찰됨 → 불합격
    """
    permit_text = clean_text(permit_context)
    result_text = clean_text(result)

    if not permit_text or not result_text:
        return None, None, None, None, None

    short_basis = _short_permit_basis(permit_text)
    basis_for_reason = short_basis or "현재 시험에 직접 연결된 허가서 기준"

    if _permit_requires_negative(permit_text):
        if _result_has_negative_meaning(result_text):
            return (
                PASS_LABEL,
                f"허가서 기준은 '{basis_for_reason}'이고, 시험결과도 이를 만족하여 {PASS_REASON_WORD}으로 판단했습니다.",
                "permit_pdf_negative_text",
                basis_for_reason,
                result_text,
            )

        if _result_has_positive_detection(result_text):
            return (
                FAIL_LABEL,
                f"허가서 기준은 '{basis_for_reason}'이나, 시험결과에서 확인 또는 검출 의미가 나타나 {FAIL_REASON_WORD}으로 판단했습니다.",
                "permit_pdf_negative_text",
                basis_for_reason,
                result_text,
            )

        return None, None, None, None, None

    if _permit_requires_match(permit_text):
        result_compact = _compact_semantic(result_text)

        if "불일치" in result_compact or "일치하지않" in result_compact:
            return (
                FAIL_LABEL,
                f"허가서 기준은 '{basis_for_reason}'이나, 시험결과에서 불일치가 확인되어 {FAIL_REASON_WORD}으로 판단했습니다.",
                "permit_pdf_match_text",
                basis_for_reason,
                result_text,
            )

        if "일치" in result_compact or "동일" in result_compact:
            return (
                PASS_LABEL,
                f"허가서 기준은 '{basis_for_reason}'이고, 시험결과도 일치하여 {PASS_REASON_WORD}으로 판단했습니다.",
                "permit_pdf_match_text",
                basis_for_reason,
                result_text,
            )

        return None, None, None, None, None

    if _permit_requires_confirmation(permit_text):
        if _result_has_negative_meaning(result_text):
            return (
                FAIL_LABEL,
                f"허가서 기준은 '{basis_for_reason}'이나, 시험결과에서 확인되지 않은 것으로 나타나 {FAIL_REASON_WORD}으로 판단했습니다.",
                "permit_pdf_confirmation_text",
                basis_for_reason,
                result_text,
            )

        if _result_has_positive_detection(result_text):
            return (
                PASS_LABEL,
                f"허가서 기준은 '{basis_for_reason}'이고, 시험결과에서 확인 또는 동정 의미가 나타나 {PASS_REASON_WORD}으로 판단했습니다.",
                "permit_pdf_confirmation_text",
                basis_for_reason,
                result_text,
            )

        return None, None, None, None, None

    return None, None, None, None, None



class JudgeEngine:
    """
    판정 단계 구조
    ----------------
    1차 판정(before): SP 문서 내부 정보만 사용한다.
      - 규칙 기반 판정(rule_before)
      - 규칙으로 불확실하면 근거자료 기반 추가 판정(gemini_second)
      - 그래도 불확실하면 보류(manual_hold_before)

    허가서 반영 판정(after): 1차와 추가 판정 후에도 보류된 항목에 한해 허가서 PDF 기준을 반영한다.
      - 현재 시험과 직접 연결되는 허가서 chunk만 사용한다.
      - 허가서 규칙 판정(permit_pdf_rule_after)
      - 허가서 근거자료 판정(permit_pdf_llm_after)
      - 허가서 기준이 직접 연결되지 않으면 1차 보류를 유지한다.
    """

    def __init__(
        self,
        rag_store: UcumRagStore,
        llm_client: ClovaJudgeClient | None = None,
        permit_store: PermitPdfStore | None = None,
        permit_policy: PermitPolicy | None = None,
        permit_catalog_valid: bool | None = None,
    ) -> None:
        self.rag_store = rag_store
        self.llm_client = llm_client
        self.permit_store = permit_store
        self.permit_policy = permit_policy or getattr(permit_store, "policy", None)
        self.permit_catalog_valid = permit_catalog_valid
        self.comparison_bases: dict[int, list[dict]] = {}

    @staticmethod
    def _record_context(record: ExtractedRecord) -> str:
        """Serialize the complete SP record for an authoritative permit call."""
        parts: list[str] = []
        for name, value in vars(record).items():
            if name in {"pdf_name", "batch_id", "batch_evidence"}:
                # File naming and grouping metadata are neither test criteria nor
                # product evidence.  Keep the legacy provider prompt unchanged.
                continue
            if value is None or value == "" or value == [] or value == {}:
                continue
            parts.append(f"{name}: {value}")
        return "\n".join(parts)

    def prepare_authoritative_request(self, record, *, position):
        from .numeric_safety import result_is_requirement
        from .permit_grouping import resolve_record_batch_evidence
        from .permit_evidence_bundle import build_evidence_bundle
        from .permit_semantic_batch import SemanticPermitRequest
        from .permit_semantic_settings import SemanticSettings, semantic_fingerprint
        policy, store = self.permit_policy, self.permit_store
        self.semantic_preparation_reason = 'guard_or_unexpanded_table'
        self.semantic_retrieval_route = 'not_searched'
        if (not policy or not policy.authoritative or getattr(policy, 'evaluation_mode', 'legacy') == 'deterministic_first'
            or self.permit_group_preflight_reason() or _permit_has_fatal_extraction_error(store.extraction_errors)
            or not self.llm_client or not self.llm_client.enabled
            or result_is_requirement(record.criteria, record.result) or not record.criteria or not record.result
            or record.ocr_suspect or record.result_table or record.tables
            or _parse_result_table(record.result) or _parse_result_table(record.raw_text)):
            return None
        batch = resolve_record_batch_evidence(record, inventory=getattr(self, 'semantic_batch_inventory', {}))
        if batch.state != 'explicit' or not batch.batch:
            self.semantic_preparation_reason = 'batch_not_explicit'
            return None
        selection = store.search_precise(record)
        self.semantic_retrieval_route = selection.route
        if not selection.complete:
            self.semantic_preparation_reason = 'retrieval_incomplete'
            return None
        company, product = getattr(self, 'semantic_identity', ('', ''))
        cache_key = (store._precise_index.source_fingerprint, tuple(c.evidence_id for c in selection.chunks), company, product)
        cache = getattr(self, '_semantic_bundles', None)
        if cache is None:
            self._semantic_bundles = cache = {}
        if cache_key not in cache:
            cache[cache_key] = build_evidence_bundle(selection, all_chunks=store.chunks,
                documents=store.source_documents, company=company, product=product)
        bundle = cache[cache_key]
        if not bundle.complete:
            self.semantic_preparation_reason = 'bundle_incomplete'
            return None
        self.semantic_preparation_reason = 'eligible'
        context = dict(vars(record))
        context['submitted_batch_evidence'] = context.get('batch_evidence')
        context['batch_evidence'] = asdict(batch)
        context = json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)
        fingerprint = hashlib.sha256(json.dumps([context, bundle.bundle_id, repr(policy),
            getattr(self.llm_client, 'execution_fingerprint', ''), self.llm_client.model,
            self.llm_client.max_completion_tokens, semantic_fingerprint(SemanticSettings('shared_evidence'))],
            ensure_ascii=False).encode('utf-8')).hexdigest()
        return SemanticPermitRequest(f'r{position}', position, bundle, context,
                                     clean_text(record.criteria), clean_text(record.result), fingerprint)

    def _active_semantic_response(self, record):
        active = _SEMANTIC_PREPARED.get()
        if active is None or active[0] is not self:
            return None
        prepared = active[1]
        current = self.prepare_authoritative_request(record, position=prepared.request.position)
        if (current is None or current.request_fingerprint != prepared.request_fingerprint
            or current.record_id != prepared.record_id):
            raise ValueError('prepared authoritative fingerprint mismatch')
        return prepared

    def permit_group_preflight_reason(self) -> str | None:
        """Return a safe global health failure before candidate classification."""
        if self.permit_catalog_valid is False:
            return "permit_catalog_invalid"
        store = self.permit_store
        if store is None or not getattr(store, "enabled", False):
            return "permit_store_unavailable"
        for raw_error in getattr(store, "extraction_errors", ()) or ():
            folded = clean_text(raw_error).casefold()
            if folded.startswith("permit index ") or re.search(
                r"^(?:could not read permit pdf:|could not extract permit |could not render permit ocr pages:)",
                folded,
            ):
                return "permit_index_unusable"
        return None

    @staticmethod
    def _permit_group_hold(
        group: PermitGroup,
        reason_code: str,
        *,
        condition_ids=(),
        evidence_ids=(),
    ) -> GroupVerdict:
        from .permit_group_judgement import GroupVerdict

        return GroupVerdict(
            status=HOLD_LABEL,
            reason=f"허가서 그룹 근거를 확정할 수 없어 검수보류입니다. ({reason_code})",
            source="permit_group_hold",
            group_id=group.group_id,
            gate_path="hold_without_llm",
            condition_ids=tuple(condition_ids),
            fact_ids=tuple(fact.fact_id for fact in group.facts),
            evidence_ids=tuple(evidence_ids),
        )

    def prepare_permit_group(
        self,
        group: PermitGroup,
        conditions_by_evidence_id: Mapping[str, Sequence[PermitCondition]],
    ) -> PreparedPermitGroup:
        """Prepare trusted inputs without making a provider call."""
        from .permit_group_judgement import (
            GateDecision,
            GroupVerdict,
            PreparedPermitGroup,
            classify_group,
            execute_permit_conditions,
        )

        store = self.permit_store
        candidate_ids = tuple(dict.fromkeys(group.candidate_evidence_ids))
        preflight_reason = self.permit_group_preflight_reason()
        if preflight_reason is not None:
            gate = GateDecision("hold_without_llm", preflight_reason)
            immediate = self._permit_group_hold(
                group,
                preflight_reason,
                evidence_ids=candidate_ids,
            )
            return PreparedPermitGroup(group, (), (), (), gate, immediate)

        chunks_by_id = {
            clean_text(getattr(chunk, "evidence_id", "")): chunk
            for chunk in getattr(store, "chunks", ())
            if clean_text(getattr(chunk, "evidence_id", ""))
        }
        candidates = tuple(
            chunks_by_id[evidence_id]
            for evidence_id in candidate_ids
            if evidence_id in chunks_by_id
        )
        conditions = tuple(
            condition
            for evidence_id in candidate_ids
            for condition in conditions_by_evidence_id.get(evidence_id, ())
        )

        # Index-integrity errors are global.  Page OCR errors only invalidate a
        # candidate whose source-page span overlaps the unreadable page.
        candidate_pages: set[int] = set()
        for candidate in candidates:
            page_start = getattr(candidate, "page_start", None)
            page_end = getattr(candidate, "page_end", None)
            if page_start is None:
                page_start = getattr(candidate, "page_number", None)
            if page_end is None:
                page_end = page_start
            if isinstance(page_start, int) and isinstance(page_end, int):
                candidate_pages.update(range(page_start, page_end + 1))

        for raw_error in getattr(store, "extraction_errors", ()) or ():
            error = clean_text(raw_error)
            folded = error.casefold()
            unreadable = re.match(r"^permit ocr page (\d+) is unreadable$", folded)
            if unreadable and int(unreadable.group(1)) in candidate_pages:
                gate = GateDecision(
                    "hold_without_llm",
                    "permit_candidate_page_unreadable",
                )
                immediate = self._permit_group_hold(
                    group,
                    "permit_candidate_page_unreadable",
                    condition_ids=(condition.condition_id for condition in conditions),
                    evidence_ids=candidate_ids,
                )
                return PreparedPermitGroup(
                    group,
                    candidates,
                    conditions,
                    (),
                    gate,
                    immediate,
                )

        executions = tuple(execute_permit_conditions(conditions, group.facts))
        gate = classify_group(group, candidates, conditions, executions)
        if gate.path == "hold_without_llm":
            immediate = self._permit_group_hold(
                group,
                gate.reason_code,
                condition_ids=(execution.condition_id for execution in executions),
                evidence_ids=(candidate.evidence_id for candidate in candidates),
            )
            return PreparedPermitGroup(
                group,
                candidates,
                conditions,
                executions,
                gate,
                immediate,
            )

        if gate.path == "deterministic":
            by_id = {condition.condition_id: condition for condition in conditions}
            failures = tuple(execution for execution in executions if execution.status == "fail")
            pending = tuple(execution for execution in executions if execution.status == "hold")
            if failures:
                status = FAIL_LABEL
                quotes = [
                    clean_text(by_id[execution.condition_id].quote)
                    for execution in failures
                    if execution.condition_id in by_id
                    and clean_text(by_id[execution.condition_id].quote)
                ]
                reason = "허가서 기준과 일치하지 않아 검수불합격입니다."
                if quotes:
                    reason = f"허가서에 '{quotes[0]}' 기준이 있으나 시험결과가 충족하지 않아 검수불합격입니다."
            elif pending:
                # Defensive closure: the classifier should normally route this
                # state to HOLD or semantic ambiguity, never PASS.
                status = HOLD_LABEL
                reason = "허가서의 모든 필수 조건을 입증하지 못해 검수보류입니다."
            else:
                status = PASS_LABEL
                reason = "허가서의 모든 구조화 필수 조건을 충족하여 검수합격입니다."

            immediate = GroupVerdict(
                status=status,
                reason=reason,
                source="permit_group_deterministic",
                group_id=group.group_id,
                gate_path="deterministic",
                condition_ids=tuple(execution.condition_id for execution in executions),
                fact_ids=tuple(dict.fromkeys(
                    fact_id for execution in executions for fact_id in execution.fact_ids
                )),
                evidence_ids=tuple(candidate.evidence_id for candidate in candidates),
            )

            return PreparedPermitGroup(
                group,
                candidates,
                conditions,
                executions,
                gate,
                immediate,
            )

        return PreparedPermitGroup(
            group,
            candidates,
            conditions,
            executions,
            gate,
            None,
        )

    def resolve_prepared_permit_group(
        self,
        prepared: PreparedPermitGroup,
        response: GroundedGroupResponse | None,
        *,
        missing_reason: str = "llm_response_unavailable",
    ) -> GroupVerdict:
        """Resolve one prepared group through the existing grounding validator."""
        from .permit_group_judgement import validate_group_response

        if prepared.immediate_verdict is not None:
            return prepared.immediate_verdict
        if response is None:
            return self._permit_group_hold(
                prepared.group,
                missing_reason,
                condition_ids=(
                    execution.condition_id for execution in prepared.executions
                ),
                evidence_ids=(
                    candidate.evidence_id for candidate in prepared.candidates
                ),
            )
        try:
            return validate_group_response(
                response,
                prepared.group,
                prepared.candidates,
                prepared.conditions,
                prepared.executions,
            )
        except Exception:
            return self._permit_group_hold(
                prepared.group,
                "llm_grounding_invalid",
                condition_ids=(
                    execution.condition_id for execution in prepared.executions
                ),
                evidence_ids=(
                    candidate.evidence_id for candidate in prepared.candidates
                ),
            )

    def judge_permit_group(self, group, conditions_by_evidence_id):
        """Compatibility wrapper for one grouped permit judgement."""
        prepared = self.prepare_permit_group(group, conditions_by_evidence_id)
        if prepared.immediate_verdict is not None:
            return prepared.immediate_verdict

        client = self.llm_client
        if client is None or not getattr(client, "enabled", False):
            return self.resolve_prepared_permit_group(
                prepared,
                None,
                missing_reason="llm_unavailable",
            )
        try:
            response = client.explain_permit_group_once(
                group=prepared.group,
                candidates=prepared.candidates,
                conditions=prepared.conditions,
                executions=prepared.executions,
            )
        except ClovaServiceError:
            raise
        except Exception:
            response = None
        return self.resolve_prepared_permit_group(prepared, response)

    def _search_rag_contexts(self, query: str, top_k: int = 9) -> list[str]:
        if self.rag_store is None:
            return []
        docs = self.rag_store.search(query, top_k=max(top_k * 2, top_k))
        if not docs:
            return []

        contexts: list[str] = []
        seen_domains: set[str] = set()

        # 현재 시험에 직접 가까운 결과를 우선 보존하면서, 허가서/기준서/기호 사전이 한쪽으로
        # 밀리지 않도록 도메인별 첫 결과를 조금 더 앞에 둔다.
        for doc in docs:
            domain = clean_text((getattr(doc, "meta", {}) or {}).get("rag_domain")) or "참고자료"
            if domain in seen_domains:
                continue
            contexts.append(_format_rag_doc_context(doc))
            seen_domains.add(domain)
            if len(contexts) >= min(4, top_k):
                break

        for doc in docs:
            formatted = _format_rag_doc_context(doc)
            if formatted in contexts:
                continue
            contexts.append(formatted)
            if len(contexts) >= top_k:
                break

        return contexts

    def _get_permit_context(self, record: ExtractedRecord) -> str:
        prepared = self._active_semantic_response(record)
        if prepared is not None:
            from .permit_evidence_bundle import render_bundle_context
            return render_bundle_context(prepared.request.bundle)
        if self.permit_store is None or not self.permit_store.enabled:
            return ""

        """
        허가서 근거 후보 문단을 만든다.

        중요한 변경점:
        - 1차 판정 로직은 그대로 둔다.
        - 허가서의 시험명과 실제 SP 시험명이 완전히 같지 않아도
          LLM이 의미적으로 같은 항목인지 매핑해서 판단할 수 있도록 후보 문단을 제공한다.
        - 단, LLM에게 관련 없는 허가서 문단이면 반드시 검수보류를 유지하라고 지시한다.
        - 따라서 이전처럼 exact anchor가 없다고 바로 포기하지 않고,
          permit_store.search() 결과를 후보로 넘긴다.
        """

        chunks = self.permit_store.search(record, top_k=12)
        if not chunks:
            return ""

        if self.permit_policy is not None and self.permit_policy.authoritative:
            # Authoritative judging must see every retrieved chunk in full.  The
            # legacy candidate clipping below remains unchanged for other policies.
            context_parts: list[str] = []
            for index, chunk in enumerate(chunks, start=1):
                page_start = chunk.page_start if chunk.page_start is not None else chunk.page_number
                page_end = chunk.page_end if chunk.page_end is not None else chunk.page_number
                page_range = str(page_start) if page_start == page_end else f"{page_start}-{page_end}"
                path = " > ".join(clean_text(value) for value in chunk.section_path_titles if clean_text(value))
                path_line = f"\n- 경로: {path}" if path else ""
                context_parts.append(
                    f"[허가서 근거 {index}]\n- 파일: {chunk.source_file}\n- 페이지: {page_range}\n"
                    f"- 섹션: {chunk.section_number} {chunk.title}{path_line}\n- 내용:\n{chunk.text}"
                )
            context = "\n\n".join(context_parts).strip()
            if not context:
                return ""
            return (
                "[허가서 PDF 판정 후보 문단]\n"
                "아래 내용은 허가서 PDF에서 검색된 전체 후보 문단입니다.\n"
                + context
            )

        direct_snippets: list[str] = []
        candidate_snippets: list[str] = []

        for chunk in chunks:
            raw_text = _permit_chunk_text(chunk)
            if not raw_text:
                continue

            snippet = _extract_direct_permit_snippet(record, chunk)
            if snippet and snippet not in direct_snippets:
                direct_snippets.append(snippet)
                continue

            # exact anchor가 없어도 LLM이 시험명 의미 매핑을 할 수 있도록 후보 문단을 보존한다.
            # 너무 긴 page 전체가 들어가면 다른 시험 기준이 섞여 오판할 수 있으므로 길이를 제한한다.
            clipped = clean_text(raw_text)
            if len(clipped) > 1800:
                clipped = clipped[:1800] + " ..."

            if clipped and clipped not in candidate_snippets:
                candidate_snippets.append(clipped)

        selected = direct_snippets[:4]

        # 직접 시험명 매칭 문단이 없을 때만 넓은 후보 문단을 넣는다.
        # 직접 매칭 문단이 있더라도 1~2개 부족하면 후보를 조금 더 붙여 LLM이 문맥을 보게 한다.
        if len(selected) < 2:
            for item in candidate_snippets:
                if item not in selected:
                    selected.append(item)
                if len(selected) >= 5:
                    break

        if not selected:
            return ""

        context = "\n\n---\n\n".join(selected).strip()

        return (
            "[허가서 PDF 판정 후보 문단]\n"
            "아래 내용은 허가서 PDF에서 검색된 후보 문단입니다.\n"
            "현재 SP 시험명과 허가서의 시험명이 완전히 같지 않을 수 있으므로, "
            "의미적으로 같은 시험인지 먼저 매핑하세요.\n"
            "예: '세포형태확인시험'과 '현미경으로 세포를 관찰하였을 때 세포가 뭉치지 않아야 함'은 같은 항목일 수 있습니다.\n"
            "예: '세포성장 및 증식확인시험'은 허가서의 세포농도/생존률 기준과 연결될 수 있습니다.\n"
            "하지만 현재 시험과 직접 관련 없는 허가서 문단이라면 절대 그 기준을 적용하지 말고 검수보류를 유지하세요.\n"
            "허가서 기준으로 판정하는 경우 판정이유에는 실제 기준과 시험결과가 어떻게 맞는지 짧게 설명하세요.\n\n"
            + context
        )

    def _judge_by_primary_rules(
        self,
        *,
        criteria: str | None,
        result: str | None,
    ) -> tuple[str | None, str | None, str | None, str | None, str | None, str]:
        """허가서 없이 수행하는 1차 규칙 기반 판정."""
        from .numeric_safety import minimum_count_veto
        minimum_veto = minimum_count_veto(criteria or "", result or "")
        if minimum_veto is not None:
            return (minimum_veto.status, minimum_veto.reason, "minimum_count_verified",
                    clean_text(criteria), clean_text(result), "rule_before")
        status, reason = _status_from_text_result(result)

        if status is not None:
            return status, reason, "text_result", clean_text(criteria), clean_text(result), "rule_before"

        status, reason, comparator, norm_criteria, norm_result = _judge_special_rule(
            criteria,
            result,
        )

        if status is not None:
            return status, reason, comparator, norm_criteria, norm_result, "rule_before"

        status, reason, comparator, norm_criteria, norm_result = _judge_multiple_numeric_requirements(
            criteria,
            result,
        )

        if status is not None:
            return status, reason, comparator, norm_criteria, norm_result, "rule_before"

        status, reason, comparator, norm_criteria, norm_result = deterministic_judge(
            criteria,
            result,
        )

        if status is not None:
            return status, reason, comparator, norm_criteria, norm_result, "rule_before"

        return None, None, None, None, None, "rule_before"

    def _judge_with_llm_primary(
        self,
        *,
        record: ExtractedRecord,
        title: str,
        criteria: str | None,
        result: str | None,
        deterministic_reason: str | None,
        norm_criteria: str | None,
        norm_result: str | None,
    ) -> tuple[str | None, str | None, str | None, str | None, str | None, str]:
        """
        허가서 없이 수행하는 추가 판정.

        1차 규칙/JSON 판정에서 확정하지 못한 보류 항목을 의미 비교한다.
        SP 문서의 시험기준과 시험결과만으로 비교 가능하면 허가서가 없어도 PASS/FAIL로 확정한다.
        실제 기준이 비어 있거나 외부 문서 없이는 기준 자체를 알 수 없는 경우만 보류로 둔다.
        """
        if self.llm_client is None:
            return None, None, None, norm_criteria, norm_result, "llm_before"

        query = " ".join(
            filter(
                None,
                [
                    record.test_name,
                    criteria,
                    result,
                    record.method,
                    record.test_date,
                    record.test_period,
                    record.remarks,
                    record.raw_text,
                ],
            )
        )

        sp_only_review = getattr(self, 'sp_only_review', False)
        # The HOLD-only baseline cannot retrieve an external permit through RAG.
        rag_contexts = [] if sp_only_review else self._search_rag_contexts(query, top_k=9)

        llm_instruction = "\n".join(
            filter(
                None,
                [
                    deterministic_reason,
                    "1차 규칙/JSON 판정에서 보류된 항목입니다. 시험기준과 시험결과의 의미를 독립적으로 비교합니다.",
                    ("현재 단계에서는 SP에 기재된 시험기준과 실제 결과만 비교합니다. 허가서 내용을 추측하거나 참고하지 마세요. 외부 기준이 필요하면 보류로 남기세요."
                     if sp_only_review else "필요하면 기호 사전, 허가서, 생물학적제제 기준 및 시험방법 근거를 참고하되, 현재 시험과 직접 관련 있는 근거만 적용합니다."),
                    "시험기준과 시험결과가 직접 비교 가능하면 반드시 검수합격 또는 검수불합격을 판단하세요.",
                    "정성 기준은 문장 의미를 분해해서 판단하세요. 예: '뭉치지 않고 구형이어야 함'은 '응집 없음'과 '구형'을 모두 확인합니다.",
                    "'확인되지 않아야 함', '검출되지 않아야 함', '관찰되지 않아야 함'은 결과의 '확인되지 않음', '미검출', '불검출', '음성'과 의미상 일치하면 검수합격입니다.",
                    "수치 제한 기준에서 시험결과가 '20 EU/mL 미만', '820 EU/mL 미만'처럼 기준과 같거나 더 엄격한 제한값 표현으로 기재되면 실제 측정값이 20 또는 820이라는 뜻이 아니라 해당 제한을 만족한다는 의미이므로 검수합격으로 판단하세요.",
                    "'유전자 중 1종 이상이 정량한계 미만'처럼 일부 항목 충족을 요구하는 기준은 표 전체에서 정량한계 미만 항목 수를 세어 판단하세요. 2개가 정량한계 이상이어도 1개가 정량한계 미만이고 기준이 1종 이상이면 검수합격입니다.",
                    "허가서가 없어도 SP 문서 안의 시험기준과 시험결과만으로 비교 가능하면 허가서 확인 대상으로 미루지 마세요.",
                    "검수보류는 시험기준/시험결과가 비어 있거나, OCR이 심하게 깨졌거나, 실제 기준이 '허가서 기준에 따름'처럼 외부 문서에만 존재하는 경우에만 사용하세요.",
                ],
            )
        )

        llm_resp = self.llm_client.explain(
            test_name=title,
            criteria=criteria,
            result=result,
            rag_contexts=rag_contexts,
            forced_status=None,
            deterministic_reason=llm_instruction,
        )

        if llm_resp is None or not llm_resp.status:
            return None, None, None, norm_criteria, norm_result, "llm_before"

        llm_reason = _sanitize_user_reason(llm_resp.reason)
        llm_norm_criteria = clean_text(llm_resp.normalized_criteria) or norm_criteria
        llm_norm_result = clean_text(llm_resp.normalized_result) or norm_result

        if llm_resp.status == PASS_LABEL:
            status = PASS_LABEL
        elif llm_resp.status == FAIL_LABEL:
            status = FAIL_LABEL
        else:
            status = HOLD_LABEL

        # 추가 판정이 PASS/FAIL을 반환하면 그 판단을 신뢰한다.
        # 보류는 명시적으로 보류를 선택한 경우에만 유지한다.
        if status == HOLD_LABEL:
            return (
                HOLD_LABEL,
                llm_reason or "시험기준과 시험결과만으로 실제 기준을 확인할 수 없어 검수보류 처리했습니다.",
                "gemini_second_hold",
                llm_norm_criteria,
                llm_norm_result,
                "gemini_second",
            )

        return (
            status,
            llm_reason or "시험기준과 시험결과를 비교해 판정했습니다.",
            "gemini_second",
            llm_norm_criteria,
            llm_norm_result,
            "gemini_second",
        )

    def _judge_with_authoritative_permit(
        self,
        *,
        record: ExtractedRecord,
        title: str,
        permit_context: str,
        first_status: str | None,
        first_reason: str | None,
        norm_criteria: str | None,
        norm_result: str | None,
    ) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None]:
        """Apply the structured permit verdict, safely and only when mapped."""
        decision_basis = {"source": "unresolved", "reason": "허가서 적용 기준을 확정하지 못했습니다."}
        self.comparison_bases.setdefault(record.order_idx, []).append(decision_basis)
        safe_hold = (
            HOLD_LABEL,
            "허가서 기준의 시험 매칭 또는 모든 조건을 확정할 수 없어 검수보류로 판단했습니다.",
            "permit_pdf_llm_authoritative",
            norm_criteria,
            norm_result or clean_text(record.result),
            "permit_pdf_llm_authoritative",
        )

        store = self.permit_store
        if self.permit_catalog_valid is False:
            return safe_hold
        if store is not None and _permit_has_fatal_extraction_error(getattr(store, "extraction_errors", None)):
            return safe_hold

        if not permit_context:
            if store is not None and getattr(store, "extraction_errors", None) and not store.enabled:
                return safe_hold
            if store is not None and store.has_ambiguous_generic_test(record):
                return safe_hold
            # A usable permit store with no semantic search candidate is a
            # genuine not-found case, so retain the primary SP verdict.
            decision_basis.update(source="sp", reason="허가서에서 해당 단계·시험 기준을 찾지 못해 SP 기준 적용")
            return None, None, None, None, None, None

        client = self.llm_client
        if client is None or not getattr(client, "enabled", True):
            return safe_hold

        query = " ".join(
            filter(
                None,
                [
                    record.test_name,
                    record.section_title,
                    record.criteria,
                    record.result,
                    record.method,
                    record.raw_text,
                    permit_context,
                ],
            )
        )
        rag_contexts = [permit_context, *self._search_rag_contexts(query, top_k=8)]
        deterministic_reason = "\n".join(
            [
                "권위 있는 허가서 기준 판정 단계입니다.",
                f"앞선 SP 판정: {first_status or '미확정'}",
                f"앞선 SP 판정 이유: {first_reason or ''}",
                "허가서 시험명과 현재 SP 시험을 의미적으로 매칭하고, 허가서의 모든 조건을 하나씩 확인하세요.",
                "허가서 섹션 번호는 매칭 근거로 사용하지 마세요.",
                "허가서 기준은 SP 기준보다 우선합니다.",
                "permit_match_status는 matched, ambiguous, not_found 중 하나여야 합니다.",
            ]
        )
        llm_criteria = "\n".join(["[SP 문서의 시험기준]", clean_text(record.criteria)])

        prepared = self._active_semantic_response(record)
        try:
            response = prepared.response if prepared is not None else client.explain(
                test_name=title,
                criteria=llm_criteria,
                result=record.result,
                rag_contexts=rag_contexts,
                forced_status=None,
                deterministic_reason=deterministic_reason,
                authoritative_permit=True,
                record_context=self._record_context(record),
            )
        except ClovaServiceError:
            raise
        except Exception:
            return safe_hold

        if response is None or not getattr(response, "status", None):
            return safe_hold

        response_status = clean_text(getattr(response, "status", None))
        if response_status not in {PASS_LABEL, FAIL_LABEL, HOLD_LABEL}:
            return safe_hold

        match_status = clean_text(getattr(response, "permit_match_status", None)).casefold()
        if match_status == "not_found":
            # No mapped permit requirement: the primary SP verdict remains authoritative.
            decision_basis.update(source="sp", reason="해당 허가 시험 기준이 없어 SP 기준 적용")
            return None, None, None, None, None, None
        if match_status == "ambiguous":
            return (
                HOLD_LABEL,
                "허가서 근거 연결을 확정하지 못해 검수보류입니다. " +
                (" / ".join(getattr(response, "_permit_grounding_errors", [])) or
                 "시험 매칭 또는 원문 연결 확인이 필요합니다."),
                "permit_pdf_llm_authoritative",
                norm_criteria,
                norm_result or clean_text(record.result),
                "permit_pdf_llm_authoritative",
            )
        if match_status not in {"matched", "method_only"} or response_status not in {PASS_LABEL, FAIL_LABEL, HOLD_LABEL}:
            return safe_hold

        matched_test = clean_text(getattr(response, "matched_permit_test", None))
        basis = clean_text(getattr(response, "permit_basis", None))
        from .permit_llm_protocol import _without_page_furniture
        context_compact = _compact_semantic(_without_page_furniture(_authoritative_permit_evidence(permit_context)))

        generic_test_titles = {
            "허가서", "permit", "pdf", "판정", "후보", "문단", "판정후보문단", "pdf판정후보문단",
            "허가서pdf판정후보문단", "시험", "확인시험", "성상", "무균시험", "기준", "조건", "확인",
        }
        def grounded(value: str, *, test_identity: bool = False) -> bool:
            if not test_identity:
                # A verbatim sentence may contain commas, DNA, or unit slashes.
                # Splitting those into short fragments incorrectly rejects real evidence.
                whole = _compact_semantic(_without_page_furniture(value))
                if whole and whole in context_compact:
                    return True
            raw_parts = re.split(r"\s*(?:>|/|\\|\||≫|→|,|;|:)\s*", value)
            parts: list[str] = []
            for raw_part in raw_parts:
                part = re.sub(r"^\s*\d+(?:\.\d+)+\.?\s*", "", raw_part).strip()
                compact = _compact_semantic(part)
                if not compact or re.fullmatch(r"\d+(?:\.\d+)*", compact):
                    return False
                if not test_identity and len(compact) < 4 and not re.search(r"\d", compact):
                    return False
                parts.append(compact)
            if not parts:
                return False
            if test_identity:
                distinctive = [part for part in parts if part not in generic_test_titles and len(part) >= 4]
                if not distinctive:
                    return False
            return all(part in context_compact for part in parts)

        if not grounded(matched_test, test_identity=True) or not grounded(basis):
            return safe_hold
        if match_status == "method_only":
            if re.search(r"생존[율률]|검출되지|이어야|되어야|없어야|않아야|해야|하여야", basis):
                return safe_hold
            decision_basis.update(source="sp", reason="허가서는 시험방법만 지정하여 SP 결과 기준 적용")
            return None, None, None, None, None, None
        if not _is_concrete_permit_basis(basis):
            return safe_hold

        failed = getattr(response, "failed_requirements", None) or []
        if not isinstance(failed, list) or any(
            not _is_concrete_permit_basis(clean_text(item)) or not grounded(clean_text(item))
            for item in failed
        ):
            return safe_hold

        trusted_result = clean_text(record.result)
        llm_norm_criteria = basis
        llm_norm_result = trusted_result
        from .numeric_safety import permit_numeric_veto
        acceptance_basis = getattr(response, "permit_acceptance_basis", None)
        if acceptance_basis is not None and (not acceptance_basis or any(not grounded(text) for text in acceptance_basis)):
            return safe_hold
        decision_basis.update(source="permit", criteria="\n".join(acceptance_basis or [basis]),
                              reason="시험 판정과 동일한 원문 연결 검증을 통과한 허가 적합 조건",
                              evidence=getattr(response, "_permit_evidence", []))
        numeric_veto = permit_numeric_veto(basis, trusted_result, acceptance_basis=acceptance_basis)
        if numeric_veto is not None and response_status != FAIL_LABEL:
            guard_reason = numeric_veto.reason
            if numeric_veto.status == HOLD_LABEL:
                sp_status, *_ = deterministic_judge(record.criteria, trusted_result)
                if sp_status in (PASS_LABEL, FAIL_LABEL):
                    sp_label = "충족" if sp_status == PASS_LABEL else "불충족"
                    guard_reason = (f"SP 기재 기준만 비교하면 {sp_label}입니다 "
                        f"(기준: {record.criteria}, 결과: {trusted_result}). "
                        "최종 판정에는 연결된 허가서 기준을 우선 적용합니다. " + guard_reason)
            return (numeric_veto.status, guard_reason, "permit_pdf_numeric_guard",
                    basis, trusted_result, "permit_pdf_llm_authoritative")
        if response_status == HOLD_LABEL:
            return (HOLD_LABEL, "허가서의 필요한 결과 근거를 추가 확인해야 합니다: " + clean_text(response.reason),
                    "permit_pdf_llm_authoritative", basis, trusted_result, "permit_pdf_llm_authoritative")
        if response_status == FAIL_LABEL:
            failed_text = " / ".join(clean_text(item) for item in failed if clean_text(item))
            permit_condition = basis if not failed_text else f"{basis} ({failed_text})"
            conflict = trusted_result
            reason = (
                f"허가서 기준 '{permit_condition}'을 충족하지 못했고, SP 시험결과 '{conflict}'와 충돌하여 "
                f"{FAIL_REASON_WORD}으로 판단했습니다."
            )
        else:
            reason = (
                f"허가서 기준 '{basis}'을 확인하고 시험결과 '{trusted_result}'가 이를 만족하여 "
                f"{PASS_REASON_WORD}으로 판단했습니다."
            )

        return (
            response_status,
            reason,
            "permit_pdf_llm_authoritative",
            llm_norm_criteria or norm_criteria,
            llm_norm_result,
            "permit_pdf_llm_authoritative",
        )

    def _judge_with_permit_second_phase(
        self,
        *,
        record: ExtractedRecord,
        title: str,
        permit_context: str,
        first_status: str | None,
        first_reason: str | None,
        comparator: str | None,
        norm_criteria: str | None,
        norm_result: str | None,
    ) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None]:
        """
        허가서 PDF를 반영하는 판정.

        정책:
        - 1차 규칙과 추가 판정은 건드리지 않는다.
        - 허가서 반영 판정은 앞 단계 후에도 검수보류인 항목에 대해서만 호출된다.
        - 허가서 시험명과 SP 시험명이 완전히 같지 않아도 LLM이 의미 매핑을 수행한다.
        - LLM이 허가서 후보 문단이 현재 시험과 관련 없다고 판단하거나 애매하면 검수보류를 유지한다.
        - LLM이 사용 불가능한 경우에만, exact anchor가 있는 문단에 한해 기존 규칙 판정을 제한적으로 사용한다.
        """
        if self.permit_policy is not None and self.permit_policy.authoritative:
            return self._judge_with_authoritative_permit(
                record=record,
                title=title,
                permit_context=permit_context,
                first_status=first_status,
                first_reason=first_reason,
                norm_criteria=norm_criteria,
                norm_result=norm_result,
            )

        if not permit_context:
            return None, None, None, None, None, None

        # LLM이 있으면 허가서 판정은 근거자료 중심으로 수행한다.
        # 이 방식은 허가서의 표현과 SP 시험명이 조금 달라도 의미 매핑이 가능하다.
        if self.llm_client is not None:
            query = " ".join(
                filter(
                    None,
                    [
                        record.test_name,
                        record.section_title,
                        record.criteria,
                        record.result,
                        record.method,
                        record.raw_text,
                        permit_context,
                    ],
                )
            )
            rag_contexts = [permit_context, *self._search_rag_contexts(query, top_k=8)]

            deterministic_reason = "\n".join(
                filter(
                    None,
                    [
                        "허가서 기준 확인 단계입니다.",
                        f"앞선 판정 결과: {first_status or '없음'}",
                        f"앞선 판정 이유: {first_reason or ''}",
                        "앞선 판정 로직은 이미 끝났으므로 수정하지 말고, 허가서 후보 문단과 관련 근거만 보고 보류 항목을 재평가하세요.",
                        "가장 먼저 현재 SP 시험명과 허가서 후보 문단의 시험명이 의미적으로 같은 항목인지 판단하세요.",
                        "같은 항목이 아니거나 관련성이 애매하면 반드시 검수보류로 유지하세요.",
                        "같은 항목이면 허가서 기준과 시험결과를 비교해 검수합격 또는 검수불합격을 판단하세요.",
                        "허가서에 복수 기준이 있으면 결과값을 단위와 의미에 맞춰 매핑하세요. 예: cells/mL 값은 세포농도, % 값은 생존률 기준과 비교합니다.",
                        "허가서 기준으로 판정한 경우 reason에는 실제 기준과 시험결과가 어떻게 맞거나 다른지 설명하세요.",
                        "normalized_criteria에는 '허가서 기준' 같은 포괄 표현이 아니라 실제 허가서 기준 문장을 짧게 쓰세요.",
                    ],
                )
            )

            llm_criteria = "\n".join(
                filter(
                    None,
                    [
                        "[SP 문서의 시험기준]",
                        clean_text(record.criteria),
                        "",
                        "[허가서 PDF 후보 기준/문단]",
                        permit_context,
                    ],
                )
            )

            llm_resp = self.llm_client.explain(
                test_name=title,
                criteria=llm_criteria,
                result=record.result,
                rag_contexts=rag_contexts,
                forced_status=None,
                deterministic_reason=deterministic_reason,
            )

            if llm_resp is not None and llm_resp.status:
                if llm_resp.status == PASS_LABEL:
                    status = PASS_LABEL
                elif llm_resp.status == FAIL_LABEL:
                    status = FAIL_LABEL
                else:
                    status = HOLD_LABEL

                reason = _sanitize_user_reason(llm_resp.reason)
                llm_norm_criteria = clean_text(llm_resp.normalized_criteria)
                llm_norm_result = clean_text(llm_resp.normalized_result)

                # LLM이 보류/애매함을 암시하면 허가서로 억지 판정하지 않고 보류 유지.
                if status == HOLD_LABEL or _llm_response_is_ambiguous(reason, llm_norm_criteria):
                    return (
                        HOLD_LABEL,
                        reason or "허가서 후보 문단과 현재 시험의 직접 연결이 명확하지 않아 검수보류로 유지했습니다.",
                        "permit_pdf_llm_hold",
                        llm_norm_criteria or norm_criteria or _short_permit_basis(permit_context),
                        llm_norm_result or norm_result,
                        "permit_pdf_llm_after",
                    )
                # 화면에 '허가서 기준'만 뜨는 문제 방지.
                if (not llm_norm_criteria) or _compact_semantic(llm_norm_criteria) in {"허가서기준", "허가서pdf기준", "기준"} or len(llm_norm_criteria) > 260:
                    llm_norm_criteria = _short_permit_basis(permit_context)

                return (
                    status,
                    reason,
                    "permit_pdf_llm_mapping",
                    llm_norm_criteria or norm_criteria,
                    llm_norm_result or norm_result or clean_text(record.result),
                    "permit_pdf_llm_after",
                )

        # LLM을 사용할 수 없는 환경에서는 broad candidate를 규칙으로 판정하면 오판 위험이 크다.
        # 따라서 현재 시험명/섹션명이 허가서 context에 정확히 들어있는 경우에만 기존 규칙을 제한적으로 사용한다.
        if not _permit_context_has_exact_anchor(record, permit_context):
            return None, None, None, None, None, None

        status, reason, permit_comparator, permit_norm_criteria, permit_norm_result = _judge_with_permit_pdf_numeric_requirements(
            permit_context=permit_context,
            result=record.result,
        )

        if status is not None:
            return (
                status,
                reason,
                permit_comparator,
                permit_norm_criteria,
                permit_norm_result,
                "permit_pdf_rule_after",
            )

        status, reason, permit_comparator, permit_norm_criteria, permit_norm_result = _judge_with_permit_pdf_text_requirements(
            permit_context=permit_context,
            result=record.result,
        )

        if status is not None:
            return (
                status,
                reason,
                permit_comparator,
                permit_norm_criteria,
                permit_norm_result,
                "permit_pdf_rule_after",
            )

        return None, None, None, None, None, None

    def _judge_single_value(
        self,
        *,
        record: ExtractedRecord,
        title: str,
        criteria: str | None,
        result: str | None,
        allow_permit: bool,
    ) -> tuple[str, str, str | None, str | None, str | None, str]:
        """단일 시험결과를 1차 규칙 판정 후 필요한 경우 근거자료로 추가 판정한다."""
        status, reason, comparator, norm_criteria, norm_result, source = self._judge_by_primary_rules(
            criteria=criteria,
            result=result,
        )

        # 추가 판정은 1차 규칙/JSON 판정이 확정하지 못한 보류 항목만 재평가한다.
        # 이미 PASS/FAIL로 닫힌 항목은 건드리지 않아 속도를 줄이고, 보류 항목은 강하게 의미 비교한다.
        should_run_llm = bool(
            (status is None or status == HOLD_LABEL)
            and clean_text(criteria)
            and clean_text(result)
            and self.llm_client is not None
        )

        if should_run_llm:
            rule_hint = "\n".join(
                filter(
                    None,
                    [
                        f"1차 규칙 판정: {status or '미확정'}",
                        f"1차 규칙 이유: {reason or ''}",
                        "추가 판정은 1차 규칙/JSON 판정에서 보류된 항목을 확정하는 단계입니다.",
                        "시험기준과 시험결과만으로 비교 가능하면 검수합격 또는 검수불합격으로 확정하고, 외부 기준이 필요한 경우에만 검수보류로 남기세요.",
                    ],
                )
            )
            llm_status, llm_reason, llm_comparator, llm_norm_criteria, llm_norm_result, llm_source = self._judge_with_llm_primary(
                record=record,
                title=title,
                criteria=criteria,
                result=result,
                deterministic_reason=rule_hint,
                norm_criteria=norm_criteria,
                norm_result=norm_result,
            )

            if llm_status is not None:
                if llm_status != HOLD_LABEL or status in (None, HOLD_LABEL):
                    status = llm_status
                    reason = llm_reason
                    comparator = llm_comparator or comparator
                    source = llm_source
                else:
                    # 추가 판정이 보류로 물러났지만 1차 규칙이 명확히 판정한 경우에는
                    # 보수적으로 1차 판정을 유지하고, 정규화 값만 보강한다.
                    source = f"{source}+gemini_hold"

                norm_criteria = llm_norm_criteria or norm_criteria
                norm_result = llm_norm_result or norm_result

        # 추가 판정까지 최종 판단이 안 된 경우 보류로 닫는다. 이후 허가서가 있으면 보정 가능하다.
        if status is None:
            status = HOLD_LABEL
            reason = "시험기준과 시험결과가 존재하지만 문서 기준과 참고 근거만으로 확정하기 어려워 검수보류로 판단했습니다."
            source = "manual_hold_before"

        # 권위 있는 허가서는 앞선 판정이 PASS/FAIL이어도 모든 항목을 재평가한다.
        # 비권위 허가서는 기존 호환 동작대로 HOLD 항목에만 적용한다.
        permit_is_authoritative = bool(self.permit_policy is not None and self.permit_policy.authoritative)
        if allow_permit and (permit_is_authoritative or status == HOLD_LABEL):
            permit_context = self._get_permit_context(record)
            permit_status, permit_reason, permit_comparator, permit_norm_criteria, permit_norm_result, permit_source = self._judge_with_permit_second_phase(
                record=record,
                title=title,
                permit_context=permit_context,
                first_status=status,
                first_reason=reason,
                comparator=comparator,
                norm_criteria=norm_criteria,
                norm_result=norm_result,
            )

            if permit_status is not None:
                status = permit_status
                reason = permit_reason or reason
                comparator = permit_comparator or comparator
                norm_criteria = permit_norm_criteria or norm_criteria
                norm_result = permit_norm_result or norm_result
                source = permit_source or source

        return (
            status,
            _sanitize_user_reason(reason),
            comparator,
            norm_criteria,
            norm_result or clean_text(result),
            source,
        )

    def judge_record_primary(self, record: ExtractedRecord) -> Evaluation:
        """Run the existing SP-only verdict without permit or provider effects.

        Deterministic permit grouping still needs the original row/table verdict
        for genuine permit misses and for UI lot-row evidence.  Temporarily
        detaching both external judgement sources keeps that baseline local and
        guarantees this helper cannot spend a provider call.
        """
        saved_store = self.permit_store
        saved_policy = self.permit_policy
        saved_catalog_valid = self.permit_catalog_valid
        saved_client = self.llm_client
        self.permit_store = None
        self.permit_policy = None
        self.permit_catalog_valid = None
        self.llm_client = None
        try:
            return JudgeEngine.judge_record(self, record)
        finally:
            self.permit_store = saved_store
            self.permit_policy = saved_policy
            self.permit_catalog_valid = saved_catalog_valid
            self.llm_client = saved_client

    def judge_record(self, record: ExtractedRecord, *, prepared_authoritative=None) -> Evaluation:
        token = _SEMANTIC_PREPARED.set((self, prepared_authoritative) if prepared_authoritative is not None else None)
        try:
            if prepared_authoritative is not None:
                self._active_semantic_response(record)
            return self._judge_record(record)
        finally:
            _SEMANTIC_PREPARED.reset(token)

    def _judge_record(self, record: ExtractedRecord) -> Evaluation:
        # A reused engine/record ID must not inherit a previous document result.
        self.comparison_bases.pop(record.order_idx, None)
        title = (
            clean_text(record.test_name)
            or clean_text(record.section_title)
            or (clean_text(record.raw_text).split("\n")[0] if clean_text(record.raw_text) else "")
            or "항목"
        )

        from .numeric_safety import result_is_requirement
        if result_is_requirement(record.criteria, record.result):
            return Evaluation(order_idx=record.order_idx, record_type=record.record_type,
                section_number=record.section_number, section_title=record.section_title, test_name=title,
                criteria=record.criteria, result=record.result, method=record.method,
                test_date=record.test_date, test_period=record.test_period,
                page_start=record.page_start, page_end=record.page_end, raw_text=record.raw_text or "",
                final_status=HOLD_LABEL, source="extraction_quality", comparison_completed=False,
                reason="시험결과란에 실제 관측 결과 대신 시험기준의 요구 문장이 반복되어 원문 및 실제 결과 확인이 필요합니다.")

        permit_is_authoritative = bool(self.permit_policy is not None and self.permit_policy.authoritative)
        allow_permit = bool(
            self.permit_store is not None
            and (self.permit_store.enabled or permit_is_authoritative)
        )
        lot_judgements: list[dict[str, str]] = []
        table_status: str | None = None

        table_rows = _parse_structured_result_table_from_record(record)

        if table_rows and "|" in clean_text(record.result):
            fallback_table_rows = _parse_result_table(record.result)
            if len(fallback_table_rows) > len(table_rows):
                table_rows = fallback_table_rows

        if not table_rows:
            table_rows = _parse_result_table(record.result)

        if not table_rows:
            table_rows = _parse_result_table(getattr(record, "raw_text", ""))

        if table_rows:
            table_status, table_reason, table_comparator, table_norm_criteria, table_norm_result = _judge_table_loq_count_requirement(
                record.criteria,
                table_rows,
            )

            if table_status is not None:
                for row in table_rows:
                    lot_judgements.append(
                        {
                            "item_label": row.get("item_label", "항목"),
                            "item_value": row.get("item_value", ""),
                            "lot_no": row.get("lot_no", ""),
                            "test_date": row.get("test_date", ""),
                            "result": row.get("result", "") or "",
                            "status": table_status,
                            "reason": _sanitize_user_reason(table_reason),
                            "normalized_criteria": table_norm_criteria or "",
                            "normalized_result": table_norm_result or "",
                            "comparator": table_comparator or "",
                            "source": "rule_before",
                            "display_columns": row.get("display_columns", []),
                            "display_values": row.get("display_values", {}),
                        }
                    )

            for row in table_rows:
                if table_status is not None:
                    continue

                row_result = row.get("result")

                status, reason, comparator, norm_criteria, norm_result, source = self._judge_single_value(
                    record=record,
                    title=title,
                    criteria=record.criteria,
                    result=row_result,
                    allow_permit=allow_permit,
                )

                lot_judgements.append(
                    {
                        "item_label": row.get("item_label", "항목"),
                        "item_value": row.get("item_value", ""),
                        "lot_no": row.get("lot_no", ""),
                        "test_date": row.get("test_date", ""),
                        "result": row_result or "",
                        "status": status or "",
                        "reason": _sanitize_user_reason(reason),
                        "normalized_criteria": norm_criteria or "",
                        "normalized_result": norm_result or "",
                        "comparator": comparator or "",
                        "source": source or "",
                        "display_columns": row.get("display_columns", []),
                        "display_values": row.get("display_values", {}),
                    }
                )

        final_status = None
        final_reason = ""
        comparator = None
        norm_criteria = None
        norm_result = None
        comparison_completed = False
        source = "rule_before"

        if lot_judgements:
            comparison_completed = True

            if any(x.get("status") == FAIL_LABEL for x in lot_judgements):
                final_status = FAIL_LABEL
            elif any(x.get("status") == HOLD_LABEL for x in lot_judgements):
                final_status = HOLD_LABEL
            else:
                final_status = PASS_LABEL

            final_reason = " ".join(
                f"{x.get('item_value') or x.get('lot_no') or x.get('item_label')}는 {x.get('reason')}"
                for x in lot_judgements
                if x.get("reason")
            )

            norm_criteria = next(
                (x.get("normalized_criteria") for x in lot_judgements if x.get("normalized_criteria")),
                None,
            )
            norm_result = next(
                (x.get("normalized_result") for x in lot_judgements if x.get("normalized_result")),
                None,
            )
            comparator = next(
                (x.get("comparator") for x in lot_judgements if x.get("comparator")),
                None,
            )
            source = next(
                (x.get("source") for x in lot_judgements if x.get("source")),
                "rule_before",
            )

            # A table-wide deterministic result is already aggregated above;
            # run one authoritative permit phase at that aggregate boundary.
            if table_status is not None and allow_permit and permit_is_authoritative:
                permit_context = self._get_permit_context(record)
                permit_status, permit_reason, permit_comparator, permit_norm_criteria, permit_norm_result, permit_source = self._judge_with_permit_second_phase(
                    record=record,
                    title=title,
                    permit_context=permit_context,
                    first_status=final_status,
                    first_reason=final_reason,
                    comparator=comparator,
                    norm_criteria=norm_criteria,
                    norm_result=norm_result,
                )
                if permit_status is not None:
                    final_status = permit_status
                    final_reason = permit_reason or final_reason
                    comparator = permit_comparator or comparator
                    norm_criteria = permit_norm_criteria or norm_criteria
                    norm_result = permit_norm_result or norm_result
                    source = permit_source or source
                    for row in lot_judgements:
                        row["status"] = permit_status
                        row["reason"] = _sanitize_user_reason(permit_reason or row.get("reason", ""))
                        row["source"] = permit_source or row.get("source", "")
                        if permit_norm_criteria:
                            row["normalized_criteria"] = permit_norm_criteria
                        if permit_norm_result:
                            row["normalized_result"] = permit_norm_result

        elif _looks_like_unparsed_table(record.result):
            comparison_completed = True
            final_status = HOLD_LABEL
            final_reason = "시험결과가 표 형태로 보이나 자동으로 행과 열을 확정하기 어려워 1차 판정에서 검수보류로 판단했습니다."
            source = "manual_hold_before"

            if allow_permit:
                permit_context = self._get_permit_context(record)
                permit_status, permit_reason, permit_comparator, permit_norm_criteria, permit_norm_result, permit_source = self._judge_with_permit_second_phase(
                    record=record,
                    title=title,
                    permit_context=permit_context,
                    first_status=final_status,
                    first_reason=final_reason,
                    comparator=comparator,
                    norm_criteria=norm_criteria,
                    norm_result=norm_result,
                )
                if permit_status is not None:
                    final_status = permit_status
                    final_reason = permit_reason or final_reason
                    comparator = permit_comparator or comparator
                    norm_criteria = permit_norm_criteria or norm_criteria
                    norm_result = permit_norm_result or norm_result
                    source = permit_source or source

        elif record.criteria and record.result:
            comparison_completed = True
            final_status, final_reason, comparator, norm_criteria, norm_result, source = self._judge_single_value(
                record=record,
                title=title,
                criteria=record.criteria,
                result=record.result,
                allow_permit=allow_permit,
            )

        elif record.result:
            status, text_reason = _status_from_text_result(record.result)

            if status is not None:
                comparison_completed = True
                final_status = status
                final_reason = text_reason or ""
                comparator = "text_result"
                norm_result = clean_text(record.result)
                source = "rule_before"
            else:
                comparison_completed = True
                final_status = HOLD_LABEL
                final_reason = "시험결과는 존재하지만 문서 기준과 참고 근거만으로 확정하기 어려워 검수보류로 판단했습니다."
                source = "manual_hold_before"

            if allow_permit and permit_is_authoritative:
                permit_context = self._get_permit_context(record)
                permit_status, permit_reason, permit_comparator, permit_norm_criteria, permit_norm_result, permit_source = self._judge_with_permit_second_phase(
                    record=record,
                    title=title,
                    permit_context=permit_context,
                    first_status=final_status,
                    first_reason=final_reason,
                    comparator=comparator,
                    norm_criteria=norm_criteria,
                    norm_result=norm_result,
                )
                if permit_status is not None:
                    final_status = permit_status
                    final_reason = permit_reason or final_reason
                    comparator = permit_comparator or comparator
                    norm_criteria = permit_norm_criteria or norm_criteria
                    norm_result = permit_norm_result or norm_result
                    source = permit_source or source

        elif record.criteria or record.result:
            comparison_completed = True
            final_status = HOLD_LABEL
            final_reason = "시험기준 또는 시험결과가 존재하지만 문서 기준과 참고 근거만으로 확정하기 어려워 검수보류로 판단했습니다."
            source = "manual_hold_before"
            if permit_is_authoritative and record.criteria and not record.result:
                # Without SP result evidence, do not ask the model to invent a
                # permit comparison; expose the authoritative safe-HOLD source.
                source = "permit_pdf_llm_authoritative"
                final_reason = "시험결과가 없어 허가서 기준과 비교할 수 없어 검수보류로 판단했습니다."

        return Evaluation(
            order_idx=record.order_idx,
            record_type=record.record_type,

            section_number=record.section_number,
            section_title=record.section_title,

            test_name=title,
            content_label=record.content_label,
            content=record.content,

            criteria=record.criteria,
            result=record.result,
            method=record.method,
            test_date=record.test_date,
            test_period=record.test_period,
            remarks=record.remarks,

            final_status=final_status,
            reason=_sanitize_user_reason(final_reason),

            page_start=record.page_start,
            page_end=record.page_end,

            normalized_criteria=norm_criteria,
            normalized_result=norm_result,
            comparator=comparator,

            source=source,
            raw_text=record.raw_text or "",
            comparison_completed=comparison_completed,
            lot_judgements=lot_judgements,
        )
