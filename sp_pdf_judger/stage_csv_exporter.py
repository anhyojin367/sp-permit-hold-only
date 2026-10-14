from __future__ import annotations

import csv
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

import fitz

from .config import FAIL_LABEL, HOLD_LABEL, PASS_LABEL
from .schemas import Evaluation, ProcessingResult
from .utils import clean_text


CSV_COLUMNS = [
    "시험명",
    "시험 방법",
    "시험 기준",
    "시험 기간",
    "시험 결과",
    "검수 결과",
]

SUMMARY_COLUMNS = [
    "제조명",
    "검수합격",
    "검수불합격",
    "검수보류",
    "총 계",
]


@dataclass(frozen=True)
class DynamicStage:
    display_name: str
    filename_stem: str


@dataclass
class StageExportResult:
    stage_name: str
    csv_path: Path
    evaluations: list[Evaluation]


def _norm_match(text: str | None) -> str:
    text = clean_text(text)

    if not text:
        return ""

    text = text.casefold()

    text = text.replace("e.coli", "ecoli")
    text = text.replace("e coli", "ecoli")
    text = text.replace("e-coli", "ecoli")
    text = text.replace("e.coLi".casefold(), "ecoli")

    text = text.replace("컴포넌트", "component")
    text = re.sub(r"[\s\-_·.,:：/(){}\[\]<>]+", "", text)

    return text


def _safe_filename(name: str) -> str:
    name = clean_text(name)

    if not name:
        return "stage"

    name = name.replace("E.coli", "E_coli")
    name = name.replace("E.coLi", "E_coli")
    name = name.replace(".", "_")
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", "_", name)
    name = name.strip("_")

    return name or "stage"


def _status_to_csv_text(status: str | None) -> str:
    status = clean_text(status)

    if status == PASS_LABEL:
        return "검수합격"

    if status == FAIL_LABEL:
        return "검수불합격"

    if status == HOLD_LABEL:
        return "검수보류"

    return status


def _status_from_explicit_reason(status: str | None, reason: str | None = None) -> str | None:
    # Keep reason-only inference for legacy records, not as a second judge.
    explicit = clean_text(status)
    if explicit in (PASS_LABEL, FAIL_LABEL, HOLD_LABEL):
        return explicit
    reason_text = clean_text(reason or "")

    if not reason_text:
        return status

    compact = re.sub(r"\s+", "", reason_text)

    failed_tokens = (
        "\uac80\uc218\ubd88\ud569\uaca9",
        "\ubd88\ud569\uaca9\uc73c\ub85c\ud310\ub2e8",
        "\uac80\uc218\ubd88\ucda9\uc871",
        "\ubd88\ucda9\uc871\uc73c\ub85c\ud310\ub2e8",
        "\ubd88\ucda9\uc871\uc785\ub2c8\ub2e4",
        "\ubd88\ucda9\uc871\ucc98\ub9ac",
    )
    passed_tokens = (
        "\uac80\uc218\ud569\uaca9",
        "\ud569\uaca9\uc73c\ub85c\ud310\ub2e8",
        "\uac80\uc218\ucda9\uc871",
        "\ucda9\uc871\uc73c\ub85c\ud310\ub2e8",
        "\ucda9\uc871\uc785\ub2c8\ub2e4",
        "\ucda9\uc871\ucc98\ub9ac",
    )
    held_tokens = (
        "\uac80\uc218\ubcf4\ub958",
        "\ubcf4\ub958\ub85c\ud310\ub2e8",
    )

    if any(token in compact for token in failed_tokens):
        return FAIL_LABEL

    if any(token in compact for token in passed_tokens) and not any(token in compact for token in failed_tokens):
        return PASS_LABEL

    if any(token in compact for token in held_tokens):
        return HOLD_LABEL

    return status


