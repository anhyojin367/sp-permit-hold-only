# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import base64
import hashlib
import html
import json
import os
import pickle
import re
import shutil
import tempfile
import unicodedata
from contextlib import nullcontext
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from uuid import uuid4
from sp_review_jobs import review_jobs
from sp_process_lock import ReviewLockTimeout, artifact_process_lock
from sp_judgement_versions import JUDGEMENT_CACHE_VERSION
from sp_pdf_judger.review_scope import current_permit_review_scope, HOLD_ONLY_VERSION

import streamlit as st
import streamlit.components.v1 as components

from sp_pdf_judger.domain_details import resolve_domain_detail_profile
from sp_pdf_judger.permit_catalog import resolve_submission_permits as resolve_permits
from sp_pdf_judger.permit_index import permit_evaluation_fingerprint
from sp_pdf_judger.llm import (
    llm_execution_fingerprint,
    resolve_clova_execution_settings,
)
from sp_pdf_judger.pipeline import DocumentJudgePipeline
from sp_pdf_judger.review_presentation import file_sha256
from sp_pdf_judger.permit_semantic_settings import (
    SemanticSettings, read_semantic_settings, semantic_cache_fingerprint,
)
from sp_pdf_judger.clova_transport import ClovaServiceError, clova_request_context
from sp_pdf_judger.permit_text_view import permit_view_for_result
from sp_pdf_judger.policy_engine import policy_fingerprint
from sp_pdf_judger.extraction_runtime import extraction_fingerprint
from sp_pdf_judger.stage_csv_exporter import (
    _is_albumin_document,
    _write_export_summary_csv,
    export_stage_csvs,
    make_stage_csv_zip,
)
from sp_pdf_judger.manufacturing_stage_ui import (
    build_stage_info_cards,
    render_manufacturing_summary_with_stage_cards,
    summarize_stage_info_cards,
)
from sp_pdf_judger.rule_regression import (
    append_rule_regression_log,
    write_output_index,
)
from sp_pdf_judger.schemas import Summary
from sp_pdf_judger.review_presentation import render_reason, render_review_context
from sp_pdf_judger.ui_html import (
    _render_section1_pdf_pages,
    render_result_html,
    render_summary_card,
)


JUDGE_COMPONENT_HEIGHT = 10000
FINAL_JUDGEMENT_VIEWPORT_HEIGHT = 860
JUDGEMENT_STATUS_DIR = Path(__file__).resolve().parent / ".sp_judgement_status"
JUDGEMENT_STATUS_INDEX = JUDGEMENT_STATUS_DIR / "status_index.json"
_RETRY_REQUEST_SESSION_KEY = "sp_explicit_review_retry"


class JudgementCacheError(RuntimeError):
    """A saved attempt cannot be read; viewing it must not trigger paid work."""


class JudgementStatusError(JudgementCacheError):
    """Do not overwrite unreadable document history or repeat the judgement."""


class JudgementPending(RuntimeError):
    """Let the UI poll the existing job without blocking its script thread."""

    def __init__(self, job):
        super().__init__("Document review is still running")
        self.job = job


def _read_judgement_cache(path: Path) -> dict[str, Any]:
    try:
        artifacts = pickle.loads(path.read_bytes())
        if (not isinstance(artifacts, dict)
                or artifacts.get("after_result") is None
                or artifacts.get("cache_version") != JUDGEMENT_CACHE_VERSION):
            raise ValueError("Invalid saved attempt")
        if (current_permit_review_scope() == 'hold_only'
                and artifacts.get('permit_review_scope') != 'hold_only'):
            raise ValueError('Saved attempt belongs to another permit review scope')
        # Check metadata here too, before treating the saved attempt as usable.
        _has_runtime_llm_failure(artifacts)
        return artifacts
    except Exception as exc:
        raise JudgementCacheError(
            "저장된 판정 결과를 읽을 수 없어 자동 재호출하지 않았습니다. "
            "문서 목록으로 돌아가 '검수 진행'을 누르면 기존 파일을 보존하고 재시도합니다."
        ) from exc


def _write_judgement_cache(path: Path, artifacts: dict[str, Any]) -> None:
    """Publish a complete pickle atomically; do not truncate a previous attempt."""
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                prefix=".judgement-", suffix=".tmp", delete=False) as stream:
            pending = Path(stream.name)
            pickle.dump(artifacts, stream)
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(path)
    finally:
        if pending is not None and pending.exists():
            pending.unlink()


def _policy_evidence_links(result: Any, rule_id: str) -> str:
    audit = next((row for row in result.metadata.get("policy_audit", []) if row["rule_id"] == rule_id), None)
    if not audit:
        return ""
    runtime_permits = getattr(result, "runtime_permit_paths", ())
    permit_source = runtime_permits or result.metadata.get("permit_paths", [])
    permits = [Path(p) for p in permit_source]
    permit_view = permit_view_for_result(result)
    links, quotes = {}, {}
    for evidence in audit.get("evidence", []):
        page = evidence.get("page")
        if not isinstance(page, int) or page < 1:
            continue
        if evidence.get("source") == "sp":
            path, label = Path(result.pdf_path), "SP"
        else:
            candidates = [p for p in permits if str(p) == evidence.get("file") or p.name == evidence.get("file")]
            if len(candidates) != 1:
                continue
            path, label = candidates[0], "허가서"
        identity = (str(path.resolve()), page)
        token = base64.urlsafe_b64encode(identity[0].encode()).decode().rstrip("=")
        links[identity] = f'<a href="/?view=pdf_viewer&amp;pdf={token}&amp;page={page}" target="_blank" rel="noopener noreferrer">{label} {page}쪽</a>'
        quote = str(evidence.get("quote") or "")
        if label == "허가서":
            # Presentation only. Keep the original evidence and PDF/page link;
            # do not treat SP result rows or ratios as permit page furniture.
            quote = permit_view.format_quote(quote)
        if quote and quote not in quotes.setdefault(identity, []):
            quotes[identity].append(quote)
    if not links:
        return ""
    content = "".join(link + "".join('<blockquote style="white-space:pre-wrap;overflow-wrap:anywhere;">'
                     + html.escape(quote) + '</blockquote>' for quote in quotes.get(identity, []))
                     for identity, link in links.items())
    return (f'<details class="policy-evidence"><summary>근거 문장 및 원문 PDF ({len(links)})</summary>'
            '<p style="font-size:13px;">허가서 인용문은 줄바꿈과 문서의 머리말·꼬리말을 정리해 표시합니다. '
            '각 쪽 링크에서 원본 PDF를 확인할 수 있습니다.</p>' + content + '</details>')


# ============================================================
# 기본 유틸
# ============================================================

def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file_sig(path: Path | None) -> str:
    if path is None or not path.exists():
        return "none"

    stat = path.stat()
    return f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"


def _artifact_key(
    pdf_path: Path,
    permit_paths: list[Path],
    original_csv_dir: Path | None,
    company: str = "",
    product: str = "",
    detail_fingerprint: str = "",
    permit_fingerprint: str = "",
    product_mapping_id: str = "",
    llm_fingerprint: str | None = None,
    semantic_fingerprint: str = '',
) -> str:
    review_scope = current_permit_review_scope()
    permit_sig = "::".join(_file_sig(path) for path in permit_paths) if permit_paths else "no_permit"
    csv_sig = _file_sig(original_csv_dir) if original_csv_dir else "no_csv_dir"

    execution_fingerprint = (
        llm_execution_fingerprint()
        if llm_fingerprint is None
        else llm_fingerprint
    )
    raw = (
        f"{JUDGEMENT_CACHE_VERSION}::"
        f"{extraction_fingerprint()}::"
        f"{_file_sig(pdf_path)}::"
        f"{permit_sig}::"
        f"{csv_sig}::"
        f"{company.strip()}::{product.strip()}::{product_mapping_id.strip()}::"
        f"{detail_fingerprint}::{permit_fingerprint}::{policy_fingerprint()}::"
        f"{permit_evaluation_fingerprint()}::{execution_fingerprint}"
    )

    if semantic_fingerprint:
        raw += f'::semantic::{semantic_fingerprint}'
    if review_scope == 'hold_only':
        raw += f'::scope::{HOLD_ONLY_VERSION}'
    if semantic_fingerprint or review_scope == 'hold_only':
        raw += '::content::' + json.dumps([file_sha256(path) for path in [pdf_path, *permit_paths]])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _assert_semantic_settings(settings):
    if settings != read_semantic_settings():
        raise JudgementCacheError('검수 중 허가서 처리 모드가 변경되어 이전 설정의 결과를 적용하지 않았습니다. 문서 목록에서 다시 검수하세요.')


def _clean_stage_name(name: str) -> str:
    return str(name or "").strip()


def _final_component_height(result: Any) -> int:
    evaluations = list(getattr(result, "evaluations", []) or [])
    lot_rows = sum(len(getattr(ev, "lot_judgements", []) or []) for ev in evaluations)
    mfg_pages = len(getattr(result, "manufacturing_summary_image_paths", []) or [])
    height = 3600 + (len(evaluations) * 430) + (lot_rows * 74) + (mfg_pages * 1250)
    return max(JUDGE_COMPONENT_HEIGHT, min(height, 90000))


def _display_status_text(status: str | None) -> str:
    status = str(status or "").strip()
    return {
        "검수합격": "충족",
        "합격": "충족",
        "검수불합격": "불충족",
        "불합격": "불충족",
        "검수보류": "보류",
    }.get(status, status or "보류")


def _display_reason_text(text: str | None) -> str:
    text = str(text or "")
    return (
        text
        .replace("검수불합격", "불충족")
        .replace("검수합격", "충족")
        .replace("검수보류", "보류")
        .replace("불합격", "불충족")
        .replace("합격", "충족")
    )


