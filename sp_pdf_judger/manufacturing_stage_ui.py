from __future__ import annotations

import base64
import csv
import hashlib
import html
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz

from .manufacturing_info_validator import (
    ManufacturingInfoValidationResult,
    FieldCompareResult,
    TestDateCompareResult,
    _norm_match,
    render_manufacturing_info_validation_card,
    validate_manufacturing_info_consistency,
)
from .schemas import ProcessingResult, Summary
from .config import PASS_LABEL, FAIL_LABEL, HOLD_LABEL
from .utils import clean_text


@dataclass
class StageTestCard:
    display_name: str
    x_pct: float
    y_pct: float
    side: str
    passed: int
    failed: int
    held: int
    total: int
    manufacturing_issues: int = 0
    manufacturing_failures: int = 0
    manufacturing_holds: int = 0
    validation_target: str = ""


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _stage_anchor_key(stage_name: str) -> str:
    normalized = _norm_match(stage_name) or clean_text(stage_name)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def _to_int_count(value: Any) -> int:
    try:
        return int(str(value).replace(",", "").strip() or "0")
    except Exception:
        return 0


def _stage_summary_exact_key(stage_name: str | None) -> str:
    """CSV 제조명과 제조요약도 박스명을 추가 별칭 없이 비교하기 위한 키.

    공백/구분기호/대소문자 차이만 제거한다.
    CHO↔CHOL, E.coli↔E.colli, 나노파티클↔나노피티클 같은
    명칭 차이는 의도된 표기일 수 있으므로 여기서 서로 매칭하지 않는다.
    """
    text = clean_text(stage_name)

    if not text:
        return ""

    text = text.casefold()
    text = re.sub(r"[\s\-_·.,:：/(){}\[\]<>]+", "", text)

    return text


def _read_stage_summary_counts(summary_path: Path | str | None) -> dict[str, dict[str, Any]]:
    """summary_after.csv를 제조요약도 카드용 단일 집계 출처로 읽는다."""
    if not summary_path:
        return {}

    path = Path(summary_path)

    if not path.exists():
        return {}

    total_markers = {"전체", "총계", "합계", "total", "overall"}
    out: dict[str, dict[str, Any]] = {}

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
    except Exception:
        return {}

    for row in rows[1:]:
        if not row or not any(str(cell).strip() for cell in row):
            continue

        padded = row + ["0"] * 5
        name = clean_text(padded[0])

        if not name or name.casefold() in total_markers:
            continue

        key = _stage_summary_exact_key(name)

        if not key:
            continue

        if key not in out:
            out[key] = {
                "stage_name": name,
                "passed": 0,
                "failed": 0,
                "held": 0,
                "total": 0,
            }

        out[key]["passed"] += _to_int_count(padded[1])
        out[key]["failed"] += _to_int_count(padded[2])
        out[key]["held"] += _to_int_count(padded[3])
        out[key]["total"] += _to_int_count(padded[4])

    return out


def _group_words_to_lines(words: list[tuple]) -> list[dict]:
    if not words:
        return []

    words = sorted(words, key=lambda w: (round(float(w[1]), 1), float(w[0])))

    lines: list[list[tuple]] = []
    current: list[tuple] = []
    current_y: float | None = None

    for word in words:
        y0 = float(word[1])

        if current_y is None or abs(y0 - current_y) <= 4.0:
            current.append(word)
            current_y = y0 if current_y is None else min(current_y, y0)
        else:
            lines.append(current)
            current = [word]
            current_y = y0

    if current:
        lines.append(current)

    out: list[dict] = []

    for line_words in lines:
        line_words = sorted(line_words, key=lambda w: float(w[0]))

        text = clean_text(
            " ".join(
                clean_text(w[4])
                for w in line_words
                if clean_text(w[4])
            )
        )

        if not text:
            continue

        x0 = min(float(w[0]) for w in line_words)
        y0 = min(float(w[1]) for w in line_words)
        x1 = max(float(w[2]) for w in line_words)
        y1 = max(float(w[3]) for w in line_words)

        out.append(
            {
                "text": text,
                "words": line_words,
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "x_center": (x0 + x1) / 2,
                "y_center": (y0 + y1) / 2,
            }
        )

    return out


def _looks_like_stage_name(label: str | None) -> bool:
    label = clean_text(label)
    compact = _norm_match(label)

    if not compact:
        return False

    blocked_exact = {
        "componenta",
        "componentb",
        "componentacomponentb",
        "제조번호",
        "제조년월일",
        "제조량",
        "제조수량",
        "총제조수량",
    }

    if compact in blocked_exact:
        return False

    blocked_contains = [
        "페이지",
        "제조요약도",
        "summaryprotocol",
        "production",
        "qualitycontrol",
        "japaneseencephalitis",
        "vaccine",
        "inactivated",
        "동국약품",
        "주식회사",
        "제조번호",
        "제조년월일",
        "제조량",
        "제조수량",
        "qa팀",
    ]

    if any(x in compact for x in blocked_contains):
        return False

    stage_words = [
        "세포주",
        "원액",
        "완제",
        "의약품",
        "바이알",
        "중간체",
        "나노",
        "componenta",
        "componentb",
        "ecoli",
        "cho",
        "chol",
    ]

    return any(word in compact for word in stage_words)


def _next_lines_contain_manufacturing_fields(
    lines: list[dict],
    idx: int,
    window: int = 5,
) -> bool:
    for next_line in lines[idx + 1 : idx + 1 + window]:
        text = clean_text(next_line.get("text", ""))

        if any(
            field in text
            for field in ["제조번호", "제조년월일", "제조량", "제조수량"]
        ):
            return True

    return False