def _evaluation_to_rows(evaluation: Evaluation) -> list[dict[str, str]]:
    test_name = clean_text(getattr(evaluation, "test_name", ""))
    method = clean_text(getattr(evaluation, "method", ""))
    criteria = clean_text(getattr(evaluation, "criteria", ""))

    base_period = (
        clean_text(getattr(evaluation, "test_period", ""))
        or clean_text(getattr(evaluation, "test_date", ""))
    )

    lot_judgements = list(getattr(evaluation, "lot_judgements", []) or [])

    if lot_judgements:
        rows: list[dict[str, str]] = []

        for item in lot_judgements:
            item_value = clean_text(
                item.get("item_value", "")
                or item.get("lot_no", "")
            )

            row_test_name = test_name

            if item_value:
                row_test_name = f"{test_name} - {item_value}"

            rows.append(
                {
                    "시험명": row_test_name,
                    "시험 방법": method,
                    "시험 기준": criteria,
                    "시험 기간": clean_text(item.get("test_date", "")) or base_period,
                    "시험 결과": clean_text(item.get("result", "")),
                    "검수 결과": _status_to_csv_text(
                        _status_from_explicit_reason(item.get("status", ""), item.get("reason", ""))
                    ),
                }
            )

        return rows

    return [
        {
            "시험명": test_name,
            "시험 방법": method,
            "시험 기준": criteria,
            "시험 기간": base_period,
            "시험 결과": clean_text(getattr(evaluation, "result", "")),
            "검수 결과": _status_to_csv_text(
                _status_from_explicit_reason(getattr(evaluation, "final_status", ""), getattr(evaluation, "reason", ""))
            ),
        }
    ]


def _summary_row(stage_name: str, evaluations: list[Evaluation]) -> dict[str, str | int]:
    passed = 0
    failed = 0
    held = 0

    for ev in evaluations:
        lot_judgements = list(getattr(ev, "lot_judgements", []) or [])
        parent_status = clean_text(
            _status_from_explicit_reason(getattr(ev, "final_status", None), getattr(ev, "reason", ""))
        )

        if lot_judgements:
            for item in lot_judgements:
                status = clean_text(_status_from_explicit_reason(item.get("status", ""), item.get("reason", "")))
                item_reason = clean_text(item.get("reason", "") or item.get("judgement_reason", ""))
                if status not in {PASS_LABEL, FAIL_LABEL, HOLD_LABEL} and not item_reason and parent_status in {PASS_LABEL, FAIL_LABEL}:
                    status = parent_status

                if status == PASS_LABEL:
                    passed += 1
                elif status == FAIL_LABEL:
                    failed += 1
                elif status == HOLD_LABEL:
                    held += 1

            continue

        status = _status_from_explicit_reason(getattr(ev, "final_status", None), getattr(ev, "reason", ""))

        if status == PASS_LABEL:
            passed += 1
        elif status == FAIL_LABEL:
            failed += 1
        elif status == HOLD_LABEL:
            held += 1

    total = passed + failed + held

    return {
        "제조명": stage_name,
        "검수합격": passed,
        "검수불합격": failed,
        "검수보류": held,
        "총 계": total,
    }


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        for row in rows:
            writer.writerow(row)


