from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
from pathlib import Path

import fitz
from typing import Any

from .manufacturing_info_validator import _norm_match
from .utils import clean_text
from .config import FAIL_LABEL, HOLD_LABEL, PASS_LABEL, STATUS_COLORS
from .schemas import ProcessingResult, Summary, TreeNode
from .utils import html_escape
from .review_presentation import render_reason
from .permit_text_view import (
    EMPTY_VIEW,
    permit_reason_view,
    permit_view_for_result,
)


def render_summary_card(summary: Summary) -> str:
    attention = ""
    if summary.failed or summary.held:
        attention = f"""
        <div class="final-attention">
          <b>▲ 확인이 필요한 항목</b>
          <span>불충족 {summary.failed}건 · 보류 {summary.held}건</span>
        </div>
        """
    else:
        attention = """
        <div class="final-attention clear">
          <b>● 모든 검수 항목이 확인되었습니다</b>
          <span>상세 결과는 아래 결과 영역에서 확인할 수 있습니다.</span>
        </div>
        """

    return f"""
    <style>
      .final-summary-card {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Malgun Gothic',sans-serif; color:#172554; }}
      .final-summary-card h2 {{ margin:0 0 14px; font-size:28px; letter-spacing:0; color:#111827; }}
      .final-overview {{ overflow:hidden; border:1px solid #bdd8f4; border-radius:16px; background:#fff; }}
      .final-overview-title {{ padding:16px 18px; background:linear-gradient(90deg,#f1f8ff,#edf6ff); border-bottom:1px solid #d8e9fa; font-size:20px; font-weight:950; color:#25446d; }}
      .final-summary-metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); padding:24px 12px 22px; text-align:center; }}
      .final-summary-metrics > div {{ padding:6px 12px; border-left:1px solid #dce8f5; }} .final-summary-metrics > div:first-child {{ border-left:0; }}
      .final-summary-metrics span {{ display:block; font-size:18px; font-weight:950; white-space:nowrap; }}
      .final-summary-metrics b {{ display:block; margin-top:9px; color:#172554; font-size:40px; line-height:1; }}
      .final-summary-metrics .pass {{ color:#159957; }} .final-summary-metrics .fail {{ color:#e5484d; }} .final-summary-metrics .hold {{ color:#db8500; }} .final-summary-metrics .total {{ color:#64748b; }}
      .final-attention {{ display:grid; gap:8px; margin-top:14px; padding:17px 18px; border:1px solid #fecaca; border-left:7px solid #ef4444; border-radius:14px; background:#fff5f5; color:#b42318; font-size:18px; line-height:1.55; }}
      .final-attention b {{ font-size:21px; }} .final-attention span {{ color:#9f4a4a; }}
      .final-attention.clear {{ border-color:#a7efd1; border-left-color:#22c55e; background:#effdf6; color:#12744a; }} .final-attention.clear span {{ color:#38745d; }}
      @media (max-width: 760px) {{ .final-summary-metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .final-summary-metrics > div:nth-child(3) {{ border-left:0; }} }}
    </style>
    <section class="final-summary-card">
      <h2>제조요약도 검수 결과</h2>
      <div class="final-overview">
        <div class="final-overview-title">전체 요약</div>
        <div class="final-summary-metrics">
          <div class="pass"><span>● 충족</span><b>{summary.passed}</b></div>
          <div class="fail"><span>● 불충족</span><b>{summary.failed}</b></div>
          <div class="hold"><span>● 보류</span><b>{summary.held}</b></div>
          <div class="total"><span>전체 항목</span><b>{summary.total}</b></div>
        </div>
      </div>{attention.strip()}</section>
    """


def _mfg_stage_anchor_key(stage_name: str) -> str:
    normalized = _norm_match(stage_name) or clean_text(stage_name)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def _section_anchor_id(section_number: str) -> str:
    section_number = clean_text(section_number)
    if not section_number:
        return ""
    return f"sp-section-{hashlib.sha1(section_number.encode('utf-8')).hexdigest()[:16]}"


def _render_section_number_anchor(section_number: str) -> str:
    anchor_id = _section_anchor_id(section_number)
    if not anchor_id:
        return ""
    return f'<span id="{anchor_id}" class="mfg-scroll-anchor"></span>'


def _node_is_section_one(node: TreeNode) -> bool:
    section_number = clean_text(getattr(node, "section_number", ""))
    if section_number:
        return section_number == "1" or section_number.startswith("1.")

    title = clean_text(getattr(node, "title", ""))
    return bool(re.match(r"^1(?:\\s|$|\\.)", title))


def _section1_pdf_page_numbers(result: ProcessingResult) -> list[int]:
    pages: set[int] = set()

    for record in list(getattr(result, "extracted_records", []) or []):
        section_number = clean_text(getattr(record, "section_number", ""))
        if not (section_number == "1" or section_number.startswith("1.")):
            continue

        try:
            start = int(getattr(record, "page_start", 0) or 0)
            end = int(getattr(record, "page_end", start) or start)
        except Exception:
            continue

        if start <= 0:
            continue

        for page_number in range(start, max(start, end) + 1):
            pages.add(page_number)

    return sorted(pages)


def _result_pdf_page_cache_dir(pdf_path: Path) -> Path:
    try:
        stat = pdf_path.stat()
        source = f"{pdf_path.resolve()}::{stat.st_size}::{stat.st_mtime_ns}"
    except Exception:
        source = str(pdf_path)
    digest = hashlib.sha1(source.encode("utf-8", "ignore")).hexdigest()[:16]
    cache_dir = Path(tempfile.gettempdir()) / "sp_result_section_pages" / digest
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _render_result_pdf_page(pdf_path: Path, page_number: int) -> Path | None:
    try:
        out_path = _result_pdf_page_cache_dir(pdf_path) / f"section1_page_{page_number}.png"
        if out_path.exists() and out_path.stat().st_size > 0:
            return out_path

        with fitz.open(pdf_path) as doc:
            if page_number < 1 or page_number > len(doc):
                return None
            pix = doc[page_number - 1].get_pixmap(matrix=fitz.Matrix(1.55, 1.55), alpha=False)
            pix.save(str(out_path))
        return out_path
    except Exception:
        return None