def _split_line_by_midpoint_with_position(
    line: dict,
    page_width: float,
) -> list[tuple[str, float, float]]:
    words = line.get("words", [])

    if not words:
        return [
            (
                clean_text(line.get("text", "")),
                float(line.get("x_center", 0)),
                float(line.get("y_center", 0)),
            )
        ]

    mid = page_width / 2

    left_words: list[tuple] = []
    right_words: list[tuple] = []

    for word in words:
        x_center = (float(word[0]) + float(word[2])) / 2

        if x_center < mid:
            left_words.append(word)
        else:
            right_words.append(word)

    def build_part(part_words: list[tuple]) -> tuple[str, float, float] | None:
        if not part_words:
            return None

        part_words = sorted(part_words, key=lambda x: float(x[0]))

        text = clean_text(
            " ".join(
                clean_text(w[4])
                for w in part_words
                if clean_text(w[4])
            )
        )

        if not text:
            return None

        x0 = min(float(w[0]) for w in part_words)
        x1 = max(float(w[2]) for w in part_words)
        y0 = min(float(w[1]) for w in part_words)
        y1 = max(float(w[3]) for w in part_words)

        return text, (x0 + x1) / 2, (y0 + y1) / 2

    left_part = build_part(left_words)
    right_part = build_part(right_words)

    if left_part and right_part:
        if _looks_like_stage_name(left_part[0]) and _looks_like_stage_name(right_part[0]):
            return [left_part, right_part]

    return [
        (
            clean_text(line.get("text", "")),
            float(line.get("x_center", 0)),
            float(line.get("y_center", 0)),
        )
    ]


def _stage_side_from_x(x_pct: float) -> str:
    return "left" if x_pct < 44 else "right"


def _extract_stage_box_positions_from_pdf(pdf_path: Path | None) -> list[dict]:
    if pdf_path is None or not Path(pdf_path).exists():
        return []

    out: list[dict] = []
    seen: set[str] = set()

    with fitz.open(pdf_path) as doc:
        for page in doc:
            page_text = clean_text(page.get_text("text") or "")

            if "제조요약도" not in "".join(page_text.split()):
                continue

            words = page.get_text("words") or []
            lines = _group_words_to_lines(words)

            if not lines:
                continue

            page_width = float(page.rect.width)
            page_height = float(page.rect.height)

            start_idx = 0

            for idx, line in enumerate(lines):
                if "제조요약도" in clean_text(line.get("text", "")):
                    start_idx = idx + 1
                    break

            for idx in range(start_idx, len(lines)):
                line = lines[idx]

                if not clean_text(line.get("text", "")):
                    continue

                if not _next_lines_contain_manufacturing_fields(lines, idx):
                    continue

                candidates = _split_line_by_midpoint_with_position(
                    line=line,
                    page_width=page_width,
                )

                for name, x_center, y_center in candidates:
                    name = clean_text(name)

                    if not _looks_like_stage_name(name):
                        continue

                    key = _norm_match(name)

                    if key in seen:
                        continue

                    seen.add(key)

                    x_pct = max(0.0, min(100.0, x_center / page_width * 100))
                    y_pct = max(0.0, min(100.0, y_center / page_height * 100))

                    out.append(
                        {
                            "display_name": name,
                            "x_pct": x_pct,
                            "y_pct": y_pct,
                            "side": _stage_side_from_x(x_pct),
                        }
                    )

    return out


def _image_to_base64(image_path: Path) -> str:
    return base64.b64encode(image_path.read_bytes()).decode("utf-8")


def _pdf_render_cache_dir(pdf_path: Path) -> Path:
    try:
        stat = pdf_path.stat()
        source = f"{pdf_path.resolve()}::{stat.st_size}::{stat.st_mtime_ns}"
    except Exception:
        source = str(pdf_path)
    digest = hashlib.sha1(source.encode("utf-8", "ignore")).hexdigest()[:16]
    cache_dir = Path(tempfile.gettempdir()) / "sp_albumin_source_pages" / digest
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _render_pdf_page_to_cache(pdf_path: Path, page_number: int, tag: str) -> Path | None:
    try:
        cache_dir = _pdf_render_cache_dir(pdf_path)
        out_path = cache_dir / f"{tag}_page_{page_number}.png"
        if out_path.exists() and out_path.stat().st_size > 0:
            return out_path
        with fitz.open(pdf_path) as doc:
            if page_number < 1 or page_number > len(doc):
                return None
            page = doc[page_number - 1]
            pix = page.get_pixmap(matrix=fitz.Matrix(1.65, 1.65), alpha=False)
            pix.save(str(out_path))
        return out_path
    except Exception:
        return None


def _albumin_source_material_page_numbers(result: ProcessingResult) -> list[int]:
    if not _is_albumin_result(result):
        return []
    pages: set[int] = set()
    for record in list(getattr(result, "extracted_records", []) or []):
        if clean_text(_get(record, "section_number", "")) != "1.2":
            continue
        try:
            start = int(_get(record, "page_start", 0) or 0)
            end = int(_get(record, "page_end", start) or start)
        except Exception:
            continue
        if start <= 0:
            continue
        for page_number in range(start, max(start, end) + 1):
            pages.add(page_number)
    return sorted(pages)


def _render_albumin_source_material_pages(result: ProcessingResult) -> str:
    pdf_path = Path(getattr(result, "pdf_path", "") or "")
    if not pdf_path.exists():
        return ""
    image_parts: list[str] = []
    for page_number in _albumin_source_material_page_numbers(result):
        image_path = _render_pdf_page_to_cache(pdf_path, page_number, "source_material")
        if not image_path or not image_path.exists():
            continue
        image_parts.append(
            f'<div class="mfg-source-page"><img src="data:image/png;base64,{_image_to_base64(image_path)}" /></div>'
        )
    if not image_parts:
        return ""
    return (
        '<section class="mfg-source-pages">'
        '<div class="mfg-source-title">원료물질 / 채장혈액원 정보 원문</div>'
        + "".join(image_parts)
        + '</section>'
    )