def _write_summary_csv(path: Path, rows: list[dict[str, str | int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS)
        writer.writeheader()

        for row in rows:
            writer.writerow(row)


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


def _split_line_by_midpoint(line: dict, page_width: float) -> list[str]:
    words = line.get("words", [])

    if not words:
        return [clean_text(line.get("text", ""))]

    mid = page_width / 2

    left_words: list[tuple] = []
    right_words: list[tuple] = []

    for word in words:
        x_center = (float(word[0]) + float(word[2])) / 2

        if x_center < mid:
            left_words.append(word)
        else:
            right_words.append(word)

    left_text = clean_text(" ".join(w[4] for w in sorted(left_words, key=lambda x: x[0])))
    right_text = clean_text(" ".join(w[4] for w in sorted(right_words, key=lambda x: x[0])))

    if left_text and right_text:
        return [left_text, right_text]

    return [clean_text(line.get("text", ""))]


def _looks_like_stage_name(label: str | None) -> bool:
    label = clean_text(label)
    compact = _norm_match(label)

    if not compact:
        return False

    blocked_exact = {
        "componenta",
        "componentb",
        "componentacomponentb",
        "공통흐름",
        "좌측흐름",
        "우측흐름",
        "공정명미확인",
        "제조번호",
        "제조년월일",
        "제조량",
        "제조수량",
        "제조번호제조번호",
        "제조년월일제조년월일",
    }

    if compact in blocked_exact:
        return False

    blocked_contains = [
        "페이지",
        "제조요약도",
        "summaryprotocol",
        "production",
        "qualitycontrol",
        "동국약품",
        "주식회사",
        "제조번호",
        "제조년월일",
        "제조량",
        "제조수량",
        "제조일",
        "유효",
        "보관조건",
    ]

    if any(x in compact for x in blocked_contains):
        return False

    if re.fullmatch(r"20\d{2}(?:[.\-/]\d{1,2}){0,2}", label):
        return False

    if re.fullmatch(r"[A-Z]{1,5}[-_][A-Z0-9-]+", label, flags=re.I):
        return False

    if re.fullmatch(r"\d+", label):
        return False

    if len(compact) < 3:
        return False

    stage_words = [
        "세포주",
        "원액",
        "완제",
        "의약품",
        "바이알",
        "중간체",
        "나노파티클",
        "componenta",
        "componentb",
        "ecoli",
        "cho",
    ]

    if not any(word in compact for word in stage_words):
        return False

    return True


def _next_lines_contain_manufacturing_fields(lines: list[dict], idx: int, window: int = 4) -> bool:
    for next_line in lines[idx + 1 : idx + 1 + window]:
        text = clean_text(next_line.get("text", ""))
        if any(field in text for field in ["제조번호", "제조년월일", "제조량", "제조수량"]):
            return True

    return False


def _extract_stage_names_from_pdf(pdf_path: Path | None) -> list[str]:
    if pdf_path is None or not Path(pdf_path).exists():
        return []

    out: list[str] = []
    seen: set[str] = set()

    with fitz.open(pdf_path) as doc:
        for page in doc:
            page_text = clean_text(page.get_text("text") or "")
            compact_page_text = re.sub(r"\s+", "", page_text)

            if "제조요약도" not in compact_page_text:
                continue

            words = page.get_text("words") or []
            lines = _group_words_to_lines(words)

            if not lines:
                continue

            page_width = float(page.rect.width)

            start_idx = 0

            for idx, line in enumerate(lines):
                if "제조요약도" in clean_text(line.get("text", "")):
                    start_idx = idx + 1
                    break

            for idx in range(start_idx, len(lines)):
                line = lines[idx]
                line_text = clean_text(line.get("text", ""))

                if not line_text:
                    continue

                if not _next_lines_contain_manufacturing_fields(lines, idx):
                    continue

                parts = _split_line_by_midpoint(line, page_width)

                for part in parts:
                    part = clean_text(part)

                    if not _looks_like_stage_name(part):
                        continue

                    key = _norm_match(part)

                    if key in seen:
                        continue

                    seen.add(key)
                    out.append(part)

    return out


def _fallback_stage_names_from_section_titles(result: ProcessingResult) -> list[str]:
    evaluations = list(getattr(result, "evaluations", []) or [])

    out: list[str] = []
    seen: set[str] = set()

    for ev in evaluations:
        section_title = clean_text(getattr(ev, "section_title", ""))

        if not section_title:
            continue

        title = re.sub(r"^\d+(?:\.\d+)*\s*", "", section_title).strip()
        title = title.replace("에 대한 시험", "").strip()
        title = title.replace("시험", "").strip()

        if not _looks_like_stage_name(title):
            continue

        key = _norm_match(title)

        if key in seen:
            continue

        seen.add(key)
        out.append(title)

    return out


def _extract_dynamic_stages(result: ProcessingResult) -> list[DynamicStage]:
    pdf_path = getattr(result, "pdf_path", None)

    stage_names = _extract_stage_names_from_pdf(Path(pdf_path) if pdf_path else None)

    if not stage_names:
        stage_names = _fallback_stage_names_from_section_titles(result)

    stages: list[DynamicStage] = []
    seen: set[str] = set()

    for name in stage_names:
        key = _norm_match(name)

        if not key or key in seen:
            continue

        seen.add(key)

        stages.append(
            DynamicStage(
                display_name=name,
                filename_stem=_safe_filename(name),
            )
        )

    return stages


def _evaluation_text(evaluation: Evaluation) -> str:
    return " ".join(
        [
            clean_text(getattr(evaluation, "section_number", "")),
            clean_text(getattr(evaluation, "section_title", "")),
            clean_text(getattr(evaluation, "test_name", "")),
            clean_text(getattr(evaluation, "method", "")),
            clean_text(getattr(evaluation, "criteria", "")),
            clean_text(getattr(evaluation, "result", "")),
            clean_text(getattr(evaluation, "raw_text", "")),
        ]
    )


def _record_text(record) -> str:
    return " ".join(
        [
            clean_text(getattr(record, "section_number", "")),
            clean_text(getattr(record, "section_title", "")),
            clean_text(getattr(record, "test_name", "")),
            clean_text(getattr(record, "content_label", "")),
            clean_text(getattr(record, "content", "")),
            clean_text(getattr(record, "raw_text", "")),
        ]
    )


def _processing_result_text(result: ProcessingResult) -> str:
    parts: list[str] = []
    parts.append(clean_text(getattr(result, "product_name", "")))
    records = list(getattr(result, "extracted_records", []) or []) or list(getattr(result, "records", []) or [])
    for record in records:
        parts.append(_record_text(record))
    for evaluation in list(getattr(result, "evaluations", []) or []):
        parts.append(_evaluation_text(evaluation))
    return " ".join(part for part in parts if part)


def _is_albumin_document(result: ProcessingResult) -> bool:
    text = _norm_match(_processing_result_text(result))
    return bool(
        ("동국알부민" in text or "humanserumalbumin" in text or "albumin" in text)
        and ("원료혈장" in text or "원획분" in text)
    )


def _is_albumin_plasma_test_section(evaluation: Evaluation) -> bool:
    section_number = clean_text(getattr(evaluation, "section_number", ""))
    return section_number == "2.2" or section_number.startswith("2.2.")


def _albumin_stage_order() -> list[str]:
    return [
        "원료혈장1",
        "원료혈장2",
        "원료혈장3",
        "원료혈장4",
        "원료혈장5",
        "원료혈장6",
        "원획분1",
        "최종원액",
        "완제의약품",
    ]


def _zero_stage_export(stage_name: str, output_dir: Path) -> StageExportResult:
    csv_path = output_dir / f"{_safe_filename(stage_name)}.csv"
    if not csv_path.exists():
        _write_csv(csv_path, [])
    return StageExportResult(stage_name=stage_name, csv_path=csv_path, evaluations=[])


def _stage_directly_matches_text(stage_name: str, text: str) -> bool:
    stage_key = _norm_match(stage_name)
    text_key = _norm_match(text)

    if not stage_key or not text_key:
        return False

    if stage_key in text_key:
        return True

    stage_without_suffix = stage_key.replace("에대한시험", "")

    if stage_without_suffix and len(stage_without_suffix) >= 3:
        if stage_without_suffix in text_key:
            return True

    return False


def _component_branch_key(stage_name: str) -> str:
    key = _norm_match(stage_name)

    if "componenta" in key:
        return "componenta"

    if "componentb" in key:
        return "componentb"

    if "cho" in key and "ecoli" not in key:
        return "cho"

    if "ecoli" in key:
        return "ecoli"

    return ""


def _stage_soft_matches_text(stage_name: str, text: str) -> bool:
    stage_key = _norm_match(stage_name)
    text_key = _norm_match(text)

    if not stage_key or not text_key:
        return False

    branch = _component_branch_key(stage_name)

    if branch and branch in text_key:
        if "중간체원액" in stage_key or "원액" in stage_key:
            if any(x in text_key for x in ["본배양액", "중간체원액", "중간체"]):
                return True

    return False


def _find_top_level_prefixes_for_stage(result: ProcessingResult, stage_name: str) -> set[str]:
    prefixes: set[str] = set()
    records = list(getattr(result, "extracted_records", []) or [])

    for record in records:
        section_number = clean_text(getattr(record, "section_number", ""))
        text = _record_text(record)

        if not section_number:
            continue

        if not re.fullmatch(r"\d+", section_number):
            continue

        if _stage_directly_matches_text(stage_name, text):
            prefixes.add(section_number)

    return prefixes


def _section_starts_with_prefix(section_number: str | None, prefix: str) -> bool:
    section_number = clean_text(section_number)
    prefix = clean_text(prefix)

    if not section_number or not prefix:
        return False

    return section_number == prefix or section_number.startswith(prefix + ".")


def _find_evaluations_for_stage(
    result: ProcessingResult,
    stage_name: str,
) -> list[Evaluation]:
    evaluations = list(getattr(result, "evaluations", []) or [])

    if _is_albumin_document(result):
        stage_key = _norm_match(stage_name)
        if stage_key == _norm_match("원료혈장1"):
            return sorted(
                [ev for ev in evaluations if _is_albumin_plasma_test_section(ev)],
                key=lambda x: getattr(x, "order_idx", 0),
            )
        if stage_key in {
            _norm_match("원료혈장2"),
            _norm_match("원료혈장3"),
            _norm_match("원료혈장4"),
            _norm_match("원료혈장5"),
            _norm_match("원료혈장6"),
        }:
            return []
        if stage_key == _norm_match("원획분1"):
            fraction_matches = []
            for ev in evaluations:
                section_number = clean_text(getattr(ev, "section_number", ""))
                text = _evaluation_text(ev)
                if (
                    section_number == "3"
                    or section_number.startswith("3.")
                    or _stage_directly_matches_text("원획분", text)
                    or _stage_soft_matches_text("원획분", text)
                ):
                    fraction_matches.append(ev)
            if fraction_matches:
                return sorted(fraction_matches, key=lambda x: getattr(x, "order_idx", 0))

    matched: list[Evaluation] = []
    seen_order_idx: set[int] = set()

    def add_matches(items: list[Evaluation]) -> None:
        for ev in items:
            order_idx = int(getattr(ev, "order_idx", -1))
            if order_idx in seen_order_idx:
                continue
            seen_order_idx.add(order_idx)
            matched.append(ev)

    direct_matches: list[Evaluation] = []

    for ev in evaluations:
        text = _evaluation_text(ev)

        if _stage_directly_matches_text(stage_name, text):
            direct_matches.append(ev)

    add_matches(direct_matches)

    soft_matches: list[Evaluation] = []

    for ev in evaluations:
        text = _evaluation_text(ev)

        if _stage_soft_matches_text(stage_name, text):
            soft_matches.append(ev)

    add_matches(soft_matches)

    if matched:
        return sorted(matched, key=lambda x: getattr(x, "order_idx", 0))

    top_prefixes = _find_top_level_prefixes_for_stage(result, stage_name)

    if top_prefixes:
        prefix_matches = [
            ev
            for ev in evaluations
            if any(
                _section_starts_with_prefix(
                    getattr(ev, "section_number", ""),
                    prefix,
                )
                for prefix in top_prefixes
            )
        ]

        return sorted(prefix_matches, key=lambda x: getattr(x, "order_idx", 0))

    return []


def export_stage_csvs(
    result: ProcessingResult,
    output_dir: Path,
) -> list[StageExportResult]:
    output_dir.mkdir(parents=True, exist_ok=True)

    if _is_albumin_document(result):
        stages = [
            DynamicStage(display_name=name, filename_stem=_safe_filename(name))
            for name in _albumin_stage_order()
        ]
    else:
        stages = _extract_dynamic_stages(result)

    export_results: list[StageExportResult] = []
    used_filenames: set[str] = set()

    for stage in stages:
        matched_evaluations = _find_evaluations_for_stage(
            result=result,
            stage_name=stage.display_name,
        )

        if not matched_evaluations:
            if _is_albumin_document(result) and _norm_match(stage.display_name) in {
                _norm_match("원료혈장2"),
                _norm_match("원료혈장3"),
                _norm_match("원료혈장4"),
                _norm_match("원료혈장5"),
                _norm_match("원료혈장6"),
            }:
                export_results.append(_zero_stage_export(stage.display_name, output_dir))
            continue

        rows: list[dict[str, str]] = []

        for ev in matched_evaluations:
            rows.extend(_evaluation_to_rows(ev))

        filename_stem = stage.filename_stem
        filename = f"{filename_stem}.csv"

        if filename in used_filenames:
            base = filename_stem
            idx = 2

            while f"{base}_{idx}.csv" in used_filenames:
                idx += 1

            filename = f"{base}_{idx}.csv"

        used_filenames.add(filename)

        csv_path = output_dir / filename
        _write_csv(csv_path, rows)

        export_results.append(
            StageExportResult(
                stage_name=stage.display_name,
                csv_path=csv_path,
                evaluations=matched_evaluations,
            )
        )

    return export_results


def _write_export_summary_csv(
    export_results: list[StageExportResult],
    csv_dir: Path,
) -> Path:
    summary_rows = [
        _summary_row(
            stage_name=export_result.stage_name,
            evaluations=export_result.evaluations,
        )
        for export_result in export_results
    ]

    total_passed = sum(int(row["검수합격"]) for row in summary_rows)
    total_failed = sum(int(row["검수불합격"]) for row in summary_rows)
    total_held = sum(int(row["검수보류"]) for row in summary_rows)
    total_count = sum(int(row["총 계"]) for row in summary_rows)

    summary_rows.append(
        {
            "제조명": "전체",
            "검수합격": total_passed,
            "검수불합격": total_failed,
            "검수보류": total_held,
            "총 계": total_count,
        }
    )

    summary_path = csv_dir / "summary.csv"
    _write_summary_csv(summary_path, summary_rows)

    return summary_path


def make_stage_csv_zip(
    result: ProcessingResult,
    output_dir: Path,
    zip_name: str = "manufacturing_stage_csvs.zip",
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_dir = output_dir / "csv"

    if csv_dir.exists():
        shutil.rmtree(csv_dir)

    csv_dir.mkdir(parents=True, exist_ok=True)

    export_results = export_stage_csvs(
        result=result,
        output_dir=csv_dir,
    )

    summary_path = _write_export_summary_csv(
        export_results=export_results,
        csv_dir=csv_dir,
    )

    zip_path = output_dir / zip_name

    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(summary_path, arcname=summary_path.name)

        for export_result in export_results:
            zf.write(export_result.csv_path, arcname=export_result.csv_path.name)

        if not export_results:
            info_path = csv_dir / "매핑된_시험_결과_없음.txt"
            info_path.write_text(
                "제조요약도 이름과 본문 시험 섹션을 매핑하지 못했습니다.\n"
                "제조요약도 박스 제목이 본문 시험 섹션명에 등장하는지 확인해 주세요.\n",
                encoding="utf-8",
            )
            zf.write(info_path, arcname=info_path.name)

    return zip_path
