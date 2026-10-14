"""SP review orchestration and currently used general-test helpers.

Document-wide checks execute through the Markdown policy engine. Historical
unused checks remain in the original Git snapshot, not in the runtime module.
"""
from __future__ import annotations
import hashlib
import re
import tempfile
from time import perf_counter
from collections import Counter
from dataclasses import asdict
from dataclasses import fields, replace
from datetime import date
from pathlib import Path
import fitz
from .config import FAIL_LABEL, HOLD_LABEL, PASS_LABEL
from .clova_transport import request_metrics, request_diagnostics, diagnostic_delta
from .domain_details import DomainDetailProfile, DomainDetailStore
from .extractor import extract_records
from .extraction_runtime import read_extraction_report, validate_extraction_report
from .hierarchy import build_document_tree
from .judgement import JudgeEngine
from .llm import ClovaExecutionSettings, ClovaJudgeClient
from .permit_semantic_settings import SemanticSettings, model_budget
from .permit_semantic_batch import PreparedAuthoritativeResponse, partition_semantic_requests, request_size_estimates
from .permit_catalog import ResolvedPermit, permit_hash_map, resolve_permits
from .permit_group_batching import (
    MAX_BATCH_PROMPT_CHARS,
    index_group_batch_response,
    make_group_batch_request,
    partition_group_batch_requests,
)
from .permit_grouping import build_permit_groups
from .permit_llm_protocol import build_group_batch_prompt
from .permit_pdf_store import PermitPdfStore
from .preview import render_first_page
from .rag import UcumRagStore
from .manufacturing_info_validator import _norm_match
from .schemas import Evaluation, ProcessingResult, Summary
from .sp_table_result_generalizer import expand_table_records_for_judgement
from .utils import clean_text, ensure_dir
from .policy_engine import RuleBook, PolicyContext, evaluate_policies, to_evaluations, normalize_record_units, normalize_record_result
from .numeric_safety import result_is_requirement
from .policy_summary import summarize_manufacturing_policies, manufacturing_policy_cards, apply_policy_test_guards
from .review_presentation import review_receipt, unit_audit_reason, file_sha256

GENERIC_EXTERNAL_CRITERIA_RE = re.compile(
    r"(허가서|허가사항|허가조건|승인사항|기준서|별도\s*기준).*(따름|준함|참조)|"
    r"(따름|준함|참조).*(허가서|허가사항|허가조건|승인사항|기준서|별도\s*기준)"
)


def _find_manufacturing_summary_pages(pdf_path: Path) -> list[int]:
    page_numbers: list[int] = []

    with fitz.open(pdf_path) as doc:
        for idx, page in enumerate(doc, start=1):
            text = clean_text(page.get_text("text") or "")
            compact = re.sub(r"\s+", "", text)

            if "제조요약도" in compact:
                page_numbers.append(idx)

    return page_numbers


def _pdf_artifact_stem(pdf_path: Path) -> str:
    """
    Windows/PyMuPDF 저장 경로가 MAX_PATH에 걸리지 않도록
    제출 PDF 파일명 대신 짧고 안정적인 작업 파일명을 만든다.

    새로 들어온 Gmail 첨부파일은 파일명이 길어질 수 있는데,
    그 파일명을 그대로 PNG/추출 폴더명에 붙이면
    PyMuPDF pix.save()가 cannot open file 오류를 낼 수 있다.
    """
    try:
        stat = pdf_path.stat()
        source = f"{pdf_path.resolve()}::{stat.st_size}::{stat.st_mtime_ns}"
    except Exception:
        source = str(pdf_path)

    digest = hashlib.sha1(source.encode("utf-8", "ignore")).hexdigest()[:16]
    return f"sp_{digest}"