def _evaluation_status_key(evaluation: Any) -> str:
    # Reasons can discuss SP-only results or historical decisions. They must
    # never override the authoritative current result used by the final card.
    final_status = (clean_text(_get(evaluation, "final_status", ""))
                    or clean_text(_get(evaluation, "status", ""))
                    or clean_text(_get(evaluation, "judgement", "")))
    explicit = {PASS_LABEL: "passed", FAIL_LABEL: "failed", HOLD_LABEL: "held"}
    if final_status in explicit:
        return explicit[final_status]

    reason = clean_text(_get(evaluation, "reason", ""))
    if not reason:
        reason = clean_text(_get(evaluation, "judgement_reason", ""))

    reason_compact = re.sub(r"\s+", "", reason)

    failed_reason_tokens = (
        "\uac80\uc218\ubd88\ud569\uaca9",
        "\ubd88\ud569\uaca9\uc73c\ub85c\ud310\ub2e8",
        "\uac80\uc218\ubd88\ucda9\uc871",
        "\ubd88\ucda9\uc871\uc73c\ub85c\ud310\ub2e8",
        "\ubd88\ucda9\uc871\uc785\ub2c8\ub2e4",
        "\ubd88\ucda9\uc871\ucc98\ub9ac",
    )
    passed_reason_tokens = (
        "\uac80\uc218\ud569\uaca9",
        "\ud569\uaca9\uc73c\ub85c\ud310\ub2e8",
        "\uac80\uc218\ucda9\uc871",
        "\ucda9\uc871\uc73c\ub85c\ud310\ub2e8",
        "\ucda9\uc871\uc785\ub2c8\ub2e4",
        "\ucda9\uc871\ucc98\ub9ac",
    )
    held_reason_tokens = (
        "\uac80\uc218\ubcf4\ub958",
        "\ubcf4\ub958\ub85c\ud310\ub2e8",
    )

    if any(token in reason_compact for token in failed_reason_tokens):
        return "failed"

    if any(token in reason_compact for token in passed_reason_tokens) and not any(token in reason_compact for token in failed_reason_tokens):
        return "passed"

    if any(token in reason_compact for token in held_reason_tokens):
        return "held"

    status = clean_text(_get(evaluation, "final_status", ""))

    if not status:
        status = clean_text(_get(evaluation, "status", ""))

    if not status:
        status = clean_text(_get(evaluation, "judgement", ""))

    s = status.casefold()
    failed_status_tokens = ("\ubd88\ud569\uaca9", "\ubd88\ucda9\uc871", "\ubd80\uc801\ud569")
    passed_status_tokens = ("\ud569\uaca9", "\ucda9\uc871", "\uc801\ud569")
    held_status_tokens = ("\ubcf4\ub958", "\ud310\ub2e8")

    if any(token in status for token in failed_status_tokens) or "fail" in s:
        return "failed"

    if any(token in status for token in held_status_tokens) or "hold" in s:
        return "held"

    if (any(token in status for token in passed_status_tokens) or "pass" in s) and not any(token in status for token in failed_status_tokens):
        return "passed"

    return ""


def _record_by_order_idx(result: ProcessingResult) -> dict[int, Any]:
    out: dict[int, Any] = {}

    for record in list(getattr(result, "extracted_records", []) or []):
        order_idx = _get(record, "order_idx", None)

        if order_idx is None:
            order_idx = _get(record, "order_index", None)

        if order_idx is None:
            continue

        try:
            out[int(order_idx)] = record
        except Exception:
            continue

    return out


def _record_context_text(record: Any | None) -> str:
    if record is None:
        return ""

    parts: list[str] = []

    for key in [
        "section_number",
        "section_title",
        "test_name",
        "content_label",
        "content",
        "raw_text",
        "normalized_text",
    ]:
        value = _get(record, key, None)

        if value:
            parts.append(clean_text(value))

    section_path = _get(record, "section_path", None)

    if isinstance(section_path, list):
        for section in section_path:
            if isinstance(section, dict):
                parts.append(clean_text(section.get("number", "")))
                parts.append(clean_text(section.get("title", "")))

    return "\n".join(part for part in parts if part)


def _evaluation_context_text(evaluation: Any, record: Any | None) -> str:
    parts: list[str] = []

    for key in [
        "section_number",
        "section_title",
        "test_name",
        "content_label",
        "content",
        "criteria",
        "result",
        "method",
        "raw_text",
    ]:
        value = _get(evaluation, key, None)

        if value:
            parts.append(clean_text(value))

    record_text = _record_context_text(record)

    if record_text:
        parts.append(record_text)

    return "\n".join(part for part in parts if part)


def _evaluation_title_context_text(evaluation: Any, record: Any | None) -> str:
    parts: list[str] = []

    for key in ["section_number", "section_title", "test_name", "content_label"]:
        value = _get(evaluation, key, None)

        if value:
            parts.append(clean_text(value))

    if record is not None:
        for key in ["section_number", "section_title", "test_name", "content_label"]:
            value = _get(record, key, None)

            if value:
                parts.append(clean_text(value))

        section_path = _get(record, "section_path", None)

        if isinstance(section_path, list):
            for section in section_path:
                if isinstance(section, dict):
                    parts.append(clean_text(section.get("number", "")))
                    parts.append(clean_text(section.get("title", "")))

    return "\n".join(part for part in parts if part)


def _section_number_prefixes_for_stage(stage_name: str) -> list[str]:
    key = _norm_match(stage_name)

    is_cho_or_chol = ("cho" in key or "chol" in key) and "ecoli" not in key
    is_ecoli = "ecoli" in key

    if is_cho_or_chol and "마스터세포주" in key:
        return ["2.1.2.1.1", "2.1.1.1.1"]

    if is_ecoli and "마스터세포주" in key:
        return ["2.1.2.1.2", "2.1.1.1.2"]

    if is_cho_or_chol and "제조용세포주" in key:
        return ["2.1.2.2.1", "2.1.1.2.1"]

    if is_ecoli and "제조용세포주" in key:
        return ["2.1.2.2.2", "2.1.1.2.2"]

    return []


def _stage_matches_evaluation(stage_name: str, evaluation: Any, record: Any | None) -> bool:
    stage_key = _norm_match(stage_name)
    title_context = _evaluation_title_context_text(evaluation, record)
    title_key = _norm_match(title_context)

    if stage_key and stage_key in title_key:
        return True

    section_number = clean_text(_get(evaluation, "section_number", ""))

    if not section_number and record is not None:
        section_number = clean_text(_get(record, "section_number", ""))

    for prefix in _section_number_prefixes_for_stage(stage_name):
        if section_number.startswith(prefix):
            return True

    key = _norm_match(stage_name)

    if any(x in key for x in ["componenta", "componentb", "나노", "최종원액", "완제의약품"]):
        full_context = _evaluation_context_text(evaluation, record)
        full_key = _norm_match(full_context)

        if key and key in full_key:
            return True

    return False


