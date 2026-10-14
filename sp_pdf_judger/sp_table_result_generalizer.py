from __future__ import annotations

import html
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal


PASS_LABEL = "적합"
FAIL_LABEL = "부적합"
HOLD_LABEL = "보류"

TableKind = Literal["multi_test_result_table", "supporting_table", "plain_text"]


# ============================================================
# 0. 데이터 모델
# ============================================================

@dataclass
class ParsedTable:
    columns: list[str]
    rows: list[list[str]]
    kind: TableKind
    orientation: str = "normal"
    source: str = "unknown"
    confidence: float = 0.0


# ============================================================
# 1. 공통 정리 함수
# ============================================================

def clean_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("↵", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(" \n\t|")


def compact_text(value: Any) -> str:
    return re.sub(r"\s+", "", clean_cell(value)).casefold()


def split_pipe_line(line: str) -> list[str]:
    cells = [clean_cell(cell) for cell in clean_cell(line).split("|")]

    while cells and not cells[0]:
        cells.pop(0)

    while cells and not cells[-1]:
        cells.pop()

    return cells


def repair_table_text(text: str) -> str:
    """
    OCR/JSON에서 표 셀 내부 줄바꿈으로 깨진 대표 패턴을 복구한다.
    너무 공격적으로 합치면 일반 문장까지 망가지므로 표에서 자주 깨지는 표현만 보수적으로 처리한다.
    """
    text = text or ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("↵", "\n")

    replacements = [
        (r"Anti-HIV\s*\n\s*1/2", "Anti-HIV 1/2"),
        (r"핵산증폭검\s*\n\s*사", "핵산증폭검사"),
        (r"Cobas\s*\n\s*TaqScreen", "Cobas TaqScreen"),
        (r"사용된\s*\n\s*진단제제\s*\n\s*제조사", "사용된 진단제제 제조사"),
        (r"사용된\s*\n\s*진단제제명", "사용된 진단제제명"),
        (r"해당\s*\n\s*혈액원", "해당 혈액원"),
        (r"시험\s*\n\s*결과", "시험결과"),
        (r"시험\s*\n\s*방법", "시험방법"),
        (r"시험\s*\n\s*기준", "시험기준"),
        (r"온도차의\s*\n\s*합", "온도차의 합"),
        (r"온도차\s*\(\s*℃\s*\)", "온도차(℃)"),
        (r"주사량\s*\(\s*mL\s*\)", "주사량(mL)"),
        (r"체중\s*\(\s*kg\s*\)", "체중(kg)"),
    ]

    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)

    return text.strip()


# ============================================================
# 2. 표 파싱
# ============================================================

def normalize_table_dict(table: dict[str, Any]) -> ParsedTable | None:
    columns = table.get("columns") or table.get("headers") or table.get("header")
    rows = table.get("rows") or table.get("data")

    if not columns or not rows:
        return None

    norm_columns = [clean_cell(col) for col in columns]
    norm_rows: list[list[str]] = []

    for row in rows:
        if isinstance(row, dict):
            norm_rows.append([clean_cell(row.get(col, "")) for col in norm_columns])
        else:
            values = list(row) if isinstance(row, (list, tuple)) else [clean_cell(row)]
            values = [clean_cell(value) for value in values]

            if len(values) < len(norm_columns):
                values += [""] * (len(norm_columns) - len(values))

            norm_rows.append(values[: len(norm_columns)])

    parsed = ParsedTable(
        columns=norm_columns,
        rows=norm_rows,
        kind="supporting_table",
        orientation=str(table.get("orientation") or "normal"),
        source=str(table.get("source") or "json_table"),
        confidence=0.95,
    )
    parsed.kind = classify_table(parsed)
    return parsed