def _render_pdf_page(
    pdf_path: Path,
    page_number: int,
    output_dir: Path,
    suffix: str,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_suffix = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", suffix or "page").strip("._-") or "page"
    out_path = output_dir / f"{safe_suffix}_page_{page_number}.png"

    with fitz.open(pdf_path) as doc:
        page = doc[page_number - 1]
        matrix = fitz.Matrix(2.0, 2.0)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        pix.save(str(out_path))

    return out_path


def _parse_date_value(value: str) -> date | None:
    value = clean_text(value)

    m = re.search(r"(20\d{2})[.\-/년\s]+(\d{1,2})(?:[.\-/월\s]+(\d{1,2}))?", value)

    if not m:
        return None

    year = int(m.group(1))
    month = int(m.group(2))
    day = int(m.group(3)) if m.group(3) else 1

    try:
        return date(year, month, day)
    except ValueError:
        return None


def _join_eval_text(ev: Evaluation) -> str:
    return "\n".join(
        clean_text(getattr(ev, attr, ""))
        for attr in [
            "test_name",
            "method",
            "criteria",
            "result",
            "test_date",
            "test_period",
            "remarks",
            "raw_text",
        ]
        if clean_text(getattr(ev, attr, ""))
    )


def _result_text(ev: Evaluation) -> str:
    return clean_text(getattr(ev, "result", "")) or clean_text(getattr(ev, "raw_text", ""))


def _has_generic_external_criteria(ev: Evaluation) -> bool:
    return bool(GENERIC_EXTERNAL_CRITERIA_RE.search(clean_text(getattr(ev, "criteria", ""))))


def _has_suitable_text(text: str) -> bool:
    text = clean_text(text)
    key = _norm_match(text)

    if not text:
        return False

    if "부적합" in text:
        return False

    return (
        "적합" in text
        or "음성" in text
        or "불검출" in text
        or "미검출" in text
        or "없음" in text
        or "인정되지않" in key
        or "관찰되지않" in key
        or "확인되지않" in key
        or "검출되지않" in key
        or "증식하지않" in key
        or "발육하지않" in key
    )


def _has_unsuitable_text(text: str) -> bool:
    text = clean_text(text)
    key = _norm_match(text)

    if not text:
        return False

    if "부적합" in text or "양성" in text:
        return True

    # Remove only the negated observation, not every observation in the result.
    # "불검출" contains "검출", while "A 불검출, B 검출" still has a failure.
    key = re.sub(r"불검출|미검출|(?:인정|관찰|확인|검출)되지않(?:음|았음|았다)?|"
                 r"(?:증식|발육)하지않(?:음|았음|았다)?", "", key)
    return any(token in key for token in ["검출", "확인됨", "관찰됨", "인정됨", "증식", "발육"])


def _date_values_from_text(text: str) -> list[date]:
    dates: list[date] = []
    for match in re.finditer(
        r"20\d{2}(?:\s*[.\-/년]\s*\d{1,2})(?:\s*[.\-/월]\s*\d{1,2})?",
        clean_text(text),
    ):
        parsed = _parse_date_value(match.group(0))
        if parsed is not None:
            dates.append(parsed)
    return dates


def _duration_days_from_evaluation(ev: Evaluation) -> int | None:
    text = "\n".join(
        clean_text(getattr(ev, attr, ""))
        for attr in ["test_period", "test_date", "raw_text"]
        if clean_text(getattr(ev, attr, ""))
    )
    dates = _date_values_from_text(text)

    if len(dates) < 2:
        return None

    return (max(dates) - min(dates)).days + 1


def _extract_float_values(text: str | None) -> list[float]:
    values: list[float] = []
    text = clean_text(text)
    if not text:
        return values

    for match in re.finditer(r"[-+]?\d+(?:\.\d+)?", text.replace(",", "")):
        try:
            values.append(float(match.group(0)))
        except ValueError:
            continue

    return values


def _set_general_method_decision(
    ev: Evaluation,
    *,
    status: str,
    standard: str,
    reason: str,
) -> None:
    ev.final_status = status
    ev.reason = reason
    ev.normalized_criteria = standard
    ev.normalized_result = clean_text(getattr(ev, "result", ""))
    ev.source = "general_test_method_rule"
    ev.comparison_completed = True


def _evaluate_absence_rule(
    ev: Evaluation,
    *,
    standard: str,
    target: str,
    min_days: int | None = None,
) -> tuple[str, str] | None:
    result_text = _result_text(ev)
    duration_days = _duration_days_from_evaluation(ev)

    if min_days is not None and duration_days is not None and duration_days < min_days:
        return (
            FAIL_LABEL,
            f"{target}은 시험기간이 {min_days}일 이상이어야 하나 확인된 시험기간이 {duration_days}일이므로 불충족으로 판단했습니다.",
        )

    if _has_unsuitable_text(result_text):
        return (
            FAIL_LABEL,
            f"{target}에서 검출/증식/발육 등 부적합 표현이 확인되어 불충족으로 판단했습니다.",
        )

    if _has_suitable_text(result_text):
        period_text = f" 시험기간 {duration_days}일도 확인되었습니다." if duration_days is not None else ""
        return (
            PASS_LABEL,
            f"{standard}에 따라 {target}이 확인되지 않는 경우 적합하며, 시험결과에서 해당 부정 표현이 확인되었습니다.{period_text} 충족으로 판단했습니다.",
        )

    if _has_generic_external_criteria(ev):
        return (
            HOLD_LABEL,
            f"{standard} 적용 대상이나 시험결과에서 {target}의 검출/부정 여부를 명확히 확인하지 못해 보류로 판단했습니다.",
        )

    return None


def _evaluate_pyrogen_rule(ev: Evaluation) -> tuple[str, str] | None:
    text = _result_text(ev)
    values = _extract_float_values(text)

    if not values:
        if _has_suitable_text(text):
            return PASS_LABEL, "발열성물질시험 결과가 적합으로 기재되어 충족으로 판단했습니다."
        if _has_unsuitable_text(text):
            return FAIL_LABEL, "발열성물질시험 결과가 부적합 또는 양성으로 기재되어 불충족으로 판단했습니다."
        return None

    max_value = max(values)
    if max_value <= 1.3:
        return PASS_LABEL, f"발열성물질시험에서 확인된 반응 합계 최대값이 {max_value:g}°C로 1.3°C 이하이므로 충족으로 판단했습니다."
    if max_value >= 2.5:
        return FAIL_LABEL, f"발열성물질시험에서 확인된 반응 합계 최대값이 {max_value:g}°C로 2.5°C 이상이므로 불충족으로 판단했습니다."
    return HOLD_LABEL, f"발열성물질시험 반응 합계 {max_value:g}°C가 1.3°C 초과 2.5°C 미만 구간이라 자동 확정하지 않고 보류로 판단했습니다."


def _evaluate_range_rule(ev: Evaluation, *, low: float, high: float, label: str, unit: str = "") -> tuple[str, str] | None:
    values = _extract_float_values(_result_text(ev))
    if not values:
        return None

    value = values[0]
    unit_text = f" {unit}" if unit else ""
    if low <= value <= high:
        return PASS_LABEL, f"{label} 결과값 {value:g}{unit_text}이 기준 범위 {low:g}~{high:g}{unit_text} 안에 있어 충족으로 판단했습니다."
    return FAIL_LABEL, f"{label} 결과값 {value:g}{unit_text}이 기준 범위 {low:g}~{high:g}{unit_text}를 벗어나 불충족으로 판단했습니다."


def _evaluate_max_rule(ev: Evaluation, *, maximum: float, label: str) -> tuple[str, str] | None:
    values = _extract_float_values(_result_text(ev))
    if not values:
        return None

    value = values[0]
    if value <= maximum:
        return PASS_LABEL, f"{label} 결과값 {value:g}이 기준 {maximum:g} 이하이므로 충족으로 판단했습니다."
    return FAIL_LABEL, f"{label} 결과값 {value:g}이 기준 {maximum:g}을 초과하여 불충족으로 판단했습니다."


def _selected_large_volume_particle_rule(ev: Evaluation) -> bool | None:
    criteria = clean_text(getattr(ev, "criteria", ""))
    compact = re.sub(r"\s+", "", criteria)

    if not compact:
        return None

    large_markers = [
        "■100mL이상",
        "☑100mL이상",
        "[x]100mL이상",
        "100mL이상",
    ]
    small_selected_markers = [
        "■100mL미만",
        "☑100mL미만",
        "[x]100mL미만",
    ]

    if any(marker.replace(" ", "") in compact for marker in small_selected_markers):
        return False

    if any(marker.replace(" ", "") in compact for marker in large_markers):
        return True

    return None


def _extract_particle_result_counts(text: str | None) -> dict[int, float]:
    text = clean_text(text).replace("㎛", "μm").replace("um", "μm")
    counts: dict[int, float] = {}

    patterns = [
        r"(?P<size>10|25)\s*μm\s*이상[^0-9]{0,20}(?P<count>\d+(?:\.\d+)?)",
        r"(?P<size>10|25)\s*um\s*이상[^0-9]{0,20}(?P<count>\d+(?:\.\d+)?)",
        r"(?P<count>\d+(?:\.\d+)?)\s*(?:EA|개)\s*/?\s*(?:mL|ml|vial)?[^0-9]{0,20}(?P<size>10|25)\s*μm",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            try:
                size = int(match.group("size"))
                count = float(match.group("count"))
            except (TypeError, ValueError):
                continue

            counts[size] = count

    return counts


def _evaluate_insoluble_particle_rule(ev: Evaluation) -> tuple[str, str] | None:
    selected_large = _selected_large_volume_particle_rule(ev)
    result_text = _result_text(ev)
    result_key = _norm_match(result_text)

    if _has_unsuitable_text(result_text) or "기준초과" in result_key:
        return FAIL_LABEL, "주사제의 불용성미립자시험 결과가 기준 초과 또는 부적합으로 확인되어 불충족으로 판단했습니다."

    if selected_large is False:
        return HOLD_LABEL, "주사제의 불용성미립자시험 기준에서 100 mL 미만 단계가 선택되어 있어 해당 단계 기준의 상세 판정이 필요합니다."

    if selected_large is True:
        counts = _extract_particle_result_counts(result_text)
        limits = {10: 25.0, 25: 3.0}
        failures = [
            f"{size} μm 이상 {counts[size]:g} EA/mL > {limit:g} EA/mL"
            for size, limit in limits.items()
            if size in counts and counts[size] > limit
        ]

        if failures:
            return (
                FAIL_LABEL,
                "100 mL 이상 주사제 기준을 적용했을 때 불용성미립자 결과가 기준을 초과하여 불충족으로 판단했습니다: "
                + "; ".join(failures),
            )

        if counts:
            checked = ", ".join(
                f"{size} μm 이상 {counts[size]:g} EA/mL"
                for size in sorted(counts)
            )
            return (
                PASS_LABEL,
                f"시험기준에서 100 mL 이상 단계가 선택되어 해당 기준만 적용했습니다. 확인된 결과({checked})가 100 mL 이상 기준(10 μm 이상 25 EA/mL 이하, 25 μm 이상 3 EA/mL 이하)을 만족하여 충족으로 판단했습니다.",
            )

        if _has_suitable_text(result_text):
            return (
                PASS_LABEL,
                "시험기준에서 100 mL 이상 단계가 선택되어 해당 기준만 적용했고, 시험결과가 적합으로 기재되어 충족으로 판단했습니다.",
            )

        return (
            HOLD_LABEL,
            "시험기준에서 100 mL 이상 단계가 선택되었으나 시험결과에서 10 μm/25 μm 입자수를 자동 확인하지 못해 보류로 판단했습니다.",
        )

    if _has_suitable_text(result_text):
        return PASS_LABEL, "주사제의 불용성미립자시험 결과가 기준에 적합한 것으로 확인되어 충족으로 판단했습니다."

    return None


def _apply_general_test_method_rules(evaluations: list[Evaluation]) -> dict:
    applied: list[dict] = []

    for ev in evaluations:
        if clean_text(getattr(ev, "record_type", "")) != "test":
            continue
        evaluation_source = clean_text(getattr(ev, "source", ""))
        if evaluation_source in {"general_test_method_rule", "permit_pdf_llm_authoritative", "extraction_quality"} or evaluation_source.startswith("permit_group_"):
            continue

        text = _join_eval_text(ev)
        key = _norm_match(text)
        test_key = _norm_match(getattr(ev, "test_name", ""))
        decision: tuple[str, str] | None = None
        standard = ""

        if "결핵균부정시험" in key:
            standard = "결핵균부정시험법: 6주 이상 관찰하며 결핵균의 발육이 인정되지 않을 때 적합"
            decision = _evaluate_absence_rule(ev, standard=standard, target="결핵균 발육", min_days=42)
        elif "마이코플라스마부정시험" in key:
            standard = "마이코플라스마부정시험: 직접도말배양법 14일 이상, 증균배양법/멤브레인필터법 28일 이상이며 마이코플라스마 증식이 인정되지 않을 때 적합"
            min_days = 28 if any(token in key for token in ["증균배양", "멤브레인필터", "멤브레인"]) else 14
            decision = _evaluate_absence_rule(ev, standard=standard, target="마이코플라스마 증식", min_days=min_days)
        elif "무균시험" in key:
            standard = "무균시험법: 멤브레인필터법 또는 직접법 14일 이상이며 균 또는 미생물의 발육/증식이 인정되지 않을 때 적합"
            decision = _evaluate_absence_rule(ev, standard=standard, target="균 또는 미생물의 발육", min_days=14)
        elif "발열성물질시험" in key:
            standard = "발열성물질시험법: 3마리 반응 합계가 1.3°C 이하이면 음성(적합), 2.5°C 이상이면 양성(부적합)"
            decision = _evaluate_pyrogen_rule(ev)
        elif "열안정성시험" in key:
            standard = "열안정성시험법: 제1법은 가온 전후 검체 차이가 없어야 하며, 제2법은 가온 후 검체가 겔화되면 안 됨"
            decision = _evaluate_absence_rule(ev, standard=standard, target="가온 전후 차이 또는 겔화")
        elif "치메로살정량" in key or "치메로살" in test_key:
            standard = "치메로살정량법: 별도 규정이 없는 한 허가받은 기준에 적합해야 하며 최종원액 치메로살 함량은 제품 표시량의 80~120%"
            decision = _evaluate_range_rule(ev, low=80, high=120, label="치메로살정량법", unit="%")
            if decision is None and _has_suitable_text(_result_text(ev)):
                decision = PASS_LABEL, "치메로살정량법 결과가 허가받은 기준에 적합한 것으로 기재되어 충족으로 판단했습니다."
        elif "폐놀정량" in key or "페놀정량" in key:
            standard = "폐놀정량법: 별도 규정이 없는 한 폐놀 함량은 0.45~0.55 w/v%"
            decision = _evaluate_range_rule(ev, low=0.45, high=0.55, label="폐놀정량법", unit="w/v%")
        elif "헴정량" in key or "혈색소" in key:
            standard = "헴정량법: 검체의 흡광도는 0.25 이하"
            decision = _evaluate_max_rule(ev, maximum=0.25, label="헴정량법")
        elif "형광항체시험" in key:
            standard = "형광항체시험법: 음성대조, 양성대조와 비교하여 형광이 존재하는 경우 양성으로 판정하고 적합"
            result_text = _result_text(ev)
            if _has_suitable_text(result_text) or "양성" in result_text or "형광" in result_text:
                decision = PASS_LABEL, "형광항체시험 결과가 양성대조/음성대조 비교 기준에 따라 적합한 것으로 확인되어 충족으로 판단했습니다."
        elif "불용성미립자" in key:
            standard = "주사제의 불용성미립자시험: 광차폐입자계수법 또는 현미경입자계수법의 용량별 10 μm/25 μm 입자수 기준을 만족해야 함"
            decision = _evaluate_insoluble_particle_rule(ev)
        elif "불용성이물" in key:
            standard = "불용성이물시험: 주사제 제1법 및 제2법 모두 불용성 이물이 없음"
            decision = _evaluate_absence_rule(ev, standard=standard, target="불용성 이물")
        elif "실용량시험" in key:
            standard = "주사제의 실용량시험법: 표시량 이상이어야 함"
            result_text = _result_text(ev)
            result_key = _norm_match(result_text)
            if "표시량이상" in result_key or _has_suitable_text(result_text):
                decision = PASS_LABEL, "주사제의 실용량시험 결과가 표시량 이상 또는 적합으로 확인되어 충족으로 판단했습니다."
            elif "표시량미만" in result_key or _has_unsuitable_text(result_text):
                decision = FAIL_LABEL, "주사제의 실용량시험 결과가 표시량 미만 또는 부적합으로 확인되어 불충족으로 판단했습니다."
        elif "엔도톡신" in key:
            standard = "엔도톡신시험법: 생물학적제제 각조에서 규정하는 엔도톡신 기준을 초과해서는 안 됨"
            result_text = _result_text(ev)
            result_key = _norm_match(result_text)
            if "초과" in result_key or _has_unsuitable_text(result_text):
                decision = FAIL_LABEL, "엔도톡신시험 결과가 기준 초과 또는 부적합으로 확인되어 불충족으로 판단했습니다."
            elif _has_suitable_text(result_text):
                decision = PASS_LABEL, "엔도톡신시험 결과가 기준 이하 또는 적합으로 확인되어 충족으로 판단했습니다."

        if decision is None:
            continue

        status, reason = decision
        _set_general_method_decision(
            ev,
            status=status,
            standard=standard,
            reason=reason,
        )
        applied.append(
            {
                "test_name": clean_text(getattr(ev, "test_name", "")),
                "status": status,
                "standard": standard,
            }
        )

    return {"applied_count": len(applied), "applied": applied}


def _general_method_priority_evaluation(record) -> tuple[Evaluation | None, dict]:
    ev = Evaluation(
        order_idx=getattr(record, "order_idx", 0),
        record_type=getattr(record, "record_type", "test"),
        section_number=getattr(record, "section_number", None),
        section_title=getattr(record, "section_title", None),
        test_name=(
            clean_text(getattr(record, "test_name", ""))
            or clean_text(getattr(record, "section_title", ""))
            or (clean_text(getattr(record, "raw_text", "")).split("\n")[0] if clean_text(getattr(record, "raw_text", "")) else "")
            or "항목"
        ),
        content_label=getattr(record, "content_label", None),
        content=getattr(record, "content", None),
        criteria=getattr(record, "criteria", None),
        result=getattr(record, "result", None),
        method=getattr(record, "method", None),
        test_date=getattr(record, "test_date", None),
        test_period=getattr(record, "test_period", None),
        remarks=getattr(record, "remarks", None),
        page_start=getattr(record, "page_start", 0),
        page_end=getattr(record, "page_end", 0),
        raw_text=getattr(record, "raw_text", "") or "",
    )
    meta = _apply_general_test_method_rules([ev])
    if meta.get("applied_count"):
        return ev, meta
    return None, meta


def _count_evaluation_statuses(evaluations):
    """
    일반 시험은 evaluation 1개를 1건으로 세고,
    표 형태 시험은 lot_judgements 내부 행을 각각 1건으로 센다.
    """
    passed = 0
    failed = 0
    held = 0

    for ev in evaluations:
        lot_judgements = list(getattr(ev, "lot_judgements", []) or [])

        if lot_judgements:
            for item in lot_judgements:
                status = clean_text(item.get("status", ""))

                if status == PASS_LABEL:
                    passed += 1
                elif status == FAIL_LABEL:
                    failed += 1
                elif status == HOLD_LABEL:
                    held += 1

            continue

        if ev.final_status == PASS_LABEL:
            passed += 1
        elif ev.final_status == FAIL_LABEL:
            failed += 1
        elif ev.final_status == HOLD_LABEL:
            held += 1

    return passed, failed, held


def _permit_index_metadata(store: PermitPdfStore) -> dict:
    """Expose only the stable, path-free fields of the permit index audit."""
    diagnostics = []
    # Preserve compatibility with lightweight legacy/test stores that predate
    # Task 3 and therefore have no durable-index diagnostics surface.
    for entry in getattr(store, "index_diagnostics", ()):
        source_file = Path(entry["source_file"]).name
        failure = entry["failure_code"]
        chunk_count = entry.get("chunk_count")
        if chunk_count is None:
            chunk_count = sum(
                chunk.source_file == source_file for chunk in store.chunks
            )
        if failure is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(failure)):
            failure = "index_failure"
        diagnostics.append({
            "source_file": source_file,
            "cache_hit": entry["cache_hit"],
            "key": entry["key"],
            "schema_version": entry["schema_version"],
            "elapsed_ms": entry["elapsed_ms"],
            "ocr_page_count": entry["ocr_page_count"],
            "failure_code": failure,
            "ocr_mode": store.policy.ocr_mode if store.policy is not None else "auto",
            "chunk_count": chunk_count,
        })
    keys = {entry["key"] for entry in diagnostics}
    schemas = {entry["schema_version"] for entry in diagnostics}
    return {
        "permit_index_diagnostics": diagnostics,
        "permit_index_cache_hit": all(entry["cache_hit"] for entry in diagnostics) if diagnostics else None,
        "permit_index_key": next(iter(keys)) if len(keys) == 1 else None,
        "permit_index_schema": next(iter(schemas)) if len(schemas) == 1 else None,
        "permit_index_elapsed_ms": sum(entry["elapsed_ms"] for entry in diagnostics),
        "permit_ocr_page_count": sum(entry["ocr_page_count"] for entry in diagnostics),
    }


def _permit_chunk_count_for_path(store: PermitPdfStore, path: Path) -> int:
    counter = getattr(store, "chunk_count_for_path", None)
    if callable(counter):
        return int(counter(path))
    return sum(chunk.source_file == path.name for chunk in store.chunks)


def _deterministic_first_enabled(pipeline: "DocumentJudgePipeline") -> bool:
    policy = getattr(pipeline.judge_engine, "permit_policy", None)
    return bool(
        getattr(pipeline, "permit_enabled", False)
        and policy is not None
        and getattr(policy, "evaluation_mode", "legacy") == "deterministic_first"
    )


def _member_fact_ids(group, record_position: int) -> tuple[str, ...]:
    marker = re.compile(rf":position:{record_position}(?::|$)")
    return tuple(
        fact.fact_id
        for fact in group.facts
        if marker.search(clean_text(fact.row_ref))
    )


def _group_evaluation(
    *,
    record,
    normalized_record,
    group,
    verdict,
    primary_evaluation: Evaluation,
    record_position: int,
    result_alias: dict | None,
) -> Evaluation:
    member_fact_ids = set(_member_fact_ids(group, record_position))
    used_member_facts = tuple(
        fact_id for fact_id in verdict.fact_ids if fact_id in member_fact_ids
    )
    title = (
        clean_text(record.test_name)
        or clean_text(record.section_title)
        or (clean_text(record.raw_text).split("\n")[0] if clean_text(record.raw_text) else "")
        or "항목"
    )
    reason = verdict.reason
    if result_alias:
        reason += (
            f"\nMD 결과 표기 사전: '{record.result}' → "
            f"'{normalized_record.result}' ({result_alias['reason']})"
        )
    lot_judgements = [
        {
            **row,
            "status": verdict.status,
            "reason": verdict.reason,
            "source": verdict.source,
        }
        for row in primary_evaluation.lot_judgements
    ]
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
        final_status=verdict.status,
        reason=reason,
        page_start=record.page_start,
        page_end=record.page_end,
        normalized_criteria=None,
        normalized_result=clean_text(normalized_record.result),
        comparator="permit_group",
        confidence="normal",
        source=verdict.source,
        raw_text=record.raw_text or "",
        comparison_completed=verdict.status != HOLD_LABEL,
        lot_judgements=lot_judgements,
        permit_group_id=group.group_id,
        permit_group_path=verdict.gate_path,
        permit_batch=group.batch,
        permit_group_evidence={
            "condition_ids": list(verdict.condition_ids),
            "fact_ids": list(used_member_facts),
            "evidence_ids": list(verdict.evidence_ids),
        },
        record_position=record_position,
    )