def _extract_stage_names_from_flowchart_records(result: ProcessingResult) -> list[str]:
    names: list[str] = []

    for record in list(getattr(result, "extracted_records", []) or []):
        if clean_text(_get(record, "record_type", "")) not in {"flowchart", "diagram"}:
            continue

        diagram_data = _get(record, "diagram_data", None) or {}

        if not isinstance(diagram_data, dict):
            continue

        nodes = diagram_data.get("nodes") or []

        if not isinstance(nodes, list):
            continue

        for node in nodes:
            if isinstance(node, dict):
                name = clean_text(node.get("name", ""))

                if name:
                    names.append(name)

    return names


def _result_text_for_albumin_detection(result: ProcessingResult) -> str:
    parts: list[str] = []
    parts.append(clean_text(getattr(result, "product_name", "")))
    records = list(getattr(result, "extracted_records", []) or []) or list(getattr(result, "records", []) or [])
    for record in records:
        parts.append(" ".join([
            clean_text(_get(record, "section_number", "")),
            clean_text(_get(record, "section_title", "")),
            clean_text(_get(record, "content", "")),
            clean_text(_get(record, "raw_text", "")),
        ]))
    for evaluation in list(getattr(result, "evaluations", []) or []):
        parts.append(" ".join([
            clean_text(_get(evaluation, "section_number", "")),
            clean_text(_get(evaluation, "section_title", "")),
            clean_text(_get(evaluation, "test_name", "")),
            clean_text(_get(evaluation, "raw_text", "")),
        ]))
    return " ".join(part for part in parts if part)


def _is_albumin_result(result: ProcessingResult) -> bool:
    text = _norm_match(_result_text_for_albumin_detection(result))
    return bool(
        ("동국알부민" in text or "humanserumalbumin" in text or "albumin" in text)
        and ("원료혈장" in text or "원획분" in text)
    )


def _albumin_display_card_names() -> set[str]:
    return {
        _norm_match("원료혈장1"),
        _norm_match("원획분1"),
        _norm_match("최종원액"),
        _norm_match("완제의약품"),
    }


def _albumin_card_position(display_name: str) -> tuple[float, float, str]:
    key = _norm_match(display_name)
    # Albumin manufacturing-summary cards stay outside the PDF image, but their
    # vertical positions track the visible process rows in the rendered summary.
    positions = {
        _norm_match("원료혈장1"): (10.0, 22.0, "left"),
        _norm_match("원획분"): (81.0, 48.0, "right"),
        _norm_match("최종원액"): (81.0, 62.0, "right"),
        _norm_match("완제의약품"): (81.0, 80.0, "right"),
    }
    return positions.get(key, (50.0, 95.0, "right"))


def _build_stage_test_summary_map(result: ProcessingResult) -> dict[str, dict]:
    evaluations = list(getattr(result, "evaluations", []) or [])
    record_map = _record_by_order_idx(result)

    stage_names = [
        stage["display_name"]
        for stage in _extract_stage_box_positions_from_pdf(getattr(result, "pdf_path", None))
    ]

    stage_names.extend(_extract_stage_names_from_flowchart_records(result))

    unique_stage_names: list[str] = []
    seen: set[str] = set()

    for name in stage_names:
        key = _norm_match(name)

        if not key or key in seen:
            continue

        seen.add(key)
        unique_stage_names.append(name)

    out: dict[str, dict] = {}

    for stage_name in unique_stage_names:
        out[_norm_match(stage_name)] = {
            "stage_name": stage_name,
            "passed": 0,
            "failed": 0,
            "held": 0,
            "total": 0,
        }

    for evaluation in evaluations:
        if _get(evaluation, "source", "") == "md_policy":
            continue  # Document rules are not additional physical laboratory tests.
        status_key = _evaluation_status_key(evaluation)

        if not status_key:
            continue

        record = None
        order_idx = _get(evaluation, "order_idx", None)

        if order_idx is not None:
            try:
                record = record_map.get(int(order_idx))
            except Exception:
                record = None

        for stage_name in unique_stage_names:
            if not _stage_matches_evaluation(stage_name, evaluation, record):
                continue

            stage_key = _norm_match(stage_name)
            lot_judgements = list(_get(evaluation, "lot_judgements", []) or [])

            if lot_judgements:
                for row in lot_judgements:
                    row_status_key = _evaluation_status_key(row)
                    if not row_status_key:
                        row_status_key = status_key
                    out[stage_key][row_status_key] += 1
                    out[stage_key]["total"] += 1
            else:
                out[stage_key][status_key] += 1
                out[stage_key]["total"] += 1
            break

    return out