def _render_structural_validation_cards(
    result: Any,
    *,
    record_type: str = "structural_validation",
    section_title: str = "문서 기본요건 적합성 확인 결과",
    fallback_title: str = "구조 검증",
) -> str:
    evaluations = [
        ev
        for ev in list(getattr(result, "evaluations", []) or [])
        if str(getattr(ev, "record_type", "") or "").strip() == record_type
    ]

    if not evaluations:
        return ""

    def status_class(status: str) -> str:
        if status == "충족":
            return "pass"
        if status == "불충족":
            return "fail"
        return "hold"

    cards: list[str] = []

    for ev in evaluations:
        status = _display_status_text(getattr(ev, "final_status", None))
        klass = status_class(status)
        title = str(getattr(ev, "test_name", "") or fallback_title)
        section_number = str(getattr(ev, "section_number", "") or "")
        criteria = str(getattr(ev, "criteria", "") or "")
        result_text = str(getattr(ev, "result", "") or "")
        reason = _display_reason_text(getattr(ev, "reason", ""))
        evidence_links = _policy_evidence_links(result, section_number) if getattr(ev, "source", "") == "md_policy" else ""
        audit = next((a for a in result.metadata.get("policy_audit", []) if a.get("rule_id") == section_number), {})
        catalog = {r["id"]: r for r in result.metadata.get("requirement_catalog", [])}
        aliases = [alias + (" (일부 구현)" if catalog.get(alias, {}).get("coverage") == "partial" else "")
                   for alias in audit.get("aliases", [])]
        alias_html = ('<div class="structural-card-aliases">관련 MD: ' + html.escape(" · ".join(aliases)) + '</div>') if aliases else ""
        # The finding reason is the concise conclusion; the duplicate result
        # field and complete source quotes remain available in the raw receipt.
        reason = _display_reason_text(audit.get("reason") or reason)

        cards.append(
            f"""
            <article class="structural-card {klass}" data-rule-id="{html.escape(section_number, quote=True)}">
              <div class="structural-card-head">
                <div>
                  <div class="structural-card-kicker">{html.escape(section_number)}</div>
                  {alias_html}
                  <div class="structural-card-title">{html.escape(title)}</div>
                </div>
                <span class="structural-card-status {klass}">{html.escape(status)}</span>
              </div>
              <div class="structural-card-body">
                <div class="structural-field"><b>검증 기준</b><div class="structural-field-value">{html.escape(criteria)}</div></div>
                <div class="structural-field"><b>판정 이유</b><div class="structural-field-value">{render_reason(reason)}{evidence_links}</div></div>
              </div>
            </article>
            """
        )

    return f"""
    <section class="structural-validation-section">
      <style>
        .structural-validation-section {{
          font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
          margin:26px 0 20px 0;
        }}
        .structural-validation-title {{
          font-size:22px;
          font-weight:900;
          color:#111827;
          margin:8px 0 14px 0;
        }}
        .structural-card-grid {{
          display:grid;
          grid-template-columns:repeat(2,minmax(0,1fr));
          gap:14px;
        }}
        .structural-card {{
          border:1px solid #dbe3ea;
          border-left:7px solid #f59e0b;
          border-radius:16px;
          background:#ffffff;
          box-shadow:0 10px 28px rgba(15,23,42,.07);
          overflow:hidden;
        }}
        .structural-card.pass {{ border-left-color:#22c55e; }}
        .structural-card.fail {{ border-left-color:#ef4444; }}
        .structural-card.hold {{ border-left-color:#f59e0b; }}
        .structural-card-head {{
          display:flex;
          justify-content:space-between;
          gap:18px;
          align-items:flex-start;
          padding:20px 22px 12px;
          border-bottom:1px solid #edf2f7;
        }}
        .structural-card-kicker {{
          color:#2563eb;
          font-size:13px;
          line-height:1.2;
          font-weight:950;
          letter-spacing:.04em;
        }}
        .structural-card-title {{
          margin-top:6px;
          color:#111827;
          font-size:20px;
          line-height:1.35;
          font-weight:950;
        }}
        .structural-card-aliases {{ color:#475569; font-size:13px; margin-top:6px; line-height:1.6; }}
        .structural-card-status {{
          flex:0 0 auto;
          border-radius:999px;
          padding:7px 13px;
          font-size:15px;
          font-weight:950;
          white-space:nowrap;
        }}
        .structural-card-status.pass {{ color:#15803d; background:#dcfce7; }}
        .structural-card-status.fail {{ color:#b91c1c; background:#fee2e2; }}
        .structural-card-status.hold {{ color:#b45309; background:#fef3c7; }}
        .structural-card-body {{
          display:grid;
          gap:11px;
          padding:16px 22px 20px;
          color:#334155;
          font-size:16px;
          line-height:1.65;
        }}
        /* Only label/value rows are grids. Nested reason/evidence paragraphs
           must remain normal blocks rather than inheriting the 92px column. */
        .structural-card-body > .structural-field {{
          display:grid;
          grid-template-columns:92px minmax(0,1fr);
          gap:14px;
        }}
        .structural-field > b {{
          color:#111827;
          font-weight:900;
        }}
        .structural-field-value {{
          min-width:0;
          white-space:pre-wrap;
          overflow-wrap:anywhere;
        }}
        @media (max-width:900px) {{
          .structural-card-grid {{ grid-template-columns:1fr; }}
          .structural-card-body > .structural-field {{ grid-template-columns:1fr; gap:4px; }}
        }}
      </style>
      <div class="structural-validation-title">{html.escape(section_title)}</div>
      <div class="structural-card-grid">
        {''.join(cards)}
      </div>
    </section>
    """


def _parent_scroll_bridge_script() -> str:
    return """
    <script>
    (function () {
      if (window.__spFinalScrollBridgeInstalled) {
        return;
      }
      window.__spFinalScrollBridgeInstalled = true;

      function canScrollInsideIframe(target, deltaY) {
        var el = target;
        while (el && el !== document.body && el !== document.documentElement) {
          var style = window.getComputedStyle(el);
          var overflowY = style.overflowY;
          var isScrollable = (overflowY === "auto" || overflowY === "scroll") && el.scrollHeight > el.clientHeight + 1;
          if (isScrollable) {
            if (deltaY < 0 && el.scrollTop > 0) {
              return true;
            }
            if (deltaY > 0 && el.scrollTop + el.clientHeight < el.scrollHeight - 1) {
              return true;
            }
          }
          el = el.parentElement;
        }
        return false;
      }

      function scrollParentBy(deltaX, deltaY) {
        try {
          if (window.parent && window.parent !== window) {
            window.parent.scrollBy({ left: deltaX || 0, top: deltaY || 0, behavior: "auto" });
            return true;
          }
        } catch (err) {
          return false;
        }
        return false;
      }

      window.addEventListener("wheel", function (event) {
        if (canScrollInsideIframe(event.target, event.deltaY)) {
          return;
        }
        if (scrollParentBy(event.deltaX, event.deltaY)) {
          event.preventDefault();
        }
      }, { passive: false });

      window.addEventListener("keydown", function (event) {
        var tagName = (event.target && event.target.tagName || "").toLowerCase();
        if (tagName === "input" || tagName === "textarea" || tagName === "select" || event.target.isContentEditable) {
          return;
        }
        var delta = 0;
        if (event.key === "ArrowUp") delta = -80;
        else if (event.key === "ArrowDown") delta = 80;
        else if (event.key === "PageUp") delta = -Math.max(360, window.innerHeight * 0.75);
        else if (event.key === "PageDown" || event.key === " ") delta = Math.max(360, window.innerHeight * 0.75);
        else if (event.key === "Home") delta = -100000;
        else if (event.key === "End") delta = 100000;
        if (delta && scrollParentBy(0, delta)) {
          event.preventDefault();
        }
      });
    })();
    </script>
    """


def _to_int(value: Any) -> int:
    try:
        return int(str(value).replace(",", "").strip() or 0)
    except Exception:
        return 0


def _signed(value: int) -> str:
    if value > 0:
        return f"+{value}"

    if value < 0:
        return str(value)

    return ""


def _pdf_status_key(path: Path) -> str:
    try:
        resolved = str(Path(path).resolve())
    except Exception:
        resolved = str(path)
    return hashlib.sha256(resolved.encode("utf-8", "ignore")).hexdigest()


def _read_status_index() -> dict[str, Any]:
    # This is the writer's strict read, unlike the inbox's display-only reader.
    # A damaged file is not an empty history that can safely be overwritten.
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate status JSON key")
            result[key] = value
        return result
    try:
        with JUDGEMENT_STATUS_INDEX.open("r", encoding="utf-8") as f:
            data = json.load(f, object_pairs_hook=unique_object)
        if not isinstance(data, dict) or not isinstance(data.get("documents"), dict):
            raise ValueError("Invalid status index structure")
        return data
    except FileNotFoundError:
        return {"documents": {}}
    except (OSError, ValueError) as exc:
        raise JudgementStatusError(
            "문서함 상태 기록을 읽을 수 없어 기존 파일을 덮어쓰지 않았습니다. "
            "저장된 판정 결과는 보존됩니다. 상태 기록의 손상 또는 접근 권한을 확인해야 합니다."
        ) from exc


def _write_status_index(data: dict[str, Any]) -> None:
    """Caller holds the status lock; readers see the old or complete new JSON."""
    _ensure_dir(JUDGEMENT_STATUS_INDEX.parent)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                dir=JUDGEMENT_STATUS_INDEX.parent, prefix=".status-index-", suffix=".tmp", delete=False) as stream:
            pending = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(JUDGEMENT_STATUS_INDEX)
    finally:
        if pending is not None and pending.exists():
            pending.unlink()


def _has_runtime_llm_failure(artifacts: dict[str, Any]) -> bool:
    """Genuine HOLDs are reusable; interrupted/failed API requests are not."""
    for stage in ("before_result", "after_result"):
        metadata = getattr(artifacts.get(stage), "metadata", {}) or {}
        if metadata.get("llm_last_error"):
            return True
        if _to_int(metadata.get("llm_call_count")) > _to_int(metadata.get("llm_success_count")):
            return True
    return False


def forget_failed_judgement_attempt(pdf_path: Path) -> None:
    """Explicit new review only; viewing a failed attempt must not auto-retry."""
    target = Path(pdf_path).resolve()
    review_jobs.forget_failed(str(target), _has_runtime_llm_failure)
    # This intent is consumed on the UI thread, never from a worker. Bind it to
    # the selected path/size/mtime identity, not a global retry permission.
    st.session_state[_RETRY_REQUEST_SESSION_KEY] = (str(target), _file_sig(target))
    for key, artifact in list(st.session_state.items()):
        if (str(key).startswith("sp_direct_judgement_artifacts::") and isinstance(artifact, dict)
                and artifact.get("pdf_path") and Path(artifact["pdf_path"]).resolve() == target
                and _has_runtime_llm_failure(artifact)):
            del st.session_state[key]


def _remember_judgement_artifacts(artifacts: dict[str, Any]) -> None:
    pdf_path = Path(artifacts.get("pdf_path") or "")
    if not pdf_path.exists():
        return

    artifact_key = str(artifacts.get("artifact_key") or "")
    if not artifact_key:
        return

    summary_dir = _ensure_dir(JUDGEMENT_STATUS_DIR / artifact_key)
    summary_before = Path(artifacts.get("summary_before_path") or "")
    summary_after = Path(artifacts.get("summary_after_path") or "")

    persisted_before = summary_dir / "summary_before.csv"
    persisted_after = summary_dir / "summary_after.csv"

    try:
        if summary_before.exists():
            shutil.copy2(summary_before, persisted_before)
        if summary_after.exists():
            shutil.copy2(summary_after, persisted_after)
    except OSError:
        return

    try:
        stat = pdf_path.stat()
        pdf_sig = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    except OSError:
        pdf_sig = {}

    # 단일 출처 원칙: summary_counts 는 이미 파일로 저장된 summary_after.csv 에서 읽는다.
    # 이렇게 하면 Gmail·시뮬레이션·판정 세 섹션이 모두 같은 파일을 참조한다.
    summary_counts = {}
    if persisted_after.exists():
        _rows = _read_summary_rows_by_position(persisted_after)
        _totals = _overall_from_rows(_rows)
        summary_counts = {
            "pass": _to_int(_totals.get("pass", 0)),
            "fail": _to_int(_totals.get("fail", 0)),
            "hold": _to_int(_totals.get("hold", 0)),
            "total": _to_int(_totals.get("total", 0)),
        }

    entry = {
        "status": "failed" if _has_runtime_llm_failure(artifacts) else "completed",
        "cache_version": JUDGEMENT_CACHE_VERSION,
        "artifact_key": artifact_key,
        "pdf_path": str(pdf_path),
        "pdf_sig": pdf_sig,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "summary_counts": summary_counts,
        "summary_before_path": str(persisted_before),
        "summary_after_path": str(persisted_after),
        "permit_paths": [str(path) for path in artifacts.get("permit_paths", [])],
        "resolved_permit_paths": [str(path) for path in artifacts.get("resolved_permit_paths", [])],
        "permit_policy_id": artifacts.get("permit_policy_id"),
        "permit_fingerprint": str(artifacts.get("permit_fingerprint") or ""),
        "semantic_fingerprint": str(artifacts.get("semantic_fingerprint") or ""),
        "permit_review_scope": artifacts.get('permit_review_scope', 'all'),
        "sp_sha256": file_sha256(pdf_path) if artifacts.get('semantic_fingerprint') or artifacts.get('permit_review_scope') == 'hold_only' else None,
        "permit_file_hashes": {str(Path(p).resolve()): file_sha256(p) for p in artifacts.get('permit_paths', [])}
            if artifacts.get('permit_review_scope') == 'hold_only' else {},
        "rule_fingerprint": str(artifacts.get("rule_fingerprint") or ""),
        "extraction_fingerprint": str(artifacts.get("extraction_fingerprint") or ""),
        "llm_execution_fingerprint": str(
            artifacts.get("llm_execution_fingerprint") or ""
        ),
        "permit_resolution_errors": list(artifacts.get("permit_resolution_errors", []) or []),
        "permit_extraction_diagnostics": list(artifacts.get("permit_extraction_diagnostics", []) or []),
        "company": str(artifacts.get("company") or ""),
        "product": str(artifacts.get("product") or ""),
        "product_mapping_id": str(
            artifacts.get("product_mapping_id") or ""
        ),
        "detail_fingerprint": str(artifacts.get("detail_fingerprint") or ""),
        "detail_sources": list(artifacts.get("detail_sources", []) or []),
    }
    _store_status_entry(pdf_path, entry)


