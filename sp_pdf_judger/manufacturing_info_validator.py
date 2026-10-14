from __future__ import annotations

import html
import hashlib
import json
import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .clova_client import create_clova_client, request_structured_response
from .config import (
    CLOVA_API_KEY,
    CLOVA_BASE_URL,
    CLOVA_MAX_COMPLETION_TOKENS,
    DEFAULT_CLOVA_MODEL,
)
from .utils import clean_text


@dataclass
class ManufacturingInfoItem:
    stage_name: str
    manufacturing_no: str = ""
    manufacturing_date: str = ""
    quantity: str = ""
    quantity_field_name: str = "제조량"
    page_number: int | None = None


@dataclass
class FieldCompareResult:
    field_name: str
    summary_value: str
    document_value: str
    status: str
    reason: str
    source_label: str = ""
    section_number: str = ""
    page_number: int | None = None
    baseline_label: str = "제조요약도 기준"
    comparison_label: str = "본문"


@dataclass
class ManufacturingInfoOccurrence:
    stage_name: str
    item: ManufacturingInfoItem
    source_label: str
    section_number: str = ""
    page_number: int | None = None


@dataclass
class ManufacturingNoOccurrence:
    manufacturing_no: str
    manufacturing_date: str = ""
    expiry_period: str = ""
    source_label: str = ""
    section_number: str = ""
    page_number: int | None = None


@dataclass
class TestDateCompareResult:
    test_name: str
    date_text: str
    status: str
    reason: str
    section_number: str = ""
    page_number: int | None = None


@dataclass
class ManufacturingInfoValidationResult:
    stage_name: str
    status: str
    fields: list[FieldCompareResult]
    test_dates: list[TestDateCompareResult] = field(default_factory=list)
    source_count: int = 0


FIELD_ALIASES = {
    "제조번호": ["제조번호"],
    "제조년월일": ["제조년월일", "제조일자", "제조일"],
    "제조량": ["제조량"],
    "제조수량": ["제조수량", "총 제조수량", "총제조수량", "신청수량"],
}

EXPIRY_FIELD_ALIASES = [
    "사용(유효)기간",
    "사용 (유효) 기간",
    "사용기간",
    "유효기간",
    "사용기한",
    "유효기한",
]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)

    return getattr(obj, key, default)


def _load_records_json_from_result(result: Any) -> list[dict]:
    metadata = getattr(result, "metadata", None)

    if not isinstance(metadata, dict):
        metadata = {}

    candidates: list[Path] = []

    records_json_path = metadata.get("records_json_path")

    if records_json_path:
        candidates.append(Path(records_json_path))

    extract_dir = (
        getattr(result, "runtime_extract_dir", None)
        or metadata.get("extract_dir")
    )

    if extract_dir:
        candidates.append(Path(extract_dir) / "05_records.json")

    for path in candidates:
        if not path.exists():
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))

            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except Exception:
            continue

    return []


def _records_from_source(source: Any) -> list[Any]:
    """
    가장 우선적으로 result.metadata['extract_dir']/05_records.json 원본을 읽는다.
    이유:
    - ExtractedRecord 변환 과정에서 diagram_data, normalized_text가 빠지면 제조요약도 검증이 흔들림.
    - 원본 05_records.json에는 diagram_data.nodes가 그대로 들어있음.
    """
    if source is None:
        return []

    raw_records = _load_records_json_from_result(source)

    if raw_records:
        return raw_records

    extracted_records = getattr(source, "extracted_records", None)

    if extracted_records is not None:
        return list(extracted_records)

    if isinstance(source, list):
        return source

    if isinstance(source, tuple):
        return list(source)

    if isinstance(source, dict):
        records = source.get("extracted_records") or source.get("records") or source.get("data")

        if isinstance(records, list):
            return records

    if isinstance(source, (str, Path)):
        path = Path(source)

        if path.exists() and path.suffix.lower() == ".json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))

                if isinstance(data, list):
                    return data

                if isinstance(data, dict):
                    records = data.get("records") or data.get("extracted_records") or data.get("data")

                    if isinstance(records, list):
                        return records
            except Exception:
                return []

    return []


def _norm_match(text: str | None) -> str:
    text = clean_text(text)

    if not text:
        return ""

    text = text.casefold()

    text = text.replace("e.colli", "ecoli")
    text = text.replace("e.coli", "ecoli")
    text = text.replace("e coli", "ecoli")
    text = text.replace("e-coli", "ecoli")

    text = text.replace("컴포넌트", "component")

    text = text.replace("나노파티클", "나노")
    text = text.replace("나노피티클", "나노")
    text = text.replace("나노파티글", "나노")

    text = re.sub(r"[\s\-_·.,:：/(){}\[\]<>]+", "", text)

    return text


def _normalize_code(value: str | None) -> str:
    value = clean_text(value)

    if not value:
        return ""

    value = value.replace("\n", "")
    value = value.replace("－", "-")
    value = value.replace("–", "-")
    value = value.replace("—", "-")

    # 제조번호는 대소문자와 표기 구분자 차이만 무시한다.
    # 예: 26ABC-01A == 26ABC01A
    # 단, 26ABC01A와 26A01처럼 실제 문자가 누락된 경우는 불일치로 본다.
    value = re.sub(r"[\s\-_/·.]+", "", value)

    return value.upper()


def _normalize_decimal_text(value: str) -> str:
    value = clean_text(value)

    try:
        decimal_value = Decimal(value)
        normalized = decimal_value.normalize()

        if normalized == normalized.to_integral():
            return str(normalized.quantize(Decimal("1")))

        return format(normalized, "f")
    except InvalidOperation:
        return value


def _normalize_quantity(value: str | None) -> tuple[str, str]:
    value = clean_text(value)

    if not value:
        return "", ""

    value = value.replace(",", "")
    value = value.replace("．", ".")
    value = value.replace("㎖", "mL")
    value = value.replace("㎕", "uL")
    value = value.replace("ℓ", "L")
    value = value.replace("Ｌ", "L")
    value = value.replace("ｌ", "L")
    value = re.sub(r"\s+", " ", value).strip()

    m = re.fullmatch(
        r"([0-9]+(?:\.[0-9]+)?)\s*(L|l|mL|ml|uL|ul|kg|KG|Kg|g|G|dose|doses|Dose|Doses|도즈|병|바이알|vial|vials|Vial|Vials|개|ea|EA)?",
        value,
        flags=re.I,
    )

    if not m:
        return "", ""

    number = _normalize_decimal_text(m.group(1))
    unit = clean_text(m.group(2) or "")
    unit_key = unit.casefold()

    if unit_key in {"l", "liter", "litre", "리터"}:
        unit_key = "l"
    elif unit_key in {"ml", "milliliter", "millilitre"}:
        unit_key = "ml"
    elif unit_key in {"ul", "μl", "µl"}:
        unit_key = "ul"
    elif unit_key in {"kg"}:
        unit_key = "kg"
    elif unit_key in {"g"}:
        unit_key = "g"
    elif unit_key in {"dose", "doses", "도즈"}:
        unit_key = "dose"
    elif unit_key in {"병", "바이알", "vial", "vials", "개", "ea"}:
        # 완제의약품 수량은 문서마다 병/바이알/vial/EA 등으로 표현될 수 있으므로 같은 개수 단위로 본다.
        unit_key = "vial"

    return number, unit_key



def _normalize_date_parts(value: str | None) -> tuple[str, str, str]:
    text = clean_text(value)

    if not text:
        return "", "", ""

    text = text.replace("．", ".")
    text = text.replace("。", ".")
    text = text.replace("－", "-")
    text = text.replace("／", "/")
    text = re.sub(r"\s+", " ", text).strip()

    patterns = [
        r"(20\d{2})\s*년\s*(\d{1,2})?\s*월?\s*(?:(\d{1,2})\s*일?)?",
        r"(20\d{2})\s*[.\-/]\s*(\d{1,2})(?:\s*[.\-/]\s*(\d{1,2}))?",
        r"(20\d{2})\s+(\d{1,2})(?:\s+(\d{1,2}))?",
    ]

    for pattern in patterns:
        m = re.search(pattern, text)

        if not m:
            continue

        year = m.group(1)
        month = m.group(2) or ""
        day = m.group(3) or ""

        if month:
            month = month.zfill(2)

        if day:
            day = day.zfill(2)

        try:
            y = int(year)
            mo = int(month) if month else 1
            da = int(day) if day else 1
            date(y, mo, da)
        except ValueError:
            continue

        return year, month, day

    return "", "", ""


def _code_matches(summary_value: str | None, document_value: str | None) -> bool:
    left = _normalize_code(summary_value)
    right = _normalize_code(document_value)

    return bool(left) and left == right


def _date_matches(summary_value: str | None, document_value: str | None) -> bool:
    sy, sm, sd = _normalize_date_parts(summary_value)
    dy, dm, dd = _normalize_date_parts(document_value)

    if not sy or not dy:
        return False

    if sy != dy:
        return False

    if sm and dm and sm != dm:
        return False

    # 한쪽이 YYYY.MM까지만 있으면 YYYY.MM까지만 비교한다.
    if sd and dd and sd != dd:
        return False

    return True


def _quantity_matches(summary_value: str | None, document_value: str | None) -> bool:
    summary_num, summary_unit = _normalize_quantity(summary_value)
    document_num, document_unit = _normalize_quantity(document_value)

    if not summary_num or not document_num:
        return False

    if summary_num != document_num:
        return False

    if summary_unit and document_unit:
        return summary_unit == document_unit

    return True

def _looks_like_quantity_only(candidate: str | None) -> bool:
    candidate = clean_text(candidate)

    if not candidate:
        return False

    # 26A01, B2604, P26-1, F26-1처럼 문자와 숫자가 섞인 제조번호는
    # _normalize_quantity()가 앞 숫자만 보고 수량으로 오인할 수 있으므로 여기서 제외한다.
    if re.search(r"[A-Za-z]", candidate):
        unit_words = r"L|l|mL|ml|uL|ul|kg|KG|Kg|g|G|병|바이알|vial|vials|개|EA|ea|dose|doses|도즈"
        return bool(re.fullmatch(rf"[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:{unit_words})", candidate, flags=re.I))

    # 3/21 같은 페이지 표기는 제조번호가 아니다.
    if re.fullmatch(r"\d+\s*/\s*\d+", candidate):
        return True

    # 단위가 명시된 값은 수량이다.
    if re.fullmatch(
        r"[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:L|l|mL|ml|uL|ul|kg|KG|Kg|g|G|병|바이알|vial|vials|개|EA|ea|dose|doses|도즈)",
        candidate,
        flags=re.I,
    ):
        return True

    return False



def _field_lookup(fields: dict[str, Any], names: list[str]) -> str:
    if not fields:
        return ""

    normalized = {
        re.sub(r"\s+", "", clean_text(k)): clean_text(v)
        for k, v in fields.items()
        if clean_text(k)
    }

    for name in names:
        key = re.sub(r"\s+", "", clean_text(name))

        if key in normalized:
            return normalized[key]

    return ""


def _quantity_field_name_from_fields(fields: dict[str, Any]) -> str:
    if not fields:
        return "제조량"

    normalized_keys = {
        re.sub(r"\s+", "", clean_text(k))
        for k in fields.keys()
    }

    if "총제조수량" in normalized_keys:
        return "총 제조수량"

    if "제조수량" in normalized_keys:
        return "제조수량"

    if "신청수량" in normalized_keys:
        return "제조수량"

    return "제조량"


def _fix_split_code(value: str, next_value: str | None = None) -> str:
    value = clean_text(value)

    if not value:
        return ""

    if value.endswith("-") and next_value:
        next_value = clean_text(next_value)

        if re.fullmatch(r"[A-Za-z0-9]+", next_value):
            return clean_text(value + next_value)

    return value


def _group_pdf_words_to_lines(words: list[tuple]) -> list[dict]:
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
        text = clean_text(" ".join(clean_text(w[4]) for w in line_words if clean_text(w[4])))

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