def parse_pipe_table_from_text(text: str) -> ParsedTable | None:
    text = repair_table_text(text)

    if "|" not in text:
        return None

    lines = [
        clean_cell(line)
        for line in text.splitlines()
        if clean_cell(line)
    ]

    # 주석/비고는 표 밖 문장으로 보는 경우가 많아서 제외
    table_candidate_lines = [
        line
        for line in lines
        if "|" in line and not clean_cell(line).startswith(("※", "*"))
    ]

    if not table_candidate_lines:
        return None

    # 첫 pipe line을 header로 본다.
    header = split_pipe_line(table_candidate_lines[0])

    if len(header) < 2:
        return None

    rows: list[list[str]] = []
    pending: list[str] | None = None
    target_len = len(header)

    for line in table_candidate_lines[1:]:
        cells = split_pipe_line(line)
        if not cells:
            continue

        if pending is None:
            if len(cells) >= target_len:
                rows.append(cells[:target_len])
            else:
                pending = cells
            continue

        # 이전 행이 줄바꿈으로 잘린 경우 이어붙인다.
        # 예:
        # Anti-HIV 1/2 | ELISA | 서울동부혈액원
        # 등 | HIV Ag/Ab 키트 | 음성
        if len(pending) < target_len:
            pending[-1] = clean_cell(f"{pending[-1]} {cells[0]}")
            pending.extend(cells[1:])

        if len(pending) >= target_len:
            rows.append(pending[:target_len])
            pending = None

    if pending:
        if len(pending) < target_len:
            pending += [""] * (target_len - len(pending))
        rows.append(pending[:target_len])

    if not rows:
        return None

    parsed = ParsedTable(
        columns=header,
        rows=rows,
        kind="supporting_table",
        orientation="normal",
        source="pipe_text",
        confidence=0.8,
    )
    parsed.kind = classify_table(parsed)
    return parsed


def extract_table_from_record(record: dict[str, Any]) -> ParsedTable | None:
    """
    record에서 표를 최대한 일반적으로 꺼낸다.

    우선순위:
    1. result_table
    2. tables[0]
    3. content/raw_text/normalized_text 안의 pipe table
    """
    result_table = record.get("result_table")
    if isinstance(result_table, dict):
        parsed = normalize_table_dict(result_table)
        if parsed:
            return parsed

    tables = record.get("tables") or []
    if isinstance(tables, list):
        for item in tables:
            if isinstance(item, dict):
                parsed = normalize_table_dict(item)
                if parsed:
                    return parsed

    text = record.get("content") or record.get("raw_text") or record.get("normalized_text") or ""
    return parse_pipe_table_from_text(str(text))


# ============================================================
# 3. 표 분류
# ============================================================

def find_column_index(columns: list[str], candidates: set[str]) -> int | None:
    normalized_candidates = {compact_text(item) for item in candidates}

    for idx, col in enumerate(columns):
        if compact_text(col) in normalized_candidates:
            return idx

    return None


def find_result_column(columns: list[str]) -> int | None:
    return find_column_index(columns, {"시험결과", "결과", "판정결과"})


def find_test_name_column(columns: list[str]) -> int | None:
    return find_column_index(columns, {"시험명", "항목", "검사항목", "시험항목"})


def find_batch_column(columns: list[str]) -> int | None:
    """Return only an exact primary batch column, never a qualified lot."""
    return find_column_index(columns, {"제조번호", "배치번호", "로트번호", "Lot No."})


def looks_transposed_result_table(table: ParsedTable) -> bool:
    """
    전치형 시험결과 표 감지.
    예:
    | Anti-HIV 1/2 | Anti-HCV | HBsAg ...
    시험방법 | 효소면역법 | ...
    시험결과 | 음성 | ...
    """
    if len(table.columns) < 3 or not table.rows:
        return False

    first_col_values = [clean_cell(row[0]) for row in table.rows if row]
    joined = " ".join(first_col_values)
    compact_joined = compact_text(joined)
    compact_columns = [compact_text(col) for col in table.columns]

    return (
        "시험결과" in compact_joined
        and ("시험방법" in compact_joined or "진단제제" in compact_joined or "제조사" in compact_joined)
        and "시험결과" not in compact_columns
    )


def classify_table(table: ParsedTable) -> TableKind:
    """
    표를 두 종류로 나눈다.

    1. multi_test_result_table
       - 표의 각 행/컬럼이 개별 시험이다.
       - UI에서 각 행 오른쪽에 신호등을 붙이고, 판정 record로 확장 가능하다.
       - 예: 바이러스부정시험 표

    2. supporting_table
       - 하나의 시험에 딸린 보조 표다.
       - UI에는 표만 보여주고, 신호등은 기존 시험결과 라인에만 표시해야 한다.
       - 예: 시험동물 온도차 표, 희석배수-시간 표, 원료목록 표
    """
    result_idx = find_result_column(table.columns)
    test_idx = find_test_name_column(table.columns)

    if result_idx is not None and test_idx is not None:
        return "multi_test_result_table"

    if looks_transposed_result_table(table):
        return "multi_test_result_table"

    return "supporting_table"