def _store_status_entry(pdf_path: Path, entry: dict) -> None:
    # Distinct artifact locks do not protect this shared read/modify/write.
    # Keep the global section short: no PDF, provider call, or CSV generation.
    try:
        with artifact_process_lock(JUDGEMENT_STATUS_DIR / ".status-index.lock", timeout=30.0):
            data = _read_status_index()
            data["documents"][_pdf_status_key(pdf_path)] = entry
            _write_status_index(data)
    except ReviewLockTimeout as exc:
        raise JudgementStatusError(
            "문서함 상태 저장 잠금을 기다리는 시간이 초과되어 기록을 덮어쓰지 않았습니다. "
            "저장된 판정 결과는 보존됩니다. 다른 작업이 끝난 뒤 다시 열어주세요."
        ) from exc


# ============================================================
# sp_pdf_judger 결과 -> summary_before.csv / summary_after.csv
# ============================================================

def _write_summary_csv_from_result(result: Any, output_path: Path) -> Path:
    """
    sp_pdf_judger 판정 결과에서 제조요약도별 summary CSV를 생성한다.

    컬럼:
    제조명, 검수합격, 검수불합격, 검수보류, 총 계
    """
    has_md_policies = bool(getattr(result, "metadata", {}).get("rule_fingerprint"))
    if _is_albumin_document(result) and not has_md_policies:
        try:
            export_dir = _ensure_dir(output_path.parent / f"{output_path.stem}_stage_summary_source")
            export_results = export_stage_csvs(result=result, output_dir=export_dir)
            source_summary = _write_export_summary_csv(
                export_results=export_results,
                csv_dir=export_dir,
            )
            shutil.copy2(source_summary, output_path)
            return output_path
        except Exception:
            pass

    cards = build_stage_info_cards(result)

    rows: list[dict[str, Any]] = []

    for card in cards:
        name = _clean_stage_name(card.display_name)

        if not name:
            continue

        rows.append(
            {
                "제조명": name,
                "검수합격": int(card.passed),
                "검수불합격": int(card.failed),
                "검수보류": int(card.held),
                "총 계": int(card.total),
            }
        )

    merged: dict[str, dict[str, Any]] = {}

    for row in rows:
        name = str(row["제조명"])

        if name not in merged:
            merged[name] = {
                "제조명": name,
                "검수합격": 0,
                "검수불합격": 0,
                "검수보류": 0,
                "총 계": 0,
            }

        merged[name]["검수합격"] += int(row["검수합격"])
        merged[name]["검수불합격"] += int(row["검수불합격"])
        merged[name]["검수보류"] += int(row["검수보류"])
        merged[name]["총 계"] += int(row["총 계"])

    final_rows = list(merged.values())
    if has_md_policies:
        final_rows = _include_document_summary_counts(result, final_rows)

    total_pass = sum(int(row["검수합격"]) for row in final_rows)
    total_fail = sum(int(row["검수불합격"]) for row in final_rows)
    total_hold = sum(int(row["검수보류"]) for row in final_rows)
    total_count = sum(int(row["총 계"]) for row in final_rows)

    final_rows.append(
        {
            "제조명": "전체",
            "검수합격": total_pass,
            "검수불합격": total_fail,
            "검수보류": total_hold,
            "총 계": total_count,
        }
    )

    _ensure_dir(output_path.parent)

    with output_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["제조명", "검수합격", "검수불합격", "검수보류", "총 계"],
        )
        writer.writeheader()
        writer.writerows(final_rows)

    return output_path


def _include_document_summary_counts(result, rows):
    """Keep stage rows, but never drop MD or unassigned tests from overall totals."""
    wanted = {"검수합격": result.summary.passed, "검수불합격": result.summary.failed,
              "검수보류": result.summary.held, "총 계": result.summary.total}
    remainder = {key: value - sum(int(row[key]) for row in rows) for key, value in wanted.items()}
    if any(value < 0 for value in remainder.values()):
        raise ValueError("제조단계 집계가 전체 판정 수를 초과합니다. 중복 집계를 확인하십시오.")
    if remainder["총 계"] != sum(remainder[key] for key in ("검수합격", "검수불합격", "검수보류")):
        raise ValueError("전체 판정 집계가 일치하지 않습니다.")
    return [*rows, *([{"제조명": "문서 공통·기타 검증", **remainder}] if remainder["총 계"] else [])]