def _split_pdf_line_parts(line: dict, page_width: float) -> list[dict]:
    words = line.get("words", [])

    if not words:
        return [
            {
                "text": clean_text(line.get("text", "")),
                "x_center": float(line.get("x_center", 0)),
                "y_center": float(line.get("y_center", 0)),
            }
        ]

    line_width = float(line.get("x1", 0)) - float(line.get("x0", 0))

    if line_width < page_width * 0.45:
        return [
            {
                "text": clean_text(line.get("text", "")),
                "x_center": float(line.get("x_center", 0)),
                "y_center": float(line.get("y_center", 0)),
            }
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

    def build_part(part_words: list[tuple]) -> dict | None:
        if not part_words:
            return None

        part_words = sorted(part_words, key=lambda w: float(w[0]))
        text = clean_text(" ".join(clean_text(w[4]) for w in part_words if clean_text(w[4])))

        if not text:
            return None

        x0 = min(float(w[0]) for w in part_words)
        x1 = max(float(w[2]) for w in part_words)
        y0 = min(float(w[1]) for w in part_words)
        y1 = max(float(w[3]) for w in part_words)

        return {
            "text": text,
            "x_center": (x0 + x1) / 2,
            "y_center": (y0 + y1) / 2,
        }

    parts = [part for part in [build_part(left_words), build_part(right_words)] if part]

    if len(parts) >= 2:
        return parts

    return [
        {
            "text": clean_text(line.get("text", "")),
            "x_center": float(line.get("x_center", 0)),
            "y_center": float(line.get("y_center", 0)),
        }
    ]


def _summary_side_bucket(x: float, page_width: float) -> str:
    pct = x / max(page_width, 1) * 100

    if pct < 42:
        return "left"

    if pct > 58:
        return "right"

    return "center"


def _extract_summary_value_from_pdf_lines(
    lines: list[str],
    labels: list[str],
) -> tuple[str, str]:
    for idx, line in enumerate(lines):
        for label in labels:
            if label not in line:
                continue

            value = clean_text(line.split(label, 1)[1])

            if not value and idx + 1 < len(lines):
                value = clean_text(lines[idx + 1])

            if value.endswith("-") and idx + 1 < len(lines):
                next_value = clean_text(lines[idx + 1])

                if re.fullmatch(r"[A-Za-z0-9]+", next_value):
                    value = clean_text(value + next_value)

            return value, label

    return "", ""


def _extract_summary_items_from_pdf_page(pdf_path: Path) -> list[ManufacturingInfoItem]:
    try:
        import fitz
        from .manufacturing_stage_ui import _extract_stage_box_positions_from_pdf
    except Exception:
        return []

    if not pdf_path.exists():
        return []

    try:
        stage_positions = _extract_stage_box_positions_from_pdf(pdf_path)
    except Exception:
        stage_positions = []

    if not stage_positions:
        return []

    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []

    try:
        for page_index, page in enumerate(doc):
            page_text = clean_text(page.get_text("text") or "")

            if "제조요약도" not in page_text:
                continue

            page_width = float(page.rect.width)
            page_height = float(page.rect.height)
            lines = _group_pdf_words_to_lines(page.get_text("words") or [])
            parts: list[dict] = []

            for line in lines:
                parts.extend(_split_pdf_line_parts(line, page_width))

            out: list[ManufacturingInfoItem] = []
            stage_infos: list[dict] = []

            for stage in stage_positions:
                name = clean_text(stage.get("display_name", ""))
                key = _norm_match(name)

                if not name or not key:
                    continue

                x = float(stage.get("x_pct", 0)) / 100 * page_width
                y = float(stage.get("y_pct", 0)) / 100 * page_height
                bucket = _summary_side_bucket(x, page_width)
                stage_infos.append({"name": name, "key": key, "x": x, "y": y, "bucket": bucket})

            for stage in stage_infos:
                current_y = stage["y"]
                current_bucket = stage["bucket"]
                next_y = current_y + page_height * 0.11

                for other in stage_infos:
                    if other is stage:
                        continue

                    if other["bucket"] != current_bucket:
                        continue

                    if other["y"] > current_y:
                        next_y = min(next_y, other["y"] - 4)

                scoped_parts = []

                for part in parts:
                    y = float(part.get("y_center", 0))

                    if y <= current_y + 4 or y >= next_y:
                        continue

                    if _summary_side_bucket(float(part.get("x_center", 0)), page_width) != current_bucket:
                        continue

                    scoped_parts.append(part)

                scoped_parts.sort(key=lambda p: (float(p.get("y_center", 0)), float(p.get("x_center", 0))))
                scoped_lines = [clean_text(part.get("text", "")) for part in scoped_parts if clean_text(part.get("text", ""))]

                manufacturing_no, _ = _extract_summary_value_from_pdf_lines(scoped_lines, ["제조번호"])
                manufacturing_date, _ = _extract_summary_value_from_pdf_lines(scoped_lines, ["제조년월일", "제조일자"])
                quantity, quantity_label = _extract_summary_value_from_pdf_lines(
                    scoped_lines,
                    ["총 제조수량", "제조수량", "제조량", "신청수량"],
                )

                out.append(
                    ManufacturingInfoItem(
                        stage_name=stage["name"],
                        manufacturing_no=manufacturing_no,
                        manufacturing_date=manufacturing_date,
                        quantity=quantity,
                        quantity_field_name=quantity_label or "제조량",
                        page_number=page_index + 1,
                    )
                )

            if out:
                return out
    finally:
        doc.close()

    return []


def extract_summary_items_from_records(records_source: Any) -> list[ManufacturingInfoItem]:
    records = _records_from_source(records_source)

    out: list[ManufacturingInfoItem] = []
    seen: set[str] = set()

    for record in records:
        record_type = clean_text(_get(record, "record_type", ""))

        if record_type not in {"flowchart", "diagram"}:
            continue

        diagram_data = _get(record, "diagram_data", None) or {}

        if not isinstance(diagram_data, dict):
            continue

        nodes = diagram_data.get("nodes") or []

        if not isinstance(nodes, list):
            continue

        for node in nodes:
            if not isinstance(node, dict):
                continue

            stage_name = clean_text(node.get("name", ""))

            if not stage_name:
                continue

            fields = node.get("fields") or {}

            if not isinstance(fields, dict):
                fields = {}

            manufacturing_no = _field_lookup(fields, FIELD_ALIASES["제조번호"])
            manufacturing_date = _field_lookup(fields, FIELD_ALIASES["제조년월일"])
            quantity = _field_lookup(
                fields,
                FIELD_ALIASES["제조수량"] + FIELD_ALIASES["제조량"],
            )
            quantity_field_name = _quantity_field_name_from_fields(fields)

            key = _norm_match(stage_name)

            if key in seen:
                continue

            seen.add(key)

            out.append(
                ManufacturingInfoItem(
                    stage_name=stage_name,
                    manufacturing_no=manufacturing_no,
                    manufacturing_date=manufacturing_date,
                    quantity=quantity,
                    quantity_field_name=quantity_field_name,
                    page_number=_get(record, "page_start", None),
                )
            )

    try:
        raw_pdf_path = _get(records_source, "pdf_path", None)
        pdf_path = Path(raw_pdf_path) if raw_pdf_path else None

        if pdf_path and pdf_path.exists():
            pdf_items = _extract_summary_items_from_pdf_page(pdf_path)

            if pdf_items:
                pdf_by_key = {
                    _norm_match(item.stage_name): item
                    for item in pdf_items
                    if _norm_match(item.stage_name)
                }

                if out:
                    merged: list[ManufacturingInfoItem] = []
                    merged_keys: set[str] = set()

                    for item in out:
                        key = _norm_match(item.stage_name)
                        pdf_item = pdf_by_key.get(key)

                        if pdf_item is not None:
                            item = ManufacturingInfoItem(
                                stage_name=item.stage_name,
                                manufacturing_no=item.manufacturing_no or pdf_item.manufacturing_no,
                                manufacturing_date=item.manufacturing_date or pdf_item.manufacturing_date,
                                quantity=item.quantity or pdf_item.quantity,
                                quantity_field_name=(
                                    item.quantity_field_name
                                    or pdf_item.quantity_field_name
                                ),
                                page_number=item.page_number or pdf_item.page_number,
                            )

                        merged.append(item)
                        if key:
                            merged_keys.add(key)

                    for pdf_item in pdf_items:
                        key = _norm_match(pdf_item.stage_name)
                        if key and key not in merged_keys:
                            merged.append(pdf_item)
                            merged_keys.add(key)

                    return merged

                return pdf_items
    except Exception:
        pass

    return out


def _join_record_text(record: Any) -> str:
    parts: list[str] = []

    for key in [
        "section_number",
        "section_title",
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


def _clean_table_extracted_value(value: str | None) -> str:
    value = clean_text(value)

    if not value:
        return ""

    # OCR 표 추출 결과는 "제조번호 | | 26A01"처럼 빈 셀 구분자(|)가 남는 경우가 많다.
    # 제조번호 검증에서는 이 선행 구분자를 값으로 보면 안 된다.
    value = re.sub(r"^[\s|:：]+", "", value)
    value = re.sub(r"[\s|:：]+$", "", value)

    # 여전히 파이프가 남아 있으면 마지막 의미 셀을 값으로 본다.
    # 예: "제조번호 | | 26A01"의 캡처값이 "| 26A01"인 경우 -> "26A01"
    if "|" in value:
        parts = [clean_text(part) for part in value.split("|") if clean_text(part)]
        if parts:
            value = parts[-1]

    return clean_text(value)


def _split_lines(text: str | None) -> list[str]:
    text = clean_text(text)

    if not text:
        return []

    return [
        clean_text(line)
        for line in text.splitlines()
        if clean_text(line)
    ]


def _extract_value_after_label_from_lines(
    lines: list[str],
    label_patterns: list[str],
    value_kind: str,
) -> str:
    label_re = "|".join(label_patterns)

    def is_value(candidate: str) -> bool:
        candidate = clean_text(candidate)

        if not candidate:
            return False

        if value_kind == "code":
            # 제조번호는 B2604, 26A01처럼 하이픈이 없는 코드도 정상 값이다.
            # 기존 정규식은 P26-1처럼 하이픈이 있는 코드만 허용해서
            # 최종원액(B2604), 완제의약품(26A01)을 본문에서 못 뽑는 문제가 있었다.
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/\-－–—]{1,40}", candidate):
                return False

            # 날짜/수량/일반 라벨이 제조번호로 들어가는 것을 방지한다.
            if (
                _normalize_date_parts(candidate) != ("", "", "")
                and not re.search(r"[A-Za-z]", candidate)
            ):
                return False

            if _looks_like_quantity_only(candidate):
                return False

            if re.search(r"(제조|년월|일자|수량|제조원|동국|대한|서울|혈액|바이알|병|확인|서명)", candidate):
                return False

            return True

        if value_kind == "date":
            return _normalize_date_parts(candidate) != ("", "", "")

        if value_kind == "quantity":
            num, unit = _normalize_quantity(candidate)
            return bool(num and unit)

        return bool(candidate)

    # 1) 같은 줄: 제조년월일 2025년 1월 1일 / 제조년월일 | 2025.01.01
    for idx, line in enumerate(lines):
        m = re.search(
            rf"(?:{label_re})\s*(?:\||:|：)?\s*(.+)$",
            line,
            flags=re.I,
        )

        if not m:
            continue

        value = _clean_table_extracted_value(m.group(1))

        if value.endswith("-") and idx + 1 < len(lines):
            next_line = clean_text(lines[idx + 1])

            if re.fullmatch(r"[A-Za-z0-9]+", next_line):
                value = clean_text(value + next_line)

        if is_value(value):
            return value

    # 2) 라벨 단독 줄 다음 값
    for idx, line in enumerate(lines):
        compact = re.sub(r"\s+", "", line)

        matched = False

        for pattern in label_patterns:
            pattern_compact = re.sub(r"[^가-힣A-Za-z0-9]", "", pattern)

            if compact == pattern_compact:
                matched = True
                break

        if not matched:
            continue

        for j in range(idx + 1, min(idx + 4, len(lines))):
            candidate = _clean_table_extracted_value(lines[j])

            if re.search(r"(제조번호|제조년월일|제조량|제조수량|총\s*제조수량|신청수량)", candidate):
                break

            if candidate.endswith("-") and j + 1 < len(lines):
                next_line = clean_text(lines[j + 1])

                if re.fullmatch(r"[A-Za-z0-9]+", next_line):
                    candidate = clean_text(candidate + next_line)

            if is_value(candidate):
                return candidate

    return ""


def _is_manufacturing_code_value(candidate: str) -> bool:
    candidate = clean_text(candidate)

    if not candidate:
        return False

    # B2604, 26A01, F26-1, P26-1 모두 허용한다.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/\-－–—]{1,40}", candidate):
        return False

    # 제조번호는 식별을 위한 숫자를 포함해야 한다. Pooling, Information 같은
    # 표 머리글을 제조번호로 오인하지 않는다.
    if not re.search(r"\d", candidate):
        return False

    if re.fullmatch(r"\d+\s*/\s*\d+", candidate):
        return False

    # 날짜/수량/라벨/회사명류를 제조번호로 오인하지 않는다.
    if (
        _normalize_date_parts(candidate) != ("", "", "")
        and not re.search(r"[A-Za-z]", candidate)
    ):
        return False

    if _looks_like_quantity_only(candidate):
        return False

    if re.search(r"(제조|년월|일자|수량|제조원|동국|대한|서울|혈액|바이알|병|확인|서명)", candidate):
        return False

    return True


def _extract_field_value_from_text(text: str, field_name: str) -> str:
    lines = _split_lines(text)

    if not lines:
        return ""

    if field_name == "제조번호":
        for idx, line in enumerate(lines):
            if re.search(r"(제조번호|제\s*조\s*번호)", line) and idx > 0:
                candidate = clean_text(lines[idx - 1])
                if _is_manufacturing_code_value(candidate):
                    return candidate

        value = _extract_value_after_label_from_lines(
            lines,
            [r"제조번호", r"제\s*조\s*번호"],
            "code",
        )

        if value:
            return value

        # fallback: 라벨 주변의 코드값을 직접 추출한다.
        # 문서 머리글의 "제조번호 : 3/21 페이지" 같은 첫 번째 오탐값 때문에
        # 실제 본문 값(B2604, 26A01)을 놓치지 않도록 모든 후보를 순회한다.
        for m in re.finditer(
            r"(?:제조번호|제\s*조\s*번호)\s*(?:\||:|：)?\s*([A-Za-z0-9][A-Za-z0-9._/\-－–—]{1,40})",
            text,
            flags=re.I,
        ):
            candidate = clean_text(m.group(1))

            if _is_manufacturing_code_value(candidate):
                return candidate

        return ""

    if field_name == "제조년월일":
        for idx, line in enumerate(lines):
            if re.search(r"(제조년월일|제조년원일|제조일자|제조일|제조\s*년월\s*일)", line) and idx > 0:
                candidate = clean_text(lines[idx - 1])
                if (
                    _normalize_date_parts(candidate) != ("", "", "")
                    and re.fullmatch(rf"\s*{_date_regex()}\s*\.?", candidate)
                ):
                    return candidate

        value = _extract_value_after_label_from_lines(
            lines,
            [r"제조년월일", r"제조년원일", r"제조일자", r"제조일", r"제조\s*년월\s*일"],
            "date",
        )

        if value:
            return value

        m = re.search(
            rf"(?:제조년월일|제조년원일|제조일자|제조일|제조\s*년월\s*일)\s*(?:\||:|：)?\s*({_date_regex()})",
            text,
            flags=re.I,
        )

        if m:
            return clean_text(m.group(1))

        return ""

    if field_name in {"제조량", "제조수량", "총 제조수량"}:
        if field_name == "제조량":
            label_patterns = [r"제\s*조\s*량", r"제조량"]
        else:
            label_patterns = [
                r"총\s*제조수량",
                r"총제조수량",
                r"제조수량",
                r"신청수량",
            ]

        label_re = "|".join(label_patterns)
        for idx, line in enumerate(lines):
            if re.search(rf"(?:{label_re})", line) and idx > 0:
                candidate = clean_text(lines[idx - 1])
                number, unit = _normalize_quantity(candidate)
                if number and unit:
                    return candidate

        value = _extract_value_after_label_from_lines(
            lines,
            label_patterns,
            "quantity",
        )

        if value:
            return value

        m = re.search(
            rf"(?:{label_re})\s*(?:\||:|：)?\s*({_quantity_regex()})",
            text,
            flags=re.I,
        )

        if m:
            return clean_text(m.group(1))

        return ""

    return ""


def _extract_expiry_value_from_text(text: str) -> str:
    lines = _split_lines(text)
    if not lines:
        return ""

    label_re = (
        r"사용\s*\(\s*유효\s*\)\s*기간"
        r"|사용\s*기간|유효\s*기간|사용\s*기한|유효\s*기한"
    )
    for idx, line in enumerate(lines):
        match = re.search(
            rf"(?:{label_re})\s*(?:\||:|：)?\s*([^|\n]+)$",
            line,
            flags=re.I,
        )
        if match:
            value = _clean_table_extracted_value(match.group(1))
            if value and not re.fullmatch(rf"(?:{label_re})", value, flags=re.I):
                return value

        if not re.search(rf"(?:{label_re})", line, flags=re.I):
            continue
        for candidate_idx in range(idx + 1, min(idx + 4, len(lines))):
            candidate = _clean_table_extracted_value(lines[candidate_idx])
            if not candidate:
                continue
            if re.search(
                r"(제조번호|제조년월일|제조일자|제조량|제조수량|저장방법|보관조건)",
                candidate,
            ):
                break
            return candidate

    return ""


def _normalize_expiry_value(value: str | None) -> str:
    value = clean_text(value)
    if not value:
        return ""

    year, month, day = _normalize_date_parts(value)
    if year and re.search(r"20\d{2}", value):
        normalized_date = year
        if month:
            normalized_date += f"-{month}"
        if day:
            normalized_date += f"-{day}"
        # 명시적인 만료일 표기는 구두점과 한글 날짜 표기 차이를 무시한다.
        if not re.search(r"(개월|년\s*간|일부터|일로부터|제조일)", value):
            return f"date:{normalized_date}"

    normalized = value.casefold()
    normalized = normalized.replace("제조년월일", "제조일")
    normalized = normalized.replace("제조일자", "제조일")
    normalized = normalized.replace("유효기한", "유효기간")
    normalized = normalized.replace("사용기한", "사용기간")
    normalized = re.sub(r"[\s\-_·.,:：/(){}\[\]<>~～]+", "", normalized)
    return normalized


def _normalize_date_value(value: str | None) -> str:
    year, month, day = _normalize_date_parts(value)
    if not year:
        return ""
    return "-".join(part for part in [year, month, day] if part)


def _flatten_table_lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [clean_text(value)] if clean_text(value) else []
    if isinstance(value, dict):
        scalar_values = [
            clean_text(item)
            for item in value.values()
            if not isinstance(item, (dict, list, tuple)) and clean_text(item)
        ]
        nested: list[str] = []
        for item in value.values():
            if isinstance(item, (dict, list, tuple)):
                nested.extend(_flatten_table_lines(item))
        return ([" | ".join(scalar_values)] if scalar_values else []) + nested
    if isinstance(value, (list, tuple)):
        lines: list[str] = []
        for item in value:
            if isinstance(item, (list, tuple)):
                cells = [clean_text(cell) for cell in item]
                if any(cells):
                    lines.append(" | ".join(cells))
            else:
                lines.extend(_flatten_table_lines(item))
        return lines
    text = clean_text(value)
    return [text] if text else []


def _record_text_with_tables(record: Any) -> str:
    parts = [_join_record_text(record)]
    for key in ["result_table", "tables", "table_data"]:
        parts.extend(_flatten_table_lines(_get(record, key, None)))
    return "\n".join(part for part in parts if clean_text(part))


def _header_index(cells: list[str], aliases: list[str]) -> int | None:
    normalized_aliases = {_norm_match(alias) for alias in aliases}
    for idx, cell in enumerate(cells):
        cell_key = _norm_match(cell)
        if any(alias and alias in cell_key for alias in normalized_aliases):
            return idx
    return None


def _extract_table_manufacturing_no_occurrences(
    record: Any,
    text: str,
) -> list[ManufacturingNoOccurrence]:
    source_label = _record_source_label(record)
    section_number = clean_text(_get(record, "section_number", ""))
    page_number = _get(record, "page_start", None)
    header_cells: list[str] = []
    out: list[ManufacturingNoOccurrence] = []

    for line in _split_lines(text):
        if "|" not in line:
            continue
        cells = [_clean_table_extracted_value(cell) for cell in re.split(r"\s*\|\s*", line)]
        code_idx = _header_index(cells, ["제조번호"])
        if code_idx is not None:
            header_cells = cells
            continue
        if not header_cells:
            continue

        code_idx = _header_index(header_cells, ["제조번호"])
        date_idx = _header_index(header_cells, ["제조년월일", "제조일자", "제조일"])
        expiry_idx = _header_index(header_cells, EXPIRY_FIELD_ALIASES)
        if code_idx is None or code_idx >= len(cells):
            continue
        manufacturing_no = clean_text(cells[code_idx])
        if not _is_manufacturing_code_value(manufacturing_no):
            continue

        out.append(
            ManufacturingNoOccurrence(
                manufacturing_no=manufacturing_no,
                manufacturing_date=(
                    clean_text(cells[date_idx])
                    if date_idx is not None and date_idx < len(cells)
                    else ""
                ),
                expiry_period=(
                    clean_text(cells[expiry_idx])
                    if expiry_idx is not None and expiry_idx < len(cells)
                    else ""
                ),
                source_label=source_label,
                section_number=section_number,
                page_number=page_number,
            )
        )

    return out


def _extract_text_manufacturing_no_occurrences(
    record: Any,
    text: str,
) -> list[ManufacturingNoOccurrence]:
    table_items = _extract_table_manufacturing_no_occurrences(record, text)
    if table_items:
        return table_items

    lines = _split_lines(text)
    source_label = _record_source_label(record)
    section_number = clean_text(_get(record, "section_number", ""))
    page_number = _get(record, "page_start", None)
    out: list[ManufacturingNoOccurrence] = []

    code_line_indexes = [
        idx
        for idx, line in enumerate(lines)
        if re.search(r"(?:제조번호|제\s*조\s*번호)", line)
    ]
    for idx in code_line_indexes:
        # A referenced input lot must not inherit the next (output) lot's date.
        # Bind labels only until the next explicit lot label, never its fields.
        end = next((other for other in code_line_indexes if other > idx), len(lines))
        window = "\n".join(lines[idx : min(end, idx + 8)])
        manufacturing_no = ""
        direct_match = re.search(
            r"(?:제조번호|제\s*조\s*번호)\s*(?:\||:|：)?\s*([A-Za-z0-9][A-Za-z0-9._/\-－–—]{1,40})",
            lines[idx],
            flags=re.I,
        )
        if direct_match:
            candidate = clean_text(direct_match.group(1))
            if _is_manufacturing_code_value(candidate):
                manufacturing_no = candidate
        if not manufacturing_no:
            for candidate_idx in range(idx + 1, min(idx + 4, len(lines))):
                candidate = _clean_table_extracted_value(lines[candidate_idx])
                if _is_manufacturing_code_value(candidate):
                    manufacturing_no = candidate
                    break
        if not _is_manufacturing_code_value(manufacturing_no):
            continue
        out.append(
            ManufacturingNoOccurrence(
                manufacturing_no=manufacturing_no,
                manufacturing_date=_extract_field_value_from_text(window, "제조년월일"),
                expiry_period=_extract_expiry_value_from_text(window),
                source_label=source_label,
                section_number=section_number,
                page_number=page_number,
            )
        )

    return out


def collect_manufacturing_no_occurrences(records_source: Any) -> list[ManufacturingNoOccurrence]:
    records = _records_from_source(records_source)
    out: list[ManufacturingNoOccurrence] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()

    def add(item: ManufacturingNoOccurrence) -> None:
        code_key = _normalize_code(item.manufacturing_no)
        if not code_key:
            return
        key = (
            code_key,
            "-".join(_normalize_date_parts(item.manufacturing_date)),
            _normalize_expiry_value(item.expiry_period),
            clean_text(item.source_label),
            clean_text(item.section_number),
            clean_text(str(item.page_number or "")),
        )
        if key in seen:
            return
        seen.add(key)
        out.append(item)

    for record in records:
        record_type = clean_text(_get(record, "record_type", ""))
        if record_type in {"flowchart", "diagram"}:
            diagram_data = _get(record, "diagram_data", None) or {}
            nodes = diagram_data.get("nodes") if isinstance(diagram_data, dict) else []
            for node in nodes or []:
                if not isinstance(node, dict):
                    continue
                fields = node.get("fields") or {}
                if not isinstance(fields, dict):
                    continue
                manufacturing_no = _field_lookup(fields, FIELD_ALIASES["제조번호"])
                if not _is_manufacturing_code_value(manufacturing_no):
                    continue
                stage_name = clean_text(node.get("name", "")) or "제조요약도"
                add(
                    ManufacturingNoOccurrence(
                        manufacturing_no=manufacturing_no,
                        manufacturing_date=_field_lookup(fields, FIELD_ALIASES["제조년월일"]),
                        expiry_period=_field_lookup(fields, EXPIRY_FIELD_ALIASES),
                        source_label=f"제조요약도 · {stage_name}",
                        section_number=clean_text(_get(record, "section_number", "")),
                        page_number=_get(record, "page_start", None),
                    )
                )
            continue

        text = _record_text_with_tables(record)
        if not text:
            continue
        for item in _extract_text_manufacturing_no_occurrences(record, text):
            add(item)

    return out


def _build_same_manufacturing_no_validation(
    records_source: Any,
) -> ManufacturingInfoValidationResult | None:
    occurrences = collect_manufacturing_no_occurrences(records_source)
    grouped: dict[str, list[ManufacturingNoOccurrence]] = {}
    for occurrence in occurrences:
        grouped.setdefault(_normalize_code(occurrence.manufacturing_no), []).append(occurrence)

    field_results: list[FieldCompareResult] = []
    compared_occurrences: set[tuple[str, str, int | None]] = set()
    for code_key, code_occurrences in grouped.items():
        if len(code_occurrences) < 2:
            continue
        display_code = clean_text(code_occurrences[0].manufacturing_no) or code_key
        for field_name, value_attr, normalize in [
            ("제조년월일", "manufacturing_date", _normalize_date_value),
            ("사용(유효)기간", "expiry_period", _normalize_expiry_value),
        ]:
            entries: list[tuple[ManufacturingNoOccurrence, str, str]] = []
            for occurrence in code_occurrences:
                raw_value = clean_text(getattr(occurrence, value_attr, ""))
                normalized_value = normalize(raw_value) if raw_value else ""
                if raw_value and normalized_value:
                    entries.append((occurrence, raw_value, normalized_value))
            if len(entries) < 2:
                continue

            unique_values: dict[str, list[tuple[ManufacturingNoOccurrence, str]]] = {}
            for occurrence, raw_value, normalized_value in entries:
                unique_values.setdefault(normalized_value, []).append((occurrence, raw_value))
                compared_occurrences.add(
                    (occurrence.source_label, occurrence.section_number, occurrence.page_number)
                )

            baseline_occurrence, baseline_value, baseline_key = entries[0]
            mismatch = len(unique_values) > 1
            comparison_text = " / ".join(
                f"{raw_value} ({occurrence.source_label})"
                for occurrence, raw_value, _ in entries[1:]
            ) or f"{baseline_value} ({baseline_occurrence.source_label})"
            conflict_entry = next(
                (
                    (occurrence, raw_value)
                    for occurrence, raw_value, normalized_value in entries[1:]
                    if normalized_value != baseline_key
                ),
                (baseline_occurrence, baseline_value),
            )
            conflict_occurrence, _ = conflict_entry
            field_results.append(
                FieldCompareResult(
                    field_name=f"{display_code} {field_name}",
                    summary_value=f"{baseline_value} ({baseline_occurrence.source_label})",
                    document_value=comparison_text,
                    status="불합격" if mismatch else "합격",
                    reason=(
                        f"동일 제조번호 {display_code}에 연결된 {field_name} 값이 문서 위치별로 다릅니다."
                        if mismatch
                        else f"동일 제조번호 {display_code}에 연결된 {field_name} 값이 모든 확인 위치에서 일치합니다."
                    ),
                    source_label="문서 전체 동일 제조번호 비교",
                    section_number=conflict_occurrence.section_number,
                    page_number=conflict_occurrence.page_number,
                    baseline_label="기준 위치",
                    comparison_label="비교 위치",
                )
            )

    if not field_results:
        return None
    status = "불합격" if any(field.status == "불합격" for field in field_results) else "합격"
    return ManufacturingInfoValidationResult(
        stage_name="동일 제조번호 문서 전체 정합성",
        status=status,
        fields=field_results,
        source_count=len(compared_occurrences),
    )



class ManufacturingInfoLlmExtractResponse(BaseModel):
    """LLM 기반 제조요약도-본문 정보 매핑 결과."""
    matched: bool = Field(description="제조요약도 항목과 본문 후보 중 대응 항목을 찾았는지 여부")
    matched_stage_name: str | None = None
    manufacturing_no: str | None = None
    manufacturing_date: str | None = None
    quantity: str | None = None
    quantity_field_name: str | None = None
    confidence: float = Field(default=0.0, description="0~1 사이 신뢰도")
    reason: str = ""


class ManufacturingInfoLlmCompareResponse(BaseModel):
    """LLM 기반 제조번호/제조년월일/제조량 비교 결과."""
    status: str = Field(description="합격, 불합격, 또는 보류")
    normalized_summary_value: str | None = None
    normalized_document_value: str | None = None
    reason: str = ""
    confidence: float = Field(default=0.0, description="0~1 사이 신뢰도")


def _get_mfg_llm_client():
    if not CLOVA_API_KEY:
        return None

    try:
        return create_clova_client(CLOVA_API_KEY, CLOVA_BASE_URL)
    except Exception:
        return None


def _record_page_label(record: Any) -> str:
    page = _get(record, "page_start", None)
    section = clean_text(_get(record, "section_number", ""))
    title = clean_text(_get(record, "section_title", "")) or clean_text(_get(record, "content_label", ""))

    parts = []
    if page is not None:
        parts.append(f"p.{page}")
    if section:
        parts.append(section)
    if title:
        parts.append(title)

    return " ".join(parts) or "본문 후보"


def _record_candidate_score_for_llm(summary_item: ManufacturingInfoItem, record: Any) -> int:
    text = _join_record_text(record)

    if not text:
        return -999

    key = _norm_match(text)
    stage_key = _norm_match(summary_item.stage_name)
    score = 0

    if any(label in text for label in ["제조번호", "제조년월일", "제조일자", "제조량", "제조수량", "총 제조수량", "신청수량", "Pooling", "일자", "사용량"]):
        score += 25

    if stage_key and stage_key in key:
        score += 80

    for token in re.findall(r"[A-Za-z가-힣0-9]+", summary_item.stage_name):
        if len(token) >= 2 and _norm_match(token) in key:
            score += 10

    for value in [summary_item.manufacturing_no, summary_item.manufacturing_date, summary_item.quantity]:
        value = clean_text(value)
        if value and _norm_match(value) in key:
            score += 20

    # 날짜/수량은 포맷 차이가 심하므로 정규화값이 있으면 별도 가점
    sy, sm, sd = _normalize_date_parts(summary_item.manufacturing_date)
    if sy and sy in text:
        score += 4
    if sm and sm in text:
        score += 4
    if sd and sd in text:
        score += 4

    q_num, q_unit = _normalize_quantity(summary_item.quantity)
    if q_num and q_num in text.replace(",", ""):
        score += 8
    if q_unit and q_unit in _normalize_quantity(text)[1]:
        score += 2

    record_type = clean_text(_get(record, "record_type", ""))
    if record_type in {"flowchart", "diagram", "test"}:
        score -= 40

    return score


def _build_document_mapping_candidates(
    summary_item: ManufacturingInfoItem,
    records: list[Any],
    *,
    limit: int = 18,
) -> str:
    scored: list[tuple[int, int, str]] = []

    for idx, record in enumerate(records):
        record_type = clean_text(_get(record, "record_type", ""))

        if record_type in {"flowchart", "diagram"}:
            continue

        score = _record_candidate_score_for_llm(summary_item, record)

        if score <= 0:
            continue

        text = _join_record_text(record)
        if not text:
            continue

        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) > 2400:
            text = text[:2400] + " ..."

        label = _record_page_label(record)
        scored.append((score, -idx, f"[JSON 후보 {len(scored)+1}] {label}\n{text}"))

    scored.sort(reverse=True)

    return "\n\n---\n\n".join(item for _, _, item in scored[:limit])