def _image_file_to_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def _render_section1_pdf_pages(result: ProcessingResult) -> str:
    pdf_path = Path(getattr(result, "pdf_path", "") or "")
    if not pdf_path.exists():
        return ""

    excluded_pages = set(getattr(result, "manufacturing_summary_page_numbers", []) or [])
    image_html: list[str] = []
    for page_number in _section1_pdf_page_numbers(result):
        if page_number in excluded_pages:
            continue

        image_path = _render_result_pdf_page(pdf_path, page_number)
        if not image_path or not image_path.exists():
            continue
        image_html.append(
            f"""
            <figure class="section-one-pdf-page">
              <figcaption>원문 페이지 {page_number}</figcaption>
              <img src="data:image/png;base64,{_image_file_to_base64(image_path)}" />
            </figure>
            """
        )

    if not image_html:
        return ""

    return f"""
    <style>
      .section-one-pdf-pages {{
        width: min(940px, 100%);
        margin: 0 auto 28px;
        padding: 0;
        box-sizing: border-box;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Malgun Gothic", sans-serif;
      }}
      .section-one-pdf-pages h2 {{
        margin: 0 0 12px;
        color: #111827;
        font-size: 20px;
        font-weight: 950;
        letter-spacing: 0;
      }}
      .section-one-pdf-page {{
        width: 100%;
        margin: 0 0 18px;
        padding: 0;
        background: #fff;
        border-radius: 12px;
        box-shadow: 0 18px 46px rgba(0, 0, 0, 0.20);
        overflow: hidden;
      }}
      .section-one-pdf-page figcaption {{
        padding: 10px 14px;
        background: #f8fafc;
        border-bottom: 1px solid #e5e7eb;
        color: #334155;
        font-size: 13px;
        font-weight: 850;
      }}
      .section-one-pdf-page img {{
        display: block;
        width: 100%;
        height: auto;
      }}
    </style>
    <section class="section-one-pdf-pages">
      <h2>일반정보</h2>
      {''.join(image_html)}
    </section>
    """

def _add_unique_alias(aliases: list[str], alias: str) -> None:
    alias = clean_text(alias)

    if not alias:
        return

    anchor_key = _mfg_stage_anchor_key(alias)
    existing_keys = {_mfg_stage_anchor_key(x) for x in aliases}

    if anchor_key not in existing_keys:
        aliases.append(alias)


def _mfg_stage_aliases_for_node_text(text: str) -> list[str]:
    """
    제조요약도 카드 클릭용 anchor 후보 생성.

    핵심:
    - 2.1.1.x 세포주 정보 섹션에는 앵커를 만들지 않는다.
    - CHOL/CHO 마스터, CHOL/CHO 제조용, E.colli/E.coli 마스터, E.colli/E.coli 제조용은
      2.1.2.x 시험 섹션에만 앵커를 만든다.
    - 원액/완제도 정보 섹션이 아니라 시험 섹션으로만 이동하게 한다.
    """
    text = clean_text(text)
    key = _norm_match(text)
    aliases: list[str] = []

    # General fallback for numbered manufacturing stages such as
    # "원료혈장1/2/3". A grouped card named "원료혈장" should scroll to the
    # first matching detailed section without changing the existing click flow.
    stage_terms = [
        "원료혈장",
        "원획분",
        "최종원액",
        "완제의약품",
        "중간체원액",
        "마스터 세포주",
        "제조용 세포주",
    ]
    for line in str(text or "").splitlines():
        compact_line = clean_text(line)
        if not compact_line:
            continue
        for term in stage_terms:
            if term not in compact_line:
                continue
            _add_unique_alias(aliases, term)
            numbered = re.search(rf"({re.escape(term)})\s*[0-9０-９]+", compact_line)
            if numbered:
                _add_unique_alias(aliases, numbered.group(0))

    plasma_source_terms = ["혈액원", "공혈자", "원료혈장", "국내혈장", "수입혈장"]
    if any(term in text for term in plasma_source_terms):
        _add_unique_alias(aliases, "원료혈장")

    # 이름 직접 매칭
    stage_names = [
        "CHOL 마스터 세포주",
        "CHO 마스터 세포주",
        "E.colli 마스터 세포주",
        "E.coli 마스터 세포주",
        "CHOL 제조용 세포주",
        "CHO 제조용 세포주",
        "E.colli 제조용 세포주",
        "E.coli 제조용 세포주",
        "Component A 중간체원액",
        "Component B 중간체원액",
        "나노피티클원액",
        "나노파티클원액",
        "최종원액",
        "완제의약품",
    ]

    for stage_name in stage_names:
        stage_key = _norm_match(stage_name)

        if stage_key and stage_key in key:
            _add_unique_alias(aliases, stage_name)

            if "CHOL" in stage_name:
                _add_unique_alias(aliases, stage_name.replace("CHOL", "CHO"))

            if "CHO" in stage_name and "CHOL" not in stage_name:
                _add_unique_alias(aliases, stage_name.replace("CHO", "CHOL"))

            if "E.colli" in stage_name:
                _add_unique_alias(aliases, stage_name.replace("E.colli", "E.coli"))

            if "E.coli" in stage_name:
                _add_unique_alias(aliases, stage_name.replace("E.coli", "E.colli"))

            if "나노피티클" in stage_name:
                _add_unique_alias(aliases, stage_name.replace("나노피티클", "나노파티클"))

            if "나노파티클" in stage_name:
                _add_unique_alias(aliases, stage_name.replace("나노파티클", "나노피티클"))

    # 세포주 시험 섹션 번호 기반 fallback
    # 중요: 2.1.1.x는 정보 섹션이라 제외하고, 2.1.2.x만 사용한다.
    if re.search(r"\b2\.1\.2\.1\.1\b", text):
        _add_unique_alias(aliases, "CHOL 마스터 세포주")
        _add_unique_alias(aliases, "CHO 마스터 세포주")

    if re.search(r"\b2\.1\.2\.1\.2\b", text):
        _add_unique_alias(aliases, "E.colli 마스터 세포주")
        _add_unique_alias(aliases, "E.coli 마스터 세포주")

    if re.search(r"\b2\.1\.2\.2\.1\b", text):
        _add_unique_alias(aliases, "CHOL 제조용 세포주")
        _add_unique_alias(aliases, "CHO 제조용 세포주")

    if re.search(r"\b2\.1\.2\.2\.2\b", text):
        _add_unique_alias(aliases, "E.colli 제조용 세포주")
        _add_unique_alias(aliases, "E.coli 제조용 세포주")

    # 혹시 section_number가 빠지고 제목만 들어오는 경우 보완
    if "에대한시험" in key or "시험" in text:
        if "chol마스터세포주" in key or "cho마스터세포주" in key:
            _add_unique_alias(aliases, "CHOL 마스터 세포주")
            _add_unique_alias(aliases, "CHO 마스터 세포주")

        if "ecolli마스터세포주" in key or "ecoli마스터세포주" in key:
            _add_unique_alias(aliases, "E.colli 마스터 세포주")
            _add_unique_alias(aliases, "E.coli 마스터 세포주")

        if "chol제조용세포주" in key or "cho제조용세포주" in key:
            _add_unique_alias(aliases, "CHOL 제조용 세포주")
            _add_unique_alias(aliases, "CHO 제조용 세포주")

        if "ecolli제조용세포주" in key or "ecoli제조용세포주" in key:
            _add_unique_alias(aliases, "E.colli 제조용 세포주")
            _add_unique_alias(aliases, "E.coli 제조용 세포주")

    # 원액/완제 시험 섹션 번호 기반 fallback
    # 3.1.x, 4.1, 5.1.1은 정보 섹션이라 제외한다.
    if re.search(r"\b3\.2\.2\b", text):
        _add_unique_alias(aliases, "Component A 중간체원액")

    if re.search(r"\b3\.2\.3\b", text):
        _add_unique_alias(aliases, "Component B 중간체원액")

    if re.search(r"\b3\.2\.4\b", text):
        _add_unique_alias(aliases, "나노피티클원액")
        _add_unique_alias(aliases, "나노파티클원액")

    if re.search(r"\b4\.2\b", text):
        _add_unique_alias(aliases, "최종원액")

    if re.search(r"\b5\.2\b", text) or re.search(r"\b5\.1\.2\b", text):
        _add_unique_alias(aliases, "완제의약품")

    return aliases