def _read_summary_csv(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.exists():
        return []

    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _read_summary_rows_by_position(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
    except Exception:
        return []

    normalized: list[dict[str, str]] = []
    for row in rows[1:]:
        if not row or not any(str(cell).strip() for cell in row):
            continue
        padded = row + [""] * 5
        normalized.append(
            {
                "name": str(padded[0]).strip(),
                "pass": str(padded[1]).strip(),
                "fail": str(padded[2]).strip(),
                "hold": str(padded[3]).strip(),
                "total": str(padded[4]).strip(),
            }
        )
    return normalized


# ============================================================
# 제조명 / 그래프 노드명 일반화 매칭
# ============================================================

def _normalize_stage_text(value: str) -> str:
    text = str(value or "")
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()

    text = text.replace("∙", ".").replace("ㆍ", ".").replace("·", ".")

    # 표기/OCR 흔들림 보정
    text = re.sub(r"e\s*\.?\s*colli", "ecoli", text)
    text = re.sub(r"e\s*\.?\s*coli", "ecoli", text)
    text = text.replace("chol", "cho")
    text = text.replace("컴포넌트", "component")

    # component A/B류 통합
    text = re.sub(r"component\s*([a-z0-9]+)", r"component\1", text)

    # 한글 공정명 공백 통합
    text = re.sub(r"마스터\s*세포주", "마스터세포주", text)
    text = re.sub(r"제조용\s*세포주", "제조용세포주", text)
    text = re.sub(r"중간체\s*원액", "중간체원액", text)
    text = re.sub(r"최종\s*원액", "최종원액", text)
    text = re.sub(r"완제\s*의약품", "완제의약품", text)
    text = re.sub(r"나노\s*파티클", "나노파티클", text)
    text = re.sub(r"나노\s*피티클", "나노파티클", text)

    # 비교용 compact key
    text = re.sub(r"[\s_\-./(){}\[\]:：,]+", "", text)

    return text


def _stage_tokens(value: str) -> set[str]:
    key = _normalize_stage_text(value)
    tokens: set[str] = set()

    terms = [
        "cho",
        "ecoli",
        "componenta",
        "componentb",
        "component",
        "마스터세포주",
        "제조용세포주",
        "세포주",
        "중간체원액",
        "중간체",
        "최종원액",
        "원액",
        "완제의약품",
        "완제",
        "의약품",
        "나노파티클",
        "나노",
    ]

    for term in terms:
        if term in key:
            tokens.add(term)

    m = re.search(r"component([a-z0-9]+)", key)
    if m:
        tokens.add("component")
        tokens.add(f"component{m.group(1)}")
        tokens.add(m.group(1))

    return tokens


def _is_blocked_graph_label(label: str) -> bool:
    key = _normalize_stage_text(label)

    if not key:
        return True

    blocked = {
        "start",
        "end",
        "ocr",
        "llm",
        "기준",
        "약전",
        "허가서",
        "시험기준판별",
        "분석선행관계",
        "허가서참고",
        "기준약전",
    }

    if key in blocked:
        return True

    return False


def _stage_match_score(left: str, right: str) -> float:
    if _is_blocked_graph_label(left) or _is_blocked_graph_label(right):
        return 0.0

    left_key = _normalize_stage_text(left)
    right_key = _normalize_stage_text(right)

    if not left_key or not right_key:
        return 0.0

    if left_key == right_key:
        return 1.0

    if len(left_key) >= 4 and len(right_key) >= 4:
        if left_key in right_key or right_key in left_key:
            return 0.95

    left_tokens = _stage_tokens(left)
    right_tokens = _stage_tokens(right)

    shared = left_tokens & right_tokens
    union = left_tokens | right_tokens

    token_score = len(shared) / max(len(union), 1) if union else 0.0
    char_score = SequenceMatcher(None, left_key, right_key).ratio()

    score = char_score * 0.45 + token_score * 0.55

    # Component A/B는 섞이면 안 됨
    left_component = ""
    right_component = ""

    lm = re.search(r"component([a-z0-9]+)", left_key)
    rm = re.search(r"component([a-z0-9]+)", right_key)

    if lm:
        left_component = lm.group(1)

    if rm:
        right_component = rm.group(1)

    if left_component and right_component:
        if left_component == right_component:
            score += 0.2
        else:
            score -= 0.45

    # 의미 토큰이 하나도 안 겹치면 과매칭 방지
    if not shared:
        score = min(score, 0.42)

    return max(0.0, min(1.0, score))


def _overall_from_rows(rows: list[dict[str, str]]) -> dict[str, int]:
    positional_rows = [
        row for row in rows
        if {"name", "pass", "fail", "hold", "total"} <= set(row.keys())
    ]

    if positional_rows:
        total_markers = {"전체", "총계", "합계", "total", "overall"}
        total_row = next(
            (row for row in positional_rows if str(row.get("name", "")).strip().casefold() in total_markers),
            None,
        )
        if total_row is None:
            return {
                "pass": sum(_to_int(row.get("pass", 0)) for row in positional_rows),
                "fail": sum(_to_int(row.get("fail", 0)) for row in positional_rows),
                "hold": sum(_to_int(row.get("hold", 0)) for row in positional_rows),
                "total": sum(_to_int(row.get("total", 0)) for row in positional_rows),
            }
        return {
            "pass": _to_int(total_row.get("pass", 0)),
            "fail": _to_int(total_row.get("fail", 0)),
            "hold": _to_int(total_row.get("hold", 0)),
            "total": _to_int(total_row.get("total", 0)),
        }

    total_row = None
    for row in rows:
        values = [str(value).strip().casefold() for value in row.values()]
        if any(value in {"전체", "총계", "합계", "total", "overall"} for value in values):
            total_row = row
            break

    if total_row is None:
        return {
            "pass": sum(_to_int(_first_row_value(row, ("pass", "합격"))) for row in rows),
            "fail": sum(_to_int(_first_row_value(row, ("fail", "불합격"))) for row in rows),
            "hold": sum(_to_int(_first_row_value(row, ("hold", "보류"))) for row in rows),
            "total": sum(_to_int(_first_row_value(row, ("total", "전체", "총"))) for row in rows),
        }

    return {
        "pass": _to_int(_first_row_value(total_row, ("pass", "합격"))),
        "fail": _to_int(_first_row_value(total_row, ("fail", "불합격"))),
        "hold": _to_int(_first_row_value(total_row, ("hold", "보류"))),
        "total": _to_int(_first_row_value(total_row, ("total", "전체", "총"))),
    }


def _first_row_value(row: dict[str, Any], needles: tuple[str, ...]) -> Any:
    for key, value in row.items():
        norm_key = str(key).casefold()
        if any(needle in norm_key for needle in needles):
            return value
    return 0


def _summary_entries(summary_path: Path) -> list[dict[str, Any]]:
    rows = _read_summary_rows_by_position(summary_path)
    total_markers = {"전체", "총계", "합계", "total", "overall"}

    if not rows:
        rows = []
        for row in _read_summary_csv(summary_path):
            name = _first_row_value(row, ("name", "제조", "stage"))
            if not name:
                values = [str(value).strip() for value in row.values() if str(value).strip()]
                name = values[0] if values else ""
            rows.append(
                {
                    "name": str(name).strip(),
                    "pass": _first_row_value(row, ("pass", "합격")),
                    "fail": _first_row_value(row, ("fail", "불합격")),
                    "hold": _first_row_value(row, ("hold", "보류")),
                    "total": _first_row_value(row, ("total", "전체", "총")),
                }
            )

    entries: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name", "")).strip()
        if not name or name.casefold() in total_markers:
            continue
        entries.append(
            {
                "name": name,
                "counts": {
                    "pass": _to_int(row.get("pass", 0)),
                    "fail": _to_int(row.get("fail", 0)),
                    "hold": _to_int(row.get("hold", 0)),
                    "total": _to_int(row.get("total", 0)),
                },
            }
        )
    return entries


def _node_label_from_dict(obj: dict[str, Any]) -> str:
    labels = []

    for key in [
        "id",
        "key",
        "name",
        "label",
        "title",
        "display_name",
        "stage_name",
        "text",
        "node_name",
    ]:
        value = obj.get(key)

        if value:
            labels.append(str(value))

    return " ".join(labels)


def _best_counts_for_label(label: str, entries: list[dict[str, Any]]) -> dict[str, int] | None:
    best_score = 0.0
    best_counts = None

    for entry in entries:
        score = _stage_match_score(label, str(entry["name"]))

        if score > best_score:
            best_score = score
            best_counts = entry["counts"]

    if best_score < 0.55:
        return None

    return dict(best_counts)


def _apply_counts_to_node_dict(obj: dict[str, Any], counts: dict[str, int]) -> None:
    passed = int(counts.get("pass", 0))
    failed = int(counts.get("fail", 0))
    held = int(counts.get("hold", 0))
    total = int(counts.get("total", 0))

    pass_keys = [
        "pass",
        "passed",
        "ok",
        "success",
        "green",
        "pass_count",
        "passed_count",
        "ok_count",
        "green_count",
        "검수합격",
        "합격",
    ]

    fail_keys = [
        "fail",
        "failed",
        "ng",
        "red",
        "fail_count",
        "failed_count",
        "ng_count",
        "red_count",
        "검수불합격",
        "불합격",
    ]

    hold_keys = [
        "hold",
        "held",
        "pending",
        "warning",
        "yellow",
        "orange",
        "hold_count",
        "held_count",
        "pending_count",
        "warning_count",
        "yellow_count",
        "orange_count",
        "검수보류",
        "보류",
    ]

    total_keys = [
        "total",
        "count",
        "total_count",
        "white",
        "white_count",
        "전체",
        "총계",
        "총 계",
    ]

    for key in pass_keys:
        obj[key] = passed

    for key in fail_keys:
        obj[key] = failed

    for key in hold_keys:
        obj[key] = held

    for key in total_keys:
        obj[key] = total

    for nested_key in [
        "counts",
        "summary",
        "stats",
        "result",
        "judgement",
        "after",
        "before",
    ]:
        nested = obj.get(nested_key)

        if isinstance(nested, dict):
            for key in pass_keys:
                nested[key] = passed

            for key in fail_keys:
                nested[key] = failed

            for key in hold_keys:
                nested[key] = held

            for key in total_keys:
                nested[key] = total



def patch_graph_json_with_summary(graph_json: str, summary_path: Path) -> str:
    """
    최종 summary CSV 값을 graph_json 내부 노드 카운트에 반영한다.
    시뮬레이션에서는 판정 완료 후 생성된 summary_after.csv만 넘긴다.
    """
    entries = _summary_entries(summary_path)

    if not entries:
        return graph_json

    try:
        data = json.loads(graph_json)
    except Exception:
        return graph_json

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            label = _node_label_from_dict(obj)
            counts = _best_counts_for_label(label, entries)

            if counts is not None:
                _apply_counts_to_node_dict(obj, counts)

            for value in obj.values():
                walk(value)

        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)

    return json.dumps(data, ensure_ascii=False)


def attach_after_summary_targets(graph_json: str, summary_path: Path) -> str:
    """Attach summary_after counts as animation targets without changing before counts."""
    entries = _summary_entries(summary_path)
    if not entries:
        return graph_json

    try:
        data = json.loads(graph_json)
    except Exception:
        return graph_json

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            label = _node_label_from_dict(obj)
            counts = _best_counts_for_label(label, entries)
            if counts is not None:
                obj["after_pass"] = int(counts.get("pass", 0))
                obj["after_fail"] = int(counts.get("fail", 0))
                obj["after_hold"] = int(counts.get("hold", 0))
                obj["after_total"] = int(counts.get("total", 0))

            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return json.dumps(data, ensure_ascii=False)


# ============================================================
# 기존 시뮬레이션 CSV 폴더에 summary_before/after 연결
# ============================================================

def _prepare_runtime_simulation_csv_dir(
    original_csv_dir: Path,
    root_dir: Path,
    summary_before_path: Path,
    summary_after_path: Path,
) -> Path:
    """
    기존 sp_app.py 시뮬레이션 구조를 유지하기 위해
    원래 그래프 CSV 폴더를 복사하고 그 주변에 새 summary_before/after를 배치한다.
    """
    runtime_parent = _ensure_dir(root_dir / "runtime_simulation_source")
    runtime_csv_dir = runtime_parent / original_csv_dir.name

    if runtime_csv_dir.exists():
        shutil.rmtree(runtime_csv_dir)

    shutil.copytree(original_csv_dir, runtime_csv_dir)

    # csv_dir.parent 기준 대응
    shutil.copy2(summary_before_path, runtime_parent / "summary_before.csv")
    shutil.copy2(summary_after_path, runtime_parent / "summary_after.csv")
    shutil.copy2(summary_after_path, runtime_parent / "summary.csv")

    # csv_dir 내부 기준 대응
    shutil.copy2(summary_before_path, runtime_csv_dir / "summary_before.csv")
    shutil.copy2(summary_after_path, runtime_csv_dir / "summary_after.csv")
    shutil.copy2(summary_after_path, runtime_csv_dir / "summary.csv")

    return runtime_csv_dir


# ============================================================
# 판정 아티팩트 생성
# ============================================================

def _attach_rule_regression_log(artifacts: dict[str, Any]) -> dict[str, Any]:
    """Persist a non-UI regression trace without changing judgement output."""
    result = artifacts.get("after_result")
    if result is None:
        return artifacts

    project_root = Path(__file__).resolve().parent
    try:
        logged = append_rule_regression_log(
            result,
            project_root=project_root,
            permit_paths=artifacts.get("permit_paths", []),
            summary_counts_path=artifacts.get("summary_after_path"),
        )
        artifacts["rule_regression_workbook_path"] = logged.workbook_path
        artifacts["rule_regression_run_id"] = logged.run_id
        artifacts["rule_regression_appended"] = logged.appended
        artifacts.pop("rule_regression_error", None)
        write_output_index(logged.workbook_path.parent)
    except Exception as exc:
        # Regression logging is observability only. It must never block judgement.
        artifacts["rule_regression_error"] = f"{type(exc).__name__}: {exc}"
    return artifacts


def get_judgement_job(
    *, pdf_path, permit_paths, original_csv_dir, company="", product="",
    product_mapping_id="", _prepared=None
):
    """Start once per full input identity, without blocking the Streamlit script."""
    captured_review_scope = current_permit_review_scope()
    if _prepared is None:
        details = resolve_domain_detail_profile(company, product)
        permits = resolve_permits(
            permit_paths,
            company,
            product,
            product_mapping_id=product_mapping_id,
        )
        execution_settings = resolve_clova_execution_settings()
        execution_fingerprint = llm_execution_fingerprint(execution_settings)
        semantic_settings = read_semantic_settings()
        key = _artifact_key(Path(pdf_path), list(permits.paths), original_csv_dir,
                            company, product, details.fingerprint, permits.fingerprint,
                            product_mapping_id, execution_fingerprint, semantic_cache_fingerprint(semantic_settings))
        _prepared = (
            details,
            permits,
            key,
            execution_settings,
            execution_fingerprint,
            semantic_settings,
        )
    key = _prepared[2]
    target = Path(pdf_path).resolve()
    retry_request = st.session_state.pop(_RETRY_REQUEST_SESSION_KEY, None)
    retry_failed = retry_request == (str(target), _file_sig(target))
    def run_captured_review(progress):
        if current_permit_review_scope() != captured_review_scope:
            raise JudgementCacheError('대기 중 허가서 적용 범위가 바뀌어 검수를 시작하지 않았습니다.')
        return ensure_judgement_artifacts(pdf_path=pdf_path, permit_paths=permit_paths,
            original_csv_dir=original_csv_dir, company=company, product=product,
            product_mapping_id=product_mapping_id,
            _background=True, _progress=progress, _prepared=_prepared,
            _retry_failed=retry_failed)
    return review_jobs.start(key, str(Path(pdf_path).resolve()), run_captured_review)


def ensure_judgement_artifacts(
    *,
    pdf_path: Path,
    permit_paths: list[Path],
    original_csv_dir: Path | None,
    company: str = "",
    product: str = "",
    product_mapping_id: str = "",
    _background: bool = False,
    _progress=None,
    _prepared=None,
    _retry_failed: bool = False,
    _defer_pending: bool = False,
) -> dict[str, Any]:
    """
    실제 sp_pdf_judger를 실행해서 before/after 판정 결과와 summary CSV를 만든다.

    before_result:
      permit_pdf_paths=[] 로 실행
      -> summary_before.csv

    after_result:
      permit_pdf_paths=permit_paths 로 실행
      -> summary_after.csv
    """
    pdf_path = Path(pdf_path)
    permit_paths = [Path(path) for path in permit_paths]
    original_csv_dir = Path(original_csv_dir) if original_csv_dir else None
    company = str(company or "").strip()
    product = str(product or "").strip()
    product_mapping_id = str(product_mapping_id or "").strip()
    semantic_settings = read_semantic_settings() if _prepared is None else (
        _prepared[5] if len(_prepared) >= 6 else SemanticSettings())
    _assert_semantic_settings(semantic_settings)
    if _prepared is None:
        detail_profile = resolve_domain_detail_profile(company, product)
        permit_resolution = resolve_permits(
            permit_paths,
            company,
            product,
            product_mapping_id=product_mapping_id,
        )
        execution_settings = resolve_clova_execution_settings()
        execution_fingerprint = llm_execution_fingerprint(execution_settings)
    else:
        detail_profile, permit_resolution = _prepared[:2]
        if len(_prepared) >= 5:
            execution_settings = _prepared[3]
            execution_fingerprint = _prepared[4]
        else:
            # Compatibility for process-local jobs created before this attempt
            # snapshot was introduced. New production jobs always use 5 items.
            execution_settings = resolve_clova_execution_settings()
            execution_fingerprint = llm_execution_fingerprint(execution_settings)
    resolved_permit_paths = list(permit_resolution.paths)

    key = _artifact_key(
        pdf_path,
        resolved_permit_paths,
        original_csv_dir,
        company,
        product,
        detail_profile.fingerprint,
        permit_resolution.fingerprint,
        product_mapping_id,
        execution_fingerprint,
        semantic_cache_fingerprint(semantic_settings),
    )
    session_key = f"sp_direct_judgement_artifacts::{key}"

    if not _background and session_key in st.session_state:
        st.session_state.pop(_RETRY_REQUEST_SESSION_KEY, None)
        return st.session_state[session_key]

    # A direct final-page visit joins an existing UI job rather than duplicating
    # its extraction/API calls. Worker threads never read/write session_state.
    if not _background:
        job = get_judgement_job(pdf_path=pdf_path, permit_paths=permit_paths,
            original_csv_dir=original_csv_dir, company=company, product=product,
            product_mapping_id=product_mapping_id,
            _prepared=(
                detail_profile,
                permit_resolution,
                key,
                execution_settings,
                execution_fingerprint,
                semantic_settings,
            ))
        if _defer_pending and not job.future.done():
            raise JudgementPending(job)
        with st.spinner("문서 판정 결과를 준비하고 있습니다..."):
            artifacts = job.future.result()
        _assert_semantic_settings(semantic_settings)
        st.session_state[session_key] = artifacts
        st.session_state["latest_judgement_artifact_key"] = key
        st.session_state["latest_judgement_pdf_path"] = str(pdf_path)
        return artifacts

    progress = _progress or (lambda message: None)
    progress("저장된 판정 및 입력 문서 확인")

    root_dir = (
        Path(tempfile.gettempdir())
        / "sp_app_direct_judgement"
        / key
    )
    # This encloses both the cache recheck and every artifact write. Other
    # server processes use the same stable lockfile outside the archive tree.
    with artifact_process_lock(root_dir.parent / ".locks" / f"{key}.lock",
            on_wait=lambda: progress("다른 프로세스의 동일 문서 판정 완료 대기")) as waited:
        _assert_semantic_settings(semantic_settings)
        if llm_execution_fingerprint() != execution_fingerprint:
            raise JudgementCacheError(
                "검수 대기 중 CLOVA 실행 설정이 변경되어 이전 설정의 결과를 적용하지 않았습니다. "
                "문서 목록에서 '검수 진행'을 눌러 다시 확인하세요."
            )
        if key != _artifact_key(pdf_path, resolved_permit_paths, original_csv_dir,
                company, product, detail_profile.fingerprint,
                permit_resolution.fingerprint, product_mapping_id,
                execution_fingerprint, semantic_cache_fingerprint(semantic_settings)):
            raise JudgementCacheError(
                "대기 중 입력 문서 또는 판정 규칙이 변경되어 이전 결과를 적용하지 않았습니다. "
                "문서 목록에서 '검수 진행'을 눌러 다시 확인하세요."
            )
        if waited and not (root_dir / "judgement_artifacts.pkl").exists():
            raise JudgementCacheError(
                "앞선 검수가 결과를 저장하지 못해 대기 요청을 자동 재실행하지 않았습니다. "
                "문서 목록에서 '검수 진행'을 눌러 다시 시도하세요."
            )
        # A click while another owner runs joins that attempt, including its
        # failure. It must not queue a second paid retry after the owner exits.
        try:
            with clova_request_context(progress, JUDGEMENT_STATUS_DIR / "clova_responses"):
                return _ensure_disk_artifacts(key=key, root_dir=root_dir, pdf_path=pdf_path,
                    resolved_permit_paths=resolved_permit_paths, original_csv_dir=original_csv_dir,
                    company=company, product=product, detail_profile=detail_profile,
                    product_mapping_id=product_mapping_id,
                    permit_resolution=permit_resolution, progress=progress,
                    _retry_failed=_retry_failed and not waited,
                    llm_execution_settings=execution_settings,
                    attempt_llm_fingerprint=execution_fingerprint, semantic_settings=semantic_settings)
        except ClovaServiceError as exc:
            # Keep failed requests separate from all actual judgement results.
            # No fake HOLD rows, summary CSV or completed pickle is published.
            _remember_service_failure(root_dir, key, pdf_path, exc,
                permit_resolution, detail_profile, execution_fingerprint, semantic_settings)
            raise


def _remember_service_failure(
    root_dir,
    key,
    pdf_path,
    error,
    permits,
    details,
    execution_fingerprint=None,
    semantic_settings=None,
):
    review_scope = current_permit_review_scope()
    path = root_dir / "service_failure.json"
    if not path.exists():
        pending = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root_dir,
                    prefix=".service-failure-", suffix=".tmp", delete=False) as stream:
                pending = Path(stream.name)
                json.dump(error.as_dict(), stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            pending.replace(path)
        finally:
            if pending is not None and pending.exists():
                pending.unlink()
    stat = pdf_path.stat()
    _store_status_entry(pdf_path, {
        "status": "failed", "cache_version": JUDGEMENT_CACHE_VERSION,
        "permit_review_scope": review_scope,
        "sp_sha256": file_sha256(pdf_path) if review_scope == 'hold_only' else None,
        "permit_file_hashes": {
            str(Path(path).resolve()): file_sha256(Path(path))
            for path in getattr(permits, 'paths', ())
        } if review_scope == 'hold_only' else {},
        "artifact_key": key, "pdf_path": str(pdf_path),
        "pdf_sig": {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "summary_counts": {}, "service_failure": error.as_dict(),
        "rule_fingerprint": policy_fingerprint(),
        "extraction_fingerprint": extraction_fingerprint(),
        "llm_execution_fingerprint": (
            execution_fingerprint or llm_execution_fingerprint()
        ),
        "permit_fingerprint": permits.fingerprint,
        "semantic_fingerprint": semantic_cache_fingerprint(semantic_settings or SemanticSettings()),
        "detail_fingerprint": details.fingerprint,
    })


def _read_service_failure(path):
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(info, dict) or "kind" not in info:
            raise ValueError("Invalid failure record")
        return ClovaServiceError(**{key: info[key] for key in
            ("kind", "status_code", "provider_code", "retry_after") if key in info})
    except (OSError, ValueError, TypeError) as exc:
        raise JudgementCacheError("저장된 API 실패 기록을 읽지 못했습니다. 자동 재호출하지 않습니다. "
                                  "문서 목록의 '검수 진행'으로 재시도하세요.") from exc


def _record_judgement_attempt(root_dir: Path, key: str, pdf_path: Path) -> None:
    """Durable start evidence, not a resumable result. Caller holds the OS lock.

    Even a truncated record blocks implicit retry when the final cache is absent.
    A previous record is overwritten only after its workspace has been archived.
    """
    with (root_dir / "attempt_started.json").open("w", encoding="utf-8") as stream:
        json.dump({"artifact_key": key, "cache_version": JUDGEMENT_CACHE_VERSION,
            "pdf_path": str(pdf_path.resolve()), "pdf_signature": _file_sig(pdf_path),
            "started_at": datetime.now().isoformat(timespec="seconds")}, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())


def _ensure_disk_artifacts(*, key, root_dir, pdf_path, resolved_permit_paths,
        original_csv_dir, company, product, product_mapping_id, detail_profile, permit_resolution,
        progress, _retry_failed, llm_execution_settings,
        attempt_llm_fingerprint, semantic_settings=None):
    """Caller must hold the artifact OS lock through publication/status writes."""
    review_scope = current_permit_review_scope()
    scope_kwargs = {'permit_review_scope': review_scope} if review_scope == 'hold_only' else {}
    semantic_settings = semantic_settings or SemanticSettings()
    _assert_semantic_settings(semantic_settings)
    semantic_kwargs = {'semantic_settings': semantic_settings} if semantic_settings.mode != 'legacy' else {}
    _ensure_dir(root_dir)
    result_cache_path = root_dir / "judgement_artifacts.pkl"
    failure_path = root_dir / "service_failure.json"
    if failure_path.exists():
        if not _retry_failed:
            raise _read_service_failure(failure_path)
        # Also archive any old failed pickle/CSV left by an interrupted retry.
        archive_dir = root_dir.with_name(f"{root_dir.name}.service-failed-{uuid4().hex}")
        root_dir.rename(archive_dir)
        _ensure_dir(root_dir)
    if not result_cache_path.exists() and any(root_dir.iterdir()):
        # Includes pre-marker legacy workspaces and unreadable/partial records.
        # Never infer reusable stage results from CSVs or a folder's existence.
        if not _retry_failed:
            raise JudgementCacheError(
                "앞선 검수가 최종 결과를 저장하지 못해 자동 재실행하지 않았습니다. "
                "중단 자료는 보존됩니다. 문서 목록에서 '검수 진행'을 누르면 "
                "기존 자료를 별도 보관하고 처음부터 재시도합니다."
            )
        # Same-parent rename preserves every old file and prevents stale CSVs
        # from leaking into the new attempt. OS lockfiles live outside root_dir.
        archive_dir = root_dir.with_name(f"{root_dir.name}.interrupted-{uuid4().hex}")
        root_dir.rename(archive_dir)
        _ensure_dir(root_dir)

    before_stage_dir = _ensure_dir(root_dir / "before_stage_csv")
    after_stage_dir = _ensure_dir(root_dir / "after_stage_csv")

    summary_before_path = root_dir / "summary_before.csv"
    summary_after_path = root_dir / "summary_after.csv"

    if result_cache_path.exists():
        try:
            artifacts = _read_judgement_cache(result_cache_path)
        except JudgementCacheError:
            if not _retry_failed:
                raise
            artifacts = None

        if artifacts is not None and (not _retry_failed or not _has_runtime_llm_failure(artifacts)):
            _assert_semantic_settings(semantic_settings)
            # Failed API attempts are reviewable history, never completed
            # reviews. _remember_judgement_artifacts keeps their failed status.
            if llm_execution_fingerprint() != attempt_llm_fingerprint:
                raise JudgementCacheError(
                    "검수 중 CLOVA 실행 설정이 변경되어 저장 결과를 적용하지 않았습니다. "
                    "문서 목록에서 '검수 진행'을 눌러 다시 확인하세요."
                )
            artifacts["root_dir"] = root_dir
            artifacts["summary_before_path"] = summary_before_path
            artifacts["summary_after_path"] = summary_after_path
            artifacts["before_stage_dir"] = before_stage_dir
            artifacts["after_stage_dir"] = after_stage_dir
            artifacts["pdf_path"] = pdf_path
            artifacts["permit_paths"] = resolved_permit_paths
            artifacts["resolved_permit_paths"] = resolved_permit_paths
            artifacts["permit_policy_id"] = (
                permit_resolution.policy.policy_id
                if permit_resolution.policy is not None
                else None
            )
            artifacts["permit_fingerprint"] = permit_resolution.fingerprint
            artifacts["product_mapping_id"] = product_mapping_id
            artifacts["permit_resolution_errors"] = list(permit_resolution.errors)
            artifacts["permit_extraction_diagnostics"] = list(
                getattr(artifacts.get("after_result"), "metadata", {}).get(
                    "permit_extraction_diagnostics", []
                )
            )
            if original_csv_dir and original_csv_dir.exists():
                artifacts["runtime_csv_dir"] = _prepare_runtime_simulation_csv_dir(
                    original_csv_dir=original_csv_dir,
                    root_dir=root_dir,
                    summary_before_path=summary_before_path,
                    summary_after_path=summary_after_path,
                )
            if not artifacts.get("rule_regression_run_id"):
                _attach_rule_regression_log(artifacts)
            if llm_execution_fingerprint() != attempt_llm_fingerprint:
                raise JudgementCacheError(
                    "검수 중 CLOVA 실행 설정이 변경되어 저장 결과를 적용하지 않았습니다. "
                    "문서 목록에서 '검수 진행'을 눌러 다시 확인하세요."
                )
            _assert_semantic_settings(semantic_settings)
            if current_permit_review_scope() != review_scope:
                raise JudgementCacheError('검수 중 허가서 적용 범위가 바뀌어 저장 결과를 적용하지 않았습니다.')
            _remember_judgement_artifacts(artifacts)
            return artifacts

        # Preserve all CSVs and the original pickle before an explicit retry.
        # A sibling folder avoids recursively copying older attempts. If this
        # copy fails, stop before starting any new extraction or paid request.
        archive_dir = root_dir.with_name(f"{root_dir.name}.failed-{uuid4().hex}")
        shutil.copytree(root_dir, archive_dir)

    _record_judgement_attempt(root_dir, key, pdf_path)
    with nullcontext():
        progress("SP 문서 추출 및 문서 기준 판정 준비")
        before_pipeline = DocumentJudgePipeline(
            permit_pdf_paths=[],
            company=company,
            product=product,
            permit_resolution=permit_resolution,
            permit_enabled=False,
            llm_execution_settings=llm_execution_settings,
            **semantic_kwargs,
            **scope_kwargs,
        )
        grouped_authority = bool(
            review_scope != 'hold_only'
            and permit_resolution.policy is not None
            and permit_resolution.policy.authoritative
            and getattr(permit_resolution.policy, "evaluation_mode", "legacy") == "deterministic_first"
        )
        before_result = before_pipeline.run(
            pdf_path,
            # Keep the SP baseline and document policies; do not pay for a row
            # semantic verdict that the authoritative grouped pass will replace.
            primary_tests_only=grouped_authority,
            progress_callback=lambda message: progress("SP: " + message),
        )

        if current_permit_review_scope() != review_scope:
            raise JudgementCacheError('검수 중 허가서 적용 범위가 바뀌어 허가서 대조를 시작하지 않았습니다.')

        if review_scope == 'hold_only':
            from sp_pdf_judger.hold_only_review import apply_held_permit_review
            after_result = apply_held_permit_review(before_result,
                pipeline_factory=lambda: DocumentJudgePipeline(
                    permit_pdf_paths=resolved_permit_paths, company=company, product=product,
                    permit_resolution=permit_resolution, llm_execution_settings=llm_execution_settings,
                    **semantic_kwargs, **scope_kwargs),
                progress_callback=lambda message: progress('보류 항목 허가서 대조: ' + message))
        elif resolved_permit_paths or (permit_resolution.policy is not None and permit_resolution.policy.authoritative):
            progress("허가서 원문 및 적용 기준 준비")
            after_pipeline = DocumentJudgePipeline(
                permit_pdf_paths=resolved_permit_paths,
                company=company,
                product=product,
                permit_resolution=permit_resolution,
                llm_execution_settings=llm_execution_settings,
                **semantic_kwargs,
            )
            after_result = after_pipeline.run(
                pdf_path,
                extracted_records=before_result.extracted_records,
                static_result=before_result,
                progress_callback=lambda message: progress("허가서 대조: " + message),
            )
        else:
            after_result = before_result

        if llm_execution_fingerprint() != attempt_llm_fingerprint:
            raise JudgementCacheError(
                "검수 중 CLOVA 실행 설정이 변경되어 완료 결과를 저장하지 않았습니다. "
                "문서 목록에서 '검수 진행'을 눌러 다시 확인하세요."
            )

        _assert_semantic_settings(semantic_settings)
        progress("판정 완료 · 요약 및 근거 파일 생성")
        if current_permit_review_scope() != review_scope:
            raise JudgementCacheError('검수 중 허가서 적용 범위가 바뀌어 결과를 저장하지 않았습니다.')
        _write_summary_csv_from_result(before_result, summary_before_path)
        _write_summary_csv_from_result(after_result, summary_after_path)

        try:
            make_stage_csv_zip(
                result=before_result,
                output_dir=before_stage_dir,
                zip_name=f"{pdf_path.stem}_before_제조요약도별_CSV.zip",
            )
        except Exception:
            pass

        try:
            make_stage_csv_zip(
                result=after_result,
                output_dir=after_stage_dir,
                zip_name=f"{pdf_path.stem}_after_제조요약도별_CSV.zip",
            )
        except Exception:
            pass

    runtime_csv_dir = None

    if original_csv_dir and original_csv_dir.exists():
        runtime_csv_dir = _prepare_runtime_simulation_csv_dir(
            original_csv_dir=original_csv_dir,
            root_dir=root_dir,
            summary_before_path=summary_before_path,
            summary_after_path=summary_after_path,
        )

    artifacts = {
        "artifact_key": key,
        "cache_version": JUDGEMENT_CACHE_VERSION,
        "root_dir": root_dir,
        "before_result": before_result,
        "after_result": after_result,
        "permit_review_scope": review_scope,
        "summary_before_path": summary_before_path,
        "summary_after_path": summary_after_path,
        "before_stage_dir": before_stage_dir,
        "after_stage_dir": after_stage_dir,
        "runtime_csv_dir": runtime_csv_dir,
        "pdf_path": pdf_path,
        "permit_paths": resolved_permit_paths,
        "resolved_permit_paths": resolved_permit_paths,
        "permit_policy_id": (
            permit_resolution.policy.policy_id
            if permit_resolution.policy is not None
            else None
        ),
        "permit_fingerprint": permit_resolution.fingerprint,
        "permit_resolution_errors": list(permit_resolution.errors),
        "permit_extraction_diagnostics": list(
            after_result.metadata.get("permit_extraction_diagnostics", [])
        ),
        "company": company,
        "product": product,
        "product_mapping_id": product_mapping_id,
        "detail_fingerprint": detail_profile.fingerprint,
        "rule_fingerprint": policy_fingerprint(),
        "extraction_fingerprint": extraction_fingerprint(),
        "llm_execution_fingerprint": attempt_llm_fingerprint,
        "semantic_fingerprint": semantic_cache_fingerprint(semantic_settings),
        "detail_sources": detail_profile.sources,
    }

    if llm_execution_fingerprint() != attempt_llm_fingerprint:
        raise JudgementCacheError(
            "검수 중 CLOVA 실행 설정이 변경되어 완료 결과를 저장하지 않았습니다. "
            "문서 목록에서 '검수 진행'을 눌러 다시 확인하세요."
        )
    _assert_semantic_settings(semantic_settings)
    if current_permit_review_scope() != review_scope:
        raise JudgementCacheError('검수 중 허가서 적용 범위가 바뀌어 결과를 저장하지 않았습니다.')
    _attach_rule_regression_log(artifacts)
    _write_judgement_cache(result_cache_path, artifacts)
    _remember_judgement_artifacts(artifacts)

    return artifacts


# ============================================================
# before/after 집계 패널
# ============================================================

def _summary_style() -> str:
    return """
    <style>
    .phase-wrap {
        margin: 8px 0 16px 0;
        padding: 14px 16px;
        border: 1px solid #3a4442;
        border-radius: 18px;
        background: rgba(20, 28, 27, 0.82);
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        color: #e5e7eb;
    }

    .phase-card {
        display: inline-block;
        vertical-align: top;
        width: calc(50% - 32px);
        min-width: 260px;
        padding: 14px 16px;
        border-radius: 14px;
        background: rgba(12, 18, 18, 0.76);
        border: 1px solid #3a4442;
    }

    .phase-card.before {
        border-left: 5px solid #f59e0b;
    }

    .phase-card.after {
        border-left: 5px solid #22c55e;
    }

    .phase-arrow {
        display: inline-flex;
        width: 48px;
        height: 112px;
        align-items: center;
        justify-content: center;
        font-size: 24px;
        font-weight: 900;
        color: #9ca3af;
    }

    .phase-kicker {
        font-size: 11px;
        font-weight: 900;
        color: #9ca3af;
        margin-bottom: 4px;
    }

    .phase-title {
        font-size: 17px;
        font-weight: 900;
        color: #f9fafb;
        margin-bottom: 10px;
    }

    .phase-grid {
        display: grid;
        grid-template-columns: 1fr 76px;
        row-gap: 6px;
        font-size: 13px;
    }

    .phase-grid span {
        color: #cbd5e1;
        font-weight: 800;
    }

    .phase-grid b {
        text-align: right;
        color: #ffffff;
    }

    .phase-grid em {
        font-style: normal;
        font-size: 11px;
        color: #60a5fa;
        margin-left: 4px;
    }

    .phase-note {
        margin-top: 12px;
        font-size: 13px;
        color: #cbd5e1;
        font-weight: 700;
    }

    .summary-details {
        margin: 8px 0;
        border: 1px solid #3a4442;
        border-radius: 12px;
        padding: 9px 12px;
        background: rgba(12, 18, 18, 0.76);
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        color: #e5e7eb;
    }

    .summary-details summary {
        cursor: pointer;
        font-weight: 900;
        color: #f9fafb;
    }

    .summary-table-box {
        overflow-x: auto;
        margin-top: 10px;
    }

    .summary-table {
        width: 100%;
        border-collapse: collapse;
        font-size: 12px;
    }

    .summary-table th,
    .summary-table td {
        border: 1px solid #3a4442;
        padding: 7px 8px;
        text-align: left;
    }

    .summary-table th {
        background: rgba(31, 41, 55, 0.72);
        font-weight: 900;
    }
    </style>
    """


# ============================================================
# 시뮬레이션 HTML JS 주입
# ============================================================

def _render_summary_table(title: str, rows: list[dict[str, str]]) -> str:
    if not rows:
        return ""

    normalized = []
    for row in rows:
        if {"name", "pass", "fail", "hold", "total"} <= set(row.keys()):
            normalized.append(row)
        else:
            values = [str(value).strip() for value in row.values()]
            padded = values + [""] * 5
            normalized.append(
                {
                    "name": padded[0],
                    "pass": padded[1],
                    "fail": padded[2],
                    "hold": padded[3],
                    "total": padded[4],
                }
            )

    body = []
    for row in normalized:
        body.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('name', '')))}</td>"
            f"<td>{html.escape(str(row.get('pass', '0')))}</td>"
            f"<td>{html.escape(str(row.get('fail', '0')))}</td>"
            f"<td>{html.escape(str(row.get('hold', '0')))}</td>"
            f"<td>{html.escape(str(row.get('total', '0')))}</td>"
            "</tr>"
        )

    return f"""
    <details class="summary-details">
      <summary>{html.escape(title)}</summary>
      <div class="summary-table-box">
        <table class="summary-table">
          <thead>
            <tr>
              <th>제조명</th>
              <th>충족</th>
              <th>불충족</th>
              <th>보류</th>
              <th>전체</th>
            </tr>
          </thead>
          <tbody>{''.join(body)}</tbody>
        </table>
      </div>
    </details>
    """