def _llm_extract_document_item_for_stage(
    summary_item: ManufacturingInfoItem,
    records: list[Any],
) -> ManufacturingInfoItem | None:
    client = _get_mfg_llm_client()

    if client is None:
        return None

    candidates_text = _build_document_mapping_candidates(summary_item, records)

    if not candidates_text:
        return None

    prompt = f"""
당신은 의약품 제조 및 품질관리요약서의 제조요약도와 본문 정보 섹션을 대조하는 검수 보조 모델입니다.

목표:
- 제조요약도 항목 1개가 05_records.json 안의 본문 후보 중 어느 항목과 대응되는지 찾습니다.
- 대응되는 본문 항목에서 제조번호, 제조년월일, 제조량/제조수량 값을 추출합니다.
- 단순 문자열 일치가 아니라 의미 매핑을 수행합니다.

중요 원칙:
- 원료혈장1/원료혈장2/원료혈장3처럼 제조요약도에서 나뉜 항목은 본문 표의 행과 매핑할 수 있습니다.
- 최종원액, 원획분, 완제의약품처럼 단계명이 다르면 같은 단계의 정보 섹션과 매핑합니다.
- 제조번호는 하이픈, 약어, OCR 누락이 있을 수 있으므로 주변 제조년월일/제조량과 함께 판단합니다.
- 제조년월일은 YYYY.MM.DD, YYYY. MM. DD., YYYY년 M월 D일 등 형식 차이를 같은 날짜로 봅니다.
- 제조량/제조수량은 5,000 바이알, 5000병, 5000 vial처럼 단위 표현 차이를 고려합니다.
- 그래도 대응 본문을 찾을 수 없으면 matched=false로 답하세요.

제조요약도 항목:
- 단계명: {summary_item.stage_name}
- 제조번호: {summary_item.manufacturing_no}
- 제조년월일: {summary_item.manufacturing_date}
- {summary_item.quantity_field_name or '제조량'}: {summary_item.quantity}

JSON 본문 후보:
{candidates_text}

출력은 JSON만 반환하세요.
""".strip()

    try:
        data = request_structured_response(
            client=client,
            model=DEFAULT_CLOVA_MODEL,
            prompt=prompt,
            response_model=ManufacturingInfoLlmExtractResponse,
            max_completion_tokens=CLOVA_MAX_COMPLETION_TOKENS,
        )

        if not data.matched or data.confidence < 0.55:
            return None

        return ManufacturingInfoItem(
            stage_name=summary_item.stage_name,
            manufacturing_no=clean_text(data.manufacturing_no),
            manufacturing_date=clean_text(data.manufacturing_date),
            quantity=clean_text(data.quantity),
            quantity_field_name=clean_text(data.quantity_field_name) or summary_item.quantity_field_name or "제조량",
        )
    except Exception as exc:
        print(f"[MFG_INFO_LLM_EXTRACT_ERROR] {exc}")
        return None


def _llm_compare_manufacturing_field(
    *,
    stage_name: str,
    field_name: str,
    summary_item: ManufacturingInfoItem,
    document_item: ManufacturingInfoItem,
    original_result: FieldCompareResult,
) -> FieldCompareResult:
    client = _get_mfg_llm_client()

    if client is None:
        return original_result

    prompt = f"""
당신은 의약품 제조요약도 정보 일치성 검증 모델입니다.
다음 제조요약도 값과 본문 정보 값을 비교하여 합격/불합격/보류 중 하나로 판단하세요.

검증 대상 단계명: {stage_name}
검증 필드: {field_name}

제조요약도 전체 값:
- 제조번호: {summary_item.manufacturing_no}
- 제조년월일: {summary_item.manufacturing_date}
- {summary_item.quantity_field_name or '제조량'}: {summary_item.quantity}

본문 매핑 전체 값:
- 제조번호: {document_item.manufacturing_no}
- 제조년월일: {document_item.manufacturing_date}
- {document_item.quantity_field_name or '제조량'}: {document_item.quantity}

비교 원칙:
- 제조번호는 하이픈/공백/OCR 줄바꿈 차이만 보정합니다.
- 제조번호 값 자체가 다르면 같은 단계로 매핑되더라도 해당 필드는 불합격으로 판단합니다.
- 제조년월일은 날짜 형식 차이만 있으면 같은 값으로 봅니다.
- 제조량과 제조수량은 5,000 바이알, 5000병, 5000 vial 등 같은 개수 단위 표현 차이를 고려합니다.
- 원료혈장처럼 제조번호가 약어로 쓰인 경우에도, 명시적인 동일 코드 근거가 없으면 제조번호 필드는 합격으로 만들지 않습니다.
- 단계 매핑과 필드값 일치 여부를 혼동하지 마세요. 같은 단계로 매핑되어도 제조번호/일자/수량 중 값이 다르면 그 필드는 불합격입니다.
- 확실한 경우만 합격으로 판단하세요.

기존 규칙 판정:
- 상태: {original_result.status}
- 이유: {original_result.reason}
- 제조요약도 값: {original_result.summary_value}
- 본문 값: {original_result.document_value}

출력은 JSON만 반환하세요.
""".strip()

    try:
        data = request_structured_response(
            client=client,
            model=DEFAULT_CLOVA_MODEL,
            prompt=prompt,
            response_model=ManufacturingInfoLlmCompareResponse,
            max_completion_tokens=CLOVA_MAX_COMPLETION_TOKENS,
        )

        status = clean_text(data.status)

        if status not in {"합격", "불합격", "보류"}:
            return original_result

        if data.confidence < 0.55:
            return original_result

        return FieldCompareResult(
            field_name=field_name,
            summary_value=clean_text(data.normalized_summary_value) or original_result.summary_value,
            document_value=clean_text(data.normalized_document_value) or original_result.document_value,
            status=status,
            reason=clean_text(data.reason) or original_result.reason,
        )
    except Exception as exc:
        print(f"[MFG_INFO_LLM_COMPARE_ERROR] {exc}")
        return original_result