def build_stage_info_cards(
    result: ProcessingResult,
    manufacturing_results: list[ManufacturingInfoValidationResult] | None = None,
    summary_counts_path: Path | str | None = None,
) -> list[StageTestCard]:
    pdf_path = getattr(result, "pdf_path", None)

    if not pdf_path:
        return []

    stage_boxes = _extract_stage_box_positions_from_pdf(Path(pdf_path))
    albumin_result = _is_albumin_result(result)
    if albumin_result:
        keep = _albumin_display_card_names()
        stage_boxes = [stage for stage in stage_boxes if _norm_match(stage["display_name"]) in keep]
    summary_map = _build_stage_test_summary_map(result)
    summary_override_map = _read_stage_summary_counts(summary_counts_path)
    manufacturing_results = manufacturing_results or []
    issue_map = {
        _norm_match(item.stage_name): (
            sum(field.status != "합격" for field in item.fields)
            + sum(test.status != "합격" for test in item.test_dates)
        )
        for item in manufacturing_results
    }
    failure_map = {
        _norm_match(item.stage_name): (
            sum(field.status == "불합격" for field in item.fields)
            + sum(test.status == "불합격" for test in item.test_dates)
        )
        for item in manufacturing_results
    }
    hold_map = {
        _norm_match(item.stage_name): (
            sum(field.status == "보류" for field in item.fields)
            + sum(test.status == "보류" for test in item.test_dates)
        )
        for item in manufacturing_results
    }

    cards: list[StageTestCard] = []
    used_keys: set[str] = set()

    for stage in stage_boxes:
        display_name = stage["display_name"]
        key = _norm_match(display_name)

        info = summary_map.get(
            key,
            {
                "stage_name": display_name,
                "passed": 0,
                "failed": 0,
                "held": 0,
                "total": 0,
            },
        )

        override_info = summary_override_map.get(_stage_summary_exact_key(display_name))

        if override_info is not None:
            info = override_info

        used_keys.add(key)

        card_x = float(stage["x_pct"])
        card_y = float(stage["y_pct"])
        card_side = str(stage["side"])
        if albumin_result:
            card_x, card_y, card_side = _albumin_card_position(display_name)

        cards.append(
            StageTestCard(
                display_name=display_name,
                x_pct=card_x,
                y_pct=card_y,
                side=card_side,
                passed=int(info["passed"]),
                failed=int(info["failed"]),
                held=int(info["held"]),
                total=int(info["total"]),
                manufacturing_issues=int(issue_map.get(key, 0)),
                manufacturing_failures=int(failure_map.get(key, 0)),
                manufacturing_holds=int(hold_map.get(key, 0)),
                validation_target=display_name,
            )
        )

    for key, info in summary_map.items():
        if key in used_keys:
            continue
        if albumin_result and key not in _albumin_display_card_names():
            continue

        display_name = str(info["stage_name"])
        override_info = summary_override_map.get(_stage_summary_exact_key(display_name))

        if override_info is not None:
            info = override_info
            display_name = str(info["stage_name"])

        cards.append(
            StageTestCard(
                display_name=display_name,
                x_pct=50.0,
                y_pct=95.0,
                side="right",
                passed=int(info["passed"]),
                failed=int(info["failed"]),
                held=int(info["held"]),
                total=int(info["total"]),
                manufacturing_issues=int(issue_map.get(key, 0)),
                manufacturing_failures=int(failure_map.get(key, 0)),
                manufacturing_holds=int(hold_map.get(key, 0)),
                validation_target=display_name,
            )
        )

    positioned = [
        card for card in cards
        if card.x_pct != 50.0 or card.y_pct != 95.0
    ]
    fallback = [
        card for card in cards
        if card.x_pct == 50.0 and card.y_pct == 95.0
    ]

    if not fallback:
        return cards

    base_counts: dict[str, int] = {}
    for card in fallback:
        base = re.sub(r"\s*[0-9０-９]+$", "", clean_text(card.display_name)).strip()
        base_counts[base] = base_counts.get(base, 0) + 1

    grouped: dict[str, StageTestCard] = {}
    group_order: list[str] = []
    for card in fallback:
        base = re.sub(r"\s*[0-9０-９]+$", "", clean_text(card.display_name)).strip()
        display_name = base if base and base_counts.get(base, 0) > 1 else card.display_name
        key = _norm_match(display_name)
        if key not in grouped:
            group_order.append(key)
            grouped[key] = StageTestCard(
                display_name=display_name,
                x_pct=0.0,
                y_pct=0.0,
                side="left",
                passed=0,
                failed=0,
                held=0,
                total=0,
                manufacturing_issues=0,
                manufacturing_failures=0,
                manufacturing_holds=0,
                validation_target=card.validation_target or card.display_name,
            )
        target = grouped[key]
        target.passed += card.passed
        target.failed += card.failed
        target.held += card.held
        target.total += card.total
        target.manufacturing_issues += card.manufacturing_issues
        target.manufacturing_failures += card.manufacturing_failures
        target.manufacturing_holds += card.manufacturing_holds

    fallback_cards = [grouped[key] for key in group_order]
    if albumin_result:
        for card in fallback_cards:
            key = _norm_match(card.display_name)
            if "원료혈장" in key:
                card.x_pct, card.y_pct, card.side = 10.0, 22.0, "left"
            elif "원획분" in key:
                card.x_pct, card.y_pct, card.side = 81.0, 48.0, "right"
            elif "최종원액" in key:
                card.x_pct, card.y_pct, card.side = 81.0, 62.0, "right"
            elif "완제의약품" in key:
                card.x_pct, card.y_pct, card.side = 81.0, 80.0, "right"
            else:
                card.x_pct, card.y_pct, card.side = 50.0, 95.0, "right"
    else:
        count = len(fallback_cards)
        for idx, card in enumerate(fallback_cards):
            card.x_pct = 0.0
            card.y_pct = 12.0 + (70.0 * idx / max(count - 1, 1))
            card.side = "left"

    return positioned + fallback_cards


def summarize_stage_info_cards(result: ProcessingResult) -> Summary:
    """
    제조요약도 옆 단계 카드와 같은 기준으로 전체 집계를 만든다.

    최종 리포트 상단 요약, summary_before/after.csv, 시뮬레이션 그래프가
    서로 다른 경로로 합산하면 숫자가 어긋난다. 이 함수는 화면에 보이는
    단계 카드 합산값을 단일 기준으로 제공한다.
    """
    cards = build_stage_info_cards(result, manufacturing_results=[])

    if not cards:
        return getattr(result, "summary", Summary())

    passed = sum(int(card.passed) for card in cards)
    failed = sum(int(card.failed) for card in cards)
    held = sum(int(card.held) for card in cards)
    total = passed + failed + held

    return Summary(
        passed=passed,
        failed=failed,
        held=held,
        total=total,
        comparable_total=total,
    )


def _uses_dense_side_layout(cards: list[StageTestCard]) -> bool:
    if len(cards) < 4:
        return False
    names = " ".join(clean_text(card.display_name) for card in cards)
    return "원료혈장" in names or "원획분" in names or "동국알부민" in names