def render_simulation_summary_panels(artifacts: dict[str, Any]) -> None:
    after_rows = _read_summary_rows_by_position(Path(artifacts["summary_after_path"]))

    st.markdown(
        _summary_style()
        + _render_summary_table("최종 판정 상세 집계", after_rows),
        unsafe_allow_html=True,
    )


def inject_after_summary_transition(html_text: str, after_graph_json: str) -> str:
    """
    시뮬레이션 수치는 최종 summary 기준으로 유지한다.
    남아 있는 이벤트 hook은 기존 iframe 이벤트 호환용이다.
    """
    safe_after_json_literal = json.dumps(after_graph_json, ensure_ascii=False)

    injection = r"""

// ===== 최종 판정 후 summary_after.csv 수치 전환 =====
try {
  const __SP_AFTER_GRAPH_JSON_TEXT__ = __AFTER_GRAPH_JSON_TEXT__;
  const __SP_AFTER_GRAPH__ = JSON.parse(__SP_AFTER_GRAPH_JSON_TEXT__);

  function __spNormStageText(value) {
    let text = String(value || '').toLowerCase();

    text = text
      .replace(/e\s*\.?\s*colli/g, 'ecoli')
      .replace(/e\s*\.?\s*coli/g, 'ecoli')
      .replace(/chol/g, 'cho')
      .replace(/컴포넌트/g, 'component')
      .replace(/component\s*([a-z0-9]+)/g, 'component$1')
      .replace(/마스터\s*세포주/g, '마스터세포주')
      .replace(/제조용\s*세포주/g, '제조용세포주')
      .replace(/중간체\s*원액/g, '중간체원액')
      .replace(/최종\s*원액/g, '최종원액')
      .replace(/완제\s*의약품/g, '완제의약품')
      .replace(/나노\s*파티클/g, '나노파티클')
      .replace(/나노\s*피티클/g, '나노파티클')
      .replace(/[\s_\-./(){}\[\]:：,]+/g, '');

    return text;
  }

  function __spLabelFromObj(obj) {
    if (!obj || typeof obj !== 'object') return '';

    const keys = [
      'id',
      'key',
      'name',
      'label',
      'title',
      'display_name',
      'stage_name',
      'text',
      'node_name'
    ];

    const parts = [];

    for (const key of keys) {
      if (obj[key] !== undefined && obj[key] !== null) {
        parts.push(String(obj[key]));
      }
    }

    return parts.join(' ');
  }

  function __spNum(value) {
    const n = Number(value || 0);
    return Number.isFinite(n) ? n : 0;
  }

  function __spCountsFromObj(obj) {
    if (!obj || typeof obj !== 'object') return null;

    const passValue =
      obj.pass ?? obj.passed ?? obj.ok ?? obj.green ??
      obj.pass_count ?? obj.passed_count ?? obj.ok_count ?? obj.green_count ??
      obj['검수합격'] ?? obj['합격'];

    const failValue =
      obj.fail ?? obj.failed ?? obj.ng ?? obj.red ??
      obj.fail_count ?? obj.failed_count ?? obj.ng_count ?? obj.red_count ??
      obj['검수불합격'] ?? obj['불합격'];

    const holdValue =
      obj.hold ?? obj.held ?? obj.pending ?? obj.warning ?? obj.yellow ?? obj.orange ??
      obj.hold_count ?? obj.held_count ?? obj.pending_count ?? obj.warning_count ??
      obj.yellow_count ?? obj.orange_count ??
      obj['검수보류'] ?? obj['보류'];

    const totalValue =
      obj.total ?? obj.count ?? obj.total_count ?? obj.white ?? obj.white_count ??
      obj['전체'] ?? obj['총계'] ?? obj['총 계'];

    const p = __spNum(passValue);
    const f = __spNum(failValue);
    const h = __spNum(holdValue);
    const t = __spNum(totalValue || (p + f + h));

    if (p === 0 && f === 0 && h === 0 && t === 0) return null;

    return {
      pass: p,
      fail: f,
      hold: h,
      total: t
    };
  }

  function __spCollectAfterCounts(root) {
    const out = {};

    function walk(obj) {
      if (Array.isArray(obj)) {
        for (const item of obj) walk(item);
        return;
      }

      if (!obj || typeof obj !== 'object') return;

      const label = __spLabelFromObj(obj);
      const key = __spNormStageText(label);
      const counts = __spCountsFromObj(obj);

      if (key && counts) {
        out[key] = counts;
      }

      for (const value of Object.values(obj)) {
        walk(value);
      }
    }

    walk(root);
    return out;
  }

  const __SP_AFTER_COUNTS__ = __spCollectAfterCounts(__SP_AFTER_GRAPH__);

  function __spBestCountsForNode(nodeKey, nodeLabel) {
    const key = __spNormStageText(nodeKey + ' ' + nodeLabel);

    if (!key) return null;

    if (__SP_AFTER_COUNTS__[key]) {
      return __SP_AFTER_COUNTS__[key];
    }

    let bestCounts = null;
    let bestScore = 0;

    for (const [candidateKey, counts] of Object.entries(__SP_AFTER_COUNTS__)) {
      if (!candidateKey) continue;

      let score = 0;

      if (key === candidateKey) {
        score = 1;
      } else if (key.includes(candidateKey) || candidateKey.includes(key)) {
        score = 0.92;
      } else {
        const common = [...candidateKey].filter(ch => key.includes(ch)).length;
        const denom = Math.max(candidateKey.length, key.length, 1);
        score = common / denom;
      }

      if (score > bestScore) {
        bestScore = score;
        bestCounts = counts;
      }
    }

    if (bestScore < 0.55) return null;

    return bestCounts;
  }

  function __spSvg(tag, attrs) {
    const el = document.createElementNS('http://www.w3.org/2000/svg', tag);

    for (const [key, value] of Object.entries(attrs || {})) {
      el.setAttribute(key, value);
    }

    return el;
  }

  function __spRemoveOldOverlay() {
    const olds = document.querySelectorAll('[data-sp-after-overlay="1"]');
    olds.forEach(el => el.remove());
  }

  function __spDrawAfterOverlayForNode(nodeKey, node, counts) {
    if (!node || !counts) return;

    const x = Number(node.x || 0);
    const y = Number(node.y || 0);
    const w = Number(node.w || 120);
    const h = Number(node.h || 42);

    if (!x || !y) return;

    const g = __spSvg('g', {
      'data-sp-after-overlay': '1'
    });

    const baseY = y + h / 2 + 23;
    const baseX = x - 47;

    const bg = __spSvg('rect', {
      x: x - 62,
      y: baseY - 18,
      width: 124,
      height: 45,
      rx: 8,
      fill: 'rgba(31, 39, 39, 0.98)'
    });
    g.appendChild(bg);

    const items = [
      { label: 'pass', value: counts.pass, color: '#20c997', dx: 0 },
      { label: 'hold', value: counts.hold, color: '#f59e0b', dx: 35 },
      { label: 'fail', value: counts.fail, color: '#ef4444', dx: 70 }
    ];

    for (const item of items) {
      const cx = baseX + item.dx;

      g.appendChild(__spSvg('circle', {
        cx: cx,
        cy: baseY,
        r: 5,
        fill: item.color
      }));

      const text = __spSvg('text', {
        x: cx + 9,
        y: baseY + 4,
        fill: '#f8fafc',
        'font-size': '12',
        'font-weight': '800'
      });
      text.textContent = String(item.value);
      g.appendChild(text);
    }

    const totalDotX = baseX + 24;
    const totalY = baseY + 24;

    g.appendChild(__spSvg('circle', {
      cx: totalDotX,
      cy: totalY,
      r: 5,
      fill: '#ffffff'
    }));

    const totalText = __spSvg('text', {
      x: totalDotX + 9,
      y: totalY + 4,
      fill: '#f8fafc',
      'font-size': '12',
      'font-weight': '900'
    });
    totalText.textContent = String(counts.total);
    g.appendChild(totalText);

    const totalLabel = __spSvg('text', {
      x: totalDotX + 35,
      y: totalY + 4,
      fill: '#94a3b8',
      'font-size': '10',
      'font-weight': '700'
    });
    totalLabel.textContent = '전체';
    g.appendChild(totalLabel);

    try {
      pipeL.appendChild(g);
    } catch (e) {
      const svg = document.querySelector('svg');
      if (svg) svg.appendChild(g);
    }
  }

  let __spAfterApplied = false;

  function __spApplyAfterCounts() {
    if (__spAfterApplied) return;
    __spAfterApplied = true;

    __spRemoveOldOverlay();

    if (typeof PNODE !== 'object' || !PNODE) return;

    for (const [nodeKey, node] of Object.entries(PNODE)) {
      const nodeLabel = [
        nodeKey,
        node && node.name,
        node && node.label,
        node && node.title,
        node && node.text
      ].filter(Boolean).join(' ');

      const counts = __spBestCountsForNode(nodeKey, nodeLabel);

      if (!counts) continue;

      node.pass = counts.pass;
      node.fail = counts.fail;
      node.hold = counts.hold;
      node.total = counts.total;
      node.passed = counts.pass;
      node.failed = counts.fail;
      node.held = counts.hold;

      __spDrawAfterOverlayForNode(nodeKey, node, counts);
    }

    try {
      if (typeof st !== 'undefined') {
        st.textContent = '최종 판정 완료: 최종 집계 기준으로 수치가 갱신되었습니다.';
      }
    } catch (e) {}
  }

  window.__SP_APPLY_AFTER_COUNTS__ = __spApplyAfterCounts;

  document.addEventListener('sp:permit-after-applied', function() {
    __spApplyAfterCounts();
  });

  document.addEventListener('sp:permit-after-finalized', function() {
    __spApplyAfterCounts();
  });

} catch (e) {}

"""

    injection = injection.replace("__AFTER_GRAPH_JSON_TEXT__", safe_after_json_literal)

    markers = [
        "})();\n</script>",
        "})();\r\n</script>",
        "})();</script>",
    ]

    for marker in markers:
        if marker in html_text:
            return html_text.replace(marker, injection + marker)

    return html_text + f"<script>{injection}</script>"