def _clean_transposed_cell(value: Any) -> str:
    text = clean_cell(value)
    text = re.sub(r"Anti-HIV\s*\n\s*1/2", "Anti-HIV 1/2", text, flags=re.I)
    text = re.sub(r"Cobas\s*\n\s*TaqScreen", "Cobas TaqScreen", text, flags=re.I)
    text = text.replace("핵산증폭검\n사", "핵산증폭검사")
    text = text.replace("사용된\n진단제제\n제조사", "사용된 진단제제 제조사")
    text = text.replace("사용된\n진단제제명", "사용된 진단제제명")
    return text


def _looks_like_generic_table_columns(columns: list[str]) -> bool:
    if not columns:
        return False
    generic_count = sum(1 for col in columns if re.fullmatch(r"열\s*\d+", clean_cell(col)))
    return generic_count >= max(2, len(columns) // 2)


def _looks_like_transposed_test_header(row: list[str]) -> bool:
    if len(row) < 3:
        return False
    joined = " ".join(clean_cell(cell) for cell in row[1:] if clean_cell(cell))
    compact = compact_text(joined)
    tokens = ["anti-hiv", "anti-hcv", "hbsag", "hivrna", "hcvrna", "hbvdna"]
    return sum(1 for token in tokens if token in compact) >= 2


def transpose_result_table_to_rowwise(table: ParsedTable) -> ParsedTable:
    """
    전치된 결과표를 행 기준 시험표로 바꾼다.
    """
    original_columns = table.columns
    original_rows = table.rows

    if (
        _looks_like_generic_table_columns(original_columns)
        and original_rows
        and _looks_like_transposed_test_header(original_rows[0])
    ):
        header_row = [_clean_transposed_cell(v) for v in original_rows[0]]
        test_names = header_row[1:]
        value_offset = 1
        data_rows = original_rows[1:]
    elif original_columns and compact_text(original_columns[0]) in {"", "항목", "구분"}:
        test_names = original_columns[1:]
        value_offset = 1
        data_rows = original_rows
    else:
        test_names = original_columns
        value_offset = 0
        data_rows = original_rows

    row_map: dict[str, list[str]] = {}

    for row in data_rows:
        if not row:
            continue

        label = _clean_transposed_cell(row[0])
        if not label:
            continue

        row_map[label] = [_clean_transposed_cell(v) for v in row[value_offset:]]

    field_labels = list(row_map.keys())
    output_columns = ["시험명"] + field_labels
    output_rows: list[list[str]] = []

    for idx, test_name in enumerate(test_names):
        test_name = _clean_transposed_cell(test_name)
        if not test_name:
            continue

        output_row = [test_name]
        for label in field_labels:
            values = row_map.get(label, [])
            output_row.append(values[idx] if idx < len(values) else "")
        output_rows.append(output_row)

    parsed = ParsedTable(
        columns=output_columns,
        rows=output_rows,
        kind="multi_test_result_table",
        orientation="rowwise_from_transposed",
        source=table.source,
        confidence=min(table.confidence, 0.85),
    )
    return parsed
def normalize_for_display(table: ParsedTable) -> ParsedTable:
    if table.kind == "multi_test_result_table" and looks_transposed_result_table(table):
        return transpose_result_table_to_rowwise(table)
    return table


# ============================================================
# 4. 판정 상태
# ============================================================

def has_explicit_criteria(record: dict[str, Any]) -> bool:
    criteria = clean_cell(record.get("criteria"))
    if criteria:
        return True

    text = clean_cell(record.get("content") or record.get("raw_text") or "")
    return "시험기준" in text or "기준" in text


def is_permit_dependent_record(record: dict[str, Any]) -> bool:
    text = " ".join(
        clean_cell(record.get(key))
        for key in [
            "section_title",
            "content_label",
            "test_name",
            "criteria",
            "content",
            "raw_text",
        ]
    )
    compact = compact_text(text)

    permit_tokens = [
        "허가서",
        "허가사항",
        "품목허가",
        "자사기준",
        "자사의",
        "기준에따름",
        "기준에따라",
        "별도기준",
    ]

    return any(token in compact for token in permit_tokens)


def can_infer_negative_required(record: dict[str, Any], test_name: str, result: str) -> bool:
    """
    보수적 판정 원칙.

    이전 버전:
    - 바이러스부정시험 + 음성 결과이면 기준을 "음성이어야 함"으로 추론했다.

    수정 버전:
    - 명시적인 시험기준, 적합 문구, 수치 비교 기준, 또는 허가서 기준이 없는 경우에는
      시험명/결과만 보고 기준을 추론하지 않는다.
    - 따라서 "바이러스부정시험 + 음성"처럼 의미상 맞아 보이는 경우도
      허가서나 명시 기준이 없으면 검수보류로 남긴다.
    """
    return False


def status_from_result_text(
    result: str,
    *,
    record: dict[str, Any] | None = None,
    has_permit: bool = False,
    strict_no_criteria_without_permit: bool = True,
    test_name: str = "",
) -> str:
    """
    UI 신호등용 fallback 판정.

    - 기존 판정 엔진 결과가 있으면 그 결과를 우선 쓰는 게 맞다.
    - 이 함수는 표 UI에서 아직 판정 결과가 없을 때만 쓰는 보조 로직이다.
    """
    record = record or {}

    # 허가서 의존 기준인데 허가서가 없으면 보류
    if is_permit_dependent_record(record) and not has_permit:
        return HOLD_LABEL

    # 명시 기준이 없고 허가서도 없는 경우
    if strict_no_criteria_without_permit and not has_permit and not has_explicit_criteria(record):
        if not can_infer_negative_required(record, test_name, result):
            return HOLD_LABEL

    value = clean_cell(result)
    norm = compact_text(value)

    if not norm:
        return HOLD_LABEL

    fail_tokens = [
        "부적합", "불합격", "fail", "failed",
        "positive", "pos", "양성", "검출", "관찰됨",
        "확인됨", "기준미달", "초과",
    ]
    hold_tokens = [
        "보류", "판단불가", "확인필요",
        "해당없음", "해당없 음", "n/a", "na", "none", "-",
    ]
    pass_tokens = [
        "적합", "합격", "pass", "passed",
        "negative", "neg", "음성", "미검출",
        "불검출", "관찰되지않음", "없음",
    ]

    if any(token in norm for token in fail_tokens):
        return FAIL_LABEL
    if norm in hold_tokens or any(token == norm for token in hold_tokens):
        return HOLD_LABEL
    if any(token in norm for token in pass_tokens):
        return PASS_LABEL

    return HOLD_LABEL


def status_class(status: str) -> str:
    if status == PASS_LABEL:
        return "pass"
    if status == FAIL_LABEL:
        return "fail"
    return "hold"


def signal_dot(status: str) -> str:
    cls = status_class(status)
    label = html.escape(status)
    return f'<span class="sp-signal-dot {cls}" title="{label}" aria-label="{label}"></span>'


# ============================================================
# 5. UI HTML 렌더링
# ============================================================

TABLE_CSS = """
<style>
.sp-table-wrap {
    width: 100%;
    margin: 14px 0 10px 0;
    overflow-x: auto;
    border: 1px solid #dbe3ee;
    border-radius: 14px;
    background: #ffffff;
}

.sp-table {
    width: 100%;
    border-collapse: collapse;
    table-layout: auto;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    color: #111827;
    background: #ffffff;
}

.sp-table th {
    background: #f8fafc;
    color: #111827;
    font-weight: 900;
    font-size: 15px;
    line-height: 1.35;
    text-align: left;
    padding: 13px 16px;
    border-bottom: 1px solid #dbe3ee;
    white-space: nowrap;
}

.sp-table td {
    color: #111827;
    font-size: 15px;
    line-height: 1.45;
    padding: 13px 16px;
    border-bottom: 1px solid #e5e7eb;
    border-right: 1px solid #eef2f7;
    vertical-align: middle;
    white-space: pre-line;
}

.sp-table tr:last-child td {
    border-bottom: none;
}

.sp-table td:last-child,
.sp-table th:last-child {
    border-right: none;
}

.sp-signal-col {
    width: 76px;
    min-width: 76px;
    text-align: center !important;
}

.sp-signal-dot {
    display: inline-block;
    width: 22px;
    height: 22px;
    border-radius: 999px;
    box-shadow: 0 0 0 5px rgba(15, 23, 42, 0.06);
    vertical-align: middle;
}

.sp-signal-dot.pass {
    background: #22c55e;
}

.sp-signal-dot.fail {
    background: #ef4444;
}

.sp-signal-dot.hold {
    background: #f59e0b;
}
</style>
""".strip()


def render_table_html(
    table: ParsedTable,
    *,
    record: dict[str, Any] | None = None,
    has_permit: bool = False,
    strict_no_criteria_without_permit: bool = True,
) -> str:
    """
    표를 HTML로 렌더링한다.

    - multi_test_result_table:
      각 행이 개별 시험이므로 오른쪽에 판정 신호등 컬럼을 붙인다.

    - supporting_table:
      하나의 시험에 딸린 보조 표이므로 표에는 신호등을 붙이지 않는다.
      신호등은 기존 시험결과 라인에서 표시한다.
    """
    record = record or {}
    table = normalize_for_display(table)

    columns = table.columns
    rows = table.rows

    if not columns or not rows:
        return ""

    result_idx = find_result_column(columns)
    test_idx = find_test_name_column(columns)
    add_signal_col = table.kind == "multi_test_result_table" and result_idx is not None

    # Pipe-text tables are inferred from plain text and are intentionally conservative.
    # If the table cannot be tied to clear test/result columns, showing it as a grid is
    # more misleading than helpful because document layouts vary heavily.
    if table.source == "pipe_text" and not add_signal_col:
        return _table_parse_notice_html(
            "표처럼 보이는 텍스트가 발견되었지만 열 구조를 안정적으로 확정하지 못해 표 UI로 표시하지 않았습니다."
        )

    if add_signal_col:
        for row in rows:
            values = [clean_cell(v) for v in row]
            if len(values) < len(columns):
                values += [""] * (len(columns) - len(values))
            test_name = values[test_idx] if test_idx is not None and test_idx < len(values) else ""
            result_text = values[result_idx] if result_idx < len(values) else ""
            if not _expanded_table_row_is_plausible(
                columns=columns,
                values=values,
                test_idx=test_idx if test_idx is not None else -1,
                result_idx=result_idx,
                test_name=test_name,
                result=result_text,
            ):
                return _table_parse_notice_html(
                    "시험명/시험결과 열이 서로 밀린 것으로 보여 표 UI와 자동 판정에 반영하지 않았습니다."
                )

    parts: list[str] = [TABLE_CSS]
    parts.append('<div class="sp-table-wrap">')
    parts.append('<table class="sp-table">')

    parts.append("<thead><tr>")
    for col in columns:
        parts.append(f"<th>{html.escape(col)}</th>")
    if add_signal_col:
        parts.append('<th class="sp-signal-col">판정</th>')
    parts.append("</tr></thead>")

    parts.append("<tbody>")
    for row in rows:
        values = [clean_cell(v) for v in row]
        if len(values) < len(columns):
            values += [""] * (len(columns) - len(values))

        parts.append("<tr>")
        for value in values[: len(columns)]:
            parts.append(f"<td>{html.escape(value)}</td>")

        if add_signal_col:
            result_text = values[result_idx] if result_idx < len(values) else ""
            test_name = values[test_idx] if test_idx is not None and test_idx < len(values) else ""
            status = status_from_result_text(
                result_text,
                record=record,
                has_permit=has_permit,
                strict_no_criteria_without_permit=strict_no_criteria_without_permit,
                test_name=test_name,
            )
            parts.append(f'<td class="sp-signal-col">{signal_dot(status)}</td>')

        parts.append("</tr>")

    parts.append("</tbody>")
    parts.append("</table>")
    parts.append("</div>")

    return "\n".join(parts)


def _table_parse_notice_html(message: str) -> str:
    return (
        '<div class="detail-text" '
        'style="padding:14px 16px;border:1px solid #f1c27d;border-radius:12px;'
        'background:#fff8ed;color:#7a4b00;font-weight:800;line-height:1.65;">'
        f'{html.escape(message)}'
        '</div>'
    )


def render_record_table_html(
    record: dict[str, Any],
    *,
    has_permit: bool = False,
    strict_no_criteria_without_permit: bool = True,
) -> str | None:
    table = extract_table_from_record(record)
    if not table:
        return None

    return render_table_html(
        table,
        record=record,
        has_permit=has_permit,
        strict_no_criteria_without_permit=strict_no_criteria_without_permit,
    )


# ============================================================
# 6. 판정 엔진용 record 확장
# ============================================================

def infer_criteria_for_table_test(
    *,
    parent_record: dict[str, Any],
    test_name: str,
    result: str,
    has_permit: bool,
    strict_no_criteria_without_permit: bool,
) -> str | None:
    parent_criteria = clean_cell(parent_record.get("criteria"))
    if parent_criteria:
        return parent_criteria

    if is_permit_dependent_record(parent_record) and not has_permit:
        return "허가서 기준 확인 필요"

    if strict_no_criteria_without_permit and not has_permit:
        if can_infer_negative_required(parent_record, test_name, result):
            return "음성이어야 함"
        return "허가서 기준 확인 필요"

    if can_infer_negative_required(parent_record, test_name, result):
        return "음성이어야 함"

    # 허가서가 있거나 LLM 2차 판정을 기대하는 경우
    return parent_criteria or None


def _is_bad_table_fragment(value: str) -> bool:
    key = compact_text(value)
    return key in {
        "",
        "등",
        "및",
        "또는",
        "해당",
        "기타",
        "elisa",
        "nat",
        "pcr",
        "neg",
        "negative",
        "음성",
        "양성",
    }


def _is_method_like(value: str) -> bool:
    key = compact_text(value)
    return key in {
        "elisa",
        "nat",
        "pcr",
        "qpcr",
        "rtpcr",
        "westernblot",
        "dotblot",
    }


def _expanded_table_row_is_plausible(
    *,
    columns: list[str],
    values: list[str],
    test_idx: int,
    result_idx: int,
    test_name: str,
    result: str,
) -> bool:
    test_key = compact_text(test_name)
    result_key = compact_text(result)

    if not test_key or not result_key:
        return False

    if _is_bad_table_fragment(test_name):
        return False

    if test_key == result_key:
        return False

    method = ""
    for idx, col in enumerate(columns):
        if compact_text(col) in {"시험방법", "방법"} and idx < len(values):
            method = values[idx]
            break

    if method and compact_text(method) == result_key:
        return False

    if _is_method_like(result) and result_idx != test_idx:
        return False

    return True


def _table_parse_warning_record(record: dict[str, Any], reason: str) -> dict[str, Any]:
    warning = deepcopy(record)
    warning["record_type"] = "content"
    warning["test_name"] = None
    warning["content_label"] = "표 자동 판정 제외"
    warning["content"] = reason
    warning["criteria"] = None
    warning["result"] = None
    warning["method"] = None
    warning["remarks"] = None
    warning["raw_text"] = reason
    warning["normalized_text"] = reason
    warning["result_table"] = None
    warning["tables"] = []
    warning["source_types"] = list(set((record.get("source_types") or []) + ["table_parse_warning"]))
    return warning


def expand_table_record_to_test_records(
    record: dict[str, Any],
    *,
    has_permit: bool = False,
    strict_no_criteria_without_permit: bool = True,
) -> list[dict[str, Any]]:
    """
    multi_test_result_table만 개별 시험으로 확장한다.

    supporting_table은 하나의 시험에 딸린 보조 표이므로 절대 쪼개지 않는다.
    예: 시험동물 온도차 표
    """
    table = extract_table_from_record(record)
    if not table:
        return [record]

    table = normalize_for_display(table)

    if table.kind != "multi_test_result_table":
        return [record]

    columns = table.columns
    rows = table.rows
    result_idx = find_result_column(columns)
    test_idx = find_test_name_column(columns)
    batch_idx = find_batch_column(columns)

    if result_idx is None or test_idx is None:
        return [record]

    expanded: list[dict[str, Any]] = []
    base_order = record.get("order_idx") or record.get("order_index") or 0
    try:
        base_order_num = float(base_order)
    except Exception:
        base_order_num = 0.0

    for row_index, row in enumerate(rows, start=1):
        values = [clean_cell(v) for v in row]
        if len(values) < len(columns):
            values += [""] * (len(columns) - len(values))

        test_name = values[test_idx]
        result = values[result_idx]

        if not test_name or not result:
            continue

        if not _expanded_table_row_is_plausible(
            columns=columns,
            values=values,
            test_idx=test_idx,
            result_idx=result_idx,
            test_name=test_name,
            result=result,
        ):
            section = clean_cell(record.get("section_number") or record.get("section_title") or "")
            return [
                _table_parse_warning_record(
                    record,
                    "표의 열 구조가 안정적으로 해석되지 않아 자동 판정에서 제외했습니다."
                    + (f" 위치: {section}." if section else "")
                    + f" 확인이 필요한 행: 시험명='{test_name}', 시험결과='{result}'.",
                )
            ]

        method = ""
        for idx, col in enumerate(columns):
            if compact_text(col) in {"시험방법", "방법"} and idx < len(values):
                method = values[idx]
                break

        remarks: list[str] = []
        for idx, col in enumerate(columns):
            cn = compact_text(col)
            if any(token in cn for token in ["혈액원", "진단제제", "제조사"]):
                if idx < len(values) and values[idx]:
                    remarks.append(f"{col}: {values[idx]}")

        criteria = infer_criteria_for_table_test(
            parent_record=record,
            test_name=test_name,
            result=result,
            has_permit=has_permit,
            strict_no_criteria_without_permit=strict_no_criteria_without_permit,
        )

        new_record = deepcopy(record)
        new_record["record_type"] = "test"
        new_record["test_name"] = test_name
        new_record["content_label"] = test_name
        new_record["content"] = None
        new_record["method"] = method or None
        new_record["criteria"] = criteria
        new_record["result"] = result
        new_record["remarks"] = " / ".join(remarks) if remarks else record.get("remarks")
        if batch_idx is not None:
            batch_value = values[batch_idx] if batch_idx < len(values) else ""
            batch_value = clean_cell(batch_value)
            new_record["batch_id"] = batch_value or None
            new_record["batch_evidence"] = (
                {
                    "source_kind": "explicit_table_column",
                    "label": columns[batch_idx],
                    "quote": f"{columns[batch_idx]}: {batch_value}",
                    "row_index": row_index,
                }
                if batch_value
                else None
            )
        new_record["raw_text"] = "\n".join(
            part
            for part in [
                f"시험명: {test_name}",
                f"시험방법: {method}" if method else "",
                f"시험기준: {criteria}" if criteria else "",
                f"시험결과: {result}",
            ]
            if part
        )
        new_record["normalized_text"] = new_record["raw_text"]
        new_record["parent_test_group"] = record.get("section_title") or record.get("content_label")
        new_record["subtest_order"] = row_index
        new_record["order_idx"] = base_order_num + (row_index / 10000.0)
        new_record["order_index"] = base_order_num + (row_index / 10000.0)
        new_record["source_types"] = list(set((record.get("source_types") or []) + ["expanded_from_table_block"]))
        new_record["result_table"] = None
        new_record["tables"] = []
        expanded.append(new_record)

    return expanded or [record]


def expand_table_records_for_judgement(
    records: list[dict[str, Any]],
    *,
    has_permit: bool = False,
    strict_no_criteria_without_permit: bool = True,
    keep_original_table_record: bool = True,
) -> list[dict[str, Any]]:
    """
    판정 엔진에 넣기 전 records 리스트에 적용한다.

    - multi_test_result_table:
      원본 표 record를 유지하고, 뒤에 개별 시험 record를 추가한다.
      원본 표 record가 중복 판정되지 않도록 record_type을 content로 유지한다.

    - supporting_table:
      그대로 둔다. 기존 시험결과 라인에서 판정받게 한다.
    """
    output: list[dict[str, Any]] = []

    for record in records:
        table = extract_table_from_record(record)

        if not table:
            output.append(record)
            continue

        expanded = expand_table_record_to_test_records(
            record,
            has_permit=has_permit,
            strict_no_criteria_without_permit=strict_no_criteria_without_permit,
        )

        if expanded == [record]:
            output.append(record)
            continue

        if keep_original_table_record:
            original = deepcopy(record)
            original["record_type"] = original.get("record_type") or "content"
            output.append(original)

        output.extend(expanded)

    return output


# ============================================================
# 7. 기존 시험 카드 안에서 사용하는 helper
# ============================================================

def should_render_content_as_table(record: dict[str, Any]) -> bool:
    return extract_table_from_record(record) is not None


def render_content_or_table_html(
    record: dict[str, Any],
    *,
    has_permit: bool = False,
    strict_no_criteria_without_permit: bool = True,
    text_css_class: str = "detail-text",
) -> str:
    """
    기존 UI에서 content 출력부를 이 함수로 대체하면 된다.

    - 표면 표 HTML
    - 표가 아니면 기존 텍스트
    """
    table_html = render_record_table_html(
        record,
        has_permit=has_permit,
        strict_no_criteria_without_permit=strict_no_criteria_without_permit,
    )

    if table_html:
        return table_html

    content = record.get("content") or record.get("raw_text") or ""
    if not content:
        return ""

    return f'<div class="{html.escape(text_css_class)}">{html.escape(str(content))}</div>'