def _node_text_for_anchor(node: TreeNode) -> str:
    parts: list[str] = []

    for attr in ["section_number", "section_title", "title", "test_name", "content_label"]:
        value = getattr(node, attr, None)

        if value:
            parts.append(clean_text(value))

    ev = getattr(node, "evaluation", None)

    if ev is not None:
        for attr in ["section_number", "section_title", "test_name", "content_label"]:
            value = getattr(ev, attr, None)

            if value:
                parts.append(clean_text(value))

    source_record = getattr(node, "source_record", None)

    if source_record is not None:
        for attr in ["section_number", "section_title", "test_name", "content_label"]:
            value = getattr(source_record, attr, None)

            if value:
                parts.append(clean_text(value))

        section_path = getattr(source_record, "section_path", None)

        if isinstance(section_path, list):
            for section in section_path:
                if isinstance(section, dict):
                    parts.append(clean_text(section.get("number", "")))
                    parts.append(clean_text(section.get("title", "")))

    return " ".join(parts)


def _is_manufacturing_info_only_node(node: TreeNode) -> bool:
    """
    제조요약도 카드가 이동하면 안 되는 정보 섹션 판별.
    """
    node_type = clean_text(getattr(node, "node_type", ""))
    section_number = clean_text(getattr(node, "section_number", ""))
    section_title = clean_text(getattr(node, "section_title", ""))
    title = clean_text(getattr(node, "title", ""))
    content_label = clean_text(getattr(node, "content_label", ""))

    text = clean_text(
        " ".join(
            [
                section_number,
                section_title,
                title,
                content_label,
                _node_text_for_anchor(node),
            ]
        )
    )

    if node_type == "content":
        return True

    # 세포주 정보 섹션: 2.1.1.x
    if section_number.startswith("2.1.1"):
        return True

    # 원액/완제 정보 섹션
    info_section_prefixes = [
        "3.1",
        "4.1",
        "5.1.1",
    ]

    if any(section_number.startswith(prefix) for prefix in info_section_prefixes):
        return True

    if "정보" in text and "시험" not in text:
        return True

    return False


def _is_cell_stage_judgement_section(text: str, section_number: str) -> bool:
    key = _norm_match(text)

    cell_test_prefixes = [
        "2.1.2.1.1",
        "2.1.2.1.2",
        "2.1.2.2.1",
        "2.1.2.2.2",
    ]

    if any(section_number.startswith(prefix) for prefix in cell_test_prefixes):
        return True

    if "시험" not in text and "에대한시험" not in key:
        return False

    cell_stage_keys = [
        "chol마스터세포주",
        "cho마스터세포주",
        "ecolli마스터세포주",
        "ecoli마스터세포주",
        "chol제조용세포주",
        "cho제조용세포주",
        "ecolli제조용세포주",
        "ecoli제조용세포주",
    ]

    return any(stage_key in key for stage_key in cell_stage_keys)


def _is_manufacturing_judgement_target_node(node: TreeNode) -> bool:
    """
    제조요약도 카드 클릭 시 이동할 대상인지 판단한다.
    정보 섹션은 제외하고, 실제 시험/판정 섹션만 허용한다.
    """
    if _is_manufacturing_info_only_node(node):
        return False

    text = _node_text_for_anchor(node)
    normalized_text = _norm_match(text)
    section_number = clean_text(getattr(node, "section_number", ""))

    if not _mfg_stage_aliases_for_node_text(text):
        return False

    if _is_cell_stage_judgement_section(text, section_number):
        return True

    # 제목/경로에 시험 의미가 있으면 허용
    if "시험" in text or "에대한시험" in normalized_text:
        return True

    node_type = clean_text(getattr(node, "node_type", ""))
    ev = getattr(node, "evaluation", None)

    if node_type == "test" or ev is not None:
        return True

    judgement_prefixes = [
        "3.2.2",
        "3.2.3",
        "3.2.4",
        "4.2",
        "5.2",
        "5.1.2",
    ]

    if any(section_number.startswith(prefix) for prefix in judgement_prefixes):
        return True

    return False