def _stabilize_dense_side_card_positions(cards: list[StageTestCard]) -> None:
    if not _uses_dense_side_layout(cards):
        return

    for side in ("left", "right"):
        side_cards = sorted(
            [card for card in cards if (card.side or "right") == side],
            key=lambda card: (card.y_pct, card.x_pct, card.display_name),
        )
        if not side_cards:
            continue

        min_top = 6.0
        max_top = 82.0
        min_gap = 18.0
        desired = [max(min_top, min(max_top, card.y_pct - 5.0)) for card in side_cards]

        for idx in range(1, len(desired)):
            desired[idx] = max(desired[idx], desired[idx - 1] + min_gap)

        overflow = desired[-1] - max_top
        if overflow > 0:
            desired = [top - overflow for top in desired]

        underflow = min_top - desired[0]
        if underflow > 0:
            desired = [top + underflow for top in desired]

        for card, top in zip(side_cards, desired):
            card.y_pct = max(min_top, min(max_top, top))


def _card_position(card: StageTestCard, dense_layout: bool = False) -> tuple[str, float]:
    """Place side cards near the corresponding manufacturing-summary region."""
    if card.side == "left":
        left = "calc(-1 * (var(--mfg-card-width) + 8px))" if dense_layout else "calc(-1 * (var(--mfg-card-width) + var(--mfg-card-gap)))"
    else:
        left = "calc(100% + var(--mfg-card-gap))"

    top = card.y_pct if dense_layout else card.y_pct - 4.0
    top = max(1.0, min(92.0, top))

    return left, top


def _border_class_for_card(card: StageTestCard) -> str:
    if card.failed > 0:
        return "fail"

    if card.held > 0:
        return "hold"

    if card.total == 0:
        return "empty"

    return "pass"


def _render_stage_test_card(card: StageTestCard, dense_layout: bool = False) -> str:
    left, top = _card_position(card, dense_layout=dense_layout)
    border_class = _border_class_for_card(card)
    title = html.escape(card.display_name)
    test_anchor = html.escape(_stage_anchor_key(card.display_name))
    test_target_id = f"mfg-stage-{test_anchor}"

    onclick = (
        "event.stopPropagation();"
        "try{"
        "var id=this.getAttribute('data-mfg-target');"
        "var el=document.getElementById(id);"
        "if(el){event.preventDefault();el.scrollIntoView({behavior:'smooth',block:'start'});return false;}"
        "}catch(e){}"
        "return true;"
    )

    return f"""
    <a class="mfg-side-card {border_class}"
       href="#{test_target_id}"
       data-mfg-target="{test_target_id}"
       onclick="{onclick}"
       style="left:{left}; top:{top:.2f}%;"
       title="{title} 시험 결과로 이동">
        <div class="mfg-side-title">{title}</div>

        <div class="mfg-count-row">
            <span class="count-pill pass">충족 {card.passed}</span>
            <span class="count-pill fail">불충족 {card.failed}</span>
            <span class="count-pill hold">보류 {card.held}</span>
        </div>

        <div class="mfg-total-row">총 {card.total}개</div>
    </a>
    """


def _display_process_date_text(text: str) -> str:
    text = clean_text(text)
    if not text:
        return ""
    return (
        text.replace("제조요약도 날짜 선행관계 판정", "정합성 검증-선행 검증")
        .replace("날짜 선행관계", "공정일자 정합성")
        .replace("날짜 선행 관계", "공정일자 정합성")
        .replace("날짜 선후관계", "공정일자 선후관계")
        .replace("검수불합격", "불충족")
        .replace("검수합격", "충족")
        .replace("검수보류", "보류")
        .replace("불합격으로 판단", "불충족으로 판단")
        .replace("합격으로 판단", "충족으로 판단")
        .replace("불합격으로판단", "불충족으로판단")
        .replace("합격으로판단", "충족으로판단")
        .replace("불합격입니다", "불충족입니다")
        .replace("합격입니다", "충족입니다")
        .replace("불합격 처리", "불충족 처리")
        .replace("합격 처리", "충족 처리")
    )


def _process_date_join_flow_display_line(lines: list[str]) -> str:
    joined = "\n".join(lines)
    if "Component A" not in joined or "Component B" not in joined:
        return ""

    def _stage_with_date(component_label: str) -> str:
        stage_name = f"{component_label} 중간체원액"
        match = re.search(rf"{re.escape(stage_name)}\(([^)]+)\)", joined)
        if match:
            return f"{stage_name}({match.group(1)})"
        return stage_name

    join_tail = ""
    for line in lines:
        if line.startswith("공통 흐름:") or line.startswith("합류 흐름:"):
            join_tail = line.split(":", 1)[1].strip()
            break

    for marker in ("나노파티클원액", "최종원액", "완제의약품"):
        marker_idx = join_tail.find(marker)
        if marker_idx >= 0:
            join_tail = join_tail[marker_idx:]
            break

    if not join_tail:
        join_tail = "나노파티클원액 → 최종원액 → 완제의약품"

    return f"합류 흐름: {_stage_with_date('Component A')}, {_stage_with_date('Component B')} → {join_tail}"


def _process_date_error_scope_display_line(lines: list[str]) -> str:
    scopes: list[str] = []
    in_error_section = False
    join_tokens = ("공통 흐름", "합류 흐름", "나노파티클원액", "최종원액", "완제의약품")

    for line in lines:
        if line == "오류 항목":
            in_error_section = True
            continue

        is_error_line = in_error_section or "오류" in line or "불일치" in line or "역전" in line
        if not is_error_line:
            continue

        if any(token in line for token in join_tokens):
            if "합류 흐름" not in scopes:
                scopes.append("합류 흐름")
            continue

        if "Component A" in line and "Component A 흐름" not in scopes:
            scopes.append("Component A 흐름")
        if "Component B" in line and "Component B 흐름" not in scopes:
            scopes.append("Component B 흐름")

    if not scopes:
        return ""
    if "합류 흐름" in scopes:
        return "오류 위치: 합류 흐름"
    return "오류 위치: " + " / ".join(scopes)


def _process_date_is_redundant_join_check_line(line: str) -> bool:
    return "마지막 공정" in line and "첫 공정" in line