def _primary_not_found_evaluation(
    *,
    record,
    normalized_record,
    primary_evaluation: Evaluation,
    group,
    record_position: int,
    result_alias: dict | None,
) -> Evaluation:
    reason = primary_evaluation.reason or ""
    if result_alias:
        reason += (
            f"\nMD 결과 표기 사전: '{record.result}' → "
            f"'{normalized_record.result}' ({result_alias['reason']})"
        )
    return replace(
        primary_evaluation,
        order_idx=record.order_idx,
        record_type=record.record_type,
        section_number=record.section_number,
        section_title=record.section_title,
        test_name=(
            clean_text(record.test_name)
            or clean_text(record.section_title)
            or primary_evaluation.test_name
            or "항목"
        ),
        content_label=record.content_label,
        content=record.content,
        criteria=record.criteria,
        result=record.result,
        method=record.method,
        test_date=record.test_date,
        test_period=record.test_period,
        remarks=record.remarks,
        reason=reason,
        page_start=record.page_start,
        page_end=record.page_end,
        raw_text=record.raw_text or "",
        lot_judgements=list(primary_evaluation.lot_judgements),
        permit_group_id=group.group_id,
        permit_group_path="primary_fallback",
        permit_batch=group.batch,
        permit_group_evidence={
            "condition_ids": [],
            "fact_ids": [],
            "evidence_ids": [],
            "reason_code": "permit_candidate_not_found_primary",
        },
        record_position=record_position,
    )