def inject_end_navigation(html_text: str) -> str:
    """
    Deprecated compatibility hook.

    최종 판정 이동 버튼은 Streamlit 본문 하단에만 노출한다. iframe 내부에
    이동 링크를 다시 주입하면 버튼이 중복으로 보이고 parent navigation도
    브라우저 환경에 따라 막힐 수 있어서 여기서는 HTML을 그대로 반환한다.
    """
    return html_text


# ============================================================
# 최종 판정 페이지 렌더링
# ============================================================

def _clear_query_params() -> None:
    try:
        st.query_params.clear()
    except Exception:
        try:
            st.experimental_set_query_params()
        except Exception:
            pass


def _return_to_document_list() -> None:
    """Navigation must not regenerate an expired judgement before leaving."""
    gmail_keys = (
        "gmail_logged_in", "gmail_connected_address", "gmail_confirm_version", "gmail_address",
        "gmail_app_password", "gmail_subject_keyword", "gmail_since_date", "gmail_confirmed_address",
        "gmail_confirmed_app_password", "gmail_confirmed_subject_keyword", "gmail_confirmed_since_date",
        "gmail_confirmed_search",
    )
    gmail_state = {key: st.session_state.get(key) for key in gmail_keys}
    confirmed_address = str(gmail_state.get("gmail_confirmed_address") or gmail_state.get("gmail_connected_address") or gmail_state.get("gmail_address") or "").strip()
    confirmed_password = str(gmail_state.get("gmail_confirmed_app_password") or gmail_state.get("gmail_app_password") or "").strip()
    st.session_state["run_sim"] = False
    st.session_state["last_gmail_sync_at"] = datetime.now().timestamp()
    st.session_state["last_gmail_sync_error"] = ""
    st.session_state["last_gmail_background_error"] = ""
    st.session_state["gmail_settings_open"] = False
    for key, value in gmail_state.items():
        if value not in (None, ""):
            st.session_state[key] = value
    if confirmed_address and confirmed_password:
        st.session_state["gmail_logged_in"] = True
        st.session_state["gmail_connected_address"] = confirmed_address
        st.session_state["gmail_address"] = confirmed_address
        st.session_state["gmail_app_password"] = confirmed_password
        st.session_state["gmail_confirmed_address"] = confirmed_address
        st.session_state["gmail_confirmed_app_password"] = confirmed_password
        st.session_state["gmail_confirm_version"] = "manual-gmail-confirm-20260602-v2"
    _clear_query_params()
    st.rerun()