def _merge_document_items(
    base_item: ManufacturingInfoItem | None,
    llm_item: ManufacturingInfoItem | None,
) -> ManufacturingInfoItem | None:
    if base_item is None:
        return llm_item

    if llm_item is None:
        return base_item

    return ManufacturingInfoItem(
        stage_name=base_item.stage_name,
        manufacturing_no=llm_item.manufacturing_no or base_item.manufacturing_no,
        manufacturing_date=llm_item.manufacturing_date or base_item.manufacturing_date,
        quantity=llm_item.quantity or base_item.quantity,
        quantity_field_name=llm_item.quantity_field_name or base_item.quantity_field_name,
        page_number=base_item.page_number,
    )


def _compare_field_with_llm_fallback(
    field_name: str,
    summary_item: ManufacturingInfoItem,
    document_item: ManufacturingInfoItem,
) -> FieldCompareResult:
    if field_name == "제조번호":
        summary_value = summary_item.manufacturing_no
        document_value = document_item.manufacturing_no
    elif field_name == "제조년월일":
        summary_value = summary_item.manufacturing_date
        document_value = document_item.manufacturing_date
    else:
        summary_value = summary_item.quantity
        document_value = document_item.quantity

    base_result = _compare_field(
        field_name,
        summary_value,
        document_value,
    )

    if base_result.status == "합격":
        return base_result

    return _llm_compare_manufacturing_field(
        stage_name=summary_item.stage_name,
        field_name=field_name,
        summary_item=summary_item,
        document_item=document_item,
        original_result=base_result,
    )

def _fallback_section_numbers_for_stage(stage_name: str) -> list[str]:
    key = _norm_match(stage_name)

    is_cho_or_chol = ("cho" in key or "chol" in key) and "ecoli" not in key
    is_ecoli = "ecoli" in key

    if is_cho_or_chol and "마스터세포주" in key:
        return ["2.1.1.1.1"]

    if is_ecoli and "마스터세포주" in key:
        return ["2.1.1.1.2"]

    if is_cho_or_chol and "제조용세포주" in key:
        return ["2.1.1.2.1"]

    if is_ecoli and "제조용세포주" in key:
        return ["2.1.1.2.2"]

    if "componenta" in key:
        return ["3.1.1"]

    if "componentb" in key:
        return ["3.1.2"]

    if "나노" in key:
        return ["3.1.3"]

    # 최종원액은 4.1 정보가 본문 기준이고, 1.2 제조요약정보는 보조 기준이다.
    # 5.1의 "사용된 최종원액"은 완제 제조에 사용된 원액 번호이므로 여기서 참조하면 안 된다.
    if "최종원액" in key:
        return ["4.1", "1.2"]

    # 완제의약품은 1.2 제조요약정보 또는 1.1 신청제품정보를 기준으로 본다.
    # 5.1은 OCR 순서가 뒤틀리면 사용된 최종원액 번호(B2049/B2604)를 완제 제조번호로 오인하므로 제외한다.
    if "완제의약품" in key:
        return ["1.2", "1.1"]

    return []


def _section_score_for_stage(stage_name: str, record: Any) -> int:
    record_type = clean_text(_get(record, "record_type", ""))

    if record_type in {"flowchart", "diagram", "test"}:
        return -999

    text = _join_record_text(record)

    if not text:
        return -999

    if not any(label in text for label in ["제조번호", "제조년월일", "제조량", "제조수량", "신청수량", "총 제조수량"]):
        return -999

    stage_key = _norm_match(stage_name)
    text_key = _norm_match(text)
    section_number = clean_text(_get(record, "section_number", ""))

    score = 0

    if stage_key and stage_key in text_key:
        score += 100

    fallback_sections = _fallback_section_numbers_for_stage(stage_name)

    if section_number in fallback_sections:
        score += 90 - fallback_sections.index(section_number)

    if record_type == "content":
        score += 20

    if "정보" in _norm_match(clean_text(_get(record, "section_title", "")) + clean_text(_get(record, "content_label", ""))):
        score += 10

    return score


def _find_document_record_for_stage(stage_name: str, records: list[Any]) -> Any | None:
    candidates: list[tuple[int, int, Any]] = []

    for idx, record in enumerate(records):
        score = _section_score_for_stage(stage_name, record)

        if score <= 0:
            continue

        order = int(_get(record, "order_index", _get(record, "order_idx", idx)) or idx)
        candidates.append((score, -order, record))

    if not candidates:
        return None

    candidates.sort(reverse=True)

    return candidates[0][2]


def _extract_document_item_from_record(stage_name: str, record: Any) -> ManufacturingInfoItem:
    text = _join_record_text(record)

    manufacturing_no = _extract_field_value_from_text(text, "제조번호")
    manufacturing_date = _extract_field_value_from_text(text, "제조년월일")

    quantity = _extract_field_value_from_text(text, "제조수량")

    if not quantity:
        quantity = _extract_field_value_from_text(text, "제조량")

    quantity_field_name = "제조수량" if any(x in text for x in ["제조수량", "총 제조수량", "신청수량"]) else "제조량"

    return ManufacturingInfoItem(
        stage_name=stage_name,
        manufacturing_no=manufacturing_no,
        manufacturing_date=manufacturing_date,
        quantity=quantity,
        quantity_field_name=quantity_field_name,
        page_number=_get(record, "page_start", None),
    )



def _record_text_contains_manufacturing_fields(record: Any) -> bool:
    text = _join_record_text(record)
    return bool(
        text
        and any(
            label in text
            for label in [
                "제조번호",
                "제조년월일",
                "제조년원일",
                "제조량",
                "제조수량",
                "총 제조수량",
                "신청수량",
                "Pooling",
                "식별번호",
                "충진량",
            ]
        )
    )


def _section_number_starts(record: Any, prefix: str) -> bool:
    section_number = clean_text(_get(record, "section_number", ""))
    return section_number == prefix or section_number.startswith(prefix + ".")


def _find_record_by_section(records: list[Any], section_numbers: list[str]) -> Any | None:
    """
    section_number가 같은 레코드가 heading/content로 여러 개 존재할 수 있으므로,
    단순히 첫 번째 레코드를 반환하지 않는다.

    기존에는 4.1, 5.1 같은 섹션에서 heading 레코드를 먼저 잡아 값이 비거나,
    이후 fallback이 1.2 제조요약정보를 잘못 잡아 최종원액 제조번호가 불일치로 표시될 수 있었다.
    여기서는 제조번호/제조년월일/제조량/제조수량을 포함한 content 레코드를 우선한다.
    """
    for section in section_numbers:
        candidates: list[tuple[int, int, Any]] = []

        for idx, record in enumerate(records):
            if clean_text(_get(record, "record_type", "")) in {"flowchart", "diagram", "test"}:
                continue

            section_number = clean_text(_get(record, "section_number", ""))

            if section_number != section:
                continue

            record_type = clean_text(_get(record, "record_type", ""))
            text = _join_record_text(record)
            order = int(_get(record, "order_index", _get(record, "order_idx", idx)) or idx)

            score = 0

            if record_type == "content":
                score += 100

            if _record_text_contains_manufacturing_fields(record):
                score += 100

            if text:
                score += 10

            candidates.append((score, -order, record))

        if candidates:
            candidates.sort(reverse=True)
            best = candidates[0][2]

            if _join_record_text(best):
                return best

    return None


def _code_regex(value: str | None) -> str:
    code = clean_text(value)

    if not code:
        return ""

    parts = re.split(r"[-－–—\s]+", code)
    parts = [re.escape(part) for part in parts if part]

    if not parts:
        return re.escape(code)

    return r"\s*[-－–—]?\s*".join(parts)


def _date_regex() -> str:
    return r"20\d{2}\s*[.\-/]\s*\d{1,2}\s*[.\-/]\s*\d{1,2}\.?"


def _quantity_regex() -> str:
    return r"[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:L|l|mL|ml|kg|KG|Kg|g|G|병|바이알|vial|Vial|개|EA|ea)?"


def _append_unit_if_missing(quantity: str, default_unit: str) -> str:
    quantity = clean_text(quantity)

    if not quantity:
        return ""

    if re.search(r"(L|l|mL|ml|kg|KG|Kg|g|G|병|바이알|vial|Vial|개|EA|ea)\b", quantity):
        return quantity

    return clean_text(f"{quantity} {default_unit}")


def _extract_row_by_code_for_plasma(text: str, code: str) -> ManufacturingInfoItem | None:
    code_pat = _code_regex(code)

    if not code_pat:
        return None

    pattern = re.compile(
        rf"(?P<code>{code_pat})\s*\|\s*(?P<date>{_date_regex()})\s*\|\s*(?P<qty>{_quantity_regex()})",
        flags=re.I,
    )

    m = pattern.search(text)

    if not m:
        return None

    quantity = _append_unit_if_missing(m.group("qty"), "L")

    return ManufacturingInfoItem(
        stage_name="",
        manufacturing_no=clean_text(m.group("code")),
        manufacturing_date=clean_text(m.group("date")),
        quantity=quantity,
        quantity_field_name="제조량",
    )


def _extract_row_by_code_for_fraction(text: str, code: str) -> ManufacturingInfoItem | None:
    code_pat = _code_regex(code)

    if not code_pat:
        return None

    pattern = re.compile(
        rf"(?P<code>{code_pat})\s*\|\s*(?P<qty>{_quantity_regex()})\s*\|\s*(?:{_quantity_regex()})\s*\|\s*(?P<date>{_date_regex()})",
        flags=re.I,
    )

    m = pattern.search(text)

    if not m:
        return None

    quantity = _append_unit_if_missing(m.group("qty"), "kg")

    return ManufacturingInfoItem(
        stage_name="",
        manufacturing_no=clean_text(m.group("code")),
        manufacturing_date=clean_text(m.group("date")),
        quantity=quantity,
        quantity_field_name="제조량",
    )


def _extract_stage_scoped_text(stage_name: str, text: str) -> str:
    """
    JSON의 content/raw_text에서 특정 단계명 주변 영역만 잘라낸다.

    중요 보강:
    - clean_text() 또는 OCR 후처리 과정에서 줄바꿈이 사라지면 기존 line 기반 cut이 실패한다.
    - 이 경우 1.2 제조요약정보 안에서 완제의약품을 찾을 때 앞 단계인 최종원액의 제조번호(B2604)를
      잘못 가져올 수 있다.
    - 따라서 먼저 원문 문자열에서 단계명 위치를 직접 찾고, 다음 단계명 전까지 잘라낸다.
    """
    text = clean_text(text)

    if not text:
        return ""

    stage_name = clean_text(stage_name)
    stage_key = _norm_match(stage_name)

    if not stage_key:
        return text

    stage_names = [
        "원료혈장1",
        "원료혈장2",
        "원료혈장3",
        "원료혈장",
        "원획분",
        "최종원액",
        "완제의약품",
    ]

    # 1) 원문 문자열에서 단계명을 직접 찾아 자른다.
    # 줄바꿈이 없어도 "최종원액 ... 완제의약품 ..." 구간을 정확히 분리할 수 있다.
    exact_patterns = [stage_name]

    # 제조요약도 단계명과 본문 단계명이 조금 다를 수 있는 경우 보조 패턴을 추가한다.
    if "최종원액" in stage_key:
        exact_patterns.append("최종원액")
    if "완제의약품" in stage_key:
        exact_patterns.append("완제의약품")
    if "원획분" in stage_key:
        exact_patterns.append("원획분")

    start_pos: int | None = None
    matched_label = ""

    for pattern in exact_patterns:
        pattern = clean_text(pattern)

        if not pattern:
            continue

        m = re.search(re.escape(pattern), text, flags=re.I)

        if m:
            start_pos = m.start()
            matched_label = pattern
            break

    if start_pos is not None:
        end_pos = len(text)

        for name in stage_names:
            name = clean_text(name)

            if not name or _norm_match(name) == _norm_match(matched_label):
                continue

            m = re.search(re.escape(name), text[start_pos + len(matched_label):], flags=re.I)

            if m:
                candidate_end = start_pos + len(matched_label) + m.start()

                if candidate_end > start_pos:
                    end_pos = min(end_pos, candidate_end)

        scoped = text[start_pos:end_pos].strip()

        if scoped:
            return scoped

    # 2) fallback: 기존 line 기반 방식
    lines = _split_lines(text)

    if not lines:
        return text

    start_idx: int | None = None

    for idx, line in enumerate(lines):
        if stage_key in _norm_match(line):
            start_idx = idx
            break

    if start_idx is None:
        return text

    end_idx = len(lines)

    for idx in range(start_idx + 1, len(lines)):
        line_key = _norm_match(lines[idx])

        if not line_key:
            continue

        if any(_norm_match(name) in line_key and _norm_match(name) != stage_key for name in stage_names):
            end_idx = idx
            break

        if re.match(r"^\s*\d+(?:\.\d+)+\s+", lines[idx]):
            end_idx = idx
            break

    return "\n".join(lines[start_idx:end_idx]).strip() or text


def _extract_item_from_record_text(stage_name: str, record: Any, *, scoped: bool = True) -> ManufacturingInfoItem:
    text = _join_record_text(record)
    if scoped:
        text = _extract_stage_scoped_text(stage_name, text)

    manufacturing_no = _extract_field_value_from_text(text, "제조번호")
    manufacturing_date = _extract_field_value_from_text(text, "제조년월일")

    quantity = _extract_field_value_from_text(text, "제조수량")
    quantity_field_name = "제조수량"

    if not quantity:
        quantity = _extract_field_value_from_text(text, "제조량")
        quantity_field_name = "제조량"

    return ManufacturingInfoItem(
        stage_name=stage_name,
        manufacturing_no=manufacturing_no,
        manufacturing_date=manufacturing_date,
        quantity=quantity,
        quantity_field_name=quantity_field_name,
        page_number=_get(record, "page_start", None),
    )




def _extract_stage_from_text_by_patterns(stage_name: str, text: str) -> ManufacturingInfoItem | None:
    """
    1.2 제조요약정보처럼 표가 줄글로 풀린 JSON 텍스트에서 단계별 값을 직접 추출한다.
    특히 최종원액(B2604)과 완제의약품(26A01)은 하이픈 없는 제조번호라 일반 라벨 추출이 실패하기 쉬워 전용 패턴을 둔다.
    """
    text = clean_text(text)
    stage_key = _norm_match(stage_name)

    if not text or not stage_key:
        return None

    scoped = _extract_stage_scoped_text(stage_name, text)

    if not scoped:
        scoped = text

    item = _extract_item_from_record_text(stage_name, {"content": scoped}, scoped=False)

    if item.manufacturing_no or item.manufacturing_date or item.quantity:
        return item

    return None


def _item_matches_summary_values(summary_item: ManufacturingInfoItem, item: ManufacturingInfoItem | None) -> bool:
    if item is None:
        return False

    checks: list[bool] = []

    if summary_item.manufacturing_no:
        if not item.manufacturing_no:
            return False
        checks.append(_code_matches(summary_item.manufacturing_no, item.manufacturing_no))

    if summary_item.manufacturing_date:
        if not item.manufacturing_date:
            return False
        checks.append(_date_matches(summary_item.manufacturing_date, item.manufacturing_date))

    if summary_item.quantity:
        if not item.quantity:
            return False
        checks.append(_quantity_matches(summary_item.quantity, item.quantity))

    return bool(checks) and all(checks)


def _item_has_required_summary_fields(summary_item: ManufacturingInfoItem, item: ManufacturingInfoItem | None) -> bool:
    if item is None:
        return False

    if summary_item.manufacturing_no and not item.manufacturing_no:
        return False

    if summary_item.manufacturing_date and not item.manufacturing_date:
        return False

    if summary_item.quantity and not item.quantity:
        return False

    return True




def _relevant_records_text(records: list[Any], section_numbers: list[str]) -> str:
    parts: list[str] = []

    for section in section_numbers:
        record = _find_record_by_section(records, [section])
        if record is not None:
            parts.append(_join_record_text(record))

    return "\n".join(part for part in parts if part)


def _text_contains_summary_code(text: str, code: str) -> bool:
    target = _normalize_code(code)
    if not target:
        return False

    # 본문 전체를 제조번호 비교 방식으로 정규화한 뒤 target 존재 여부를 본다.
    # 예: "제조번호 | 26A01" / "제조번호: 26A01" / "26A-01" 모두 대응.
    haystack = _normalize_code(text)
    return target in haystack


def _text_contains_summary_date(text: str, date_value: str) -> bool:
    sy, sm, sd = _normalize_date_parts(date_value)
    if not sy:
        return False

    for m in re.finditer(r"20\d{2}\s*(?:[.\-/년]\s*)?\d{1,2}(?:\s*(?:[.\-/월]\s*)?\d{1,2})?", clean_text(text)):
        if _date_matches(date_value, m.group(0)):
            return True

    return False