def _render_mfg_stage_anchors_for_node(node: TreeNode) -> str:
    if not _is_manufacturing_judgement_target_node(node):
        return ""

    aliases = _mfg_stage_aliases_for_node_text(_node_text_for_anchor(node))

    if not aliases:
        return ""

    anchors = []

    for alias in aliases:
        anchor = _mfg_stage_anchor_key(alias)
        anchors.append(
            f'<span id="mfg-stage-{anchor}" class="mfg-scroll-anchor"></span>'
        )

    return "".join(anchors)


def _primary_mfg_stage_anchor_id_for_node(node: TreeNode) -> str:
    if not _is_manufacturing_judgement_target_node(node):
        return ""

    aliases = _mfg_stage_aliases_for_node_text(_node_text_for_anchor(node))

    if not aliases:
        return ""

    return f"mfg-stage-{_mfg_stage_anchor_key(aliases[0])}"


def _normalize_display_value(value: str | None) -> str:
    if not value:
        return ""

    value = value.replace("\\r\\n", "\n").replace("\\n", "\n")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("µ", "μ")
    value = value.replace("×", "x")

    normal_to_sup = str.maketrans(
        {
            "0": "⁰",
            "1": "¹",
            "2": "²",
            "3": "³",
            "4": "⁴",
            "5": "⁵",
            "6": "⁶",
            "7": "⁷",
            "8": "⁸",
            "9": "⁹",
            "-": "⁻",
            "+": "⁺",
        }
    )

    def repl(match):
        base = match.group("base")
        exp = match.group("exp").translate(normal_to_sup)
        return f"{base} x 10{exp}"

    value = re.sub(
        r"(?P<base>\d+(?:\.\d+)?)\s*[xX]\s*10\s*(?:\^\s*)?(?P<exp>[+\-]?\d+)",
        repl,
        value,
    )

    return value.strip()


def _format_criteria_display(value: str | None) -> str | None:
    text = _normalize_display_value(value)

    if not text:
        return None

    compact = re.sub(r"\s+", " ", text).strip()
    compact = compact.replace("（", "(").replace("）", ")")
    compact = re.sub(r"기준\s*\(([^)]+)\)\s*(이상|이하|미만|초과)", r"\1 \2", compact)
    compact = re.sub(r"^(이상|이하|미만|초과)\s+(.+)$", r"\2 \1", compact)
    compact = re.sub(r"\(([0-9]+(?:\.[0-9]+)?)\)\s*(이상|이하|미만|초과)", r"\1 \2", compact)

    return compact


def _status_from_explicit_reason(status: str | None, reason: str | None = None) -> str | None:
    # Mixed explanations (SP vs permit, previous vs current) are not statuses.
    explicit = clean_text(status)
    if explicit in (PASS_LABEL, FAIL_LABEL, HOLD_LABEL):
        return explicit
    reason_text = clean_text(reason or "")

    if not reason_text:
        return status

    compact = re.sub(r"\s+", "", reason_text)

    if "검수불합격" in compact or "불합격으로판단" in compact:
        return FAIL_LABEL

    if "검수합격" in compact or ("합격으로판단" in compact and "불합격" not in compact):
        return PASS_LABEL

    if "검수보류" in compact or "보류로판단" in compact:
        return HOLD_LABEL

    return status


def _status_light(status: str | None, reason: str | None = None) -> str:
    status = _status_from_explicit_reason(status, reason)

    if status == PASS_LABEL:
        color = STATUS_COLORS[PASS_LABEL]
    elif status == FAIL_LABEL:
        color = STATUS_COLORS[FAIL_LABEL]
    elif status == HOLD_LABEL:
        color = STATUS_COLORS[HOLD_LABEL]
    else:
        color = "#d1d5db"

    return f'<span style="display:inline-block;width:18px;height:18px;border-radius:50%;background:{color};box-shadow:0 0 0 4px rgba(17,24,39,0.06);"></span>'


def _kv_line(label: str, value: str | None, status: str | None = None) -> str:
    display_value = _format_criteria_display(value) if label == "시험기준" else _normalize_display_value(value)

    if not display_value:
        return ""

    status_html = _status_light(status) if status else ""
    grid_cols = "150px 1fr 44px" if status_html else "150px 1fr"

    return f"""
    <div style="display:grid;grid-template-columns:{grid_cols};column-gap:18px;align-items:end;margin-top:14px;font-size:18px;">
      <div style="font-weight:900;color:#111827;line-height:1.2;font-size:18px;">{html_escape(label)}</div>
      <div style="color:#111827;white-space:pre-wrap;line-height:1.7;padding:0 1px 9px 1px;border-bottom:1px solid #9ca3af;font-size:18px;">{html_escape(display_value)}</div>
      {f'<div style="display:flex;align-items:center;justify-content:center;padding-bottom:9px;">{status_html}</div>' if status_html else ''}
    </div>
    """