def _judge_deterministic_first_tests(
    *,
    pipeline: "DocumentJudgePipeline",
    records,
    rule_book,
    rule_product: str,
    unit_normalizations: list[dict],
    progress,
) -> tuple[list[Evaluation], list[dict], dict]:
    grouping_records = list(records)
    test_positions = [
        position
        for position, record in enumerate(records)
        if clean_text(getattr(record, "record_type", "")) == "test"
    ]
    test_position_set = set(test_positions)
    aliases_by_position: dict[int, dict] = {}
    primary_by_position: dict[int, Evaluation] = {}
    result_normalizations: list[dict] = []
    for test_index, position in enumerate(test_positions, 1):
        record = records[position]
        progress(
            f"시험 {test_index}/{len(test_positions)} · "
            f"{record.section_title or ''} · {record.test_name or ''}"
        )
        audit_start = len(unit_normalizations)
        normalized = normalize_record_units(
            record,
            rule_book,
            rule_product,
            audit=unit_normalizations,
        )
        for entry in unit_normalizations[audit_start:]:
            entry["record_position"] = position
        normalized, result_alias = normalize_record_result(
            normalized, rule_book, rule_product
        )
        grouping_records[position] = normalized
        primary_by_position[position] = pipeline.judge_engine.judge_record_primary(
            normalized
        )
        if result_alias:
            aliases_by_position[position] = result_alias
            result_normalizations.append(result_alias)

    groups, grouping_audit = build_permit_groups(
        grouping_records,
        pipeline.permit_store,
    )
    conditions_by_evidence_id: dict[str, list] = {}
    for condition in getattr(pipeline.permit_store, "conditions", ()):
        conditions_by_evidence_id.setdefault(condition.evidence_id, []).append(condition)
    condition_lookup = {
        evidence_id: tuple(conditions)
        for evidence_id, conditions in conditions_by_evidence_id.items()
    }

    calls_before = int(getattr(pipeline.llm_client, "call_count", 0) or 0)
    transport_before = request_metrics()
    evaluations_by_position: dict[int, Evaluation] = {}
    group_audit: list[dict | None] = [None] * len(groups)
    path_counts = {
        "deterministic": 0,
        "ambiguous_llm": 0,
        "hold_without_llm": 0,
        "primary_fallback": 0,
    }
    queued_groups = []

    def fan_out_verdict(group, record_positions, verdict) -> None:
        for position in record_positions:
            if (
                not isinstance(position, int)
                or position < 0
                or position >= len(records)
                or position not in test_position_set
                or position in evaluations_by_position
            ):
                continue
            evaluations_by_position[position] = _group_evaluation(
                record=records[position],
                normalized_record=grouping_records[position],
                group=group,
                verdict=verdict,
                primary_evaluation=primary_by_position[position],
                record_position=position,
                result_alias=aliases_by_position.get(position),
            )

    def record_verdict(
        group_index: int,
        group,
        record_positions,
        verdict,
        *,
        classification_path: str,
    ) -> None:
        group_audit[group_index] = {
            "group_id": group.group_id,
            "record_positions": list(record_positions),
            "status": verdict.status,
            "path": classification_path,
            "source": verdict.source,
            "condition_ids": list(verdict.condition_ids),
            "fact_ids": list(verdict.fact_ids),
            "evidence_ids": list(verdict.evidence_ids),
        }
        fan_out_verdict(group, record_positions, verdict)

    for group_index, (group, source_audit) in enumerate(
        zip(groups, grouping_audit)
    ):
        record_positions = tuple(
            group.record_positions
            or tuple(source_audit.get("record_positions", ()))
        )
        preflight_reason = pipeline.judge_engine.permit_group_preflight_reason()
        if (
            not group.candidate_evidence_ids
            and group.blocker_code is None
            and preflight_reason is None
            and not getattr(pipeline.judge_engine.permit_policy, "authoritative", False)
        ):
            path_counts["primary_fallback"] += 1
            primary_status = (
                primary_by_position[record_positions[0]].final_status
                if record_positions
                and record_positions[0] in primary_by_position
                else HOLD_LABEL
            )
            primary_source = (
                primary_by_position[record_positions[0]].source
                if record_positions
                and record_positions[0] in primary_by_position
                else "permit_group_hold"
            )
            group_audit[group_index] = {
                "group_id": group.group_id,
                "record_positions": list(record_positions),
                "status": primary_status,
                "path": "primary_fallback",
                "source": primary_source,
                "condition_ids": [],
                "fact_ids": [],
                "evidence_ids": [],
                "reason_code": "permit_candidate_not_found_primary",
            }
            for position in record_positions:
                if (
                    not isinstance(position, int)
                    or position < 0
                    or position >= len(records)
                    or position not in test_position_set
                    or position in evaluations_by_position
                ):
                    continue
                evaluations_by_position[position] = _primary_not_found_evaluation(
                    record=records[position],
                    normalized_record=grouping_records[position],
                    primary_evaluation=primary_by_position[position],
                    group=group,
                    record_position=position,
                    result_alias=aliases_by_position.get(position),
                )
            continue

        prepared = pipeline.judge_engine.prepare_permit_group(
            group,
            condition_lookup,
        )
        path_counts[prepared.gate.path] += 1
        if prepared.immediate_verdict is not None:
            record_verdict(
                group_index,
                group=group,
                record_positions=record_positions,
                verdict=prepared.immediate_verdict,
                classification_path=prepared.gate.path,
            )
            continue
        queued_groups.append(
            (group_index, group, record_positions, prepared)
        )

    attempted_batch_sizes: list[int] = []
    # Build the same complete plan online and offline. No live API is needed
    # to measure batching, and disabled credentials must not imply saved calls.
    request_pairs = [
        (
            make_group_batch_request(
                group=prepared.group,
                candidates=prepared.candidates,
                conditions=prepared.conditions,
                executions=prepared.executions,
            ),
            (group_index, group, record_positions, prepared),
        )
        for group_index, group, record_positions, prepared in queued_groups
    ]
    oversized = []
    bounded_pairs = []
    compacted_candidates = sum(
        sum(count - 1 for count in request.payload.get("equivalent_candidate_counts", {}).values())
        for request, _ in request_pairs
    )
    for request, queued in request_pairs:
        prompt_chars = len(build_group_batch_prompt((request,)))
        if prompt_chars <= MAX_BATCH_PROMPT_CHARS:
            bounded_pairs.append((request, queued))
            continue
        group_index, group, record_positions, prepared = queued
        oversized.append({"group_id": group.group_id, "prompt_chars": prompt_chars})
        verdict = pipeline.judge_engine.resolve_prepared_permit_group(
            prepared, None, missing_reason="llm_request_too_large",
        )
        record_verdict(group_index, group, record_positions, verdict,
                       classification_path="hold_without_llm")
        path_counts["ambiguous_llm"] -= 1
        path_counts["hold_without_llm"] += 1
    request_pairs = bounded_pairs
    queued_groups = [queued for _, queued in request_pairs]
    queued_by_request_identity = {
        id(request): queued for request, queued in request_pairs
    }
    batches = partition_group_batch_requests(
        tuple(request for request, _queued in request_pairs)
    )
    progress(
        f"허가서 비교 계획 · 시험행 {len(test_positions)}개 → 묶음 {len(groups)}개 · "
        f"코드 판정 {path_counts['deterministic']}개 · "
        f"대조 확인 필요 {path_counts['hold_without_llm']}개 · "
        f"CLOVA 예정 {len(batches)}요청 ({len(queued_groups)}묶음)"
    )
    client = pipeline.llm_client
    batch_method = getattr(client, "explain_permit_group_batch_once", None)
    batch_client_available = bool(
        client is not None
        and getattr(client, "enabled", False)
        and callable(batch_method)
    )
    if queued_groups and not batch_client_available:
        for group_index, group, record_positions, prepared in queued_groups:
            verdict = pipeline.judge_engine.resolve_prepared_permit_group(
                prepared,
                None,
                missing_reason="llm_unavailable",
            )
            record_verdict(
                group_index,
                group,
                record_positions,
                verdict,
                classification_path=prepared.gate.path,
            )
    elif queued_groups:
        for batch_index, batch in enumerate(batches, 1):
            attempted_batch_sizes.append(len(batch))
            progress(f"허가서 의미 판정 묶음 {batch_index}/{len(batches)}")
            response = batch_method(requests=batch)
            indexed = index_group_batch_response(batch, response)
            for request in batch:
                group_index, group, record_positions, prepared = (
                    queued_by_request_identity[id(request)]
                )
                indexed_item = indexed.get(request.group_id)
                if indexed_item is None:
                    item_response = None
                    missing_reason = "llm_batch_framing_invalid"
                else:
                    item_response = indexed_item.response
                    missing_reason = (
                        indexed_item.error_code or "llm_response_unavailable"
                    )
                verdict = pipeline.judge_engine.resolve_prepared_permit_group(
                    prepared,
                    item_response,
                    missing_reason=missing_reason,
                )
                record_verdict(
                    group_index,
                    group,
                    record_positions,
                    verdict,
                    classification_path=prepared.gate.path,
                )

    # The grouping layer is expected to cover every test exactly once.  Close a
    # violated internal invariant to HOLD rather than dropping a displayed row.
    for position in test_positions:
        if position in evaluations_by_position:
            continue
        record = records[position]
        evaluations_by_position[position] = Evaluation(
            order_idx=record.order_idx,
            record_type=record.record_type,
            section_number=record.section_number,
            section_title=record.section_title,
            test_name=clean_text(record.test_name) or clean_text(record.section_title) or "항목",
            content_label=record.content_label,
            content=record.content,
            criteria=record.criteria,
            result=record.result,
            method=record.method,
            test_date=record.test_date,
            test_period=record.test_period,
            remarks=record.remarks,
            final_status=HOLD_LABEL,
            reason="허가서 그룹과 원본 시험 행의 연결을 확인할 수 없어 검수보류입니다.",
            page_start=record.page_start,
            page_end=record.page_end,
            source="permit_group_hold",
            raw_text=record.raw_text or "",
            comparison_completed=False,
            permit_group_path="hold_without_llm",
            record_position=position,
        )
        path_counts["hold_without_llm"] += 1

    calls_after = int(getattr(pipeline.llm_client, "call_count", 0) or 0)
    provider_calls = max(0, calls_after - calls_before)
    metadata = {
        "permit_evaluation_mode": "deterministic_first",
        "permit_group_raw_test_count": len(test_positions),
        "permit_group_count": len(groups),
        "permit_group_path_counts": path_counts,
        "permit_group_batch_count": len(attempted_batch_sizes),
        "permit_group_batch_sizes": attempted_batch_sizes,
        "permit_group_planned_batch_count": len(batches),
        "permit_group_planned_batch_sizes": [len(batch) for batch in batches],
        "permit_group_planned_prompt_chars": [len(build_group_batch_prompt(batch)) for batch in batches],
        "permit_group_compacted_candidate_count": compacted_candidates,
        "permit_group_oversized_count": len(oversized),
        "permit_group_oversized": oversized,
        "permit_group_provider_calls": provider_calls,
        "permit_group_transport": {key: count - transport_before[key]
                                   for key, count in request_metrics().items()},
        # This is a planning estimate, not measured paid HTTP requests or a
        # completed-review count. Structural HOLDs remain explicitly reported.
        "permit_group_estimated_saved_calls": max(0, len(test_positions) - len(batches)),
        "permit_group_audit": [item for item in group_audit if item is not None],
    }
    return (
        [evaluations_by_position[position] for position in test_positions],
        result_normalizations,
        metadata,
    )