def _text_contains_summary_manufacturing_date(text: str, date_value: str) -> bool:
    target_year, target_month, target_day = _normalize_date_parts(date_value)
    if not target_year:
        return False

    lines = _split_lines(text)
    label_re = re.compile(
        r"(제조년월일|제조년원일|제조일자|제조\s*년월\s*일)",
        flags=re.I,
    )

    for idx, line in enumerate(lines):
        if not label_re.search(line):
            continue

        window = "\n".join(lines[max(0, idx - 1) : min(len(lines), idx + 3)])
        if _text_contains_summary_date(window, date_value):
            return True

    return False


def _text_contains_summary_quantity(text: str, quantity_value: str) -> bool:
    target_number, target_unit = _normalize_quantity(quantity_value)
    if not target_number:
        return False

    compact_text = clean_text(text).replace(",", "")

    # 숫자만 먼저 비교한다. 단위는 _quantity_matches에서 병/바이알/vial 등을 통합 처리한다.
    for m in re.finditer(r"[0-9]+(?:\.[0-9]+)?\s*(?:L|l|mL|ml|uL|ul|dose|doses|도즈|병|바이알|vial|vials|개|ea|EA)?", compact_text):
        candidate = m.group(0)
        c_number, c_unit = _normalize_quantity(candidate)
        if not c_number:
            continue
        if c_number != target_number:
            continue
        if target_unit and c_unit and target_unit != c_unit:
            continue
        return True

    return False


def _repair_item_from_exact_summary_presence(
    *,
    summary_item: ManufacturingInfoItem,
    item: ManufacturingInfoItem | None,
    records: list[Any],
    section_numbers: list[str],
) -> ManufacturingInfoItem | None:
    """
    제조요약도 값이 본문 해당 섹션에 그대로 존재하면, 일반 파서가 잘못 잡은 값을 보정한다.

    필요한 이유:
    - 1.2 표 텍스트가 "제조번호 | | 26A01"처럼 풀리면 일반 라벨 파서가 값을 놓칠 수 있다.
    - 5.1 주변에는 "사용된 최종원액 제조번호"가 같이 있어 완제 제조번호로 오인할 수 있다.
    - 최종원액/완제의약품처럼 제조번호가 B2604, 26A01 형태이면 하이픈 기반 제조번호 규칙에서 누락될 수 있다.

    원칙:
    - summary_item의 값이 관련 본문 섹션 텍스트에 정확히 존재하는 경우에만 보정한다.
    - 추정으로 값을 만들지 않는다.
    """
    text = _relevant_records_text(records, section_numbers)
    if not text:
        return item

    if item is None:
        item = ManufacturingInfoItem(stage_name=summary_item.stage_name)

    manufacturing_no = item.manufacturing_no
    manufacturing_date = item.manufacturing_date
    quantity = item.quantity
    quantity_field_name = item.quantity_field_name or summary_item.quantity_field_name

    if summary_item.manufacturing_no:
        if not manufacturing_no or not _code_matches(summary_item.manufacturing_no, manufacturing_no):
            if _text_contains_summary_code(text, summary_item.manufacturing_no):
                manufacturing_no = summary_item.manufacturing_no

    if summary_item.manufacturing_date:
        if not manufacturing_date or not _date_matches(summary_item.manufacturing_date, manufacturing_date):
            if _text_contains_summary_manufacturing_date(text, summary_item.manufacturing_date):
                manufacturing_date = summary_item.manufacturing_date

    if summary_item.quantity:
        if not quantity or not _quantity_matches(summary_item.quantity, quantity):
            if _text_contains_summary_quantity(text, summary_item.quantity):
                quantity = summary_item.quantity

    return ManufacturingInfoItem(
        stage_name=summary_item.stage_name,
        manufacturing_no=manufacturing_no,
        manufacturing_date=manufacturing_date,
        quantity=quantity,
        quantity_field_name=quantity_field_name,
        page_number=item.page_number,
    )

def _extract_finished_product_from_summary_info_text(stage_name: str, text: str) -> ManufacturingInfoItem | None:
    """
    1.2 제조요약정보 전용 파서.

    PDF 표 구조상 완제의약품이라는 행 제목이 제조번호/제조년월일/총 제조수량보다
    뒤쪽 또는 왼쪽 병합셀에 위치하면서 OCR 텍스트 순서가 뒤집힐 수 있다.
    그래서 '완제의약품' 라벨 이후만 잘라내는 방식은 제조번호 26A01을 놓친다.

    기준:
    - '총 제조수량/제조수량' 라인을 기준으로 잡고
    - 그 앞쪽 가까운 제조번호/제조년월일을 완제의약품 값으로 본다.
    """
    lines = _split_lines(text)

    if not lines:
        return None

    quantity_idx: int | None = None
    quantity = ""

    for idx, line in enumerate(lines):
        if re.search(r"(총\s*제조수량|총제조수량|제조수량|신청수량)", line):
            found_quantity = _extract_field_value_from_text(line, "제조수량")
            if found_quantity:
                quantity_idx = idx
                quantity = found_quantity
                break

    if quantity_idx is None:
        return None

    manufacturing_no = ""
    manufacturing_date = ""

    # 완제의약품 블록은 총 제조수량 바로 앞 10~15줄 안에 제조번호/제조년월일이 존재한다.
    start = max(0, quantity_idx - 15)
    for idx in range(quantity_idx - 1, start - 1, -1):
        line = lines[idx]

        if not manufacturing_date and re.search(r"(제조년월일|제조년원일|제조일자|제조일|제조\s*년월\s*일)", line):
            manufacturing_date = _extract_field_value_from_text(line, "제조년월일")

        if not manufacturing_no and re.search(r"(제조번호|제\s*조\s*번호)", line):
            manufacturing_no = _extract_field_value_from_text(line, "제조번호")

        if manufacturing_no and manufacturing_date:
            break

    # 일부 OCR에서는 라벨과 값이 줄이 분리된다. 그래도 주변 구간 전체를 대상으로 한 번 더 시도한다.
    window_text = "\n".join(lines[start : quantity_idx + 1])

    if not manufacturing_no:
        manufacturing_no = _extract_field_value_from_text(window_text, "제조번호")

    if not manufacturing_date:
        manufacturing_date = _extract_field_value_from_text(window_text, "제조년월일")

    if not quantity:
        quantity = _extract_field_value_from_text(window_text, "제조수량")

    if not (manufacturing_no or manufacturing_date or quantity):
        return None

    return ManufacturingInfoItem(
        stage_name=stage_name,
        manufacturing_no=manufacturing_no,
        manufacturing_date=manufacturing_date,
        quantity=quantity,
        quantity_field_name="제조수량",
    )


def _extract_final_bulk_from_records(records: list[Any], summary_item: ManufacturingInfoItem) -> ManufacturingInfoItem | None:
    best_partial: ManufacturingInfoItem | None = None

    # 4.1 정보는 "제조번호 B2604"가 최종원액 라벨보다 앞에 나오는 구조가 있으므로
    # stage 기준으로 scope를 자르면 제조번호가 잘려나간다. 따라서 4.1은 전체 텍스트에서 추출한다.
    for section in ["4.1", "1.2"]:
        record = _find_record_by_section(records, [section])
        if record is None:
            continue

        text = _join_record_text(record)

        if section == "4.1":
            item = _extract_item_from_record_text(summary_item.stage_name, record, scoped=False)
        else:
            item = _extract_stage_from_text_by_patterns("최종원액", text)

        if item is None:
            continue

        item.stage_name = summary_item.stage_name
        item.page_number = _get(record, "page_start", None)

        item = _repair_item_from_exact_summary_presence(
            summary_item=summary_item,
            item=item,
            records=records,
            section_numbers=["4.1", "1.2"],
        )

        if _item_has_required_summary_fields(summary_item, item):
            return item

        if best_partial is None and item is not None and (item.manufacturing_no or item.manufacturing_date or item.quantity):
            best_partial = item

    best_partial = _repair_item_from_exact_summary_presence(
        summary_item=summary_item,
        item=best_partial,
        records=records,
        section_numbers=["4.1", "1.2"],
    )

    return best_partial


def _extract_summary_info_item_from_records(
    records: list[Any],
    summary_item: ManufacturingInfoItem,
) -> tuple[ManufacturingInfoItem | None, Any | None]:
    """Extract one stage directly from section 1.2 제조요약정보 when it is present."""
    record = _find_record_by_section(records, ["1.2"])

    if record is None:
        return None, None

    text = _join_record_text(record)
    if not text:
        return None, None

    item = _extract_stage_from_text_by_patterns(summary_item.stage_name, text)
    if item is None:
        return None, None

    item.stage_name = summary_item.stage_name
    item.page_number = _get(record, "page_start", None)
    item.quantity_field_name = item.quantity_field_name or summary_item.quantity_field_name

    if _item_has_any_value(item):
        return item, record

    return None, None


def _extract_finished_product_from_records(records: list[Any], summary_item: ManufacturingInfoItem) -> ManufacturingInfoItem | None:
    best_partial: ManufacturingInfoItem | None = None

    # 완제의약품은 1.2 제조요약정보가 최우선이다.
    # 5.1 정보는 "사용된 최종원액" 번호가 섞여 제조번호 오판이 잦으므로 fallback에서 제외한다.
    for section in ["1.2", "1.1"]:
        record = _find_record_by_section(records, [section])
        if record is None:
            continue

        text = _join_record_text(record)

        if section == "1.2":
            item = _extract_finished_product_from_summary_info_text(summary_item.stage_name, text)
            if item is None:
                item = _extract_stage_from_text_by_patterns("완제의약품", text)
        else:
            item = _extract_item_from_record_text(summary_item.stage_name, record, scoped=False)
            item.quantity_field_name = "제조수량"

        if item is None:
            continue

        item.stage_name = summary_item.stage_name
        item.page_number = _get(record, "page_start", None)

        item = _repair_item_from_exact_summary_presence(
            summary_item=summary_item,
            item=item,
            records=records,
            section_numbers=["1.2", "1.1"],
        )

        if _item_has_required_summary_fields(summary_item, item):
            return item

        if best_partial is None and item is not None and (item.manufacturing_no or item.manufacturing_date or item.quantity):
            best_partial = item

    best_partial = _repair_item_from_exact_summary_presence(
        summary_item=summary_item,
        item=best_partial,
        records=records,
        section_numbers=["1.2", "1.1"],
    )

    return best_partial


def _extract_document_item_from_json_rules(
    summary_item: ManufacturingInfoItem,
    records: list[Any],
) -> ManufacturingInfoItem | None:
    """
    1순위 매핑: OCR/PDF를 다시 보지 않고 05_records.json의 구조화 결과로 매핑한다.

    사용 근거:
    - 제조요약도는 record_type=flowchart의 diagram_data.nodes에 단계명/제조번호/제조년월일/제조량이 구조화되어 있다.
    - 본문 정보는 section_number/content/raw_text에 이미 텍스트화되어 있다.
    - PDF 이미지를 다시 보는 방식은 표 위치/줄바꿈/OCR 품질에 따라 흔들리므로 JSON을 primary source로 사용한다.
    """
    stage_key = _norm_match(summary_item.stage_name)
    summary_code = clean_text(summary_item.manufacturing_no)

    summary_info_item, _summary_info_record = _extract_summary_info_item_from_records(
        records,
        summary_item,
    )
    if summary_info_item is not None and _item_matches_summary_values(summary_item, summary_info_item):
        return summary_info_item

    # 원료혈장1/2/3: 3.1.1 원료혈장 정보 표의 식별번호 행과 매핑
    if "원료혈장" in stage_key:
        for record in records:
            if not _section_number_starts(record, "3.1.1"):
                continue

            text = _join_record_text(record)
            item = _extract_row_by_code_for_plasma(text, summary_code)

            if item is not None:
                item.stage_name = summary_item.stage_name
                item.page_number = _get(record, "page_start", None)
                return item

    # 원획분: 3.2.1 정보 표의 제조번호/제조량/제조년월일 행과 매핑
    if "원획분" in stage_key:
        for record in records:
            if not _section_number_starts(record, "3.2.1"):
                continue

            text = _join_record_text(record)
            item = _extract_row_by_code_for_fraction(text, summary_code)

            if item is not None:
                item.stage_name = summary_item.stage_name
                item.page_number = _get(record, "page_start", None)
                return item

        record = _find_record_by_section(records, ["3.2.1"])
        if record is not None:
            return _extract_item_from_record_text(summary_item.stage_name, record, scoped=False)

    # 최종원액: 4.1 정보 우선, 실패 시 1.2 제조요약정보에서 최종원액 행을 직접 추출
    if "최종원액" in stage_key:
        item = _extract_final_bulk_from_records(records, summary_item)
        if item is not None:
            return item

    # 완제의약품:
    # 1.2 제조요약정보에서 완제의약품 행을 직접 추출한다.
    # 실패 시 1.1 신청제품정보만 보조 추출한다.
    # 5.1은 사용된 최종원액 제조번호가 섞여 오판 위험이 있어 제외한다.
    if "완제의약품" in stage_key:
        item = _extract_finished_product_from_records(records, summary_item)
        if item is not None:
            return item

    # 단계명이 본문에 직접 있는 일반 케이스
    record = _find_document_record_for_stage(summary_item.stage_name, records)
    if record is not None:
        item = _extract_item_from_record_text(summary_item.stage_name, record, scoped=True)
        if item.manufacturing_no or item.manufacturing_date or item.quantity:
            return item

    return None


def _should_use_llm_mapping(
    summary_item: ManufacturingInfoItem,
    base_item: ManufacturingInfoItem | None,
) -> bool:
    if base_item is None:
        return True

    # 필수 필드가 비어 있으면 LLM으로 JSON 후보 중 대응 항목을 보강한다.
    if summary_item.manufacturing_no and not base_item.manufacturing_no:
        return True

    if summary_item.manufacturing_date and not base_item.manufacturing_date:
        return True

    if summary_item.quantity and not base_item.quantity:
        return True

    return False



def extract_document_items_from_records(
    records_source: Any,
    summary_items: list[ManufacturingInfoItem],
) -> dict[str, ManufacturingInfoItem]:
    records = _records_from_source(records_source)
    out: dict[str, ManufacturingInfoItem] = {}

    for summary_item in summary_items:
        # 1) JSON 구조 기반 deterministic 매핑을 먼저 사용한다.
        #    PDF 이미지를 다시 보지 않고 05_records.json의 diagram_data/content/raw_text를 우선한다.
        base_item = _extract_document_item_from_json_rules(summary_item, records)

        # 2) JSON 규칙으로 값을 충분히 못 찾은 경우에만 LLM에게 JSON 후보를 주고 의미 매핑을 시킨다.
        llm_item = None
        if _should_use_llm_mapping(summary_item, base_item):
            llm_item = _llm_extract_document_item_for_stage(summary_item, records)

        merged_item = _merge_document_items(base_item, llm_item)

        if merged_item is not None:
            out[summary_item.stage_name] = merged_item

    return out



def _compare_field(
    field_name: str,
    summary_value: str,
    document_value: str,
) -> FieldCompareResult:
    summary_value = clean_text(summary_value)
    document_value = clean_text(document_value)

    if not summary_value and not document_value:
        return FieldCompareResult(
            field_name=field_name,
            summary_value=summary_value,
            document_value=document_value,
            status="불합격",
            reason="제조요약도와 본문 정보에서 모두 값을 확인하지 못했습니다.",
        )

    if not summary_value:
        return FieldCompareResult(
            field_name=field_name,
            summary_value=summary_value,
            document_value=document_value,
            status="불합격",
            reason="제조요약도에서 값을 확인하지 못했습니다.",
        )

    if not document_value:
        return FieldCompareResult(
            field_name=field_name,
            summary_value=summary_value,
            document_value=document_value,
            status="불합격",
            reason="본문 정보 섹션에서 대응 값을 확인하지 못했습니다.",
        )

    if field_name == "제조번호":
        ok = _code_matches(summary_value, document_value)
    elif field_name == "제조년월일":
        ok = _date_matches(summary_value, document_value)
    elif field_name in {"제조량", "제조수량", "총 제조수량"}:
        ok = _quantity_matches(summary_value, document_value)
    else:
        ok = clean_text(summary_value) == clean_text(document_value)

    if ok:
        return FieldCompareResult(
            field_name=field_name,
            summary_value=summary_value,
            document_value=document_value,
            status="합격",
            reason="제조요약도 값과 본문 정보 값이 일치합니다.",
        )

    return FieldCompareResult(
        field_name=field_name,
        summary_value=summary_value,
        document_value=document_value,
        status="불합격",
        reason=f"제조요약도 값({summary_value})과 본문 값({document_value})이 일치하지 않습니다.",
    )


def _stage_base_name(stage_name: str | None) -> str:
    return re.sub(r"\s*[0-9０-９]+$", "", clean_text(stage_name)).strip()


def _stage_context_matches(stage_name: str, context: str) -> bool:
    context_key = _norm_match(context)
    stage_key = _norm_match(stage_name)
    base_key = _norm_match(_stage_base_name(stage_name))

    if stage_key and stage_key in context_key:
        return True

    return bool(base_key and len(base_key) >= 3 and base_key in context_key)


def _exact_stage_context_matches(stage_name: str, context: str) -> bool:
    stage_key = _norm_match(stage_name)
    return bool(stage_key and stage_key in _norm_match(context))