def _render_lot_table(ev) -> str:
    rows = getattr(ev, "lot_judgements", None) or []

    if not rows:
        return ""

    display_columns: list[str] = []
    for row in rows:
        for col in row.get("display_columns") or []:
            col_text = _normalize_display_value(col)
            if col_text and col_text not in display_columns:
                display_columns.append(col_text)

    if display_columns:
        header = "".join(
            f'<th style="border:1px solid #dbe3ea;padding:12px 14px;text-align:left;background:#f8fafc;font-size:17px;">{html_escape(col)}</th>'
            for col in display_columns
        )
        header += '<th style="border:1px solid #dbe3ea;padding:12px 14px;text-align:center;background:#f8fafc;font-size:17px;">판정</th>'

        body_rows = []
        for row in rows:
            values = row.get("display_values") or {}
            cells = []
            for col in display_columns:
                value = values.get(col, "")
                if not value:
                    compact_col = re.sub(r"\s+", "", col)
                    if "시험결과" in compact_col or compact_col in {"결과", "실험결과", "측정결과"}:
                        value = row.get("result", "")
                    elif "시험기간" in compact_col or "시험일" in compact_col or compact_col in {"기간", "일자"}:
                        value = row.get("test_date", "")
                    elif not cells:
                        value = row.get("item_value") or row.get("lot_no", "")

                cells.append(
                    f'<td style="border:1px solid #dbe3ea;padding:12px 14px;font-size:17px;">{html_escape(value)}</td>'
                )

            body_rows.append(
                "<tr>"
                + "".join(cells)
                + f'<td style="border:1px solid #dbe3ea;padding:12px 14px;text-align:center;">{_status_light(row.get("status"), row.get("reason"))}</td>'
                + "</tr>"
            )

        return f"""
        <div style="display:grid;grid-template-columns:150px 1fr;column-gap:18px;align-items:start;margin-top:14px;font-size:18px;">
          <div style="font-weight:900;color:#111827;line-height:1.2;padding-top:12px;font-size:18px;">시험결과</div>
          <div style="padding:0 0 10px 0;border-bottom:1px solid #9ca3af;overflow-x:auto;">
            <table style="width:100%;border-collapse:collapse;background:#ffffff;">
              <thead><tr>{header}</tr></thead>
              <tbody>{''.join(body_rows)}</tbody>
            </table>
          </div>
        </div>
        """

    first_label = rows[0].get("item_label") or "항목"

    header = "".join(
        [
            f'<th style="border:1px solid #dbe3ea;padding:12px 14px;text-align:left;background:#f8fafc;font-size:17px;">{html_escape(first_label)}</th>',
            '<th style="border:1px solid #dbe3ea;padding:12px 14px;text-align:left;background:#f8fafc;font-size:17px;">시험기간</th>',
            '<th style="border:1px solid #dbe3ea;padding:12px 14px;text-align:left;background:#f8fafc;font-size:17px;">시험결과</th>',
            '<th style="border:1px solid #dbe3ea;padding:12px 14px;text-align:center;background:#f8fafc;font-size:17px;">판정</th>',
        ]
    )

    body_rows = []

    for row in rows:
        item_value = row.get("item_value") or row.get("lot_no", "")
        body_rows.append(
            "<tr>"
            f'<td style="border:1px solid #dbe3ea;padding:12px 14px;font-size:17px;">{html_escape(item_value)}</td>'
            f'<td style="border:1px solid #dbe3ea;padding:12px 14px;font-size:17px;">{html_escape(row.get("test_date", ""))}</td>'
            f'<td style="border:1px solid #dbe3ea;padding:12px 14px;font-size:17px;">{html_escape(row.get("result", ""))}</td>'
            f'<td style="border:1px solid #dbe3ea;padding:12px 14px;text-align:center;">{_status_light(row.get("status"), row.get("reason"))}</td>'
            "</tr>"
        )

    return f"""
    <div style="display:grid;grid-template-columns:150px 1fr;column-gap:18px;align-items:start;margin-top:14px;font-size:18px;">
      <div style="font-weight:900;color:#111827;line-height:1.2;padding-top:12px;font-size:18px;">시험결과</div>
      <div style="padding:0 0 10px 0;border-bottom:1px solid #9ca3af;overflow-x:auto;">
        <table style="width:100%;border-collapse:collapse;background:#ffffff;">
          <thead><tr>{header}</tr></thead>
          <tbody>{''.join(body_rows)}</tbody>
        </table>
      </div>
    </div>
    """