def _process_date_status_class(status: str) -> str:
    status = clean_text(status)
    if "불합격" in status:
        return "fail"
    if "보류" in status:
        return "hold"
    if "합격" in status:
        return "pass"
    return "empty"


def _render_process_date_summary_card(
    result: ProcessingResult,
    cards: list[StageTestCard],
    dense_layout: bool = False,
) -> str:
    status = clean_text(getattr(result, "manufacturing_summary_status", ""))
    reason = clean_text(getattr(result, "manufacturing_summary_reason", ""))

    if not status and not reason:
        return ""

    if status == "검수합격":
        fallback = "제조요약도 공정일자가 앞 공정에서 뒤 공정으로 정상 연결되어 검수합격입니다."
    elif status == "검수불합격":
        fallback = "제조요약도 공정일자에 역전 또는 불일치가 있어 확인이 필요합니다."
    elif status == "검수보류":
        fallback = "제조요약도 공정일자 정보를 안정적으로 확정하기 어려워 검수보류입니다."
    else:
        fallback = "제조요약도 공정일자 흐름을 검증했습니다."

    display_reason = _display_process_date_text(reason or fallback)
    lines = [
        _display_process_date_text(line.lstrip("- ").strip())
        for line in display_reason.splitlines()
        if _display_process_date_text(line.lstrip("- ").strip())
    ]
    lead_line = lines[0] if lines else fallback
    detail_lines = [
        line
        for line in lines[1:]
        if line not in {"공정 흐름", "합류 검증"}
    ]
    error_scope_line = _process_date_error_scope_display_line(detail_lines)
    join_flow_line = _process_date_join_flow_display_line(detail_lines)
    if join_flow_line:
        replaced_common_flow = False
        for idx, line in enumerate(detail_lines):
            if line.startswith("공통 흐름:") or line.startswith("합류 흐름:"):
                detail_lines[idx] = join_flow_line
                replaced_common_flow = True
                break
        if not replaced_common_flow:
            detail_lines.append(join_flow_line)
    if error_scope_line:
        detail_lines = [error_scope_line] + [line for line in detail_lines if line != error_scope_line]
    detail_lines = [
        line
        for line in detail_lines
        if (
            line != "오류 항목"
            and not line.startswith("합류 검증")
            and not _process_date_is_redundant_join_check_line(line)
        )
    ][:4]

    page_text = ""
    page_numbers = getattr(result, "manufacturing_summary_page_numbers", None) or []
    if page_numbers:
        page_text = "대상 페이지 " + " / ".join(str(x) for x in page_numbers)

    left_cards = [card for card in cards if card.side == "left"]
    right_cards = [card for card in cards if card.side != "left"]
    left_max = max((card.y_pct for card in left_cards), default=0.0)
    right_max = max((card.y_pct for card in right_cards), default=0.0)
    side = "left" if (left_max <= right_max or len(left_cards) <= len(right_cards)) else "right"
    max_y = left_max if side == "left" else right_max
    top = max(52.0, min(84.0, max_y + 13.0))
    left_css = (
        "calc(-1 * (var(--mfg-card-width) + var(--mfg-card-gap)))"
        if side == "left"
        else "calc(100% + var(--mfg-card-gap))"
    )
    status_class = _process_date_status_class(status)
    details_html = "".join(
        f'<div class="mfg-date-side-detail">{html.escape(line)}</div>'
        for line in detail_lines
    )

    inline_class = " mfg-date-inline-card" if dense_layout else ""
    return f"""
    <div class="mfg-date-side-card {status_class}{inline_class}" style="left:{left_css}; top:{top:.2f}%;">
        <div class="mfg-date-side-kicker">{html.escape(page_text or "제조요약도")}</div>
        <div class="mfg-date-side-title">정합성 검증-선행 검증</div>
        <div class="mfg-date-side-badge">{html.escape(status or "미판정")}</div>
        <div class="mfg-date-side-body">{html.escape(lead_line)}</div>
        {details_html}
    </div>
    """