@st.fragment(run_every=1.0)
def _render_pending_final_review(job) -> None:
    if job.future.done():
        st.rerun()
    message, elapsed = job.snapshot()
    st.info(f"{message} · 경과 {int(elapsed)}초\n\n"
            "상세 판정 결과를 준비 중입니다. 첫 화면으로 돌아가도 작업은 유지되며, "
            "같은 문서의 요청은 진행 중인 작업에 연결됩니다.")


def render_final_judgement_page(
    *,
    selected_doc: Any,
    original_csv_dir: Path | None,
) -> None:
    # Keep the existing button/position, but handle it before any PDF/API work.
    if st.button("← 첫 화면으로 돌아가기"):
        _return_to_document_list()
        return

    if selected_doc is None:
        st.error("최종 판정에 사용할 SP 문서를 찾지 못했습니다.")
        return

    pdf_path = Path(selected_doc.path)

    permit_paths = [
        Path(path)
        for path in getattr(selected_doc, "permit_files", ()) or []
    ]

    progress_slot = st.empty()
    progress_slot.markdown(
        """
        <div style="margin:1rem 0;padding:1rem 1.15rem;border:1px solid #bfdbfe;border-radius:14px;
                    background:#eff6ff;color:#174e9b;font-weight:850;">
            최종 판정 데이터를 생성하고 있습니다. 문서 기준과 참고 근거를 대조하는 중이라 문서 크기에 따라 시간이 걸릴 수 있습니다.
        </div>
        """,
        unsafe_allow_html=True,
    )

    try:
        artifacts = ensure_judgement_artifacts(
            pdf_path=pdf_path,
            permit_paths=permit_paths,
            original_csv_dir=Path(original_csv_dir) if original_csv_dir else None,
            company=str(getattr(selected_doc, "company", "") or ""),
            product=str(getattr(selected_doc, "product", "") or ""),
            product_mapping_id=str(
                getattr(selected_doc, "product_mapping_id", "") or ""
            ),
            _defer_pending=True,
        )
    except JudgementPending as pending:
        progress_slot.empty()
        _render_pending_final_review(pending.job)
        return
    except Exception as exc:
        progress_slot.empty()
        st.error(f"판정 생성에 실패했습니다. 이전 결과로 대체하지 않습니다: {exc}")
        return
    progress_slot.empty()

    if _has_runtime_llm_failure(artifacts):
        st.error("CLOVA 호출 실패로 최종 판정이 완료되지 않았습니다. 연결 및 API 설정을 확인한 뒤 첫 화면의 '검수 진행'으로 재시도하세요. 실패 기록은 보존됩니다.")
        return

    result = artifacts["after_result"]

    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] {
            display: none !important;
        }

        [data-testid="collapsedControl"] {
            display: none !important;
        }

        .block-container {
            padding-top: 1.05rem;
            padding-left: 2.2rem;
            padding-right: 2.2rem;
            max-width: 100% !important;
        }
        .final-report-layout { display:grid; grid-template-columns:minmax(560px,1.25fr) minmax(420px,.75fr); gap:26px; align-items:start; margin:.3rem 0 1.2rem; }
        .final-page-frame { padding:18px; border:1px solid #d8e4f1; border-radius:22px; background:#f8fafc; box-shadow:0 12px 30px rgba(15,23,42,.08); }
        .final-page-frame-title { margin:0 0 14px; font-size:26px; font-weight:950; color:#111827; }
        .final-page-frame img { display:block; width:100%; max-width:980px; margin:0 auto; border-radius:14px; border:1px solid #e5e7eb; background:#fff; box-shadow:0 10px 28px rgba(15,23,42,.08); }
        .final-side-card { position:sticky; top:16px; padding:22px; border:1px solid #d8e4f1; border-radius:22px; background:#ffffff; box-shadow:0 12px 30px rgba(15,23,42,.08); }
        .final-kicker { font-size:.9rem; font-weight:950; letter-spacing:.08em; color:#2563eb; }
        .final-doc-title { margin:.35rem 0 .35rem; color:#111827; font-size:32px; line-height:1.25; font-weight:950; }
        .final-doc-sub { margin:0 0 18px; color:#536173; font-size:18px; line-height:1.55; overflow-wrap:anywhere; }
        .final-metrics { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.65rem; margin:0 0 18px; }
        .final-metric { min-width:0; padding:.8rem .9rem; border:1px solid #dbeafe; border-radius:13px; background:#f8fbff; }
        .final-metric span { display:block; font-size:15px; color:#64748b; font-weight:850; }
        .final-metric b { display:block; margin-top:.25rem; font-size:18px; color:#172554; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .final-metric.pass b { color:#15803d; } .final-metric.fail b { color:#b91c1c; } .final-metric.hold b { color:#b45309; }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.final-preview-marker), div[data-testid="stVerticalBlockBorderWrapper"]:has(.final-summary-marker) { border-color:#d8e4f1 !important; border-radius:16px !important; background:#fff !important; box-shadow:0 10px 26px rgba(15,23,42,.07) !important; }
        .final-panel-title { margin:0 0 .8rem; font-size:1.02rem; font-weight:900; color:#172554; }
        .final-permit-status { margin-top:.85rem; padding:.7rem .8rem; border-radius:9px; font-size:.82rem; font-weight:750; line-height:1.4; }
        .final-permit-status.applied { color:#14734b; background:#effdf6; border:1px solid #a7efd1; }
        .final-permit-status.missing { color:#9a5b08; background:#fff8e8; border:1px solid #f7d79c; }
        @media (max-width: 1180px) { .final-report-layout { grid-template-columns:1fr; } .final-side-card { position:relative; top:auto; } }
        </style>
        """,
        unsafe_allow_html=True,
    )



    doc_company = str(getattr(selected_doc, "company", "") or "제출 문서")
    doc_product = str(getattr(selected_doc, "product", "") or "제품 정보 없음")
    doc_version = str(getattr(selected_doc, "version", "버전 정보 없음") or "버전 정보 없음")
    received_date = str(getattr(selected_doc, "received_date", "접수일 정보 없음") or "접수일 정보 없음")
    product_number = str(getattr(selected_doc, "product_number", "식별번호 없음") or "식별번호 없음")
    permit_state = f"허가서 {len(permit_paths)}건 연계" if permit_paths else "허가서 미연계"

    # 판정 결과 총합을 CSV 단일 출처에서 읽어 Gmail·시뮬레이션 섹션과 일치시킨다.
    _summary_after_path = Path(artifacts.get("summary_after_path") or "")
    if _summary_after_path.exists():
        _csv_rows = _read_summary_rows_by_position(_summary_after_path)
        _t = _overall_from_rows(_csv_rows)
        display_summary: Summary = Summary(
            passed=_t["pass"],
            failed=_t["fail"],
            held=_t["hold"],
            total=_t["total"],
            comparable_total=_t["total"],
        )
    else:
        display_summary = result.summary if result.metadata.get("rule_fingerprint") else summarize_stage_info_cards(result)
    preview_html = """
      <div class="final-page-sheet">
        <div class="final-page-missing">
          미리보기 이미지를 생성하지 못했습니다.
        </div>
      </div>
    """
    preview_image_path = getattr(result, "preview_image_path", None)

    if preview_image_path:
        import base64

        preview_b64 = base64.b64encode(Path(preview_image_path).read_bytes()).decode("utf-8")
        preview_html = f"""
          <div class="final-page-sheet">
            <img src="data:image/png;base64,{preview_b64}" />
          </div>
        """

    permit_status_html = (
        f'<div class="final-permit-status applied">허가서 PDF 적용됨 · {html.escape(", ".join(path.name for path in permit_paths))}</div>'
        if permit_paths
        else '<div class="final-permit-status missing">허가서 PDF 미적용</div>'
    )

    # 제조요약도 기준 CSV 다운로드 버튼과 내부 판정 호출 통계는 최종 판정 화면에 표시하지 않는다.

    combined_parts: list[str] = []
    combined_parts.append(
        f"""
        <style>
          html {{ scroll-behavior:smooth; }}
          body {{ margin:0; background:#ffffff; color:#111827; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Malgun Gothic',sans-serif; }}
          .final-page-stage {{
            --final-side-width: 520px;
            --final-side-gap: 28px;
            width:100%;
            max-width:1860px;
            margin:0 auto 32px;
            padding:0 calc(var(--final-side-width) + var(--final-side-gap) + 14px) 0 12px;
            box-sizing:border-box;
          }}
          .final-page-canvas {{
            position:relative;
            width:min(980px,100%);
            margin:0 auto;
            overflow:visible;
          }}
          .final-page-sheet {{
            background:#ffffff;
            border:1px solid #d8e4f1;
            border-radius:18px;
            box-shadow:0 18px 46px rgba(15,23,42,.12);
            padding:18px;
          }}
          .final-page-sheet img {{
            display:block;
            width:100%;
            height:auto;
            border-radius:12px;
            border:1px solid #e5e7eb;
            background:#fff;
          }}
          .final-page-missing {{
            padding:22px;
            border:1px dashed #cbd5e1;
            border-radius:14px;
            color:#64748b;
            font-size:18px;
            background:#fff;
          }}
          .final-page-summary-card {{
            position:absolute;
            left:calc(100% + var(--final-side-gap));
            top:42px;
            width:var(--final-side-width);
            box-sizing:border-box;
            padding:34px 38px 36px;
            border:1px solid #d8e4f1;
            border-left:11px solid #2563eb;
            border-radius:28px;
            background:rgba(255,255,255,.98);
            box-shadow:0 24px 54px rgba(15,23,42,.20);
          }}
          .final-card-kicker {{
            color:#2563eb;
            font-size:17px;
            font-weight:950;
            letter-spacing:.12em;
            text-transform:uppercase;
          }}
          .final-card-title {{
            margin:14px 0 9px;
            color:#111827;
            font-size:46px;
            line-height:1.25;
            font-weight:950;
          }}
          .final-card-sub {{
            margin:0 0 25px;
            color:#64748b;
            font-size:23px;
            line-height:1.45;
            font-weight:850;
            overflow-wrap:anywhere;
          }}
          .final-card-counts {{
            display:grid;
            grid-template-columns:repeat(3,minmax(0,1fr));
            gap:14px;
            margin:18px 0 16px;
          }}
          .final-card-pill {{
            display:flex;
            justify-content:center;
            align-items:center;
            border-radius:999px;
            padding:14px 0;
            font-size:22px;
            font-weight:950;
            white-space:nowrap;
          }}
          .final-card-pill.pass {{ color:#15803d; background:#dcfce7; }}
          .final-card-pill.fail {{ color:#b91c1c; background:#fee2e2; }}
          .final-card-pill.hold {{ color:#b45309; background:#fef3c7; }}
          .final-card-total {{
            margin-top:16px;
            border-radius:999px;
            background:#e5e7eb;
            color:#111827;
            text-align:center;
            padding:16px 0;
            font-size:29px;
            font-weight:950;
          }}
          .final-card-meta {{
            display:grid;
            gap:15px;
            margin-top:26px;
            padding-top:25px;
            border-top:1px solid #e5e7eb;
          }}
          .final-card-meta div {{
            display:flex;
            justify-content:space-between;
            gap:18px;
            font-size:22px;
            line-height:1.4;
          }}
          .final-card-meta span {{ color:#64748b; font-weight:850; }}
          .final-card-meta b {{ color:#172554; font-weight:950; text-align:right; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
          .final-permit-status {{ margin-top:26px; padding:20px 22px; border-radius:18px; font-size:20px; font-weight:900; line-height:1.55; }}
          .final-permit-status.applied {{ color:#14734b; background:#effdf6; border:1px solid #a7efd1; }}
          .final-permit-status.missing {{ color:#9a5b08; background:#fff8e8; border:1px solid #f7d79c; }}
          @media (max-width:1180px) {{
            .final-page-stage {{ padding:0 8px; }}
            .final-page-summary-card {{ position:relative; left:auto; top:auto; width:100%; margin:14px 0 0; }}
          }}
        </style>
        <section class="final-page-stage">
          <div class="final-page-canvas">
            {preview_html}
            <aside class="final-page-summary-card">
              <div class="final-card-kicker">AI DOCUMENT REVIEW</div>
              <div class="final-card-title">전체 요약</div>
              <p class="final-card-sub">{html.escape(doc_company)} · {html.escape(doc_product)}</p>
              <div class="final-card-counts">
                <span class="final-card-pill pass">충족 {display_summary.passed}</span>
                <span class="final-card-pill fail">불충족 {display_summary.failed}</span>
                <span class="final-card-pill hold">보류 {display_summary.held}</span>
              </div>
              <div class="final-card-total">총 {display_summary.total}개</div>
              <div class="final-card-meta">
                <div><span>제출 버전</span><b>{html.escape(doc_version)}</b></div>
                <div><span>접수일</span><b>{html.escape(received_date)}</b></div>
                <div><span>허가서</span><b>{html.escape(permit_state)}</b></div>
                <div><span>제품/제조번호</span><b>{html.escape(product_number)}</b></div>
              </div>
              {permit_status_html}
            </aside>
          </div>
        </section>
        """
    )

    combined_parts.append(render_review_context(result))
    general_info_html = _render_section1_pdf_pages(result)
    if general_info_html:
        combined_parts.append(general_info_html)

    if getattr(result, "manufacturing_summary_image_paths", None):
        combined_parts.append(
            """
            <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                        font-size:22px;font-weight:900;color:#111827;margin:8px 0 14px 0;">
                제조요약도
            </div>
            """
        )
        combined_parts.append(
            render_manufacturing_summary_with_stage_cards(
                result,
                summary_counts_path=artifacts.get("summary_after_path"),
            )
        )

    structural_validation_html = _render_structural_validation_cards(result)
    if structural_validation_html:
        combined_parts.append(structural_validation_html)

    numeric_precision_html = _render_structural_validation_cards(
        result,
        record_type="numeric_precision_validation",
        section_title="계산 및 수치 정합성 확인 결과",
        fallback_title="수치 정합성 검증",
    )
    if numeric_precision_html:
        combined_parts.append(numeric_precision_html)

    temporal_sequence_html = _render_structural_validation_cards(
        result,
        record_type="temporal_sequence_validation",
        section_title="공정 및 시간적 순서 확인 결과",
        fallback_title="시간적 순서 검증",
    )
    if temporal_sequence_html:
        combined_parts.append(temporal_sequence_html)

    combined_parts.append(
        """
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:26px 0 18px 0;" />
        <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                    font-size:22px;font-weight:900;color:#111827;margin:8px 0 14px 0;">
            개별 시험 판정 결과
        </div>
        """
    )

    combined_parts.append(render_result_html(result))

    components.html(
        "\n".join(combined_parts),
        height=FINAL_JUDGEMENT_VIEWPORT_HEIGHT,
        scrolling=True,
    )