def _has_numbered_stage_suffix(stage_name: str) -> bool:
    return bool(re.search(r"[0-9０-９]+\s*$", clean_text(stage_name)))


def _record_stage_context(record: Any) -> str:
    parts: list[str] = []

    for key in ["section_number", "section_title", "test_name", "content_label"]:
        value = clean_text(_get(record, key, ""))
        if value:
            parts.append(value)

    section_path = _get(record, "section_path", None)
    if isinstance(section_path, list):
        for section in section_path:
            if not isinstance(section, dict):
                continue
            number = clean_text(section.get("number", ""))
            title = clean_text(section.get("title", ""))
            if number:
                parts.append(number)
            if title:
                parts.append(title)

    return " ".join(parts)


def _record_primary_stage_keys(
    record: Any,
    summary_items: list[ManufacturingInfoItem],
) -> tuple[set[str], set[str]]:
    """
    섹션 경로의 가장 가까운 제조단계를 찾는다.

    부모 단계명이 경로에 함께 있어도 더 구체적인 자식 단계가 있으면 부모를
    해당 정보 섹션의 주 단계로 보지 않는다. 번호형 sibling은 base 단계명으로
    한 표에 묶일 수 있으므로 grouped keys로 반환한다.
    """
    titles: list[str] = []
    section_path = _get(record, "section_path", None)
    if isinstance(section_path, list):
        for section in reversed(section_path):
            if isinstance(section, dict):
                title = clean_text(section.get("title", ""))
                if title:
                    titles.append(title)

    for key in ["section_title", "content_label"]:
        value = clean_text(_get(record, key, ""))
        if value:
            titles.append(value)

    generic_suffixes = ["세부정보", "상세정보", "정보", "시험정보", "시험"]
    for title in titles:
        title_key = _norm_match(title)
        if not title_key:
            continue

        comparable_keys = {title_key}
        for suffix in generic_suffixes:
            suffix_key = _norm_match(suffix)
            if title_key.endswith(suffix_key):
                comparable_keys.add(title_key[: -len(suffix_key)])

        exact_keys = {
            _norm_match(item.stage_name)
            for item in summary_items
            if _norm_match(item.stage_name) in comparable_keys
        }
        if exact_keys:
            return exact_keys, set(exact_keys)

        grouped_keys = {
            _norm_match(item.stage_name)
            for item in summary_items
            if _norm_match(_stage_base_name(item.stage_name)) in comparable_keys
        }
        if grouped_keys:
            return set(), grouped_keys

    return set(), set()


def _record_source_label(record: Any) -> str:
    section_number = clean_text(_get(record, "section_number", ""))
    section_title = clean_text(_get(record, "section_title", ""))
    page_number = _get(record, "page_start", None)

    label = clean_text(" ".join(part for part in [section_number, section_title] if part))
    if not label:
        label = "문서 본문"
    if page_number:
        label = f"{label} · {page_number}쪽"
    return label


def _item_has_any_value(item: ManufacturingInfoItem | None) -> bool:
    return bool(
        item
        and (
            clean_text(item.manufacturing_no)
            or clean_text(item.manufacturing_date)
            or clean_text(item.quantity)
        )
    )


def _merge_occurrence_items(
    primary: ManufacturingInfoItem | None,
    secondary: ManufacturingInfoItem | None,
    stage_name: str,
) -> ManufacturingInfoItem | None:
    if primary is None and secondary is None:
        return None

    primary = primary or ManufacturingInfoItem(stage_name=stage_name)
    secondary = secondary or ManufacturingInfoItem(stage_name=stage_name)

    return ManufacturingInfoItem(
        stage_name=stage_name,
        manufacturing_no=primary.manufacturing_no or secondary.manufacturing_no,
        manufacturing_date=primary.manufacturing_date or secondary.manufacturing_date,
        quantity=primary.quantity or secondary.quantity,
        quantity_field_name=(
            primary.quantity_field_name
            or secondary.quantity_field_name
            or "제조량"
        ),
        page_number=primary.page_number or secondary.page_number,
    )


def _extract_tabular_occurrence_by_code(
    summary_item: ManufacturingInfoItem,
    text: str,
) -> ManufacturingInfoItem | None:
    """
    섹션 번호나 표 열 순서를 고정하지 않고 제조번호가 포함된 한 행에서
    날짜와 제조량을 찾는다.
    """
    summary_code = clean_text(summary_item.manufacturing_no)
    if not summary_code:
        return None

    target_quantity_number, target_quantity_unit = _normalize_quantity(summary_item.quantity)

    header_cells: list[str] = []
    for line in _split_lines(text):
        line_cells = [
            _clean_table_extracted_value(cell)
            for cell in re.split(r"\s*\|\s*", line)
            if _clean_table_extracted_value(cell)
        ]
        if any("제조번호" in cell for cell in line_cells):
            header_cells = line_cells
            continue

        if not _text_contains_summary_code(line, summary_code):
            continue

        cells = line_cells
        if not cells:
            cells = [clean_text(line)]

        number_indexes = [
            idx
            for idx, header in enumerate(header_cells)
            if "제조번호" in header and "사용" not in header
        ]
        if number_indexes:
            if not any(
                idx < len(cells) and _code_matches(summary_code, cells[idx])
                for idx in number_indexes
            ):
                continue
        elif len(cells) > 1 and not _code_matches(summary_code, cells[0]):
            # 헤더가 없더라도 제조번호는 일반적으로 행의 첫 값이다. 다른 원료를
            # 참조하는 후행 열에 코드가 나온 경우는 해당 단계 정보로 보지 않는다.
            continue

        manufacturing_no = ""
        manufacturing_date = ""
        quantity_candidates: list[str] = []

        for cell in cells:
            if not manufacturing_no and _code_matches(summary_code, cell):
                manufacturing_no = summary_code
                continue

            if not manufacturing_date and _normalize_date_parts(cell) != ("", "", ""):
                manufacturing_date = cell
                continue

            number, unit = _normalize_quantity(cell)
            if not number:
                continue
            if not _looks_like_quantity_only(cell) and number != target_quantity_number:
                continue

            if target_quantity_number and number == target_quantity_number:
                quantity_candidates.insert(0, cell)
            else:
                quantity_candidates.append(cell)

        if not manufacturing_no:
            manufacturing_no = summary_code

        quantity = quantity_candidates[0] if quantity_candidates else ""
        if quantity and target_quantity_unit:
            quantity = _append_unit_if_missing(quantity, target_quantity_unit)

        item = ManufacturingInfoItem(
            stage_name=summary_item.stage_name,
            manufacturing_no=manufacturing_no,
            manufacturing_date=manufacturing_date,
            quantity=quantity,
            quantity_field_name=summary_item.quantity_field_name or "제조량",
        )
        if _item_has_any_value(item):
            return item

    return None


def _extract_fraction_occurrence_by_date_quantity(
    summary_item: ManufacturingInfoItem,
    text: str,
) -> ManufacturingInfoItem | None:
    """
    원획분 표에서 제조번호가 틀린 행도 잡기 위해 제조년월일+제조량으로 행을 찾는다.
    예: 제조요약도 원획분3은 F26-3인데 3.2.1 행이 F26-5로 잘못 적힌 경우.
    """
    if "원획분" not in _norm_match(summary_item.stage_name):
        return None

    if not summary_item.manufacturing_date or not summary_item.quantity:
        return None

    target_qty_number, target_qty_unit = _normalize_quantity(summary_item.quantity)
    if not target_qty_number:
        return None

    for line in _split_lines(text):
        cells = [
            _clean_table_extracted_value(cell)
            for cell in re.split(r"\s*\|\s*", line)
            if _clean_table_extracted_value(cell)
        ]
        if len(cells) < 4:
            continue
        if any("제조번호" in cell for cell in cells):
            continue

        date_match = any(
            _normalize_date_parts(cell) != ("", "", "")
            and _date_matches(summary_item.manufacturing_date, cell)
            for cell in cells
        )
        if not date_match:
            continue

        quantity_cell = ""
        for cell in cells[1:]:
            number, _unit = _normalize_quantity(cell)
            if number and _quantity_matches(summary_item.quantity, cell):
                quantity_cell = cell
                break
        if not quantity_cell:
            continue

        code_match = re.search(r"\b[A-Z]{1,5}\d{2,}(?:-[A-Z0-9]+)?\b", cells[0], flags=re.I)
        if not code_match:
            continue

        manufacturing_no = clean_text(code_match.group(0))
        manufacturing_date = next(
            (
                clean_text(cell)
                for cell in cells
                if _normalize_date_parts(cell) != ("", "", "")
                and _date_matches(summary_item.manufacturing_date, cell)
            ),
            "",
        )
        quantity = _append_unit_if_missing(quantity_cell, target_qty_unit or "kg")

        return ManufacturingInfoItem(
            stage_name=summary_item.stage_name,
            manufacturing_no=manufacturing_no,
            manufacturing_date=manufacturing_date,
            quantity=quantity,
            quantity_field_name=summary_item.quantity_field_name or "제조량",
        )

    return None


def _extract_stage_reference_code(
    summary_item: ManufacturingInfoItem,
    text: str,
) -> str:
    """
    '사용된 최종원액', '사용된 원료혈장'처럼 downstream 단계가 upstream
    제조번호를 참조하는 값도 검증한다. 번호가 붙은 sibling 단계는 정확한
    제조번호가 실제 참조 구간에 있을 때만 연결한다.
    """
    lines = _split_lines(text)
    base_key = _norm_match(_stage_base_name(summary_item.stage_name))
    summary_code = clean_text(summary_item.manufacturing_no)
    if not lines or not base_key or not summary_code:
        return ""

    for idx, line in enumerate(lines):
        line_key = _norm_match(line)
        if "사용" not in line_key or base_key not in line_key:
            continue

        window = "\n".join(lines[max(0, idx - 1) : min(len(lines), idx + 5)])
        if _text_contains_summary_code(window, summary_code):
            return summary_code

        if _has_numbered_stage_suffix(summary_item.stage_name):
            continue

        candidates = [
            lines[candidate_idx]
            for candidate_idx in [idx - 1, idx + 1, idx + 2]
            if 0 <= candidate_idx < len(lines)
        ]
        for candidate in candidates:
            candidate = _clean_table_extracted_value(candidate)
            if _is_manufacturing_code_value(candidate):
                return candidate

    return ""


def _should_use_summary_info_code_date_reference(summary_item: ManufacturingInfoItem) -> bool:
    stage_key = _norm_match(summary_item.stage_name)

    if not _has_numbered_stage_suffix(summary_item.stage_name):
        return False

    return bool("원료혈장" in stage_key or "원획분" in stage_key)


def _summary_info_reference_matches(
    summary_item: ManufacturingInfoItem,
    item: ManufacturingInfoItem | None,
) -> bool:
    if item is None:
        return False

    if summary_item.manufacturing_no:
        if not item.manufacturing_no or not _code_matches(summary_item.manufacturing_no, item.manufacturing_no):
            return False

    if summary_item.manufacturing_date:
        if not item.manufacturing_date or not _date_matches(summary_item.manufacturing_date, item.manufacturing_date):
            return False

    if item.quantity and summary_item.quantity:
        return _quantity_matches(summary_item.quantity, item.quantity)

    return True


def _extract_summary_info_code_date_reference(
    records: list[Any],
    summary_item: ManufacturingInfoItem,
) -> tuple[ManufacturingInfoItem | None, Any | None]:
    if not _should_use_summary_info_code_date_reference(summary_item):
        return None, None

    record = _find_record_by_section(records, ["1.2"])
    if record is None:
        return None, None

    text = _join_record_text(record)
    code = clean_text(summary_item.manufacturing_no)
    code_pat = _code_regex(code)
    if not text or not code_pat:
        return None, None

    match = re.search(
        rf"(?P<code>{code_pat})\s*/\s*(?P<date>{_date_regex()})",
        text,
        flags=re.I,
    )
    if not match:
        return None, None

    item = ManufacturingInfoItem(
        stage_name=summary_item.stage_name,
        manufacturing_no=clean_text(match.group("code")),
        manufacturing_date=clean_text(match.group("date")),
        quantity="",
        quantity_field_name=summary_item.quantity_field_name or "제조량",
        page_number=_get(record, "page_start", None),
    )
    return item, record


def _should_use_summary_item_as_occurrence(summary_item: ManufacturingInfoItem) -> bool:
    stage_key = _norm_match(summary_item.stage_name)

    if not stage_key:
        return False

    if "최종원액" in stage_key or "완제의약품" in stage_key:
        return False

    if not ("원료혈장" in stage_key or "원획분" in stage_key):
        return False

    if not _has_numbered_stage_suffix(summary_item.stage_name):
        return False

    return bool(
        clean_text(summary_item.manufacturing_no)
        and clean_text(summary_item.manufacturing_date)
        and clean_text(summary_item.quantity)
    )