def _safe_group_provider_audit(items) -> list[dict]:
    safe: list[dict] = []
    for item in items or ():
        if not isinstance(item, dict) or not clean_text(item.get("group_id")):
            continue
        row = {
            "group_id": clean_text(item.get("group_id")),
            "event": clean_text(item.get("event")) or "group_event",
        }
        if clean_text(item.get("error_type")):
            row["error_type"] = clean_text(item.get("error_type"))
        safe.append(row)
    return safe


def _safe_group_last_error(value) -> str:
    return "permit_group_request_failed" if clean_text(value) else ""


def _safe_semantic_provider_audit(items) -> list[dict]:
    safe, redacted = [], 0
    for item in items or ():
        if not isinstance(item, dict):
            continue
        if item.get('event') not in {'semantic_grounding', 'semantic_envelope_repair', 'semantic_item_repair'}:
            redacted += 1
            continue
        row = {'event': item['event']}
        for key in ('candidate_id', 'candidate_count', 'clause_count', 'records'):
            if type(item.get(key)) is int and item[key] >= 0:
                row[key] = item[key]
        if re.fullmatch(r'r\d+', str(item.get('record_id', ''))):
            row['record_id'] = item['record_id']
        safe.append(row)
    if redacted:
        safe.append({'event': 'redacted_legacy_audit', 'entries': redacted})
    return safe