def render_manufacturing_summary_with_stage_cards(
    result: ProcessingResult,
    summary_counts_path: Path | str | None = None,
) -> str:
    if "manufacturing_info_cards" in result.metadata:
        # Modern results are judged once by the MD engine. Rendering must not
        # run an independent legacy policy or invent extra test date checks.
        manufacturing_results = [ManufacturingInfoValidationResult(
            stage_name=row["stage_name"], status=row["status"], source_count=row["source_count"],
            fields=[FieldCompareResult(**item) for item in row["fields"]],
            test_dates=[TestDateCompareResult(**item) for item in row["test_dates"]])
            for row in result.metadata["manufacturing_info_cards"]]
    else:
        manufacturing_results = validate_manufacturing_info_consistency(result)
    info_validation_html = render_manufacturing_info_validation_card(
        result,
        validation_results=manufacturing_results,
    )

    image_paths = list(getattr(result, "manufacturing_summary_image_paths", []) or [])
    cards = build_stage_info_cards(
        result,
        manufacturing_results=manufacturing_results,
        summary_counts_path=summary_counts_path,
    )

    if not image_paths:
        return info_validation_html

    image_path = Path(image_paths[0])

    if not image_path.exists():
        return info_validation_html

    image_b64 = _image_to_base64(image_path)
    dense_side_layout = _uses_dense_side_layout(cards)
    if dense_side_layout:
        _stabilize_dense_side_card_positions(cards)
    cards_html = "\n".join(
        _render_stage_test_card(card, dense_layout=dense_side_layout)
        for card in cards
    )
    date_summary_html = _render_process_date_summary_card(result, cards, dense_layout=dense_side_layout)
    dense_class = " dense-side-layout" if dense_side_layout else ""

    manufacturing_summary_html = f"""
    <style>
    html {{
        scroll-behavior: smooth;
    }}

    .mfg-summary-area {{
        --mfg-card-width: 252px;
        --mfg-card-gap: 18px;
        --mfg-side-space: calc(var(--mfg-card-width) + var(--mfg-card-gap));
        width: 100%;
        max-width: 1580px;
        margin: 22px auto 0;
        padding: 0 calc(var(--mfg-side-space) + 12px);
        box-sizing: border-box;
        overflow: visible;
    }}

    .mfg-image-box {{
        position: relative;
        width: min(940px, 100%);
        margin: 0 auto;
        overflow: visible;
        background: #ffffff;
        border-radius: 12px;
        box-shadow: 0 18px 46px rgba(0, 0, 0, 0.26);
    }}

    .mfg-image-box img {{
        display: block;
        width: 100%;
        height: auto;
        border-radius: 12px;
    }}

    .mfg-side-card {{
        position: absolute;
        display: block;
        width: var(--mfg-card-width);
        min-height: 116px;
        background: rgba(255, 255, 255, 0.98);
        border: 1px solid #d6dee8;
        border-radius: 16px;
        box-shadow: 0 10px 22px rgba(15, 23, 42, 0.14);
        padding: 16px 17px;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        z-index: 8;
        text-decoration: none;
        color: inherit;
        cursor: pointer;
        box-sizing: border-box;
        text-align: left;
    }}

    .mfg-side-card:hover {{
        transform: translateY(-1px);
        box-shadow: 0 9px 20px rgba(15, 23, 42, 0.17);
    }}

    .dense-side-layout {{
        --mfg-card-width: 236px;
        --mfg-card-gap: 34px;
        max-width: 1700px;
    }}

    .dense-side-layout .mfg-side-card {{
        min-height: 98px;
        padding: 13px 14px;
        border-radius: 14px;
    }}

    .dense-side-layout .mfg-side-title {{
        font-size: 15px;
        margin-bottom: 11px;
    }}

    .dense-side-layout .count-pill {{
        padding: 6px 0;
        font-size: 12px;
    }}

    .dense-side-layout .mfg-total-row {{
        margin-top: 8px;
        padding: 6px 0;
        font-size: 13px;
    }}

    .mfg-side-card.pass {{
        border-left: 7px solid #22c55e;
    }}

    .mfg-side-card.fail {{
        border-left: 7px solid #ef4444;
    }}

    .mfg-side-card.hold {{
        border-left: 7px solid #f59e0b;
    }}

    .mfg-side-card.empty {{
        border-left: 7px solid #94a3b8;
    }}

    .mfg-side-title {{
        font-size: 16px;
        font-weight: 900;
        color: #111827;
        line-height: 1.3;
        margin-bottom: 13px;
        word-break: keep-all;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }}

    .mfg-count-row {{
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 6px;
        width: 100%;
    }}

    .count-pill {{
        display: inline-flex;
        align-items: center;
        justify-content: center;
        border-radius: 999px;
        padding: 7px 0;
        font-size: 13px;
        font-weight: 900;
        white-space: nowrap;
        line-height: 1;
        letter-spacing: -0.35px;
        min-width: 0;
    }}

    .count-pill.pass {{
        color: #15803d;
        background: #dcfce7;
    }}

    .count-pill.fail {{
        color: #b91c1c;
        background: #fee2e2;
    }}

    .count-pill.hold {{
        color: #b45309;
        background: #fef3c7;
    }}

    .mfg-total-row {{
        margin-top: 9px;
        width: 100%;
        border-radius: 999px;
        padding: 7px 0;
        font-size: 14px;
        font-weight: 900;
        text-align: center;
        color: #111827;
        background: #e5e7eb;
        line-height: 1;
    }}

    .mfg-date-side-card {{
        position: absolute;
        width: var(--mfg-card-width);
        min-height: 128px;
        padding: 15px 16px;
        border: 1px solid #d6dee8;
        border-radius: 16px;
        background: rgba(255, 255, 255, 0.98);
        box-shadow: 0 10px 22px rgba(15, 23, 42, 0.14);
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        box-sizing: border-box;
        z-index: 7;
        color: #111827;
    }}

    .mfg-date-side-card.pass {{
        border-left: 7px solid #22c55e;
    }}

    .mfg-date-side-card.fail {{
        border-left: 7px solid #ef4444;
    }}

    .mfg-date-side-card.hold {{
        border-left: 7px solid #f59e0b;
    }}

    .mfg-date-side-card.empty {{
        border-left: 7px solid #94a3b8;
    }}

    .mfg-date-side-kicker {{
        color: #64748b;
        font-size: 12px;
        font-weight: 850;
        margin-bottom: 5px;
    }}

    .mfg-date-side-title {{
        color: #111827;
        font-size: 16px;
        font-weight: 950;
        line-height: 1.28;
        margin-bottom: 8px;
    }}

    .mfg-date-side-badge {{
        display: inline-flex;
        align-items: center;
        border-radius: 999px;
        padding: 5px 9px;
        background: #f1f5f9;
        color: #172554;
        font-size: 12px;
        font-weight: 950;
        margin-bottom: 9px;
    }}

    .mfg-date-side-body,
    .mfg-date-side-detail {{
        color: #334155;
        font-size: 13px;
        font-weight: 780;
        line-height: 1.45;
        word-break: keep-all;
    }}

    .mfg-date-side-detail {{
        margin-top: 7px;
        padding-top: 7px;
        border-top: 1px solid #e5e7eb;
    }}

    .dense-side-layout .mfg-date-inline-card {{
        position: relative;
        left: auto !important;
        top: auto !important;
        width: min(520px, calc(100% - 32px));
        margin: 18px auto 0;
        z-index: 5;
    }}

    @media (max-width: 1180px) {{
        .mfg-summary-area {{
            padding: 0 8px;
        }}

        .mfg-side-card,
        .mfg-date-side-card {{
            position: relative;
            left: auto !important;
            top: auto !important;
            display: inline-block;
            width: calc(50% - 10px);
            margin: 10px 4px 0;
            vertical-align: top;
        }}
    }}

    @media (max-width: 760px) {{
        .mfg-side-card,
        .mfg-date-side-card {{
            width: 100%;
            margin: 10px 0 0;
        }}
    }}
    </style>

    <div class="mfg-summary-area{dense_class}">
        <div class="mfg-image-box">
            <img src="data:image/png;base64,{image_b64}" />
            {cards_html}
            {date_summary_html}
        </div>
    </div>
    """

    return manufacturing_summary_html + info_validation_html