def collect_manufacturing_info_occurrences(
    records_source: Any,
    summary_items: list[ManufacturingInfoItem] | None = None,
) -> dict[str, list[ManufacturingInfoOccurrence]]:
    """
    제조요약도 외의 문서 전체에서 단계별 제조정보 출처를 모두 수집한다.

    고정 섹션 번호 대신 다음 근거를 사용한다.
    - 섹션 경로/제목에 나타난 제조단계명
    - 제조요약도에 기재된 제조번호가 실제 본문에 나타나는 행
    """
    records = _records_from_source(records_source)
    summary_items = summary_items or extract_summary_items_from_records(records_source)
    out: dict[str, list[ManufacturingInfoOccurrence]] = {
        _norm_match(item.stage_name): [] for item in summary_items
    }
    seen: set[tuple[str, str, str, str, str]] = set()

    for summary_item in summary_items:
        stage_key = _norm_match(summary_item.stage_name)
        item, record = _extract_summary_info_item_from_records(records, summary_item)
        use_code_date_reference = False

        if item is not None and record is not None:
            if not _item_matches_summary_values(summary_item, item):
                item, record = None, None

        if item is None or record is None:
            item, record = _extract_summary_info_code_date_reference(records, summary_item)
            use_code_date_reference = item is not None and record is not None

        if item is None or record is None:
            continue

        if use_code_date_reference and not _summary_info_reference_matches(summary_item, item):
            continue

        section_number = clean_text(_get(record, "section_number", ""))
        page_number = _get(record, "page_start", None)
        source_label = _record_source_label(record)
        dedupe_key = (
            stage_key,
            source_label,
            _normalize_code(item.manufacturing_no),
            "-".join(_normalize_date_parts(item.manufacturing_date)),
            "|".join(_normalize_quantity(item.quantity)),
        )

        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        out.setdefault(stage_key, []).append(
            ManufacturingInfoOccurrence(
                stage_name=summary_item.stage_name,
                item=item,
                source_label=source_label,
                section_number=section_number,
                page_number=page_number,
            )
        )

    for record in records:
        record_type = clean_text(_get(record, "record_type", ""))
        if record_type in {"flowchart", "diagram", "test"}:
            continue

        text = _join_record_text(record)
        if not text:
            continue

        context = _record_stage_context(record)
        context_key = _norm_match(context)
        summary_context = any(
            token in context_key
            for token in ["제조요약", "신청제품정보", "제품정보"]
        )
        source_label = _record_source_label(record)
        section_number = clean_text(_get(record, "section_number", ""))
        page_number = _get(record, "page_start", None)
        primary_exact_keys, primary_stage_keys = _record_primary_stage_keys(
            record,
            summary_items,
        )
        code_match_count = sum(
            1
            for candidate in summary_items
            if candidate.manufacturing_no
            and _text_contains_summary_code(text, candidate.manufacturing_no)
        )

        for summary_item in summary_items:
            stage_key = _norm_match(summary_item.stage_name)
            is_finished_product_application_info = (
                "완제의약품" in stage_key
                and section_number == "1.1"
                and "신청제품정보" in context_key
                and summary_item.manufacturing_no
                and _text_contains_summary_code(text, summary_item.manufacturing_no)
            )
            reference_code = _extract_stage_reference_code(summary_item, text)
            if reference_code:
                reference_item = ManufacturingInfoItem(
                    stage_name=summary_item.stage_name,
                    manufacturing_no=reference_code,
                    page_number=page_number,
                )
                reference_source = f"{source_label} · 사용 단계 참조"
                reference_key = (
                    stage_key,
                    reference_source,
                    _normalize_code(reference_code),
                    "",
                    "",
                )
                if reference_key not in seen:
                    seen.add(reference_key)
                    out.setdefault(stage_key, []).append(
                        ManufacturingInfoOccurrence(
                            stage_name=summary_item.stage_name,
                            item=reference_item,
                            source_label=reference_source,
                            section_number=section_number,
                            page_number=page_number,
                        )
                    )

            exact_context_match = stage_key in primary_exact_keys
            exact_stage_in_text = _exact_stage_context_matches(summary_item.stage_name, text)
            if primary_stage_keys:
                context_match = stage_key in primary_stage_keys
            else:
                context_match = _exact_stage_context_matches(
                    summary_item.stage_name,
                    context,
                ) or (
                    not _has_numbered_stage_suffix(summary_item.stage_name)
                    and _stage_context_matches(summary_item.stage_name, context)
                )
            stage_in_text = exact_stage_in_text or (
                not _has_numbered_stage_suffix(summary_item.stage_name)
                and _stage_context_matches(summary_item.stage_name, text)
            )
            code_match = bool(
                summary_item.manufacturing_no
                and _text_contains_summary_code(text, summary_item.manufacturing_no)
            )

            fraction_identity_item = None
            if not code_match and _section_number_starts(record, "3.2.1"):
                fraction_identity_item = _extract_fraction_occurrence_by_date_quantity(
                    summary_item,
                    text,
                )

            if not (
                context_match
                or code_match
                or (summary_context and stage_in_text)
                or fraction_identity_item is not None
            ):
                continue

            row_item = (
                _extract_tabular_occurrence_by_code(summary_item, text)
                if code_match
                else fraction_identity_item
            )

            scoped_item = None
            if is_finished_product_application_info:
                scoped_item = _extract_item_from_record_text(
                    summary_item.stage_name,
                    record,
                    scoped=False,
                )
                if scoped_item:
                    scoped_item.quantity_field_name = summary_item.quantity_field_name or "제조수량"
            elif exact_context_match:
                scoped_item = _extract_item_from_record_text(
                    summary_item.stage_name,
                    record,
                    scoped=False,
                )
            elif context_match or (summary_context and stage_in_text):
                scoped_item = _extract_item_from_record_text(
                    summary_item.stage_name,
                    record,
                    scoped=stage_in_text,
                )

            # 여러 단계가 한 레코드에 섞인 제조요약정보 표에서는 제조번호 행을
            # 우선하고, 단일 단계 정보 섹션에서는 단계 범위 추출을 우선한다.
            if fraction_identity_item is not None:
                item = fraction_identity_item
            elif is_finished_product_application_info:
                item = _merge_occurrence_items(scoped_item, row_item, summary_item.stage_name)
            elif exact_context_match:
                item = _merge_occurrence_items(scoped_item, row_item, summary_item.stage_name)
            elif code_match_count > 1:
                item = row_item
            else:
                item = _merge_occurrence_items(scoped_item, row_item, summary_item.stage_name)

            if not _item_has_any_value(item):
                continue

            item.stage_name = summary_item.stage_name
            item.page_number = page_number

            dedupe_key = (
                stage_key,
                source_label,
                _normalize_code(item.manufacturing_no),
                "-".join(_normalize_date_parts(item.manufacturing_date)),
                "|".join(_normalize_quantity(item.quantity)),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            out.setdefault(stage_key, []).append(
                ManufacturingInfoOccurrence(
                    stage_name=summary_item.stage_name,
                    item=item,
                    source_label=source_label,
                    section_number=section_number,
                    page_number=page_number,
                )
            )

    for summary_item in summary_items:
        stage_key = _norm_match(summary_item.stage_name)

        if out.get(stage_key) or not _should_use_summary_item_as_occurrence(summary_item):
            continue

        source_label = "제조요약정보"
        dedupe_key = (
            stage_key,
            source_label,
            _normalize_code(summary_item.manufacturing_no),
            "-".join(_normalize_date_parts(summary_item.manufacturing_date)),
            "|".join(_normalize_quantity(summary_item.quantity)),
        )

        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        out.setdefault(stage_key, []).append(
            ManufacturingInfoOccurrence(
                stage_name=summary_item.stage_name,
                item=summary_item,
                source_label=source_label,
                section_number="1.3",
                page_number=summary_item.page_number,
            )
        )

    return out


def _as_date(value: str | None) -> date | None:
    year, month, day = _normalize_date_parts(value)
    if not year or not month or not day:
        return None
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _date_text(value: date | None) -> str:
    return value.strftime("%Y.%m.%d") if value else ""


def _extract_date_range(value: str | None) -> tuple[date | None, date | None]:
    """
    시험일의 표기 정밀도를 보존해 실제 포함 구간으로 변환한다.

    - 2025.01.15 -> 2025.01.15 하루
    - 2025.01 -> 2025.01.01 ~ 2025.01.31
    - 2025 -> 2025.01.01 ~ 2025.12.31
    - 2025.01 ~ 2025.02 -> 2025.01.01 ~ 2025.02.28
    """
    text = clean_text(value)
    if not text:
        return None, None

    matches = re.findall(
        r"20\d{2}(?:\s*(?:[.\-/년]\s*)\d{1,2})?(?:\s*(?:[.\-/월]\s*)\d{1,2})?\.?",
        text,
    )
    intervals: list[tuple[date, date]] = []
    for match in matches:
        year, month, day = _normalize_date_parts(match)
        if not year:
            continue
        year_num = int(year)
        if not month:
            intervals.append((date(year_num, 1, 1), date(year_num, 12, 31)))
            continue
        month_num = int(month)
        if not day:
            intervals.append(
                (
                    date(year_num, month_num, 1),
                    date(year_num, month_num, monthrange(year_num, month_num)[1]),
                )
            )
            continue
        parsed = date(year_num, month_num, int(day))
        intervals.append((parsed, parsed))

    if not intervals:
        return None, None
    return min(start for start, _ in intervals), max(end for _, end in intervals)


def _evaluation_date_entries(evaluation: Any) -> list[tuple[str, str]]:
    base_name = clean_text(_get(evaluation, "test_name", "")) or "시험"
    base_date = clean_text(_get(evaluation, "test_period", "")) or clean_text(
        _get(evaluation, "test_date", "")
    )
    lot_judgements = list(_get(evaluation, "lot_judgements", []) or [])
    if not lot_judgements:
        return [(base_name, base_date)]

    entries: list[tuple[str, str]] = []
    for item in lot_judgements:
        if not isinstance(item, dict):
            continue
        item_name = clean_text(item.get("item_value", "") or item.get("lot_no", ""))
        test_name = f"{base_name} - {item_name}" if item_name else base_name
        raw_date = (
            clean_text(item.get("test_period", ""))
            or clean_text(item.get("test_date", ""))
            or base_date
        )
        entries.append((test_name, raw_date))
    return entries or [(base_name, base_date)]


def _flowchart_next_stage_dates(
    records: list[Any],
    summary_items: list[ManufacturingInfoItem],
) -> dict[str, date]:
    summary_dates = {
        _norm_match(item.stage_name): _as_date(item.manufacturing_date)
        for item in summary_items
    }
    out: dict[str, date] = {}

    for record in records:
        if clean_text(_get(record, "record_type", "")) not in {"flowchart", "diagram"}:
            continue
        diagram_data = _get(record, "diagram_data", None) or {}
        if not isinstance(diagram_data, dict):
            continue
        nodes = diagram_data.get("nodes") or []
        edges = diagram_data.get("edges") or []
        if not isinstance(nodes, list) or not isinstance(edges, list):
            continue

        node_stage_keys = {
            clean_text(node.get("node_id", "")): _norm_match(node.get("name", ""))
            for node in nodes
            if isinstance(node, dict)
        }
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            from_key = node_stage_keys.get(clean_text(edge.get("from", "")), "")
            to_key = node_stage_keys.get(clean_text(edge.get("to", "")), "")
            next_date = summary_dates.get(to_key)
            if not from_key or not next_date:
                continue
            existing = out.get(from_key)
            if existing is None or next_date < existing:
                out[from_key] = next_date

    dated_items = [
        (key, value)
        for key, value in summary_dates.items()
        if value is not None
    ]
    for stage_key, stage_date in dated_items:
        if stage_key in out:
            continue
        later_dates = [candidate_date for _, candidate_date in dated_items if candidate_date > stage_date]
        if later_dates:
            out[stage_key] = min(later_dates)

    return out


def _evaluation_context(evaluation: Any, record: Any | None) -> str:
    parts = [
        clean_text(_get(evaluation, key, ""))
        for key in ["section_number", "section_title", "test_name", "content_label"]
    ]
    if record is not None:
        parts.append(_record_stage_context(record))
    return " ".join(part for part in parts if part)


def _is_albumin_records_source(records_source: Any, records: list[Any] | None = None) -> bool:
    records = records if records is not None else _records_from_source(records_source)
    fragments: list[str] = []
    for record in records[:80]:
        fragments.append(_join_record_text(record))
        fragments.append(clean_text(_get(record, "title", "")))
        fragments.append(clean_text(_get(record, "section_title", "")))
    text = _norm_match(" ".join(fragments))
    return bool(
        ("동국알부민" in text or "humanserumalbumin" in text or "albumin" in text)
        and ("원료혈장" in text or "원획분" in text)
    )


def _move_albumin_plasma_test_dates_to_first(
    out: dict[str, list[TestDateCompareResult]],
    summary_items: list[ManufacturingInfoItem],
) -> None:
    plasma_keys = [
        _norm_match(item.stage_name)
        for item in summary_items
        if "원료혈장" in _norm_match(item.stage_name)
    ]
    plasma_keys = [key for key in plasma_keys if key]
    if not plasma_keys:
        return

    target_key = _norm_match("원료혈장1")
    if target_key not in out:
        target_key = plasma_keys[0]

    merged: list[TestDateCompareResult] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for key in plasma_keys:
        for item in out.get(key, []):
            signature = (
                clean_text(item.test_name),
                clean_text(item.date_text),
                clean_text(item.status),
                clean_text(item.section_number),
                clean_text(str(item.page_number or "")),
            )
            if signature in seen:
                continue
            seen.add(signature)
            merged.append(item)

    for key in plasma_keys:
        out[key] = []
    out[target_key] = merged



def validate_stage_test_date_ranges(
    records_source: Any,
    summary_items: list[ManufacturingInfoItem] | None = None,
) -> dict[str, list[TestDateCompareResult]]:
    records = _records_from_source(records_source)
    summary_items = summary_items or extract_summary_items_from_records(records_source)
    evaluations = list(_get(records_source, "evaluations", []) or [])
    out: dict[str, list[TestDateCompareResult]] = {
        _norm_match(item.stage_name): [] for item in summary_items
    }
    if not evaluations:
        return out

    record_map: dict[int, Any] = {}
    for idx, record in enumerate(records):
        order = _get(record, "order_index", _get(record, "order_idx", idx))
        try:
            record_map[int(order)] = record
        except (TypeError, ValueError):
            continue

    next_dates = _flowchart_next_stage_dates(records, summary_items)
    summary_dates = {
        _norm_match(item.stage_name): _as_date(item.manufacturing_date)
        for item in summary_items
    }

    for evaluation in evaluations:
        order_idx = _get(evaluation, "order_idx", None)
        record = None
        try:
            record = record_map.get(int(order_idx))
        except (TypeError, ValueError):
            record = None
        context = _evaluation_context(evaluation, record)

        exact_matches = [
            item
            for item in summary_items
            if _norm_match(item.stage_name)
            and _norm_match(item.stage_name) in _norm_match(context)
        ]
        matches = exact_matches or [
            item
            for item in summary_items
            if _stage_context_matches(item.stage_name, context)
        ]
        if not matches:
            continue

        # 원료혈장1/2/3처럼 세부 단계가 하나의 시험 섹션으로 묶이면 가장
        # 이른 제조일부터 공통 다음 단계 전까지를 그룹 허용구간으로 사용한다.
        stage_item = sorted(
            matches,
            key=lambda item: _as_date(item.manufacturing_date) or date.max,
        )[0]
        stage_keys = [_norm_match(item.stage_name) for item in matches]
        stage_date_candidates = [
            summary_dates.get(key) for key in stage_keys if summary_dates.get(key)
        ]
        next_date_candidates = [
            next_dates.get(key) for key in stage_keys if next_dates.get(key)
        ]
        lower = min(stage_date_candidates) if stage_date_candidates else None
        upper = min(next_date_candidates) if next_date_candidates else None
        if lower is None:
            continue

        allowed = f"{_date_text(lower)} 이상"
        if upper:
            allowed += f", {_date_text(upper)} 미만"

        for test_name, raw_date in _evaluation_date_entries(evaluation):
            start_date, end_date = _extract_date_range(raw_date)
            if not start_date or not end_date:
                out.setdefault(_norm_match(stage_item.stage_name), []).append(
                    TestDateCompareResult(
                        test_name=test_name,
                        date_text=raw_date or "미확인",
                        status="보류",
                        reason=(
                            f"해당 시험은 제조단계에 연결되었지만 시험일을 확인하지 못했습니다. "
                            f"허용구간은 {allowed}입니다."
                        ),
                        section_number=clean_text(_get(evaluation, "section_number", "")),
                        page_number=_get(evaluation, "page_start", None),
                    )
                )
                continue

            ok = start_date >= lower and (upper is None or end_date < upper)
            if ok:
                reason = (
                    f"시험일 {raw_date}의 전체 범위가 해당 제조단계 허용구간"
                    f"({allowed})에 포함됩니다."
                )
            else:
                reason = (
                    f"시험일 {raw_date}의 전체 범위가 해당 제조단계 허용구간"
                    f"({allowed})을 벗어났습니다."
                )

            out.setdefault(_norm_match(stage_item.stage_name), []).append(
                TestDateCompareResult(
                    test_name=test_name,
                    date_text=raw_date,
                    status="합격" if ok else "불합격",
                    reason=reason,
                    section_number=clean_text(_get(evaluation, "section_number", "")),
                    page_number=_get(evaluation, "page_start", None),
                )
            )

    if _is_albumin_records_source(records_source, records):
        _move_albumin_plasma_test_dates_to_first(out, summary_items)

    return out


def _is_quantity_field(field_name: str) -> bool:
    key = _norm_match(field_name)
    return bool("제조량" in key or "제조수량" in key or "총제조수량" in key)


def _should_compare_occurrence_quantity(
    summary_item: ManufacturingInfoItem,
    occurrence: ManufacturingInfoOccurrence,
) -> bool:
    """Return True only when an occurrence is a valid manufacturing-quantity source.

    Some albumin sections contain usage amounts (사용량) or formula amounts (분량).
    Those values are valid document data, but they are not 제조량 and should not be
    compared against the manufacturing-summary quantity.
    """
    item = occurrence.item
    field_key = _norm_match(item.quantity_field_name)
    source_key = _norm_match(
        " ".join(
            [
                occurrence.source_label,
                occurrence.section_number,
                getattr(item, "source_label", ""),
            ]
        )
    )
    stage_key = _norm_match(summary_item.stage_name)

    usage_tokens = [
        "사용량",
        "분량",
        "원료약품",
        "사용단계참조",
        "사용된원료혈장",
        "사용된최종원액",
    ]
    if any(_norm_match(token) in field_key for token in usage_tokens):
        return False
    if any(_norm_match(token) in source_key for token in usage_tokens):
        return False

    # For albumin fractions, 4.x sections often describe downstream usage.
    # Manufacturing quantity must come from the 원획분 detail section itself.
    if _norm_match("원획분") in stage_key:
        section = clean_text(occurrence.section_number)
        label = clean_text(occurrence.source_label)
        return section.startswith("3.2.1") or "3.2.1" in label

    return True


def _should_mark_missing_quantity_as_info(
    summary_item: ManufacturingInfoItem,
    stage_occurrences: list[ManufacturingInfoOccurrence],
) -> bool:
    stage_key = _norm_match(summary_item.stage_name)

    if "원료혈장" not in stage_key:
        return False

    if not _has_numbered_stage_suffix(summary_item.stage_name):
        return False

    for occurrence in stage_occurrences:
        if clean_text(occurrence.section_number) != "1.2":
            continue
        if occurrence.item.quantity:
            continue
        if occurrence.item.manufacturing_no or occurrence.item.manufacturing_date:
            return True

    return False


def validate_manufacturing_info_consistency(
    records_source: Any,
) -> list[ManufacturingInfoValidationResult]:
    summary_items = extract_summary_items_from_records(records_source)
    occurrences = collect_manufacturing_info_occurrences(records_source, summary_items)
    test_dates = validate_stage_test_date_ranges(records_source, summary_items)

    results: list[ManufacturingInfoValidationResult] = []

    for summary_item in summary_items:
        stage_key = _norm_match(summary_item.stage_name)
        stage_occurrences = list(occurrences.get(stage_key, []))

        if not stage_occurrences:
            results.append(
                ManufacturingInfoValidationResult(
                    stage_name=summary_item.stage_name,
                    status="보류",
                    fields=[
                        FieldCompareResult(
                            field_name="정보 섹션",
                            summary_value="제조요약도 값 존재",
                            document_value="본문 정보 섹션 미확인",
                            status="보류",
                            reason="명시적인 제조정보 라벨과 연결된 본문 값을 찾지 못해 자동 비교하지 않았습니다.",
                        )
                    ],
                    test_dates=list(test_dates.get(stage_key, [])),
                    source_count=0,
                )
            )
            continue

        field_results: list[FieldCompareResult] = []

        for occurrence in stage_occurrences:
            item = occurrence.item
            comparisons = [
                ("제조번호", summary_item.manufacturing_no, item.manufacturing_no),
                ("제조년월일", summary_item.manufacturing_date, item.manufacturing_date),
                (
                    summary_item.quantity_field_name or "제조량",
                    summary_item.quantity,
                    item.quantity,
                ),
            ]
            for field_name, summary_value, document_value in comparisons:
                # 해당 출처가 실제로 제공하는 값만 비교한다. 다른 단계가 함께
                # 들어 있는 표에서 존재하지 않는 열을 억지로 불합격 처리하지 않는다.
                if not clean_text(document_value):
                    continue
                if _is_quantity_field(field_name) and not _should_compare_occurrence_quantity(summary_item, occurrence):
                    continue
                result = _compare_field(field_name, summary_value, document_value)
                result.source_label = occurrence.source_label
                result.section_number = occurrence.section_number
                result.page_number = occurrence.page_number
                field_results.append(result)

        required_fields = [
            ("제조번호", summary_item.manufacturing_no),
            ("제조년월일", summary_item.manufacturing_date),
            (summary_item.quantity_field_name or "제조량", summary_item.quantity),
        ]
        compared_field_names = {field.field_name for field in field_results}
        for field_name, summary_value in required_fields:
            if not clean_text(summary_value) or field_name in compared_field_names:
                continue
            if _is_quantity_field(field_name) and _should_mark_missing_quantity_as_info(summary_item, stage_occurrences):
                field_results.append(
                    FieldCompareResult(
                        field_name=field_name,
                        summary_value=summary_value,
                        document_value="제조요약정보에 제조량 미기재",
                        status="합격",
                        reason="제조량 정보 없음 - 1.2 제조요약정보에 제조번호와 제조년월일만 기재되어 수량 비교는 제외했습니다.",
                        source_label="1.2 제조요약정보",
                    )
                )
                continue
            field_results.append(
                FieldCompareResult(
                    field_name=field_name,
                    summary_value=summary_value,
                    document_value="",
                    status="보류",
                    reason="문서 JSON/OCR에서 비교 가능한 본문 값을 확인하지 못했습니다.",
                    source_label="문서 전체",
                )
            )

        stage_date_results = list(test_dates.get(stage_key, []))
        all_statuses = [
            *(field.status for field in field_results),
            *(item.status for item in stage_date_results),
        ]
        if all_statuses and all(status == "합격" for status in all_statuses):
            overall_status = "합격"
        elif any(status == "불합격" for status in all_statuses):
            overall_status = "불합격"
        else:
            overall_status = "보류"

        results.append(
            ManufacturingInfoValidationResult(
                stage_name=summary_item.stage_name,
                status=overall_status,
                fields=field_results,
                test_dates=stage_date_results,
                source_count=len(stage_occurrences),
            )
        )

    same_no_result = _build_same_manufacturing_no_validation(records_source)
    if same_no_result is not None:
        results.append(same_no_result)

    return results


def _validation_anchor_key(stage_name: str) -> str:
    normalized = _norm_match(stage_name) or clean_text(stage_name)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def _section_anchor_id(section_number: str) -> str:
    section_number = clean_text(section_number)
    if not section_number:
        return ""
    return f"sp-section-{hashlib.sha1(section_number.encode('utf-8')).hexdigest()[:16]}"


def _scroll_attrs(source_target: str, fallback_target: str) -> str:
    source_target = html.escape(source_target or fallback_target)
    fallback_target = html.escape(fallback_target)
    return (
        f' role="button" tabindex="0" data-mfg-source-target="{source_target}"'
        f' data-mfg-fallback-target="{fallback_target}"'
        ' onclick="event.preventDefault();event.stopPropagation();try{'
        "var el=document.getElementById(this.getAttribute('data-mfg-source-target'))"
        "||document.getElementById(this.getAttribute('data-mfg-fallback-target'));"
        "if(el){el.scrollIntoView({behavior:'smooth',block:'start'});}"
        '}catch(e){}return false;"'
        ' onkeydown="if(event.key===\'Enter\'||event.key===\' \'){this.click();}"'
        ' title="해당 원문 위치로 이동"'
    )


def render_manufacturing_info_validation_card(
    records_source: Any,
    validation_results: list[ManufacturingInfoValidationResult] | None = None,
) -> str:
    results = validate_manufacturing_info_consistency(records_source) if validation_results is None else validation_results

    if not results:
        return """
        <div class="mfg-compact-card">
            <h3 class="mfg-compact-title">정합성 검증-제조량, 제조일자, 제조번호 검증</h3>
            <p class="mfg-compact-desc">비교 가능한 제조요약도 데이터가 없습니다.</p>
        </div>
        """

    rows: list[str] = []
    passed_checks = 0
    failed_checks = 0
    held_checks = 0

    for result in results:
        anchor = _validation_anchor_key(result.stage_name)
        stage_source_target = f"mfg-stage-{anchor}"
        validation_anchor = f"mfg-validation-{anchor}"
        issue_fields = [field for field in result.fields if field.status != "합격"]
        info_fields = [
            field
            for field in result.fields
            if field.status == "합격" and "정보 없음" in field.reason
        ]
        issue_dates = [item for item in result.test_dates if item.status != "합격"]
        passed_checks += sum(field.status == "합격" for field in result.fields)
        passed_checks += sum(item.status == "합격" for item in result.test_dates)
        failed_checks += sum(field.status == "불합격" for field in result.fields)
        failed_checks += sum(item.status == "불합격" for item in result.test_dates)
        held_checks += sum(field.status == "보류" for field in result.fields)
        held_checks += sum(item.status == "보류" for item in result.test_dates)

        issue_rows: list[str] = []
        for field in issue_fields:
            source = field.source_label or "문서 본문"
            issue_kind = (
                f"{field.field_name} 확인 불가"
                if field.status == "보류"
                else f"{field.field_name} 불일치"
            )
            issue_class = "hold" if field.status == "보류" else "fail"
            source_target = _section_anchor_id(field.section_number) or stage_source_target
            scroll_attrs = _scroll_attrs(source_target, stage_source_target)
            issue_rows.append(
                f"""
                <div class="mfg-issue-row {issue_class}"{scroll_attrs}>
                    <div class="mfg-issue-kind">{html.escape(issue_kind)}</div>
                    <div class="mfg-issue-source">{html.escape(source)}</div>
                    <div class="mfg-issue-values">
                        <span>{html.escape(field.baseline_label)} <b>{html.escape(field.summary_value or '-')}</b></span>
                        <span>{html.escape(field.comparison_label)} <b>{html.escape(field.document_value or '-')}</b></span>
                    </div>
                    <div class="mfg-issue-reason">{html.escape(field.reason)}</div>
                </div>
                """
            )

        for field in info_fields:
            source = field.source_label or "문서 본문"
            source_target = _section_anchor_id(field.section_number) or stage_source_target
            scroll_attrs = _scroll_attrs(source_target, stage_source_target)
            issue_rows.append(
                f"""
                <div class="mfg-issue-row info"{scroll_attrs}>
                    <div class="mfg-issue-kind">{html.escape(field.field_name)} 정보 없음</div>
                    <div class="mfg-issue-source">{html.escape(source)}</div>
                    <div class="mfg-issue-values">
                        <span>{html.escape(field.baseline_label)} <b>{html.escape(field.summary_value or '-')}</b></span>
                        <span>{html.escape(field.comparison_label)} <b>{html.escape(field.document_value or '-')}</b></span>
                    </div>
                    <div class="mfg-issue-reason">{html.escape(field.reason)}</div>
                </div>
                """
            )

        for item in issue_dates:
            source_parts = [
                item.section_number,
                f"{item.page_number}쪽" if item.page_number else "",
            ]
            source = " · ".join(part for part in source_parts if part) or "시험 상세"
            issue_kind = (
                "시험일 확인 필요"
                if item.status == "보류"
                else "시험일 허용구간 이탈"
            )
            issue_class = "hold" if item.status == "보류" else "fail"
            source_target = _section_anchor_id(item.section_number) or stage_source_target
            scroll_attrs = _scroll_attrs(source_target, stage_source_target)
            issue_rows.append(
                f"""
                <div class="mfg-issue-row {issue_class}"{scroll_attrs}>
                    <div class="mfg-issue-kind">{issue_kind}</div>
                    <div class="mfg-issue-source">{html.escape(source)}</div>
                    <div class="mfg-issue-values">
                        <span>시험 <b>{html.escape(item.test_name)}</b></span>
                        <span>시험일 <b>{html.escape(item.date_text)}</b></span>
                    </div>
                    <div class="mfg-issue-reason">{html.escape(item.reason)}</div>
                </div>
                """
            )

        issue_count = len(issue_fields) + len(issue_dates)
        date_passes = sum(item.status == "합격" for item in result.test_dates)
        date_failures = sum(item.status == "불합격" for item in result.test_dates)
        date_holds = sum(item.status == "보류" for item in result.test_dates)
        date_summary = (
            f"시험일만 {len(result.test_dates)}건"
            f" · 시험일 충족 {date_passes}"
            f" · 시험일 불충족 {date_failures}"
            f" · 시험일 보류 {date_holds}"
        )
        stage_class = {
            "합격": "pass",
            "보류": "hold",
        }.get(result.status, "fail")
        if stage_class == "pass":
            stage_status = "전체 일치"
        elif stage_class == "hold":
            stage_status = f"정보·시험일 확인 필요 {issue_count}건"
        else:
            stage_status = f"정보·시험일 불일치 {issue_count}건"
        baseline_values: dict[str, str] = {}
        for field in result.fields:
            if field.summary_value and field.field_name not in baseline_values:
                baseline_values[field.field_name] = field.summary_value
        baseline_html = "".join(
            f'<span><b>{html.escape(field_name)}</b> {html.escape(value)}</span>'
            for field_name, value in baseline_values.items()
        )
        stage_scroll_attrs = _scroll_attrs(stage_source_target, stage_source_target)
        rows.append(
            f"""
            <section id="{validation_anchor}" class="mfg-stage-compact {stage_class}"{stage_scroll_attrs}>
                <div class="mfg-stage-head">
                    <div>
                        <div class="mfg-stage-name">{html.escape(result.stage_name)}</div>
                        <div class="mfg-stage-meta">
                            본문 출처 {result.source_count}곳 · {date_summary}<br>아래 상세 규칙과 같은 점검 결과이며 별도 오류로 중복 집계하지 않습니다.
                        </div>
                    </div>
                    <span class="mfg-stage-status {stage_class}">{stage_status}</span>
                </div>
                <div class="mfg-baseline-list">{baseline_html}</div>
                {''.join(issue_rows) if issue_rows else '<div class="mfg-no-issues">제조번호, 제조년월일, 제조량과 시험일 범위가 모두 일치합니다.</div>'}
            </section>
            """
        )

    total_checks = passed_checks + failed_checks + held_checks
    overall_pass = failed_checks == 0 and held_checks == 0
    overall_class = "fail" if failed_checks else ("hold" if held_checks else "pass")
    overall_text = (
        "전체 일치"
        if overall_pass
        else ("불일치 항목 있음" if failed_checks else "자동 확인이 필요한 항목 있음")
    )
    overall_border = {
        "pass": "#22c55e",
        "hold": "#f59e0b",
        "fail": "#ef4444",
    }[overall_class]

    return f"""
    <style>
    html {{ scroll-behavior: smooth; }}
    .mfg-compact-card {{
        margin: 18px auto 0;
        max-width: 1580px;
        padding: 32px 34px;
        border: 1px solid #d8e2ef;
        border-left: 6px solid {overall_border};
        border-radius: 12px;
        background: #ffffff;
        box-shadow: 0 6px 18px rgba(15, 23, 42, 0.06);
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        color: #111827;
    }}
    .mfg-compact-head {{
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 16px;
        margin-bottom: 14px;
    }}
    .mfg-compact-title {{ margin: 0 0 10px; font-size: 34px; font-weight: 900; letter-spacing: 0; }}
    .mfg-compact-desc {{ margin: 0; color: #64748b; font-size: 20px; line-height: 1.65; }}
    .mfg-total-badge {{
        min-width: 280px;
        padding: 16px 18px;
        border-radius: 8px;
        font-size: 21px;
        font-weight: 900;
    }}
    .mfg-total-badge.pass {{ background: #f0fdf4; color: #15803d; border: 1px solid #bbf7d0; }}
    .mfg-total-badge.fail {{ background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }}
    .mfg-total-badge.hold {{ background: #fffbeb; color: #b45309; border: 1px solid #fde68a; }}
    .mfg-total-badge small {{ display: block; margin-top: 8px; color: #475569; font-size: 17px; line-height: 1.45; }}
    .mfg-stage-grid {{ display: grid; gap: 18px; }}
    .mfg-stage-compact {{
        scroll-margin-top: 24px;
        border: 1px solid #e5edf7;
        border-left: 5px solid #22c55e;
        border-radius: 8px;
        padding: 24px 28px;
        background: #f8fafc;
        cursor: pointer;
    }}
    .mfg-stage-compact.fail {{ border-left-color: #ef4444; background: #fffafa; }}
    .mfg-stage-compact.hold {{ border-left-color: #f59e0b; background: #fffbeb; }}
    .mfg-stage-compact:hover, .mfg-stage-compact:focus-visible {{
        outline: 2px solid #60a5fa;
        outline-offset: 2px;
        box-shadow: 0 10px 22px rgba(15, 23, 42, 0.08);
    }}
    .mfg-stage-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }}
    .mfg-stage-name {{ font-size: 24px; font-weight: 900; letter-spacing: 0; }}
    .mfg-stage-meta {{ margin-top: 7px; color: #64748b; font-size: 18px; line-height: 1.45; }}
    .mfg-stage-status {{ padding: 10px 15px; border-radius: 999px; font-size: 18px; font-weight: 900; white-space: nowrap; }}
    .mfg-stage-status.pass {{ color: #15803d; background: #dcfce7; }}
    .mfg-stage-status.fail {{ color: #b91c1c; background: #fee2e2; }}
    .mfg-stage-status.hold {{ color: #b45309; background: #fef3c7; }}
    .mfg-baseline-list {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }}
    .mfg-baseline-list span {{
        padding: 10px 13px;
        border: 1px solid #dbe5f0;
        border-radius: 6px;
        color: #334155;
        background: #ffffff;
        font-size: 18px;
    }}
    .mfg-no-issues {{ margin-top: 18px; color: #166534; font-size: 19px; font-weight: 800; }}
    .mfg-issue-row {{
        display: grid;
        grid-template-columns: minmax(210px, .9fr) minmax(230px, 1fr) minmax(360px, 1.6fr);
        gap: 12px 18px;
        margin-top: 14px;
        padding: 20px 22px;
        border: 1px solid #fecaca;
        border-radius: 8px;
        background: #ffffff;
        cursor: pointer;
    }}
    .mfg-issue-row.hold {{ border-color: #fde68a; }}
    .mfg-issue-row.info {{ border-color: #bfdbfe; background: #f8fbff; }}
    .mfg-issue-row.info .mfg-issue-kind {{ color: #1d4ed8; }}
    .mfg-issue-row:hover, .mfg-issue-row:focus-visible {{
        outline: 2px solid #93c5fd;
        outline-offset: 2px;
        box-shadow: 0 8px 18px rgba(15, 23, 42, 0.08);
    }}
    .mfg-issue-kind {{ color: #b91c1c; font-size: 20px; font-weight: 900; }}
    .mfg-issue-row.hold .mfg-issue-kind {{ color: #b45309; }}
    .mfg-issue-source {{ color: #475569; font-size: 18px; line-height: 1.45; }}
    .mfg-issue-values {{ display: flex; flex-wrap: wrap; gap: 8px 18px; font-size: 18px; line-height: 1.45; }}
    .mfg-issue-reason {{ grid-column: 1 / -1; color: #7f1d1d; font-size: 18px; line-height: 1.6; }}
    @media (max-width: 760px) {{
        .mfg-compact-head, .mfg-stage-head {{ display: block; }}
        .mfg-total-badge, .mfg-stage-status {{ display: inline-block; margin-top: 10px; min-width: 0; }}
        .mfg-issue-row {{ grid-template-columns: 1fr; }}
        .mfg-issue-reason {{ grid-column: auto; }}
    }}
    </style>
    <div class="mfg-compact-card">
        <div class="mfg-compact-head">
            <div>
                <h3 class="mfg-compact-title">정합성 검증-제조량, 제조일자, 제조번호 검증</h3>
                <p class="mfg-compact-desc">
                    제조요약도 값을 문서 전체의 제조번호·제조년월일·제조량과 비교합니다.
                </p>
            </div>
            <div class="mfg-total-badge {overall_class}">
                <div>{overall_text}</div>
                <small>총 {total_checks}건 · 일치 {passed_checks}건 · 불일치 {failed_checks}건 · 확인 필요 {held_checks}건</small>
            </div>
        </div>
        <div class="mfg-stage-grid">{''.join(rows)}</div>
    </div>
    """