def _sanitize_target_metadata(value):
    """Remove absolute local paths recursively from target-policy metadata."""
    if isinstance(value, Path):
        return value.name
    if isinstance(value, dict):
        return {
            key: _sanitize_target_metadata(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_target_metadata(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_target_metadata(item) for item in value)
    if isinstance(value, str):
        try:
            path = Path(value)
            if path.is_absolute():
                return path.name
        except (OSError, ValueError):
            pass
    return value


class DocumentJudgePipeline:
    def __init__(
        self,
        permit_pdf_paths: list[Path] | None = None,
        *,
        company: str | None = None,
        product: str | None = None,
        detail_store: DomainDetailStore | None = None,
        permit_resolution: ResolvedPermit | None = None,
        permit_enabled: bool = True,
        rule_dir: Path | None = None,
        llm_execution_settings: ClovaExecutionSettings | None = None,
        semantic_settings: SemanticSettings | None = None,
        permit_review_scope: str = 'all',
    ) -> None:
        from .review_scope import validate_review_scope
        self.permit_review_scope = validate_review_scope(permit_review_scope)
        self.company = clean_text(company)
        self.semantic_settings = semantic_settings or SemanticSettings()
        self.product = clean_text(product)
        self.detail_store = detail_store or DomainDetailStore()
        self.detail_profile = self.detail_store.resolve(self.company, self.product)
        self.permit_resolution = permit_resolution or resolve_permits(
            permit_pdf_paths or [], self.company, self.product
        )
        self.permit_enabled = permit_enabled
        self.rule_dir = rule_dir
        active_permit_paths = (
            list(self.permit_resolution.paths) if permit_enabled else []
        )
        active_permit_policy = (
            self.permit_resolution.policy if permit_enabled else None
        )
        self.rag_store = UcumRagStore()
        self.llm_client = ClovaJudgeClient(
            domain_detail_context=self.detail_profile.render_for_llm(),
            execution_settings=llm_execution_settings,
        )
        if self.semantic_settings.mode == 'shared_evidence':
            self.llm_client.max_completion_tokens = self.semantic_settings.max_completion_tokens
        self.permit_store = PermitPdfStore(
            active_permit_paths,
            policy=active_permit_policy,
            pdf_sha256_by_path=permit_hash_map(self.permit_resolution) if permit_enabled else {},
        )
        self.judge_engine = JudgeEngine(
            self.rag_store,
            self.llm_client,
            permit_store=self.permit_store,
            permit_policy=active_permit_policy,
            permit_catalog_valid=self.permit_resolution.catalog_valid if permit_enabled else None,
        )
        self.judge_engine.semantic_identity = (self.company, self.product)
        self.judge_engine.sp_only_review = self.permit_review_scope == 'hold_only' and not permit_enabled

    def _activate_detail_profile(
        self,
        static_result: ProcessingResult | None,
    ) -> DomainDetailProfile:
        metadata = static_result.metadata if static_result is not None else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        company = (
            self.company
            or clean_text(metadata.get("document_company"))
            or clean_text(metadata.get("company"))
        )
        product = (
            self.product
            or clean_text(metadata.get("document_product"))
            or clean_text(metadata.get("product"))
        )
        self.detail_profile = self.detail_store.resolve(company, product)
        self.llm_client.set_domain_detail_context(
            self.detail_profile.render_for_llm()
        )
        return self.detail_profile

    def run(
        self,
        pdf_path: Path,
        *,
        extracted_records: list[ExtractedRecord] | None = None,
        static_result: ProcessingResult | None = None,
        progress_callback=None,
        primary_tests_only: bool = False,
    ) -> ProcessingResult:
        review_scope = getattr(self, 'permit_review_scope', 'all')
        if review_scope == 'hold_only' and self.permit_enabled:
            raise ValueError('보류 전용 허가서 판정은 완료된 SP 판정에서 보류 항목만 선택해야 합니다.')
        run_started = perf_counter()
        transport_start = request_diagnostics()
        semantic_settings = getattr(self, 'semantic_settings', SemanticSettings())
        semantic_stats = dict(mode=semantic_settings.mode, eligible_records=0, batched_records=0,
            initial_batch_requests=0, singleton_records=0, candidate_count=0, shared_evidence_sets=0,
            batch_prompt_chars=0, equivalent_single_prompt_chars=0, estimated_batch_input=0,
            estimated_single_input=0, estimate_kind='utf8-byte-estimate', fallback_reasons={}, retrieval_routes={},
            preparation_seconds=0.0, batch_request_seconds=0.0)
        progress = progress_callback or (lambda message: None)
        progress("문서 및 MD 규칙 확인")
        if static_result is not None and Path(static_result.pdf_path).resolve() != Path(pdf_path).resolve():
            raise ValueError("다른 PDF의 추출 결과/미리보기를 재사용할 수 없습니다.")
        detail_profile = self._activate_detail_profile(static_result)
        # Reload on every run: editing a Markdown policy must affect the next judgement.
        rule_book = RuleBook(self.rule_dir) if self.rule_dir else RuleBook()
        receipt = review_receipt(pdf_path, self.permit_store.permit_pdf_paths, rule_book.fingerprint,
                                 llm_enabled=self.llm_client.enabled, model=self.llm_client.model)
        resolved_permit_hashes = getattr(
            self.permit_store, "pdf_sha256_by_path", {}
        )
        for path, item in zip(self.permit_store.permit_pdf_paths, receipt["permits"]):
            expected_hash = resolved_permit_hashes.get(Path(path).resolve())
            current_hash = item.get("sha256")
            if expected_hash and (
                not current_hash or current_hash.casefold() != expected_hash.casefold()
            ):
                raise RuntimeError(
                    "허가서 파일이 확인된 버전 이후 변경되었습니다. "
                    "변경된 파일로 다시 검수해 주세요."
                )
        from .rule_profile import resolve_rule_product
        rule_product, rule_profile_audit = resolve_rule_product(self.product, self.permit_store, rule_book)
        search_aliases = rule_book.search_alias_groups(rule_product)
        self.permit_store.configure_search_aliases(search_aliases)
        artifact_stem = _pdf_artifact_stem(pdf_path)
        work_root = ensure_dir(Path(tempfile.gettempdir()) / "sp_pdf_judger_preview")
        # Results keep these paths for evidence/UI. Never let a later or concurrent
        # request overwrite them, and do not delete them when this call returns.
        work_dir = Path(tempfile.mkdtemp(prefix=f"{artifact_stem}_", dir=work_root))

        if static_result is not None:
            preview_path = static_result.preview_image_path
        else:
            preview_path = work_dir / "page1.png"
            render_first_page(pdf_path, preview_path)

        static_metadata = (
            getattr(static_result, "metadata", {})
            if static_result is not None
            else {}
        )
        if not isinstance(static_metadata, dict):
            static_metadata = {}
        reused_extract_dir = (
            (
                getattr(static_result, "runtime_extract_dir", None)
                or static_metadata.get("extract_dir")
            )
            if extracted_records is not None and static_result is not None
            else None
        )
        extract_dir = Path(reused_extract_dir) if reused_extract_dir else ensure_dir(work_dir / "extract")

        if extracted_records is None:
            progress("PDF 원문·표 추출")
            records = extract_records(pdf_path, extract_dir)
            record_dicts = [
                dict(vars(record))
                for record in records
            ]
            expanded_record_dicts = expand_table_records_for_judgement(
                record_dicts,
                has_permit=self.permit_store.enabled,
                keep_original_table_record=False,
            )
            if records:
                record_type = type(records[0])
                allowed_record_fields = {field.name for field in fields(record_type)}
                records = [
                    record_type(**{key: value for key, value in row.items() if key in allowed_record_fields})
                    for row in expanded_record_dicts
                ]
            else:
                records = []
        else:
            records = list(extracted_records)

        extraction_report = read_extraction_report(extract_dir)
        validate_extraction_report(extraction_report)
        if not records:
            raise RuntimeError("PDF에서 검토할 레코드를 추출하지 못했습니다. 빈 결과를 충족으로 처리하지 않습니다.")

        test_records = [
            r for r in records
            if getattr(r, "record_type", "") == "test"
        ]

        evaluations: list[Evaluation] = []
        comparison_bases_by_evaluation = {}
        comparison_bases_by_position = {}
        result_normalizations = []
        unit_normalizations = []
        priority_general_applied: list[dict] = []
        grouped_metadata: dict = {"test_evaluation_mode": "primary_only" if primary_tests_only else "standard"}
        deterministic_first = _deterministic_first_enabled(self) and not primary_tests_only
        tests_started = perf_counter()
        semantic_stats['singleton_records'] = len(test_records)
        if deterministic_first:
            evaluations, result_normalizations, grouped_metadata = (
                _judge_deterministic_first_tests(
                    pipeline=self,
                    records=records,
                    rule_book=rule_book,
                    rule_product=rule_product,
                    unit_normalizations=unit_normalizations,
                    progress=progress,
                )
            )
        else:
            prepared_responses = {}
            test_positions = [(position, record) for position, record in enumerate(records) if record.record_type == 'test']
            if (semantic_settings.mode == 'shared_evidence' and not primary_tests_only
                    and self.judge_engine.permit_store.enabled and self.llm_client.enabled):
                preparation_started = perf_counter()
                from .permit_grouping import build_stage_batch_inventory
                self.judge_engine.semantic_batch_inventory = build_stage_batch_inventory(records)
                budget = model_budget(self.llm_client.model, self.llm_client.max_completion_tokens)
                requests = []
                fallback_reasons, retrieval_routes = Counter(), Counter()
                if budget is not None:
                    for position, record in test_positions:
                        normalized = normalize_record_units(record, rule_book, rule_product)
                        normalized, _ = normalize_record_result(normalized, rule_book, rule_product)
                        request = self.judge_engine.prepare_authoritative_request(normalized, position=position)
                        if request is not None:
                            requests.append(request)
                        else:
                            fallback_reasons[self.judge_engine.semantic_preparation_reason] += 1
                        retrieval_routes[self.judge_engine.semantic_retrieval_route] += 1
                else:
                    fallback_reasons['unknown_model_budget'] = len(test_positions)
                partition = partition_semantic_requests(requests, semantic_settings, budget)
                if partition.oversized_ids:
                    from .clova_transport import ClovaServiceError
                    raise ClovaServiceError('request')
                batched = [r for batch in partition.batches for r in batch]
                batch_sizes = [request_size_estimates(batch) for batch in partition.batches]
                single_sizes = [request_size_estimates([r]) for r in batched]
                semantic_stats.update(eligible_records=len(requests), batched_records=len(batched),
                    initial_batch_requests=len(partition.batches), singleton_records=len(test_records)-len(batched),
                    prepared_single_requests=len(partition.singles),
                    candidate_count=sum(len(r.bundle.candidates) for r in requests),
                    shared_evidence_sets=len({r.bundle.bundle_id for r in batched}),
                    batch_prompt_chars=sum(s['prompt_chars'] for s in batch_sizes),
                    equivalent_single_prompt_chars=sum(s['prompt_chars'] for s in single_sizes),
                    estimated_batch_input=sum(s['input_estimate'] for s in batch_sizes),
                    estimated_single_input=sum(s['input_estimate'] for s in single_sizes),
                    fallback_reasons=dict(fallback_reasons), retrieval_routes=dict(retrieval_routes),
                    preparation_seconds=perf_counter()-preparation_started)
                batch_started = perf_counter()
                # A lone eligible row still owns the complete prepared evidence.
                # Sending a one-item envelope must not revert to legacy top-k retrieval.
                envelopes = [*partition.batches, *((request,) for request in partition.singles)]
                for batch_index, batch in enumerate(envelopes, 1):
                    progress(f'허가서 독립 시험 묶음 {batch_index}/{len(envelopes)} · {len(batch)}개 시험')
                    responses = self.llm_client.explain_authoritative_semantic_batch(requests=batch)
                    if responses is None:
                        from .clova_transport import ClovaServiceError
                        raise ClovaServiceError('service')
                    for request in batch:
                        prepared_responses[request.position] = PreparedAuthoritativeResponse(
                            request.request_fingerprint, request.record_id, responses[request.record_id], request)
                semantic_stats['batch_request_seconds'] = perf_counter()-batch_started
            for test_index, (position, record) in enumerate(test_positions, 1):
                progress(f"시험 {test_index}/{len(test_records)} · {record.section_title or ''} · {record.test_name or ''}")
                priority_eval, priority_meta = _general_method_priority_evaluation(record)
                authoritative = bool(self.judge_engine.permit_policy and self.judge_engine.permit_policy.authoritative)
                if priority_eval is not None and not authoritative and not result_is_requirement(record.criteria, record.result):
                    if review_scope == 'hold_only':
                        priority_eval.record_position = position
                    evaluations.append(priority_eval)
                    priority_general_applied.extend(priority_meta.get("applied", []))
                    continue
                audit_start = len(unit_normalizations)
                normalized = normalize_record_units(
                    record,
                    rule_book,
                    rule_product,
                    audit=unit_normalizations,
                )
                normalized, result_alias = normalize_record_result(
                    normalized, rule_book, rule_product
                )
                evaluation = (
                    self.judge_engine.judge_record_primary(normalized)
                    if primary_tests_only else self.judge_engine.judge_record(normalized,
                        **({'prepared_authoritative': prepared_responses[position]} if position in prepared_responses else {}))
                )
                if result_alias:
                    result_normalizations.append(result_alias)
                    evaluation.reason = (evaluation.reason or "") + f"\nMD 결과 표기 사전: '{record.result}' → '{normalized.result}' ({result_alias['reason']})"
                # UI/evidence keeps the submitted spelling even when an explicit MD alias was used.
                evaluation.criteria, evaluation.result = record.criteria, record.result
                if semantic_settings.mode == 'shared_evidence' or review_scope == 'hold_only':
                    evaluation.record_position = position
                    for entry in unit_normalizations[audit_start:]:
                        entry['record_position'] = position
                comparison_bases_by_position[position] = list(
                    self.judge_engine.comparison_bases.get(record.order_idx, []))
                comparison_bases_by_evaluation[len(evaluations)] = list(
                    self.judge_engine.comparison_bases.get(record.order_idx, []))
                evaluations.append(evaluation)

        semantic_stats['test_phase_seconds'] = perf_counter()-tests_started
        general_method_meta = _apply_general_test_method_rules(evaluations)
        if priority_general_applied:
            general_method_meta["priority_applied_count"] = len(priority_general_applied)
            general_method_meta["priority_applied"] = priority_general_applied
            general_method_meta["applied_count"] = (
                int(general_method_meta.get("applied_count", 0)) + len(priority_general_applied)
            )
            general_method_meta["applied"] = [
                *priority_general_applied,
                *(general_method_meta.get("applied") or []),
            ]

        if static_result is not None:
            manufacturing_page_numbers = list(static_result.manufacturing_summary_page_numbers)
            manufacturing_image_paths = list(static_result.manufacturing_summary_image_paths)
        else:
            manufacturing_page_numbers = _find_manufacturing_summary_pages(pdf_path)

            manufacturing_output_dir = ensure_dir(
                work_dir / "manufacturing_summary"
            )

            manufacturing_image_paths = [
                _render_pdf_page(
                    pdf_path=pdf_path,
                    page_number=page_number,
                    output_dir=manufacturing_output_dir,
                    suffix="manufacturing_summary",
                )
                for page_number in manufacturing_page_numbers
            ]

        next_order_idx = max((getattr(record, "order_idx", 0) for record in records), default=0) + 1
        activate_policy_store = getattr(
            self.permit_store, "activate_policy_context_compatibility", None
        )
        restore_policy_store = getattr(
            self.permit_store, "restore_policy_context_compatibility", None
        )
        policy_store_state = (
            activate_policy_store() if callable(activate_policy_store) else None
        )
        try:
            policy_context = PolicyContext(pdf_path, records,
                self.permit_store.permit_pdf_paths, rule_product, self.llm_client,
                comparison_bases=self.judge_engine.comparison_bases,
                permit_authoritative=bool(self.judge_engine.permit_policy and self.judge_engine.permit_policy.authoritative),
                permit_search_aliases=search_aliases, company=self.company,
                permit_store=self.permit_store,
                **({'comparison_bases_by_position': comparison_bases_by_position}
                   if semantic_settings.mode == 'shared_evidence' and not deterministic_first else {}))
            if review_scope == 'hold_only':
                policy_context.permit_review_scope = review_scope
            progress(f"시험 {len(test_records)}/{len(test_records)} 완료 · MD 정합성 검증")
            policy_findings = evaluate_policies(policy_context, rule_book)
        finally:
            if callable(restore_policy_store):
                restore_policy_store(policy_store_state)
        test_guard_audit = apply_policy_test_guards(evaluations, policy_findings)
        # Attach comparison provenance after vetoes, so a failure cannot erase it.
        for evaluation in evaluations:
            for entry in unit_normalizations:
                evaluation_position = getattr(evaluation, "record_position", None)
                if isinstance(evaluation_position, int):
                    matches = entry.get("record_position") == evaluation_position
                else:
                    matches = (
                        entry.get("record_position") is None
                        and entry["order_idx"] == evaluation.order_idx
                    )
                if matches:
                    evaluation.reason = (evaluation.reason or "") + "\n" + unit_audit_reason(entry)
        manufacturing_status, manufacturing_reason, manufacturing_meta = summarize_manufacturing_policies(
            policy_findings, rule_book, manufacturing_page_numbers)
        manufacturing_cards = manufacturing_policy_cards(policy_findings, rule_book, records=records)
        evaluations.extend(to_evaluations(policy_findings, rule_book, next_order_idx))
        policy_audit = [asdict(f) for f in policy_findings]
        structural_meta = {"md_rules": [f for f in policy_audit if any(a.startswith("A.") for a in f["aliases"])]}
        numeric_meta = {"md_rules": [f for f in policy_audit if any(a.startswith("B.") for a in f["aliases"])]}
        temporal_meta = {"md_rules": [f for f in policy_audit if any(a.startswith("C.") for a in f["aliases"])]}

        tree = build_document_tree(records, evaluations)

        passed, failed, held = _count_evaluation_statuses(evaluations)

        comparable_total = passed + failed + held

        summary = Summary(
            passed=passed,
            failed=failed,
            held=held,
            total=comparable_total,
            comparable_total=comparable_total,
        )

        if receipt["sp"]["sha256"] and receipt["sp"]["sha256"] != file_sha256(pdf_path):
            raise RuntimeError("검수 도중 SP 파일이 변경되었습니다. 변경된 파일로 다시 검수해 주세요.")
        for path, item in zip(self.permit_store.permit_pdf_paths, receipt["permits"]):
            if item["sha256"] and item["sha256"] != file_sha256(path):
                raise RuntimeError("검수 도중 허가서 파일이 변경되었습니다. 변경된 파일로 다시 검수해 주세요.")
        result = ProcessingResult(
            pdf_path=pdf_path,
            preview_image_path=preview_path,
            extracted_records=records,
            evaluations=evaluations,
            tree=tree,
            summary=summary,
            metadata={
                'permit_review_scope': review_scope,
                "llm_enabled": self.llm_client.enabled,
                "llm_model": self.llm_client.model,
                "llm_call_count": self.llm_client.call_count,
                "llm_success_count": self.llm_client.success_count,
                "llm_last_error": (
                    _safe_group_last_error(self.llm_client.last_error)
                    if deterministic_first or semantic_settings.mode == 'shared_evidence'
                    else self.llm_client.last_error
                ),
                "record_count": len(records),
                'semantic_batch_diagnostics': {**semantic_stats,
                    'run_seconds': perf_counter()-run_started,
                    'transport': diagnostic_delta(transport_start, request_diagnostics())},
                "rule_fingerprint": rule_book.fingerprint,
                "policy_audit": policy_audit,
                "policy_test_guard_audit": test_guard_audit,
                "comparison_bases": self.judge_engine.comparison_bases,
                "comparison_bases_by_evaluation": comparison_bases_by_evaluation,
                "permit_search_aliases": search_aliases,
                "result_normalizations": result_normalizations,
                "unit_normalizations": unit_normalizations,
                "review_receipt": receipt,
                "permit_llm_audit": (
                    _safe_group_provider_audit(getattr(self.llm_client, "permit_audit", []))
                    if deterministic_first
                    else _safe_semantic_provider_audit(getattr(self.llm_client, 'permit_audit', []))
                    if semantic_settings.mode == 'shared_evidence' else getattr(self.llm_client, "permit_audit", [])
                ),
                "manufacturing_summary_judgement": manufacturing_status,
                "extract_dir": None if deterministic_first else str(extract_dir),
                "run_workspace": None if deterministic_first else str(work_dir),
                "extraction_reused": bool(reused_extract_dir),
                "extraction_report": extraction_report,
                "rag_doc_count": len(self.rag_store.docs),
                "rag_sources": self.rag_store.loaded_sources,
                "permit_pdf_count": len(self.permit_store.permit_pdf_paths),
                "permit_chunk_count": len(self.permit_store.chunks),
                "permit_paths": [
                    path.name if deterministic_first else str(path)
                    for path in self.permit_store.permit_pdf_paths
                ],
                "resolved_permit_paths": [
                    path.name if deterministic_first else str(path)
                    for path in self.permit_resolution.paths
                ],
                "permit_policy_id": (
                    self.permit_resolution.policy.policy_id
                    if self.permit_resolution.policy is not None
                    else None
                ),
                "permit_fingerprint": self.permit_resolution.fingerprint,
                "permit_resolution_errors": list(self.permit_resolution.errors),
                "permit_extraction_errors": list(self.permit_store.extraction_errors),
                **_permit_index_metadata(self.permit_store),
                "permit_extraction_diagnostics": [
                    {
                        "path": path.name if deterministic_first else str(path),
                        "ocr_mode": (
                            self.permit_resolution.policy.ocr_mode
                            if self.permit_resolution.policy is not None
                            else "auto"
                        ),
                        "chunk_count": _permit_chunk_count_for_path(
                            self.permit_store, path
                        ),
                    }
                    for path in self.permit_store.permit_pdf_paths
                ],
                "document_company": detail_profile.requested_company,
                "document_product": detail_profile.requested_product,
                "rule_profile": rule_profile_audit,
                "requirement_catalog": [r.model_dump() for r in rule_book.requirements],
                "domain_detail_matched_company": detail_profile.matched_company,
                "domain_detail_matched_product": detail_profile.matched_product,
                "domain_detail_sources": detail_profile.sources,
                "domain_detail_fingerprint": detail_profile.fingerprint,
                "domain_detail_context": detail_profile.render_for_llm(),
                "manufacturing_summary_page_numbers": manufacturing_page_numbers,
                "manufacturing_summary_meta": manufacturing_meta,
                "manufacturing_info_cards": [asdict(card) for card in manufacturing_cards],
                "structural_validation": structural_meta,
                "numeric_precision_validation": numeric_meta,
                "general_test_method_rules": general_method_meta,
                "temporal_sequence_validation": temporal_meta,
                **grouped_metadata,
            },
            manufacturing_summary_image_paths=manufacturing_image_paths,
            manufacturing_summary_status=manufacturing_status,
            manufacturing_summary_reason=manufacturing_reason,
            manufacturing_summary_page_numbers=manufacturing_page_numbers,
            runtime_extract_dir=extract_dir,
            runtime_workspace=work_dir,
            runtime_permit_paths=tuple(self.permit_store.permit_pdf_paths),
        )
        if deterministic_first:
            result.metadata = _sanitize_target_metadata(result.metadata)
        return result