def _display_judgement_reason_text(text: str | None) -> str:
    text = clean_text(text or "")
    if not text:
        return ""
    return (
        text.replace("검수불합격", "불충족")
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


def _render_reason_box(ev, *, permit_view=EMPTY_VIEW) -> str:
    if not _has_visible_judgement(ev):
        return ""

    rows = getattr(ev, "lot_judgements", None) or []
    is_permit = permit_view.has_permit_basis(ev)
    is_permit_reason = is_permit
    readable_quotes = []

    def display_basis(value):
        raw = clean_text(value)
        shown = permit_view.format_quote(raw) if is_permit else raw
        if is_permit and raw != shown and shown not in readable_quotes:
            readable_quotes.append(shown)
        return _display_judgement_reason_text(shown)

    def display_reason(value, basis):
        if is_permit_reason:
            value = permit_reason_view(value, basis, permit_view)
        return _display_judgement_reason_text(value)

    if rows:
        reason_html = "".join(
            f'<div style="margin-top:6px;"><b>{html_escape(row.get("item_value") or row.get("lot_no", ""))}</b>: {render_reason(display_reason(row.get("reason", ""), row.get("normalized_criteria") or ev.normalized_criteria))}</div>'
            for row in rows
            if (row.get("item_value") or row.get("lot_no")) and row.get("reason")
        )
        notes = "\n".join(line for line in (getattr(ev, "reason", "") or "").splitlines()
                          if line.startswith(("MD 단위 표기 대응:", "MD 결과 표기 사전:")))
        if notes:
            reason_html += render_reason(notes)
    else:
        if not getattr(ev, "reason", ""):
            return ""
        reason_html = render_reason(display_reason(ev.reason, ev.normalized_criteria))

    normalized = ""

    if ev.normalized_criteria or ev.normalized_result:
        criteria_label = ("적용 허가서 기준" if is_permit
                          else "정규화 시험기준")
        normalized = f"""
        <div style="margin-top:12px;color:#4b5563;line-height:1.7;font-size:16px;">
          {f'<div style="white-space:pre-line;overflow-wrap:anywhere;">{criteria_label}: {html_escape(display_basis(ev.normalized_criteria))}</div>' if ev.normalized_criteria else ''}
          {f'<div>정규화 시험결과: {html_escape(_display_judgement_reason_text(ev.normalized_result))}</div>' if ev.normalized_result else ''}
        </div>
        """

    if readable_quotes:
        # Expanded excerpts use the same source-aware presentation as the
        # main explanation. Raw criteria/evidence remain in the review data;
        # the linked PDF is the authority for original pagination/layout.
        normalized += ('<details class="permit-quote" style="margin-top:10px;color:#4b5563;">'
                       '<summary style="cursor:pointer;">허가서 근거 문장 보기</summary>'
                       '<p style="font-size:13px;">줄바꿈과 문서의 머리말·꼬리말을 정리한 표시입니다. '
                       '원래 서식은 연결된 허가서 PDF에서 확인할 수 있습니다.</p>'
                       + "".join('<blockquote style="margin:10px 0;white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;">'
                                 + html_escape(q) + '</blockquote>' for q in readable_quotes) + '</details>')

    return f"""
    <div style="margin-top:20px;padding:20px 22px;border-radius:16px;background:linear-gradient(180deg, #f8fafc 0%, #f1f5f9 100%);border:1px solid #dbe3ea;border-left:6px solid #111827;">
      <div style="font-weight:900;color:#111827;margin-bottom:10px;font-size:18px;">판정 이유</div>
      <div style="color:#111827;line-height:1.85;font-size:17px;overflow-wrap:anywhere;">{reason_html}</div>
      {normalized}
    </div>
    """


def _has_visible_judgement(ev) -> bool:
    # Incomplete comparison is not an unprocessed record: evidence/quality
    # HOLDs already count in the summary and must also explain themselves.
    return bool(getattr(ev, "comparison_completed", False) or (
        getattr(ev, "final_status", None) == HOLD_LABEL and getattr(ev, "reason", None)
    ))


def _render_test_leaf(node: TreeNode, depth_px: int, *, permit_view=EMPTY_VIEW) -> str:
    ev = node.evaluation

    if ev is None:
        return ""

    section_anchor_html = _render_section_number_anchor(_node_section_number(node))
    anchor_html = _render_mfg_stage_anchors_for_node(node)
    primary_anchor = _primary_mfg_stage_anchor_id_for_node(node)
    id_attr = f' id="{primary_anchor}"' if primary_anchor else ""

    display_status = (
        _status_from_explicit_reason(
            getattr(ev, "final_status", None),
            getattr(ev, "reason", None),
        )
        if _has_visible_judgement(ev)
        else None
    )

    result_block = (
        _render_lot_table(ev)
        if getattr(ev, "lot_judgements", None)
        else _kv_line(
            "시험결과",
            getattr(ev, "result", None),
            status=display_status,
        )
    )

    body = "".join(
        [
            _kv_line("시험방법", getattr(ev, "method", None)),
            _kv_line("시험기준", getattr(ev, "criteria", None)),
            _kv_line("시험일자", getattr(ev, "test_date", None)),
            _kv_line("시험기간", getattr(ev, "test_period", None)),
            result_block,
            _render_reason_box(ev, permit_view=permit_view),
            _kv_line("비고", getattr(ev, "remarks", None)),
        ]
    )

    return f"""
    {section_anchor_html}
    {anchor_html}
    <div{id_attr} style="scroll-margin-top:95px;margin-left:{depth_px}px;border:1px solid #e5e7eb;border-radius:16px;background:#ffffff;margin-top:14px;overflow:hidden;">
      <div style="padding:20px 22px 10px 22px;font-size:22px;font-weight:900;color:#111827;">
        <span>{html_escape(node.title)}</span>
      </div>
      <div style="padding:6px 22px 22px 22px;">{body}</div>
    </div>
    """


def _render_content_leaf(node: TreeNode, depth_px: int) -> str:
    content = "\n\n".join([x for x in node.info_lines if _normalize_display_value(x)])

    if not content:
        return ""

    section_anchor_html = _render_section_number_anchor(_node_section_number(node))
    anchor_html = _render_mfg_stage_anchors_for_node(node)
    primary_anchor = _primary_mfg_stage_anchor_id_for_node(node)
    id_attr = f' id="{primary_anchor}"' if primary_anchor else ""

    return f"""
    {section_anchor_html}
    {anchor_html}
    <div{id_attr} style="scroll-margin-top:95px;margin-left:{depth_px}px;border:1px solid #e5e7eb;border-radius:16px;background:#ffffff;margin-top:14px;overflow:hidden;">
      <div style="padding:20px 22px 10px 22px;font-size:22px;font-weight:900;color:#111827;">
        <span>{html_escape(node.title)}</span>
      </div>
      <div style="padding:8px 16px 16px 16px;">
        <div style="white-space:pre-wrap;line-height:1.8;color:#111827;">{html_escape(content)}</div>
      </div>
    </div>
    """




def _node_section_number(node: TreeNode) -> str:
    section_number = clean_text(getattr(node, "section_number", ""))

    if section_number:
        return section_number

    title = clean_text(getattr(node, "title", ""))

    m = re.match(r"^\s*(\d+(?:\.\d+)*)\b", title)

    if m:
        return m.group(1)

    source_record = getattr(node, "source_record", None)

    if source_record is not None:
        section_number = clean_text(getattr(source_record, "section_number", ""))

        if section_number:
            return section_number

    return ""


def _collect_tree_section_numbers(nodes: list[TreeNode]) -> set[str]:
    section_numbers: set[str] = set()

    def walk(node: TreeNode) -> None:
        section_number = _node_section_number(node)

        if section_number:
            section_numbers.add(section_number)

        for child in getattr(node, "children", []) or []:
            walk(child)

    for node in nodes or []:
        walk(node)

    return section_numbers


def _load_records_json_for_ui(result: ProcessingResult) -> list[dict[str, Any]]:
    """
    최종 UI 트리에는 누락되어도 05_records.json에는 존재하는 섹션을 보강 렌더링하기 위한 로더.
    """
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


def _record_section_number(record: dict[str, Any]) -> str:
    return clean_text(record.get("section_number", ""))


def _record_text_value(record: dict[str, Any]) -> str:
    # normalized_text는 content/raw_text가 비었을 때만 사용한다.
    # normalized_text에는 같은 내용이 중복 결합되어 있는 경우가 있어 우선순위를 낮춘다.
    for key in ["content", "raw_text", "normalized_text"]:
        value = clean_text(record.get(key))

        if value:
            return value

    return ""


def _record_section_title(records: list[dict[str, Any]], section_number: str, fallback: str) -> str:
    for record in records:
        if _record_section_number(record) != section_number:
            continue

        title = clean_text(record.get("section_title") or record.get("content_label"))

        if title:
            return title

    return fallback


def _render_record_content_block(title: str, content: str, depth_px: int) -> str:
    content = clean_text(content)

    if not content:
        return ""

    return f"""
    <div style="margin-left:{depth_px}px;border:1px solid #e5e7eb;border-radius:16px;background:#ffffff;margin-top:14px;overflow:hidden;">
      <div style="padding:20px 22px 10px 22px;font-size:22px;font-weight:900;color:#111827;">
        <span>{html_escape(title)}</span>
      </div>
      <div style="padding:8px 16px 16px 16px;">
        <div style="white-space:pre-wrap;line-height:1.8;color:#111827;">{html_escape(content)}</div>
      </div>
    </div>
    """


def _render_missing_record_section(
    section_number: str,
    records: list[dict[str, Any]],
    existing_section_numbers: set[str],
    depth_px: int,
) -> str:
    """
    JSON에는 존재하지만 최종 TreeNode에는 빠진 섹션을 UI에 보강 표시한다.
    현재는 1.2 제조요약정보를 1 일반정보 하위에 다시 노출한다.
    """
    if not records:
        return ""

    if section_number in existing_section_numbers:
        return ""

    section_records = [
        record
        for record in records
        if _record_section_number(record) == section_number
        and clean_text(record.get("record_type", "")) != "heading"
    ]

    if not section_records:
        return ""

    fallback_titles = {
        "1.2": "제조요약정보",
        "1.3": "제조요약도",
    }

    section_title = _record_section_title(
        records,
        section_number,
        fallback_titles.get(section_number, "추출정보"),
    )

    inner = ""

    for record in section_records:
        record_type = clean_text(record.get("record_type", ""))
        content = _record_text_value(record)

        if not content:
            continue

        block_title = "제조요약도 JSON 추출정보" if record_type == "flowchart" else "세부정보"
        inner += _render_record_content_block(block_title, content, depth_px=18)

    if not inner:
        return ""

    section_anchor_html = _render_section_number_anchor(section_number)

    return f"""
    {section_anchor_html}
    <details open style="scroll-margin-top:95px;margin-left:{depth_px}px;border:1px solid #dbe3ea;border-radius:14px;padding:12px 16px;background:#fafcff;margin-top:12px;">
      <summary style="list-style:none;cursor:pointer;font-size:22px;font-weight:900;display:flex;align-items:center;color:#111827;">
        {html_escape(section_number)} {html_escape(section_title)}
      </summary>
      <div style="padding-top:6px;">{inner}</div>
    </details>
    """



def _render_section(
    node: TreeNode,
    depth_px: int = 0,
    *,
    records: list[dict[str, Any]] | None = None,
    existing_section_numbers: set[str] | None = None,
    permit_view=EMPTY_VIEW,
) -> str:
    section_anchor_html = _render_section_number_anchor(_node_section_number(node))
    anchor_html = _render_mfg_stage_anchors_for_node(node)
    primary_anchor = _primary_mfg_stage_anchor_id_for_node(node)
    id_attr = f' id="{primary_anchor}"' if primary_anchor else ""

    summary_html = f"""
    <summary style="list-style:none;cursor:pointer;font-size:22px;font-weight:900;display:flex;align-items:center;color:#111827;">
      {html_escape(node.title)}
    </summary>
    """

    inner = ""

    for child in node.children:
        if child.node_type == "test":
            inner += _render_test_leaf(child, depth_px=18, permit_view=permit_view)
        elif child.node_type == "content":
            inner += _render_content_leaf(child, depth_px=18)
        else:
            inner += _render_section(
                child,
                depth_px=18,
                records=records,
                existing_section_numbers=existing_section_numbers,
                permit_view=permit_view,
            )

    # 05_records.json에는 있는데 최종 tree 렌더링에서 빠진 1.2 제조요약정보를
    # 1 일반정보 하위에 보강 표시한다.
    if _node_section_number(node) == "1":
        inner += _render_missing_record_section(
            "1.2",
            records or [],
            existing_section_numbers or set(),
            depth_px=18,
        )

    return f"""
    {section_anchor_html}
    {anchor_html}
    <details{id_attr} open style="scroll-margin-top:95px;margin-left:{depth_px}px;border:1px solid #dbe3ea;border-radius:14px;padding:12px 16px;background:#fafcff;margin-top:12px;">
      {summary_html}
      <div style="padding-top:6px;">{inner}</div>
    </details>
    """


def _mfg_join_flow_display_line(lines: list[str]) -> str:
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


def _mfg_error_scope_display_line(lines: list[str]) -> str:
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


def _mfg_is_redundant_join_check_line(line: str) -> bool:
    return "마지막 공정" in line and "첫 공정" in line


def render_manufacturing_summary_card(result: ProcessingResult) -> str:
    status = result.manufacturing_summary_status
    reason = result.manufacturing_summary_reason

    if not status and not reason:
        return ""

    if status == PASS_LABEL:
        color = STATUS_COLORS[PASS_LABEL]
    elif status == FAIL_LABEL:
        color = STATUS_COLORS[FAIL_LABEL]
    elif status == HOLD_LABEL:
        color = STATUS_COLORS[HOLD_LABEL]
    else:
        color = "#9ca3af"

    page_text = ""

    if result.manufacturing_summary_page_numbers:
        page_text = " / ".join(str(x) for x in result.manufacturing_summary_page_numbers)
        page_text = f"대상 페이지: {page_text}"

    conclusion = ""
    if status == PASS_LABEL:
        conclusion = "제조요약도 단계의 날짜 흐름이 앞 공정에서 뒤 공정으로 정상 연결되어 검수합격입니다."
    elif status == FAIL_LABEL:
        conclusion = "제조요약도 단계의 공정일자 선후관계에 역전 또는 불일치가 있어 검수불합격입니다."
    elif status == HOLD_LABEL:
        conclusion = "제조요약도 날짜 정보를 안정적으로 확정하기 어려워 검수보류입니다."

    display_reason = _display_judgement_reason_text(reason or "")
    if conclusion and conclusion not in display_reason:
        display_reason = (display_reason + "\n\n" + _display_judgement_reason_text(conclusion)).strip()

    reason_lines = [
        clean_text(line)
        for line in display_reason.splitlines()
        if clean_text(line)
    ]
    lead_line = reason_lines[0] if reason_lines else conclusion
    detail_lines = reason_lines[1:]
    flow_lines = [
        line.lstrip("- ").strip()
        for line in detail_lines
        if ("→" in line or "->" in line)
        and not _mfg_is_redundant_join_check_line(line.lstrip("- ").strip())
    ]
    error_scope_line = _mfg_error_scope_display_line(detail_lines)
    join_flow_line = _mfg_join_flow_display_line(flow_lines)
    if join_flow_line:
        replaced_common_flow = False
        for idx, line in enumerate(flow_lines):
            if line.startswith("공통 흐름:") or line.startswith("합류 흐름:"):
                flow_lines[idx] = join_flow_line
                replaced_common_flow = True
                break
        if not replaced_common_flow:
            flow_lines.append(join_flow_line)
    check_lines = [
        line.lstrip("- ").strip()
        for line in detail_lines
        if (
            line.lstrip("- ").strip() not in flow_lines
            and line not in {"공정 흐름", "합류 검증"}
            and not _mfg_is_redundant_join_check_line(line.lstrip("- ").strip())
        )
    ]
    flow_html = "".join(
        f'<div class="mfg-flow-box">{html_escape(line)}</div>'
        for line in flow_lines[:8]
    )
    if error_scope_line:
        check_lines = [error_scope_line] + [line for line in check_lines if line != error_scope_line and line != "오류 항목"]
    check_html = "".join(
        f'<li>{html_escape(line)}</li>'
        for line in check_lines[:12]
    )

    return f"""
    <style>
      .mfg-date-card {{
        margin-top:22px;margin-bottom:26px;padding:26px 28px;border-radius:20px;
        background:#ffffff;border:1px solid #dbe3ea;border-left:7px solid {color};
        box-shadow:0 10px 26px rgba(15,23,42,0.06);
        font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Malgun Gothic',sans-serif;
      }}
      .mfg-date-head {{ display:flex;align-items:flex-start;justify-content:space-between;gap:20px; }}
      .mfg-date-title {{ font-size:28px;font-weight:950;color:#111827;line-height:1.25; }}
      .mfg-date-sub {{ margin-top:9px;font-size:17px;font-weight:800;color:#64748b; }}
      .mfg-date-badge {{ flex:0 0 auto;display:inline-flex;align-items:center;gap:10px;padding:10px 15px;border-radius:999px;background:#f8fafc;border:1px solid #e2e8f0;font-size:17px;font-weight:950;color:#111827; }}
      .mfg-date-dot {{ width:13px;height:13px;border-radius:999px;background:{color}; }}
      .mfg-date-conclusion {{ margin-top:18px;padding:17px 19px;border-radius:16px;background:#f8fafc;border:1px solid #e5e7eb;color:#111827;font-size:19px;font-weight:850;line-height:1.7; }}
      .mfg-flow-grid {{ margin-top:16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px; }}
      .mfg-flow-box {{ padding:14px 16px;border-radius:14px;background:#f8fbff;border:1px solid #dbeafe;color:#172554;font-size:17px;font-weight:800;line-height:1.65; }}
      .mfg-date-details {{ margin:14px 0 0;padding:0;display:grid;gap:10px;list-style:none; }}
      .mfg-date-details li {{ padding:12px 14px;border-radius:12px;background:#ffffff;border:1px solid #edf2f7;color:#334155;font-size:17px;line-height:1.6; }}
    </style>
    <section class="mfg-date-card">
      <div class="mfg-date-head">
        <div>
          <div class="mfg-date-title">정합성 검증-선행 검증</div>
          {f'<div class="mfg-date-sub">{html_escape(page_text)}</div>' if page_text else ''}
        </div>
        <div class="mfg-date-badge">
          <span class="mfg-date-dot"></span>
          <span>{html_escape(status or "미판정")}</span>
        </div>
      </div>
      <div class="mfg-date-conclusion">{html_escape(lead_line)}</div>
      {f'<div class="mfg-flow-grid">{flow_html}</div>' if flow_html else ''}
      {f'<ul class="mfg-date-details">{check_html}</ul>' if check_html else ''}
    </section>
    """


def render_result_html(result: ProcessingResult) -> str:
    permit_view = permit_view_for_result(result)
    styles = """
    <style>
    html {
      scroll-behavior: smooth;
    }

    .mfg-scroll-anchor {
      display: block;
      position: relative;
      top: -95px;
      visibility: hidden;
      height: 0;
      overflow: hidden;
    }

    .section-one-pdf-pages {
      width: min(940px, 100%);
      margin: 0 auto 28px;
      padding: 0;
      box-sizing: border-box;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Malgun Gothic", sans-serif;
    }

    .section-one-pdf-pages h2 {
      margin: 0 0 12px;
      color: #111827;
      font-size: 20px;
      font-weight: 950;
      letter-spacing: 0;
    }

    .section-one-pdf-page {
      width: 100%;
      margin: 0 0 18px;
      padding: 0;
      background: #fff;
      border-radius: 12px;
      box-shadow: 0 18px 46px rgba(0, 0, 0, 0.20);
      overflow: hidden;
    }

    .section-one-pdf-page figcaption {
      padding: 10px 14px;
      background: #f8fafc;
      border-bottom: 1px solid #e5e7eb;
      color: #334155;
      font-size: 13px;
      font-weight: 850;
    }

    .section-one-pdf-page img {
      display: block;
      width: 100%;
      height: auto;
    }

    details > summary::-webkit-details-marker { display:none; }
    details > summary::before {
      content: "▸";
      font-size: 20px;
      color: #4b5563;
      margin-right: 10px;
      display: inline-block;
      transform: rotate(0deg);
      transition: transform 0.15s ease-in-out;
    }
    details[open] > summary::before {
      transform: rotate(90deg);
    }
    </style>
    """

    judgement_nodes = [
        node for node in result.tree
        if not _node_is_section_one(node)
    ]

    if not judgement_nodes:
        body = """
        <div style="border:1px solid #e5e7eb;border-radius:14px;padding:18px;background:#ffffff;color:#6b7280;">
          표시할 시험 항목이 없습니다.
        </div>
        """
    else:
        records = _load_records_json_for_ui(result)
        existing_section_numbers = _collect_tree_section_numbers(judgement_nodes)

        body = "".join(
            _render_section(
                node,
                depth_px=0,
                records=records,
                existing_section_numbers=existing_section_numbers,
                permit_view=permit_view,
            )
            for node in judgement_nodes
        )

    return styles + body
