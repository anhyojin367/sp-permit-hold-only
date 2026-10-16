# -*- coding: utf-8 -*-
from __future__ import annotations

from sp_variant_runtime import bootstrap_runtime
bootstrap_runtime()

import os
import base64
import csv
import hashlib
import json
import mimetypes
import re
import shutil
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, unquote

import streamlit as st
import streamlit.components.v1 as components

from sp_gmail_ingest import DEFAULT_STORE_DIR, GmailConfig, download_gmail_sp_pdfs
from sp_company_logos import find_company_logo
from sp_document_upload import list_uploaded_sps, render_upload_dialog
from sp_document_models import InboxDocument, ReferenceDocument
from sp_pdf_judger.domain_details import resolve_domain_detail_profile
from sp_pdf_judger.policy_engine import policy_fingerprint
from sp_pdf_judger.extraction_runtime import extraction_fingerprint
from sp_pdf_judger.llm import llm_execution_fingerprint
from sp_pdf_judger.permit_catalog import (
    DONGKOOK_UNKNOWN_SKY_M_MAPPING_ID,
    resolve_submission_permits,
)
from sp_pdf_judger.review_presentation import COUNT_HELP
from sp_simulation_jobs import get_simulation_job, forget_failed_simulation
from sp_flowchart_data import simulation_structure_notice
from sp_sim2_viewer import build_simulation_html as build_sim2_html
from sp_judgement_versions import JUDGEMENT_CACHE_VERSION
STORE_DIR = Path(os.getenv("SP_PDF_DIR", str(DEFAULT_STORE_DIR)))
STATIC_PDF_DIR = Path("static") / "pdf_view"
SYNC_INTERVAL_SECONDS = int(os.getenv("SP_GMAIL_SYNC_SECONDS", "120"))
DEFAULT_SUBJECT_KEYWORD = "[식약처]"
ALL_PRODUCT_LABEL = "전체 제품"
TARGET_REFERENCE_VERSION = "4.0"
INBOX_DOCUMENT_LIST_HEIGHT = 610
REFERENCE_DOCUMENT_LIST_HEIGHT = 455
DEFAULT_GMAIL_SINCE = date(2026, 1, 1)
GMAIL_CONFIRMATION_VERSION = "manual-gmail-confirm-20260602-v2"
JUDGEMENT_STATUS_DIR = Path(__file__).resolve().parent / ".sp_judgement_status"
JUDGEMENT_STATUS_INDEX = JUDGEMENT_STATUS_DIR / "status_index.json"
APP_CACHE_VERSION = "sp-ui-cache-v112-readable-evidence"
JUDGEMENT_STATUS_CACHE_VERSION = JUDGEMENT_CACHE_VERSION


def _ensure_current_cache_version() -> None:
    session_key = "_sp_app_cache_version"
    previous_version = st.session_state.get(session_key)
    if previous_version == APP_CACHE_VERSION:
        return

    for key in list(st.session_state.keys()):
        if str(key).startswith("sp_direct_judgement_artifacts::"):
            st.session_state.pop(key, None)
    st.session_state.pop("latest_judgement_artifact_key", None)
    try:
        # Cache data is shared across sessions. Opening a final-page link or a
        # new tab must not discard already parsed PDFs for every browser.
        # Existing sessions still migrate on a real application version change;
        # each cached loader also retains its file/version signature checks.
        if previous_version is not None:
            st.cache_data.clear()
    except Exception:
        pass
    st.session_state[session_key] = APP_CACHE_VERSION


def _load_judgement_bridge():
    from sp_judgement_bridge import (
        _has_runtime_llm_failure,
        attach_after_summary_targets,
        ensure_judgement_artifacts,
        get_judgement_job,
        forget_failed_judgement_attempt,
        patch_graph_json_with_summary,
        render_final_judgement_page,
    )

    return {
        "has_runtime_llm_failure": _has_runtime_llm_failure,
        "attach_after_summary_targets": attach_after_summary_targets,
        "ensure_judgement_artifacts": ensure_judgement_artifacts,
        "get_judgement_job": get_judgement_job,
        "forget_failed_judgement_attempt": forget_failed_judgement_attempt,
        "patch_graph_json_with_summary": patch_graph_json_with_summary,
        "render_final_judgement_page": render_final_judgement_page,
    }


COMPANY_LOGO_DOMAINS: dict[str, str] = {
    "(주)한국백신": "koreavaccine.com",
    "LG화학": "lgchem.com",
    "일양약품": "ilyang.co.kr",
    "(주)유바이오로직스": "eubiologics.com",
    "파마리서치바이오": "pharmaresearchbio.com",
    "SK바이오사이언스": "skbioscience.com",
    "SK플라즈마": "skplasma.com",
    "GC녹십자": "gcbiopharma.com",
    "(주)녹십자": "gcbiopharma.com",
    "동국바이오사이언스": "dongkookpharm.co.kr",
    "동국화학": "dongkookpharm.co.kr",
    "동국플라즈마": "dongkookpharm.co.kr",
    "동국약품 주식회사": "dongkookpharm.co.kr",
}

ORG_LOGO_DOMAINS: dict[str, str] = {
    "식약처": "mfds.go.kr",
    "동국대": "dongguk.edu",
}

ORG_LOGO_FILES: dict[str, str] = {
    "동국대": "dongguk_university_trim.png",
    "식약처": "mfds_trim.png",
}


REFERENCE_LIBRARY: tuple[ReferenceDocument, ...] = (
    ReferenceDocument(
        "동국화학",
        "동국알부민20%주",
        "사람혈청알부민 제조 및 품질관리요약서 기준 SP",
        "MFDS-HSA-DKCHEM",
        "v8.0",
        "2026.04.03",
        "적용중",
        21,
        103,
        "바이오의약품품질과",
    ),
    ReferenceDocument(
        "동국플라즈마",
        "동국알부민20%주",
        "사람혈청알부민 제조 및 품질관리요약서 기준 SP",
        "MFDS-HSA-STD",
        "v8.0",
        "2026.04.03",
        "적용중",
        42,
        118,
        "바이오의약품품질과",
    ),
    ReferenceDocument(
        "동국플라즈마",
        "동국알부민20%주",
        "사람혈청알부민 제조 및 품질관리요약서 기준 SP",
        "MFDS-HSA-STD",
        "v7.2",
        "2025.11.15",
        "이전버전",
        39,
        104,
        "바이오의약품품질과",
    ),
    ReferenceDocument(
        "동국플라즈마",
        "동국알부민20%주",
        "사람혈청알부민 제조 및 품질관리요약서 기준 SP",
        "MFDS-HSA-STD",
        "v6.5",
        "2025.06.01",
        "보관",
        37,
        96,
        "바이오의약품품질과",
    ),
    ReferenceDocument(
        "SK바이오사이언스",
        "스카이코비원멀티주",
        "재조합단백질 백신 제조 및 품질관리요약서 기준 SP",
        "MFDS-COV-REC",
        "v8.0",
        "2026.05.10",
        "적용중",
        56,
        142,
        "백신검정과",
    ),
    ReferenceDocument(
        "동국바이오사이언스",
        "스카이코비원멀티주",
        "스카이코비원멀티주 제조 및 품질관리요약서 기준 SP",
        "MFDS-COV-DGBIO",
        "v8.0",
        "2026.05.10",
        "적용중",
        56,
        142,
        "백신검정과",
    ),
    ReferenceDocument(
        "SK바이오사이언스",
        "스카이코비원멀티주",
        "재조합단백질 백신 제조 및 품질관리요약서 기준 SP",
        "MFDS-COV-REC",
        "v7.1",
        "2025.12.20",
        "이전버전",
        52,
        128,
        "백신검정과",
    ),
    ReferenceDocument(
        "동국약품 주식회사",
        "일본뇌염백신",
        "불활화 일본뇌염 백신 제조 및 품질관리요약서 기준 SP",
        "MFDS-JEV-INACT",
        "v4.2",
        "2026.02.18",
        "적용중",
        48,
        131,
        "백신검정과",
    ),
    ReferenceDocument(
        "동국약품 주식회사",
        "일본뇌염백신",
        "불활화 일본뇌염 백신 제조 및 품질관리요약서 기준 SP",
        "MFDS-JEV-INACT",
        "v3.9",
        "2025.09.30",
        "이전버전",
        46,
        122,
        "백신검정과",
    ),
    ReferenceDocument(
        "(주)녹십자",
        "안티트롬빈III주 500아이유",
        "안티트롬빈III주 500아이유 제조 및 품질관리요약서 기준 SP",
        "GC-ATIII-500IU",
        "v3.0",
        "2022.11.04",
        "적용중",
        28,
        0,
        "혈액제제검정과",
    ),
    ReferenceDocument(
        "(주)녹십자",
        "지씨플루멀티주",
        "지씨플루멀티주 제조 및 품질관리요약서 기준 SP",
        "GC-FLU-MULTI",
        "v3.2",
        "2025.05.20",
        "적용중",
        20,
        0,
        "백신검정과",
    ),
    ReferenceDocument(
        "(주)녹십자",
        "녹십자-알부민주20%",
        "녹십자-알부민주20% 제조 및 품질관리요약서 기준 SP",
        "GC-HSA-20",
        "v4.0",
        "2024.03.27",
        "적용중",
        21,
        0,
        "혈액제제검정과",
    ),
    ReferenceDocument(
        "GC녹십자",
        "중증혈우병주",
        "혈액제제 제조 및 품질관리요약서 기준 SP",
        "MFDS-BLD-FVIII",
        "v2.3",
        "2026.01.12",
        "적용중",
        44,
        109,
        "혈액제제검정과",
    ),
    ReferenceDocument(
        "셀트리온",
        "렉키로나주",
        "단클론항체 제조 및 품질관리요약서 기준 SP",
        "MFDS-MAB-STD",
        "v5.0",
        "2026.03.04",
        "적용중",
        61,
        156,
        "첨단바이오품질과",
    ),
    ReferenceDocument(
        "대웅제약",
        "보툴리눔톡신",
        "독소제제 제조 및 품질관리요약서 기준 SP",
        "MFDS-TOX-BTX",
        "v3.1",
        "2026.04.22",
        "적용중",
        36,
        91,
        "생물제제검정과",
    ),
    ReferenceDocument(
        "한미약품 주식회사",
        "한미알부민주",
        "알부민 제제 제조 및 품질관리요약서 기준 SP",
        "MFDS-HSA-HM",
        "v1.5",
        "2026.02.01",
        "적용중",
        38,
        97,
        "혈액제제검정과",
    ),
)


EXPANDED_REFERENCE_LIBRARY: tuple[ReferenceDocument, ...] = (
    ReferenceDocument(
        "(주)한국백신",
        "코박스인플루4가PF주",
        "인플루엔자 백신 제조 및 품질관리요약서 기준 SP",
        "MFDS-KV-IV4PF",
        "v2.1",
        "2026.03.18",
        "적용중",
        51,
        126,
        "백신검정과",
    ),
    ReferenceDocument(
        "(주)한국백신",
        "코박스인플루4가PF주",
        "인플루엔자 백신 제조 및 품질관리요약서 기준 SP",
        "MFDS-KV-IV4PF",
        "v1.8",
        "2025.10.02",
        "이전버전",
        47,
        113,
        "백신검정과",
    ),
    ReferenceDocument(
        "LG화학",
        "유박스비주",
        "B형간염 백신 제조 및 품질관리요약서 기준 SP",
        "MFDS-LGC-HBV",
        "v5.3",
        "2026.04.25",
        "적용중",
        49,
        120,
        "백신검정과",
    ),
    ReferenceDocument(
        "LG화학",
        "유박스비주",
        "B형간염 백신 제조 및 품질관리요약서 기준 SP",
        "MFDS-LGC-HBV",
        "v5.0",
        "2025.12.06",
        "이전버전",
        46,
        111,
        "백신검정과",
    ),
    ReferenceDocument(
        "일양약품",
        "일양플루백신프리필드시린지주",
        "인플루엔자 백신 제조 및 품질관리요약서 기준 SP",
        "MFDS-IY-FLU-PFS",
        "v3.4",
        "2026.02.14",
        "적용중",
        43,
        108,
        "백신검정과",
    ),
    ReferenceDocument(
        "일양약품",
        "일양플루백신프리필드시린지주",
        "인플루엔자 백신 제조 및 품질관리요약서 기준 SP",
        "MFDS-IY-FLU-PFS",
        "v3.1",
        "2025.08.21",
        "이전버전",
        40,
        97,
        "백신검정과",
    ),
    ReferenceDocument(
        "(주)유바이오로직스",
        "유비콜-플러스",
        "경구용 콜레라 백신 제조 및 품질관리요약서 기준 SP",
        "MFDS-EB-OCV",
        "v4.0",
        "2026.05.02",
        "적용중",
        54,
        136,
        "백신검정과",
    ),
    ReferenceDocument(
        "(주)유바이오로직스",
        "유비콜-플러스",
        "경구용 콜레라 백신 제조 및 품질관리요약서 기준 SP",
        "MFDS-EB-OCV",
        "v3.6",
        "2025.11.18",
        "이전버전",
        50,
        124,
        "백신검정과",
    ),
    ReferenceDocument(
        "파마리서치바이오",
        "리엔톡스주",
        "보툴리눔 독소제제 제조 및 품질관리요약서 기준 SP",
        "MFDS-PRB-BTX",
        "v2.2",
        "2026.01.27",
        "적용중",
        39,
        92,
        "생물제제검정과",
    ),
    ReferenceDocument(
        "파마리서치바이오",
        "리엔톡스주",
        "보툴리눔 독소제제 제조 및 품질관리요약서 기준 SP",
        "MFDS-PRB-BTX",
        "v2.0",
        "2025.07.11",
        "이전버전",
        36,
        84,
        "생물제제검정과",
    ),
    ReferenceDocument(
        "SK바이오사이언스",
        "스카이셀플루4가",
        "세포배양 인플루엔자 백신 제조 및 품질관리요약서 기준 SP",
        "MFDS-SKB-CFLU",
        "v6.2",
        "2026.03.29",
        "적용중",
        53,
        135,
        "백신검정과",
    ),
    ReferenceDocument(
        "SK바이오사이언스",
        "스카이셀플루4가",
        "세포배양 인플루엔자 백신 제조 및 품질관리요약서 기준 SP",
        "MFDS-SKB-CFLU",
        "v5.8",
        "2025.09.08",
        "이전버전",
        49,
        121,
        "백신검정과",
    ),
    ReferenceDocument(
        "SK플라즈마",
        "에스케이알부민20%주",
        "혈장분획제제 제조 및 품질관리요약서 기준 SP",
        "MFDS-SKP-ALB20",
        "v4.1",
        "2026.04.06",
        "적용중",
        45,
        116,
        "혈액제제검정과",
    ),
    ReferenceDocument(
        "SK플라즈마",
        "에스케이알부민20%주",
        "혈장분획제제 제조 및 품질관리요약서 기준 SP",
        "MFDS-SKP-ALB20",
        "v3.7",
        "2025.10.17",
        "이전버전",
        41,
        101,
        "혈액제제검정과",
    ),
    ReferenceDocument(
        "GC녹십자",
        "그린진에프주",
        "혈액응고인자 제제 제조 및 품질관리요약서 기준 SP",
        "MFDS-GC-FVIII",
        "v2.8",
        "2026.03.07",
        "적용중",
        47,
        119,
        "혈액제제검정과",
    ),
    ReferenceDocument(
        "GC녹십자",
        "그린진에프주",
        "혈액응고인자 제제 제조 및 품질관리요약서 기준 SP",
        "MFDS-GC-FVIII",
        "v2.4",
        "2025.08.28",
        "이전버전",
        43,
        105,
        "혈액제제검정과",
    ),
)


st.set_page_config(
    page_title="SP 문서 AI 자동검토 시스템",
    page_icon="SP",
    layout="wide",
    initial_sidebar_state="collapsed",
)


def main() -> None:
    st.warning("HOLD-only 독립 버전: SP 자체 판정 후 보류 시험만 허가서로 재검토합니다. 이미 PASS/FAIL인 항목은 재검토하지 않으며, 허가서 기반 문서 전체 정합성 검사는 수행하지 않습니다(합격 처리 아님). 근거 부족 시 보류가 유지됩니다.")
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_current_cache_version()
    _init_gmail_state()

    view = ""
    try:
        value = st.query_params.get("view", "")
        if isinstance(value, list):
            view = str(value[0]) if value else ""
        else:
            view = str(value or "")
    except Exception:
        view = ""

    # 첫 화면은 한 화면 안에 맞추고, 긴 결과/문서 보기 화면은 스크롤을 허용한다.
    running_sim = bool(st.session_state.get("run_sim"))
    _inject_styles(fit_first_page=(view not in {"judge_final", "pdf_viewer"} and not running_sim))

    if view == "pdf_viewer":
        _render_pdf_viewer_page()
        return

    # 검수 시뮬레이션/최종 판정 진입 중에는 Gmail 자동 수신을 돌리지 않는다.
    # 버튼 클릭 직후 IMAP 확인이 같이 돌면서 화면이 멈춘 것처럼 보이는 현상을 방지한다.
    documents = _load_inbox_documents()
    selected_doc = _ensure_selected_document(documents)

    refs = _references_for_document(selected_doc)
    gate = _version_gate(selected_doc, refs)

    if view == "judge_final":
        _render_topbar(show_gmail=False)

        query_pdf = ""
        try:
            raw_query_pdf = st.query_params.get("pdf", "")
            if isinstance(raw_query_pdf, list):
                raw_query_pdf = raw_query_pdf[0] if raw_query_pdf else ""
            query_pdf = unquote(str(raw_query_pdf or "")).strip()
        except Exception:
            query_pdf = ""

        if query_pdf:
            query_pdf_path = Path(query_pdf)
            if query_pdf_path.exists():
                query_pdf_text = str(query_pdf_path)
                st.session_state["selected_pdf_path"] = query_pdf_text
                st.session_state["sim_pdf"] = query_pdf_text
                st.session_state["judge_pdf_path"] = query_pdf_text
                st.session_state["latest_judgement_pdf_path"] = query_pdf_text
                try:
                    selected_doc = _document_from_pdf(query_pdf_path)
                except Exception:
                    selected_doc = None

        if selected_doc is None:
            fallback_pdf = (
                query_pdf
                or st.session_state.get("judge_pdf_path")
                or st.session_state.get("latest_judgement_pdf_path")
                or st.session_state.get("sim_pdf")
                or st.session_state.get("selected_pdf_path")
            )
            if fallback_pdf:
                try:
                    selected_doc = _document_from_pdf(Path(str(fallback_pdf)))
                except Exception:
                    selected_doc = None

        original_csv_dir = None
        if selected_doc is not None:
            original_csv_dir = _find_simulation_csv_dir(selected_doc.product)

        try:
            bridge = _load_judgement_bridge()
        except ModuleNotFoundError as exc:
            st.error(f"최종 판정 모듈 실행에 필요한 패키지가 없습니다: {exc.name}")
            return

        bridge["render_final_judgement_page"](
            selected_doc=selected_doc,
            original_csv_dir=original_csv_dir,
        )
        return

    _render_topbar(show_gmail=not running_sim)

    # 검수 시뮬레이션 화면에서는 제출 문서함을 숨기고,
    # 플로우차트를 전체 폭으로 크게 보여준다.
    if running_sim:
        with st.container(border=True):
            _render_reference_panel(selected_doc, refs, gate)
        return

    left, right = st.columns([0.92, 1.55], gap="large")

    with left:
        with st.container(border=True):
            _render_inbox_panel(documents, selected_doc)

    with right:
        with st.container(border=True):
            _render_reference_panel(selected_doc, refs, gate)

    _maybe_poll_gmail()

SIMULATION_HEIGHT = 1120


def _simulation_finish_delay_seconds(graph_json: str, permit_enabled: bool) -> float:
    # Presentation only; never a substitute for actual completed artifacts.
    return 12.0


@st.fragment(run_every=1.0)
def _render_pending_review(job, *, rules_only=False):
    if job.future.done():
        st.rerun()
    message, elapsed = job.snapshot()
    if rules_only:
        message = f"SP 룰 전용 · 허가서 미검증 · {message}"
    st.markdown(
        f'<div class="sim-transition-notice">{_escape(message)}'
        f' · 경과 {int(elapsed)}초<br>실제 판정 처리 중입니다. 목록으로 이동해도 작업은 유지됩니다.</div>',
        unsafe_allow_html=True,
    )


@st.fragment(run_every=1.0)
def _render_pending_simulation(job):
    if job.future.done():
        st.rerun()
    message, elapsed = job.snapshot()
    st.markdown(
        f'<div class="sim-transition-notice">{_escape(message)}'
        f' · 경과 {int(elapsed)}초<br>시뮬레이션 준비 중입니다. 목록으로 이동해도 작업은 유지됩니다.</div>',
        unsafe_allow_html=True,
    )


def _render_inline_simulation(product: str, pdf_path: Path | None = None) -> float:
    """MFDS 라이브러리 섹터 안에서 검수 시뮬레이션을 렌더링한다."""
    csv_dir = _find_simulation_csv_dir(product)

    if csv_dir is None:
        st.warning("이 제품의 제조요약도 CSV 데이터를 찾을 수 없습니다.")
        return 0.0

    if pdf_path is None:
        st.warning("시뮬레이션에 사용할 제출 SP PDF를 찾지 못했습니다.")
        return 0.0

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        st.warning(f"시뮬레이션에 사용할 제출 SP PDF 파일이 존재하지 않습니다: {pdf_path}")
        return 0.0

    # 최종 판정 페이지 이동 시 문서 경로가 유실되지 않도록 고정 저장한다.
    pdf_path_text = str(pdf_path)
    st.session_state["sim_pdf"] = pdf_path_text
    st.session_state["selected_pdf_path"] = pdf_path_text
    st.session_state["judge_pdf_path"] = pdf_path_text
    st.session_state["latest_judgement_pdf_path"] = pdf_path_text

    try:
        selected_doc_for_judge = _document_from_pdf(pdf_path)
    except Exception as exc:
        st.error(f"제출 문서 정보를 읽지 못해 검수를 중단했습니다: {exc}")
        return 0.0
    if selected_doc_for_judge is None:
        st.error("제출 문서 정보를 확인할 수 없어 검수를 중단했습니다.")
        return 0.0
    if getattr(selected_doc_for_judge, "permit_link_error", ""):
        st.error(selected_doc_for_judge.permit_link_error)
        return 0.0

    permit_paths = []

    if selected_doc_for_judge is not None:
        permit_paths = [
            Path(path)
            for path in getattr(selected_doc_for_judge, "permit_files", ()) or []
        ]

    try:
        bridge = _load_judgement_bridge()
    except ModuleNotFoundError as exc:
        st.error(f"검수 모듈 실행에 필요한 패키지가 없습니다: {exc.name}")
        return 0.0

    artifacts = _cached_simulation_artifacts_for_doc(
        selected_doc_for_judge,
        pdf_path=pdf_path,
        permit_paths=permit_paths,
        csv_dir=Path(csv_dir),
    )

    # 카운트 단일 출처 원칙:
    # 시뮬레이션 상단 노드, 최종 전체 요약, 제조요약도 옆 박스가 모두
    # 동일한 summary_after.csv를 보도록 시뮬레이션 진입 시에도 판정 아티팩트를 먼저 확보한다.
    # 기존에는 최종 판정 페이지에서만 ensure_judgement_artifacts()를 실행했기 때문에
    # 시뮬레이션은 정적 CSV/이전 CSV를 보고, 최종 페이지는 새 summary_after.csv를 봐서 숫자가 어긋났다.
    if artifacts is None and selected_doc_for_judge is not None:
        try:
            job = bridge["get_judgement_job"](
                pdf_path=pdf_path,
                permit_paths=permit_paths,
                original_csv_dir=Path(csv_dir),
                company=str(getattr(selected_doc_for_judge, "company", "") or ""),
                product=str(getattr(selected_doc_for_judge, "product", "") or ""),
                product_mapping_id=str(
                    getattr(selected_doc_for_judge, "product_mapping_id", "") or ""
                ),
            )
            if not job.future.done():
                _render_pending_review(job, rules_only=not permit_paths)
                return -1.0
            artifacts = job.future.result()
        except Exception as exc:
            st.error(f"판정 생성에 실패하여 검수를 중단했습니다. 이전 결과로 대체하지 않습니다: {exc}")
            return 0.0

    if artifacts and bridge.get("has_runtime_llm_failure", lambda _: False)(artifacts):
        st.error("CLOVA 호출에 실패하여 검수가 완료되지 않았습니다. 연결 및 API 설정을 확인한 뒤 목록에서 '검수 진행'을 눌러 재시도하세요. 실패 기록은 보존됩니다.")
        return 0.0

    if not artifacts or not artifacts.get("summary_after_path") or not Path(artifacts["summary_after_path"]).is_file():
        st.error("현재 문서의 판정 요약이 없어 검수를 중단했습니다. 이전 결과로 대체하지 않습니다.")
        return 0.0

    runtime_csv_dir = Path(artifacts.get("runtime_csv_dir")) if artifacts and artifacts.get("runtime_csv_dir") else Path(csv_dir)

    # 판정 완료 후 생성된 summary_after.csv를 단일 출처로 사용한다.
    summary_after_path = Path(artifacts["summary_after_path"])
    try:
        graph_job = get_simulation_job(
            runtime_csv_dir, pdf_path, summary_after_path, version=APP_CACHE_VERSION,
        )
        if not graph_job.future.done():
            _render_pending_simulation(graph_job)
            return -1.0
        graph_json, product_name = graph_job.future.result()
    except Exception as exc:
        st.error(f"제조요약도 데이터를 읽을 수 없습니다: {exc}. 목록에서 검수 진행을 다시 눌러 주세요.")
        return 0.0

    final_graph_json = graph_json
    has_permit_pdf = bool(permit_paths)
    structure_notice = simulation_structure_notice(final_graph_json)
    if structure_notice:
        st.warning(structure_notice)

    html = build_sim2_html(
        final_graph_json,
        product_name or product,
        final_url=f"./?view=judge_final&pdf={quote(pdf_path_text, safe='')}",
        permit_enabled=has_permit_pdf,
    )

    components.html(html, height=SIMULATION_HEIGHT, scrolling=True)
    return _simulation_finish_delay_seconds(final_graph_json, has_permit_pdf)


def _cached_simulation_artifacts_for_doc(
    doc: InboxDocument | None,
    *,
    pdf_path: Path,
    permit_paths: list[Path],
    csv_dir: Path,
) -> dict | None:
    if doc is None or getattr(doc, "permit_link_error", ""):
        return None

    record = _status_record_for_doc(doc, _load_judgement_status_index())
    if not isinstance(record, dict) or record.get("status") != "completed":
        return None

    # An index entry is not permission to reuse an older PDF's final counts.
    try:
        stat = pdf_path.stat()
        signature = record.get("pdf_sig") or {}
        if (signature.get("size"), signature.get("mtime_ns")) != (stat.st_size, stat.st_mtime_ns):
            return None
    except OSError:
        return None

    detail_profile = resolve_domain_detail_profile(
        getattr(doc, "company", ""),
        getattr(doc, "product", ""),
    )
    if str(record.get("detail_fingerprint") or "") != detail_profile.fingerprint:
        return None

    before_path, after_path = _summary_paths_from_status_record(record)
    if before_path is None or after_path is None:
        return None

    return {
        "artifact_key": str(record.get("artifact_key", "") or _pdf_status_key(pdf_path)),
        "summary_before_path": before_path,
        "summary_after_path": after_path,
        "runtime_csv_dir": csv_dir,
        "pdf_path": pdf_path,
        "permit_paths": permit_paths,
        "reused_summary": True,
    }


def _summary_paths_from_status_record(record: dict) -> tuple[Path | None, Path | None]:
    before = Path(str(record.get("summary_before_path", "") or ""))
    after = Path(str(record.get("summary_after_path", "") or ""))
    if before.is_file() and after.is_file():
        return before, after

    artifact_key = str(record.get("artifact_key", "") or "").strip()
    if artifact_key:
        status_dir = JUDGEMENT_STATUS_DIR / artifact_key
        local_before = status_dir / "summary_before.csv"
        local_after = status_dir / "summary_after.csv"
        if local_before.is_file() and local_after.is_file():
            return local_before, local_after

    return None, None


def _decode_zip_unicode_name(value: str) -> str:
    """일부 압축/메일 저장 과정에서 생기는 #Uxxxx 형태의 한글 파일명을 복원한다."""
    def repl(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 16))
        except Exception:
            return match.group(0)

    return re.sub(r"#U([0-9A-Fa-f]{4})", repl, value or "")


def _simulation_dir_match_key(value: str) -> str:
    decoded = _decode_zip_unicode_name(value)
    decoded = decoded.replace("제조요약도", "").replace("csv", "")
    return re.sub(r"[\s_\-./()%·,\[\]]+", "", decoded.strip().lower())


def _find_simulation_csv_dir(product: str) -> Path | None:
    base = Path(__file__).parent
    csv_dirs = sorted([d for d in base.iterdir() if d.is_dir() and d.name.endswith("_csv")], key=lambda x: x.name)
    if not csv_dirs:
        return None

    product_key = _simulation_dir_match_key(product or "")
    if product_key:
        scored: list[tuple[int, Path]] = []
        for d in csv_dirs:
            dir_key = _simulation_dir_match_key(d.name)
            score = 0
            if dir_key == product_key:
                score = 100
            elif product_key and product_key in dir_key:
                score = 80
            elif dir_key and dir_key in product_key:
                score = 70

            # 제품명 전체가 폴더명과 완전히 같지 않아도, 스카이코비원/알부민처럼
            # 제품 핵심어가 포함되면 무작위 첫 번째 CSV 폴더로 떨어지지 않게 한다.
            for token in ("스카이코비원", "알부민", "일본뇌염", "코박스", "유박스", "유비콜"):
                token_key = _simulation_dir_match_key(token)
                if token_key and token_key in product_key and token_key in dir_key:
                    score = max(score, 60)

            if score:
                scored.append((score, d))

        if scored:
            return max(scored, key=lambda item: item[0])[1]

    # 매칭 실패 시에도 기존 동작은 유지하되, 정렬된 첫 번째 폴더를 사용해 재현성을 높인다.
    return csv_dirs[0]


def _maybe_poll_gmail() -> None:
    if st.session_state.get("gmail_settings_open", False):
        return

    config = _current_gmail_config()
    if config is None:
        return

    last_sync = st.session_state.get("last_gmail_sync_at", 0)
    if time.time() - float(last_sync or 0) < SYNC_INTERVAL_SECONDS:
        return

    result = _sync_gmail(config, show_result=False, show_spinner=False)
    if result.get("downloaded"):
        st.rerun()


def _render_topbar(*, show_gmail: bool = True) -> None:
    _init_gmail_state()
    if not show_gmail:
        st.session_state["gmail_settings_open"] = False
    logo_strip = "".join(
        _organization_logo_html(name, domain)
        for name, domain in ORG_LOGO_DOMAINS.items()
    )
    if show_gmail:
        brand_col, upload_col, gmail_col = st.columns([5.4, 1.1, 1.1], gap="large", vertical_alignment="center")
    else:
        brand_col = st.container()
        gmail_col = None

    with brand_col:
        st.markdown(
            f"""
            <div class="topbar-brand-block" data-app-version="{APP_CACHE_VERSION}">
              <div class="brand-wrap">
                <div class="agency-logo-strip">{logo_strip}</div>
                <div>
                  <div class="brand-title">SP 문서 AI 자동검토 시스템</div>
                </div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    if gmail_col is not None:
        with upload_col:
            if st.button("파일 업로드", key="manual-upload-toggle", use_container_width=True):
                render_upload_dialog(STORE_DIR, _load_inbox_documents(), _on_upload_saved)
        with gmail_col:
            _render_gmail_toggle()

    if show_gmail and st.session_state.get("gmail_settings_open", False):
        with st.container(border=True, key="gmail_floating_panel"):
            _render_gmail_settings_content(_gmail_connection_state())


def _render_gmail_toggle() -> None:
    state = _gmail_connection_state()
    label = "정보 연계 설정"
    icon = "⌃" if st.session_state.get("gmail_settings_open", False) else "⌄"
    if st.button(f"{label} {icon}", key="gmail-settings-toggle", use_container_width=True):
        st.session_state["gmail_settings_open"] = not st.session_state.get("gmail_settings_open", False)
        st.rerun()


def _render_gmail_settings_content(state: dict[str, str]) -> None:
    st.markdown(_gmail_status_html(state), unsafe_allow_html=True)
    if st.button("연결 해제", key="gmail-disconnect", use_container_width=True, disabled=state["state"] == "offline"):
        st.session_state["gmail_user_disconnected"] = True
        st.session_state["gmail_logged_in"] = False
        st.session_state["gmail_connected_address"] = ""
        st.session_state["gmail_app_password"] = ""
        st.session_state["gmail_confirm_version"] = ""
        st.session_state["gmail_confirmed_address"] = ""
        st.session_state["gmail_confirmed_app_password"] = ""
        st.session_state.pop("selected_pdf_path", None)
        st.session_state["last_gmail_sync_error"] = ""
        st.rerun()

    st.markdown(
        """
        <div class="setting-copy">
          sp문서와 허가서 정보가 있는 문서함에 연계합니다.
        </div>
        """,
        unsafe_allow_html=True,
    )

    address = st.text_input("Mail 주소", key="gmail_address", placeholder="example@gmail.com")
    password = st.text_input(
        "Mail 앱 비밀번호",
        key="gmail_app_password",
        type="password",
        placeholder="16자리 앱 비밀번호",
    )
    keyword = st.text_input("메일 제목 포함 문구", key="gmail_subject_keyword")
    since_date = st.date_input(
        "수신 시작일",
        key="gmail_since_date",
        min_value=date(2020, 1, 1),
        max_value=date(2035, 12, 31),
    )

    if st.button("Mail 확인", use_container_width=True):
        config = _build_gmail_config(address, password, keyword, since_date)
        if config is None:
            st.warning("Mail 주소와 16자리 앱 비밀번호를 입력해야 합니다.")
        else:
            result = _sync_gmail(config, show_result=False)
            if result.get("ok"):
                st.session_state["gmail_settings_open"] = False
                st.rerun()
            else:
                st.warning(result.get("error") or "Mail 확인 중 오류가 발생했습니다.")


def _render_gmail_panel() -> None:
    _init_gmail_state()
    state = _gmail_connection_state()
    status_col, btn_col = st.columns([6, 1], gap="small")
    with status_col:
        st.markdown(_gmail_status_html(state), unsafe_allow_html=True)
    with btn_col:
        if st.button("연결 해제", use_container_width=True, disabled=state["state"] == "offline"):
            st.session_state["gmail_user_disconnected"] = True
            st.session_state["gmail_logged_in"] = False
            st.session_state["gmail_connected_address"] = ""
            st.session_state["gmail_app_password"] = ""
            st.session_state["gmail_confirm_version"] = ""
            st.session_state["gmail_confirmed_address"] = ""
            st.session_state["gmail_confirmed_app_password"] = ""
            st.session_state.pop("selected_pdf_path", None)
            st.session_state["last_gmail_sync_error"] = ""
            st.rerun()

    expanded = state["state"] != "connected"

    with st.expander("Mail 로그인 및 자동 수신 설정", expanded=expanded):
        st.markdown(
            """
            <div class="setting-copy">
              sp문서와 허가서 정보가 있는 문서함에 연계합니다.
            </div>
            """,
            unsafe_allow_html=True,
        )

        col1, col2, col3, col4, col5 = st.columns([1.25, 1.25, .9, .85, .7], gap="medium")
        with col1:
            address = st.text_input("Mail 주소", key="gmail_address", placeholder="example@gmail.com")
        with col2:
            password = st.text_input(
                "Mail 앱 비밀번호",
                key="gmail_app_password",
                type="password",
                placeholder="16자리 앱 비밀번호",
            )
        with col3:
            keyword = st.text_input(
                "메일 제목 포함 문구",
                key="gmail_subject_keyword",
            )
        with col4:
            since_date = st.date_input(
                "수신 시작일",
                key="gmail_since_date",
                min_value=date(2020, 1, 1),
                max_value=date(2035, 12, 31),
            )
        with col5:
            st.markdown("<div class='button-spacer'></div>", unsafe_allow_html=True)
            if st.button("Mail 확인", use_container_width=True):
                config = _build_gmail_config(address, password, keyword, since_date)
                if config is None:
                    st.warning("Mail 주소와 16자리 앱 비밀번호를 입력해야 합니다.")
                else:
                    _sync_gmail(config, show_result=True)
        status = _last_sync_status()
        current_store_dir = _current_gmail_store_dir() or STORE_DIR
        st.markdown(
            f"""
            <div class="gmail-meta">
              <span>자동 확인 주기: {SYNC_INTERVAL_SECONDS}초</span>
              <span>마지막 확인: {status}</span>
              <span>수신 기준일: {_gmail_since_date().strftime('%Y.%m.%d')} 이후</span>
              <span>저장 폴더: {current_store_dir}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )


def _local_gmail_defaults() -> dict[str, object]:
    # Never consume Streamlit's user-global secrets in this standalone copy.
    gmail = {}
    if not gmail:
        secrets_path = Path(__file__).resolve().parent / ".streamlit" / "secrets.toml"
        if secrets_path.exists():
            current_section = ""
            parsed: dict[str, str] = {}
            try:
                for raw_line in secrets_path.read_text(encoding="utf-8-sig").splitlines():
                    line = raw_line.strip().lstrip("\ufeff")
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("[") and line.endswith("]"):
                        current_section = line.strip("[]").strip()
                        continue
                    if current_section == "gmail" and "=" in line:
                        key, value = line.split("=", 1)
                        parsed[key.strip()] = value.strip().strip('"').strip("'")
            except Exception:
                parsed = {}
            gmail = parsed
    address = str(gmail.get("address", "") or "").strip()
    password = str(gmail.get("app_password", "") or "").strip()
    keyword = str(gmail.get("subject_keyword", DEFAULT_SUBJECT_KEYWORD) or DEFAULT_SUBJECT_KEYWORD).strip()
    since_raw = gmail.get("since_date", DEFAULT_GMAIL_SINCE)
    since_date = DEFAULT_GMAIL_SINCE
    if isinstance(since_raw, datetime):
        since_date = since_raw.date()
    elif isinstance(since_raw, date):
        since_date = since_raw
    else:
        try:
            since_date = date.fromisoformat(str(since_raw))
        except ValueError:
            since_date = DEFAULT_GMAIL_SINCE
    return {
        "address": address,
        "app_password": password.replace(" ", ""),
        "subject_keyword": keyword or DEFAULT_SUBJECT_KEYWORD,
        "since_date": since_date,
        "search": _imap_since_term(since_date),
    }


def _init_gmail_state() -> None:
    defaults = _local_gmail_defaults()
    default_address = str(defaults.get("address", "") or "")
    default_password = str(defaults.get("app_password", "") or "")
    default_keyword = str(defaults.get("subject_keyword", DEFAULT_SUBJECT_KEYWORD) or DEFAULT_SUBJECT_KEYWORD)
    default_since = defaults.get("since_date") if isinstance(defaults.get("since_date"), date) else DEFAULT_GMAIL_SINCE
    default_search = str(defaults.get("search", "") or _imap_since_term(default_since))

    st.session_state.setdefault("gmail_user_disconnected", False)
    st.session_state.setdefault("gmail_address", default_address)
    st.session_state.setdefault("gmail_app_password", default_password)
    st.session_state.setdefault("gmail_subject_keyword", default_keyword)
    st.session_state.setdefault("gmail_since_date", default_since)
    st.session_state.setdefault("gmail_logged_in", False)
    st.session_state.setdefault("gmail_connected_address", "")
    st.session_state.setdefault("gmail_confirm_version", "")
    st.session_state.setdefault("gmail_confirmed_address", default_address)
    st.session_state.setdefault("gmail_confirmed_app_password", default_password)
    st.session_state.setdefault("gmail_confirmed_subject_keyword", default_keyword)
    st.session_state.setdefault("gmail_confirmed_since_date", default_since)
    st.session_state.setdefault("gmail_confirmed_search", default_search)
    st.session_state.setdefault("gmail_settings_open", False)
    if default_address and default_password:
        st.session_state["gmail_user_disconnected"] = False
        st.session_state["gmail_address"] = default_address
        st.session_state["gmail_app_password"] = default_password
        st.session_state["gmail_subject_keyword"] = default_keyword
        st.session_state["gmail_since_date"] = default_since
        st.session_state["gmail_logged_in"] = True
        st.session_state["gmail_connected_address"] = default_address
        st.session_state["gmail_confirm_version"] = GMAIL_CONFIRMATION_VERSION
        st.session_state["gmail_confirmed_address"] = default_address
        st.session_state["gmail_confirmed_app_password"] = default_password
        st.session_state["gmail_confirmed_subject_keyword"] = default_keyword
        st.session_state["gmail_confirmed_since_date"] = default_since
        st.session_state["gmail_confirmed_search"] = default_search


def _gmail_connection_state() -> dict[str, str]:
    error = str(st.session_state.get("last_gmail_sync_error", "") or "").strip()
    address = (
        st.session_state.get("gmail_connected_address")
        or st.session_state.get("gmail_address", "")
    ).strip()
    if _gmail_session_confirmed():
        return {
            "state": "connected",
            "title": "Mail 연결됨",
            "detail": "",
        }
    if error:
        return {
            "state": "error",
            "title": "Mail 연결 확인 필요",
            "detail": error,
        }
    if address or st.session_state.get("gmail_app_password"):
        return {
            "state": "pending",
            "title": "Mail 연결 확인 필요",
            "detail": "Mail 확인 버튼으로 연결 상태를 확인하세요.",
        }
    return {
        "state": "offline",
        "title": "Mail 연결 안 됨",
        "detail": "Mail 주소와 앱 비밀번호를 입력해야 자동 수신이 시작됩니다.",
    }


def _gmail_status_html(state: dict[str, str]) -> str:
    state_name = state.get("state", "offline")
    return f"""
    <div class="gmail-status {state_name}">
      <span class="gmail-status-dot"></span>
      <b>{_escape(state.get("title", ""))}</b>
      <span>{_escape(state.get("detail", ""))}</span>
    </div>
    """


def _gmail_session_confirmed() -> bool:
    return (
        bool(st.session_state.get("gmail_logged_in"))
        and st.session_state.get("gmail_confirm_version") == GMAIL_CONFIRMATION_VERSION
        and bool(str(st.session_state.get("gmail_confirmed_address", "")).strip())
    )


def _gmail_since_date() -> date:
    raw_value = st.session_state.get("gmail_since_date", "")
    if isinstance(raw_value, datetime):
        return raw_value.date()
    if isinstance(raw_value, date):
        return raw_value
    raw = str(raw_value or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return DEFAULT_GMAIL_SINCE


def _gmail_confirmed_since_date() -> date:
    raw_value = st.session_state.get("gmail_confirmed_since_date", DEFAULT_GMAIL_SINCE)
    if isinstance(raw_value, datetime):
        return raw_value.date()
    if isinstance(raw_value, date):
        return raw_value
    raw = str(raw_value or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return DEFAULT_GMAIL_SINCE


def _imap_since_term(value: date) -> str:
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"SINCE {value.day:02d}-{months[value.month - 1]}-{value.year}"


def _save_gmail_state(address: str, password: str, keyword: str, since_date: date | None = None) -> None:
    st.session_state["gmail_confirmed_address"] = address.strip()
    st.session_state["gmail_confirmed_app_password"] = password.strip()
    st.session_state["gmail_confirmed_subject_keyword"] = keyword.strip() or DEFAULT_SUBJECT_KEYWORD
    if since_date is not None:
        st.session_state["gmail_confirmed_since_date"] = since_date
        st.session_state["gmail_confirmed_search"] = _imap_since_term(since_date)


def _build_gmail_config(
    address: str,
    password: str,
    keyword: str,
    since_date: date | None = None,
) -> GmailConfig | None:
    address = address.strip()
    password = password.strip().replace(" ", "")
    keyword = keyword.strip() or DEFAULT_SUBJECT_KEYWORD
    if not address or not password:
        return None
    since_date = since_date or _gmail_since_date()
    return GmailConfig(
        address=address,
        app_password=password,
        mailbox=os.getenv("GMAIL_MAILBOX", "INBOX").strip() or "INBOX",
        search=_imap_since_term(since_date),
        subject_keyword=keyword,
    )


def _current_gmail_config() -> GmailConfig | None:
    if not _gmail_session_confirmed():
        return None
    address = str(st.session_state.get("gmail_confirmed_address", "")).strip()
    password = str(st.session_state.get("gmail_confirmed_app_password", "")).strip()
    keyword = str(st.session_state.get("gmail_confirmed_subject_keyword", DEFAULT_SUBJECT_KEYWORD)).strip() or DEFAULT_SUBJECT_KEYWORD
    search = str(st.session_state.get("gmail_confirmed_search", "")).strip() or _imap_since_term(_gmail_confirmed_since_date())
    if not address or not password:
        return None
    return GmailConfig(
        address=address,
        app_password=password.replace(" ", ""),
        mailbox=os.getenv("GMAIL_MAILBOX", "INBOX").strip() or "INBOX",
        search=search,
        subject_keyword=keyword,
    )


def _sync_gmail(config: GmailConfig, *, show_result: bool, show_spinner: bool = False) -> dict:
    had_confirmed = _gmail_session_confirmed()
    confirmed_address = str(st.session_state.get("gmail_confirmed_address", "") or "").strip()

    store_dir = _gmail_account_store_dir(config.address)
    if show_spinner:
        with st.spinner("Mail 수신함 확인 중"):
            result = download_gmail_sp_pdfs(store_dir, config=config)
    else:
        result = download_gmail_sp_pdfs(store_dir, config=config)

    st.session_state["last_gmail_sync_at"] = time.time()
    st.session_state["last_gmail_sync_result"] = result
    _clear_document_caches()
    if result.get("ok"):
        st.session_state["last_gmail_sync_error"] = ""
        st.session_state["last_gmail_background_error"] = ""
        st.session_state["gmail_user_disconnected"] = False
        st.session_state["gmail_logged_in"] = True
        st.session_state["gmail_connected_address"] = config.address
        st.session_state["gmail_confirm_version"] = GMAIL_CONFIRMATION_VERSION
        st.session_state["gmail_confirmed_address"] = config.address
        st.session_state["gmail_confirmed_app_password"] = config.app_password
        st.session_state["gmail_confirmed_subject_keyword"] = config.subject_keyword
        st.session_state["gmail_confirmed_search"] = config.search
        st.session_state["gmail_confirmed_since_date"] = _gmail_since_date()
    else:
        if had_confirmed:
            st.session_state["last_gmail_background_error"] = result.get("error") or ""
            st.session_state["last_gmail_sync_error"] = ""
            st.session_state["gmail_logged_in"] = True
            st.session_state["gmail_confirm_version"] = GMAIL_CONFIRMATION_VERSION
            if confirmed_address:
                st.session_state["gmail_connected_address"] = confirmed_address
        elif show_result or confirmed_address != config.address:
            st.session_state["last_gmail_sync_error"] = result.get("error") or ""
            st.session_state["gmail_logged_in"] = False
            st.session_state["gmail_connected_address"] = ""
            st.session_state["gmail_confirm_version"] = ""
        else:
            st.session_state["last_gmail_background_error"] = result.get("error") or ""
            st.session_state["last_gmail_sync_error"] = ""

    if show_result:
        if result.get("ok"):
            downloaded = len(result.get("downloaded", []))
            matched = result.get("matched_messages", 0)
            st.success(f"조건 일치 메일 {matched}건, 새 PDF {downloaded}개를 확인했습니다.")
            st.rerun()
        else:
            st.warning(result.get("error") or "Mail 확인 중 오류가 발생했습니다.")

    return result


def _clear_document_caches() -> None:
    for cache_func in (
        _load_gmail_index_cached,
        _document_from_pdf_cached,
        _extract_pdf_text,
        _load_judgement_status_index_cached,
    ):
        try:
            cache_func.clear()
        except Exception:
            pass


def _last_sync_status() -> str:
    error = st.session_state.get("last_gmail_sync_error")
    if error:
        return "연결 확인 필요"
    value = st.session_state.get("last_gmail_sync_at")
    if not value:
        return "대기 중"
    return time.strftime("%H:%M:%S", time.localtime(value))


def _on_upload_saved(selected: Path) -> None:
    selected = selected.resolve()
    _clear_document_caches()
    st.session_state["selected_pdf_path"] = str(selected)
    selected_doc = _document_from_pdf(selected)
    st.session_state["reference_company_filter"] = selected_doc.company
    st.session_state["reference_product_filter"] = selected_doc.product
    for key in list(st.session_state):
        if str(key).startswith("sp_direct_judgement_artifacts::") or str(key).startswith("sim_signature"):
            st.session_state.pop(key, None)
    st.session_state.pop("latest_judgement_artifact_key", None)


def _load_inbox_documents() -> list[InboxDocument]:
    local_documents = [_document_from_pdf(path) for path in list_uploaded_sps(STORE_DIR)]
    if not _gmail_session_confirmed():
        return local_documents

    since = _gmail_confirmed_since_date()
    store_dir = _current_gmail_store_dir()

    if store_dir is None or not store_dir.exists():
        return local_documents

    index = _load_gmail_index(store_dir)
    files = index.get("files", {})

    if not isinstance(files, dict):
        return local_documents

    pdfs: list[Path] = []

    for filename, record in files.items():
        if not isinstance(record, dict):
            continue

        attachment_type = str(record.get("attachment_type", "")).strip().casefold()

        # 제출 SP 문서함에는 SP 첨부만 표시한다.
        # 허가서는 SP 카드의 related_permit_files를 통해서만 연결한다.
        if attachment_type == "permit":
            continue

        pdf = store_dir / str(filename)

        if not pdf.exists():
            continue

        if not _is_after_date(pdf, since):
            continue

        pdfs.append(pdf)

    pdfs = sorted(
        pdfs,
        key=lambda item: (
            str(_index_record(item).get("received_at", "")),
            item.stat().st_mtime,
        ),
        reverse=True,
    )

    return local_documents + [_document_from_pdf(pdf) for pdf in pdfs]


def _current_gmail_store_dir() -> Path | None:
    address = str(st.session_state.get("gmail_confirmed_address", "")).strip()
    if not address:
        return None
    return _gmail_account_store_dir(address)


def _gmail_account_store_dir(address: str) -> Path:
    account_key = _safe_account_key(address)
    return STORE_DIR / "accounts" / account_key


def _safe_account_key(address: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", address.strip().casefold()).strip("._-")
    digest = hashlib.sha1(address.strip().casefold().encode("utf-8", "ignore")).hexdigest()[:8]
    return f"{clean[:48] or 'gmail'}_{digest}"


def _document_from_pdf(pdf: Path) -> InboxDocument:
    pdf = Path(pdf)
    return _document_from_pdf_cached(
        str(pdf),
        _file_cache_signature(pdf),
        _gmail_index_signature(pdf.parent),
    )


@st.cache_data(show_spinner=False)
def _document_from_pdf_cached(
    pdf_str: str,
    file_signature: tuple[int, int],
    index_signature: tuple[int, int],
) -> InboxDocument:
    pdf = Path(pdf_str)
    text = _extract_pdf_text(pdf, file_signature)
    filename = pdf.stem
    lower_name = filename.casefold()

    company = _extract_company(text, lower_name)
    product = _extract_product(text, lower_name)
    version = _extract_version(text, lower_name)
    number = _extract_product_number(text, lower_name)
    product, product_mapping_id = _resolve_product_mapping(
        company, product, number
    )
    title = _extract_title(text, filename, product)
    received_date = _extract_received_date(pdf)
    subject = _extract_subject_from_index(pdf)
    permit_link_error = ""
    try:
        permit_files = _related_permit_files(pdf)
    except PermitLinkError as exc:
        permit_files = ()
        permit_link_error = str(exc)

    return InboxDocument(
        path=pdf,
        company=company,
        product=product,
        title=title,
        product_number=number,
        version=version,
        received_date=received_date,
        subject=subject,
        source="직접 업로드" if _index_record(pdf).get("source") == "upload" else "Gmail",
        permit_files=permit_files,
        product_mapping_id=product_mapping_id,
        permit_link_error=permit_link_error,
    )


def _pdf_status_key(path: Path) -> str:
    try:
        resolved = str(Path(path).resolve())
    except Exception:
        resolved = str(path)
    return hashlib.sha256(resolved.encode("utf-8", "ignore")).hexdigest()


def _load_judgement_status_index() -> dict:
    return _load_judgement_status_index_cached(_judgement_status_signature())


def _judgement_status_signature() -> tuple[int, int]:
    try:
        stat = JUDGEMENT_STATUS_INDEX.stat()
        return int(stat.st_size), int(stat.st_mtime_ns)
    except OSError:
        return 0, 0


@st.cache_data(show_spinner=False)
def _load_judgement_status_index_cached(signature: tuple[int, int]) -> dict:
    if not JUDGEMENT_STATUS_INDEX.exists():
        return {"documents": {}}

    try:
        with JUDGEMENT_STATUS_INDEX.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"documents": {}}

    if not isinstance(data, dict):
        return {"documents": {}}

    if not isinstance(data.get("documents"), dict):
        data["documents"] = {}

    return data


def _safe_int(value: object) -> int:
    try:
        return int(str(value).replace(",", "").strip() or "0")
    except Exception:
        return 0


def _summary_counts_from_csv(path: Path | None) -> dict[str, int] | None:
    if path is None or not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
    except Exception:
        return None

    if len(rows) < 2:
        return None

    data_rows = [row for row in rows[1:] if row and any(str(cell).strip() for cell in row)]
    total_markers = {"전체", "총계", "합계", "total", "overall"}
    total_row = next(
        (row for row in data_rows if str(row[0]).strip().casefold() in total_markers),
        None,
    )

    if total_row is None:
        total = [0, 0, 0, 0]
        for row in data_rows:
            padded = row + ["0"] * 5
            total[0] += _safe_int(padded[1])
            total[1] += _safe_int(padded[2])
            total[2] += _safe_int(padded[3])
            total[3] += _safe_int(padded[4])
        return {"pass": total[0], "fail": total[1], "hold": total[2], "total": total[3]}

    padded = total_row + ["0"] * 5
    return {
        "pass": _safe_int(padded[1]),
        "fail": _safe_int(padded[2]),
        "hold": _safe_int(padded[3]),
        "total": _safe_int(padded[4]),
    }


def _status_record_for_doc(doc: InboxDocument, index: dict) -> dict | None:
    if getattr(doc, "permit_link_error", ""):
        return None
    from sp_pdf_judger.permit_semantic_settings import read_semantic_settings, semantic_cache_fingerprint
    from sp_pdf_judger.review_presentation import file_sha256
    from sp_pdf_judger.review_scope import current_permit_review_scope
    docs = index.get("documents", {})
    if not isinstance(docs, dict):
        return None

    exact = docs.get(_pdf_status_key(doc.path))
    # Identically-sized files or similar names are not interchangeable submissions.
    if not isinstance(exact, dict) or exact.get("cache_version") != JUDGEMENT_STATUS_CACHE_VERSION:
        return None
    try:
        linked_permits = _related_permit_files(doc.path)
    except PermitLinkError:
        return None
    review_mode = "with_permit" if linked_permits else "sp_rules_only"
    if exact.get("review_mode") != review_mode:
        return None
    if current_permit_review_scope() == 'hold_only':
        if exact.get('permit_review_scope') != 'hold_only':
            return None
        try:
            sp_hash = file_sha256(doc.path)
            if not sp_hash or exact.get('sp_sha256') != sp_hash:
                return None
        except OSError:
            return None
    if exact.get("rule_fingerprint") != policy_fingerprint():
        return None
    if exact.get("extraction_fingerprint") != extraction_fingerprint():
        return None
    if exact.get("llm_execution_fingerprint") != llm_execution_fingerprint():
        return None
    semantic_settings = read_semantic_settings()
    if exact.get('semantic_fingerprint', '') != semantic_cache_fingerprint(semantic_settings):
        return None
    if semantic_settings.mode == 'shared_evidence' and exact.get('sp_sha256') != file_sha256(doc.path):
        return None
    current = resolve_submission_permits(
        linked_permits,
        doc.company,
        doc.product,
        product_mapping_id=str(getattr(doc, "product_mapping_id", "") or ""),
    )
    if exact.get("permit_fingerprint") != current.fingerprint:
        return None
    if current_permit_review_scope() == 'hold_only':
        try:
            permit_hashes = {str(Path(path).resolve()): file_sha256(Path(path))
                             for path in current.paths}
            if any(not value for value in permit_hashes.values()):
                return None
            if exact.get('permit_file_hashes') != permit_hashes:
                return None
        except OSError:
            return None
    details = resolve_domain_detail_profile(doc.company, doc.product)
    if exact.get("detail_fingerprint") != details.fingerprint:
        return None
    return exact



def _judgement_status_for_doc(doc: InboxDocument) -> dict:
    current_sim_pdf = str(st.session_state.get("sim_pdf", "") or "")
    is_running = bool(st.session_state.get("run_sim")) and current_sim_pdf == str(doc.path)

    index = _load_judgement_status_index()
    record = _status_record_for_doc(doc, index)

    if not isinstance(record, dict):
        return {"state": "running" if is_running else "pending", "summary": None, "updated_at": ""}

    try:
        stat = doc.path.stat()
        sig = record.get("pdf_sig", {})
        is_same_file = (
            int(sig.get("size", -1)) == int(stat.st_size)
            and int(sig.get("mtime_ns", -1)) == int(stat.st_mtime_ns)
        )
    except Exception:
        is_same_file = True

    if not is_same_file:
        return {"state": "running" if is_running else "pending", "summary": None, "updated_at": ""}

    if record.get("status") == "failed":
        return {"state": "failed", "summary": None, "updated_at": str(record.get("updated_at", ""))}

    raw_summary = record.get("summary_counts")
    if not isinstance(raw_summary, dict):
        legacy_summary = _summary_counts_from_csv(Path(str(record.get("summary_after_path", ""))))
        return {
            "state": "completed_legacy" if record.get("status") == "completed" else ("running" if is_running else "pending"),
            "summary": legacy_summary,
            "updated_at": str(record.get("updated_at", "")),
        }

    summary = {
        "pass": _safe_int(raw_summary.get("pass", 0)),
        "fail": _safe_int(raw_summary.get("fail", 0)),
        "hold": _safe_int(raw_summary.get("hold", 0)),
        "total": _safe_int(raw_summary.get("total", 0)),
    }

    if summary is None:
        return {"state": "running" if is_running else "pending", "summary": None, "updated_at": ""}

    # 판정 엔진이 아직 결과를 확정하지 않았을 때는 모든 항목을 보류로 초기화한다.
    # 이는 실제 보류 판정이 아니므로 문서함에는 결과를 노출하지 않는다.
    if summary["total"] > 0 and summary["pass"] == 0 and summary["fail"] == 0 and summary["hold"] == summary["total"]:
        return {"state": "running" if is_running else "pending", "summary": None, "updated_at": str(record.get("updated_at", ""))}

    return {
        "state": "completed",
        "summary": summary,
        "updated_at": str(record.get("updated_at", "")),
    }


def _review_status_html(status: dict) -> str:
    state = status.get("state")
    if state == "failed":
        return '<b class="review-status pending">검수 오류 · 재시도 필요</b>'
    if state == "completed":
        return '<b class="review-status completed">검수 완료</b>'
    if state == "completed_legacy":
        return '<b class="review-status completed">검수 완료</b>'
    if state == "running":
        return '<b class="review-status running">검수 진행중</b>'
    return '<b class="review-status pending">검수 전</b>'


def _review_summary_html(status: dict) -> str:
    # 검수가 끝나기 전에는 결과 수치를 노출하지 않는다.
    if status.get("state") not in {"completed", "completed_legacy"}:
        return ""

    summary = status.get("summary")
    if not summary:
        return ""

    passed = _safe_int(summary.get("pass", 0))
    failed = _safe_int(summary.get("fail", 0))
    held = _safe_int(summary.get("hold", 0))
    total = _safe_int(summary.get("total", 0))
    return (
        '<div class="doc-review-summary">'
        f'<div><span class="dot pass"></span><span>충족</span><b>{passed}건</b></div>'
        f'<div><span class="dot fail"></span><span>불충족</span><b>{failed}건</b></div>'
        f'<div><span class="dot hold"></span><span>보류</span><b>{held}건</b></div>'
        f'<div class="total"><span>전체</span><b>{total}건</b></div>'
        '</div>'
        f'<details class="count-help"><summary>집계 기준</summary>{COUNT_HELP}</details>'
    )


def _is_after_gmail_since(pdf: Path) -> bool:
    return _is_after_date(pdf, _gmail_confirmed_since_date())


def _is_after_date(pdf: Path, since: date) -> bool:
    record = _index_record(pdf)
    raw = str(record.get("received_at", "")).strip()
    if raw:
        try:
            return datetime.fromisoformat(raw).date() >= since
        except ValueError:
            pass
    return datetime.fromtimestamp(pdf.stat().st_mtime).date() >= since


def _is_sp_pdf(pdf: Path) -> bool:
    return not _is_permit_pdf(pdf)


def _is_permit_pdf(pdf: Path) -> bool:
    record = _index_record(pdf)
    attachment_type = str(record.get("attachment_type", "")).strip().casefold()
    if attachment_type == "sp":
        return False
    if attachment_type == "permit":
        return True
    name = " ".join([
        pdf.name,
        str(record.get("original_name", "")),
        str(record.get("subject", "")),
    ]).casefold()
    # "허가서검증본"처럼 허가서 유무/반영을 검증하는 SP 문서는 문서함에 남겨야 한다.
    if any(token in name for token in ("허가서검증", "허가서 검증", "permit validation", "permit_validation", "permit-validation")):
        return False
    return any(token in name for token in ("허가서", "품목허가", "허가사항", "허가증", "permit", "approval", "license"))


class PermitLinkError(ValueError):
    """Invalid linkage is not permission to silently run a permit-free review."""


def _related_permit_files(pdf: Path) -> tuple[Path, ...]:
    record = _index_record(pdf)
    index = _load_gmail_index(pdf.parent)
    error = "허가서 연결 오류: 연결 정보를 확인한 뒤 허가서를 다시 추가·교체해 주세요."
    files = index.get("files", {})
    if not isinstance(files, dict):
        raise PermitLinkError(error)

    def linked_path(name):
        # Reject unsafe names before resolving or accessing any outside path.
        if (not isinstance(name, str) or not name.strip()
                or name in {".", ".."} or any(char in name for char in ("/", "\\", ":", "\0"))
                or Path(name).suffix.casefold() != ".pdf"):
            raise PermitLinkError(error)
        candidate = pdf.parent / name
        if candidate.resolve().parent != pdf.parent.resolve():
            raise PermitLinkError(error)
        return candidate

    # 1순위: index에 명시된 related_permit_files만 사용
    related_names = record.get("manual_permit_files", record.get("related_permit_files", []))
    if not isinstance(related_names, (list, tuple)):
        raise PermitLinkError(error)
    permits: list[Path] = []

    for name in related_names:
        candidate = linked_path(name)
        other = files.get(name, {})
        if not isinstance(other, dict):
            raise PermitLinkError(error)
        attachment_type = other.get("attachment_type", "")
        if name in files and (not isinstance(attachment_type, str) or attachment_type.strip().casefold() != "permit"):
            raise PermitLinkError(error)

        permits.append(candidate)

    if permits or "manual_permit_files" in record or related_names:
        return tuple(permits)

    # 2순위: 같은 message_id에 있는 permit만 연결
    # message_id가 없는 로컬 파일에는 폴더 안 허가서를 자동으로 붙이지 않는다.
    message_id = str(record.get("message_id", "") or "").strip()

    if not message_id:
        return tuple()

    for name, other in files.items():
        if name == pdf.name or not isinstance(other, dict):
            continue

        if str(other.get("message_id", "") or "").strip() != message_id:
            continue

        attachment_type = other.get("attachment_type", "")
        if not isinstance(attachment_type, str):
            raise PermitLinkError(error)
        attachment_type = attachment_type.strip().casefold()

        if attachment_type == "sp":
            continue
        if attachment_type != "permit":
            raise PermitLinkError(error)

        permits.append(linked_path(name))

    return tuple(permits)


def _file_cache_signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return int(stat.st_size), int(stat.st_mtime_ns)
    except OSError:
        return 0, 0


def _gmail_index_signature(folder: Path) -> tuple[int, int]:
    try:
        stat = (Path(folder) / "_gmail_index.json").stat()
        return int(stat.st_size), int(stat.st_mtime_ns)
    except OSError:
        return 0, 0


@st.cache_data(show_spinner=False)
def _extract_pdf_text(pdf: Path, signature: tuple[int, int]) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf))
        chunks = []
        for page in reader.pages[:4]:
            chunks.append(page.extract_text() or "")
        return "\n".join(chunks)
    except Exception:
        return ""


def _extract_company(text: str, lower_name: str) -> str:
    compact = _compact(text)

    if "동국바이오사이언스" in compact:
        return "동국바이오사이언스"
    if "동국화학" in compact:
        return "동국화학"
    if "동국약품" in compact:
        return "동국약품 주식회사"
    if "녹십자" in compact:
        return "(주)녹십자"

    patterns = [
        r"제조사\s*명칭\s*([가-힣A-Za-z0-9().㈜\s]+)",
        r"신\s*청\s*자\s*([가-힣A-Za-z0-9().㈜\s]+)",
        r"회사명\s*[:：]?\s*([가-힣A-Za-z0-9().㈜\s]+)",
        r"^([가-힣A-Za-z0-9().㈜\s]{2,30})\s+Summary Protocol",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact, re.MULTILINE)
        if match:
            return _clean_company_name(
                match.group(1),
                ("주소", "QA팀", "제 품 명", "제품명", "제조번호", "부서명", "Ver"),
            )

    if "녹십자" in lower_name or "지씨플루" in lower_name or "안티트롬빈" in lower_name:
        return "(주)녹십자"
    if "알부민" in lower_name:
        return "동국플라즈마"
    if "스카이코비원" in lower_name:
        return "SK바이오사이언스"
    if "동국화학" in lower_name:
        return "동국화학"
    if "일본뇌염" in lower_name or "제조요약도" in lower_name:
        return "동국약품 주식회사"

    return "회사명 미확인"


def _extract_product(text: str, lower_name: str) -> str:
    # Prefer the labelled PDF field; a filename is not product identity evidence.
    match = re.search(r"제\s*품\s*명\s*[:|]?\s*([^\r\n]+)", text)
    if match:
        candidate = _clean_phrase(match.group(1), ("생기수재명", "허가번호", "제조번호"))
        if candidate and candidate not in {"미확인", "제품명"}:
            return candidate
    if "안티트롬빈" in lower_name:
        return "안티트롬빈III주 500아이유"
    if "지씨플루" in lower_name:
        return "지씨플루멀티주"
    if "녹십자" in lower_name and "알부민" in lower_name:
        return "녹십자-알부민주20%"
    if "알부민" in lower_name:
        return "동국알부민20%주"
    if "스카이코비원" in lower_name:
        return "스카이코비원멀티주"
    if "일본뇌염" in lower_name or "제조요약도" in lower_name:
        return "일본뇌염백신"

    compact = _compact(text)
    patterns = [
        r"제품명\s*([가-힣A-Za-z0-9().%·\s]+)",
        r"([가-힣A-Za-z0-9().%·\s]+주)\s*\[",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            return _clean_phrase(match.group(1), ("생기수재명", "허가번호", "제조번호"))

    return "제품명 미확인"


def _extract_title(text: str, filename: str, product: str) -> str:
    compact = _compact(text)
    if "제조 및 품질관리요약서" in compact:
        return f"{product} 제조 및 품질관리요약서"
    if "Summary Protocol" in compact:
        return f"{product} Summary Protocol"
    cleaned = re.sub(r"^\d{8}_\d{6}_", "", filename)
    return cleaned[:42]


def _extract_version(text: str, lower_name: str) -> str:
    match = re.search(r"\bVer\.?\s*[:：]?\s*(\d+(?:\.\d+)*)", text, re.IGNORECASE)
    if match:
        return f"v{match.group(1)}"
    match = re.search(r"\bv(?:ersion)?\.?\s*[:：]?\s*(\d+(?:\.\d+)*)", text, re.IGNORECASE)
    if match:
        return f"v{match.group(1)}"

    if "안티트롬빈" in lower_name:
        return "v3.0"
    if "지씨플루" in lower_name:
        return "v3.2"
    if "녹십자" in lower_name and "알부민" in lower_name:
        return "v4.0"
    if "스카이코비원" in lower_name:
        return "v4.0"
    if "일본뇌염" in lower_name:
        return "v3.9"
    if "제조요약도" in lower_name:
        return ""
    return ""


def _extract_product_number(text: str, lower_name: str) -> str:
    compact = _compact(text)
    patterns = [
        r"제조번호\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9._/\-\s]{2,40})",
        r"제품번호\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9._/\-\s]{2,40})",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            return _clean_identifier(match.group(1))

    if "안티트롬빈" in lower_name:
        return "270B26004"
    if "지씨플루" in lower_name:
        return "V50526009"
    if "녹십자" in lower_name and "알부민" in lower_name:
        return "162A25557"
    if "알부민" in lower_name:
        return "HSA260526"
    if "스카이코비원" in lower_name:
        return "SKP-ALB-2026"
    if "일본뇌염" in lower_name or "제조요약도" in lower_name:
        return "JEV-DGUPI-2603-001"
    return "미확인"


def _map_unknown_sky_covione_product(company: str, product: str, product_number: str) -> str:
    """Map only the identifiable Dongkook SKY-M unknown-product case."""
    return _resolve_product_mapping(company, product, product_number)[0]


def _resolve_product_mapping(
    company: str,
    product: str,
    product_number: str,
) -> tuple[str, str]:
    """Return the display product and the exact rule that produced it."""
    if (
        company == "동국바이오사이언스"
        and product == "제품명 미확인"
        and product_number.upper().startswith("SKY-M")
    ):
        return "스카이코비원멀티주", DONGKOOK_UNKNOWN_SKY_M_MAPPING_ID
    return product, ""


def _extract_received_date(pdf: Path) -> str:
    index_record = _index_record(pdf)
    raw = str(index_record.get("received_at", "")).strip()
    if raw:
        try:
            return datetime.fromisoformat(raw).strftime("%Y.%m.%d")
        except ValueError:
            pass
    return datetime.fromtimestamp(pdf.stat().st_mtime).strftime("%Y.%m.%d")


def _extract_subject_from_index(pdf: Path) -> str:
    return str(_index_record(pdf).get("subject", "")).strip()


def _index_record(pdf: Path) -> dict:
    index = _load_gmail_index(pdf.parent)
    return index.get("files", {}).get(pdf.name, {})


def _load_gmail_index(folder: Path) -> dict:
    folder = Path(folder)
    return _load_gmail_index_cached(str(folder), _gmail_index_signature(folder))


@st.cache_data(show_spinner=False)
def _load_gmail_index_cached(folder_str: str, index_signature: tuple[int, int]) -> dict:
    folder = Path(folder_str)
    index_path = folder / "_gmail_index.json"
    if not index_path.exists():
        return {}
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return _normalized_gmail_index(index, folder)


def _normalized_gmail_index(index: dict, folder: Path) -> dict:
    files = index.get("files", {})
    if not isinstance(files, dict):
        return index

    by_message: dict[str, list[dict]] = {}
    for record in files.values():
        if not isinstance(record, dict):
            continue
        if record.get("source") == "upload" or "manual_permit_files" in record:
            continue
        if not record.get("size_bytes"):
            try:
                record["size_bytes"] = (folder / str(record.get("file", ""))).stat().st_size
            except OSError:
                record["size_bytes"] = 0
        message_id = str(record.get("message_id", "") or record.get("subject", ""))
        by_message.setdefault(message_id, []).append(record)

    for records in by_message.values():
        if len(records) > 1 and all(record.get("attachment_type") == "permit" for record in records):
            largest = max(records, key=lambda record: int(record.get("size_bytes") or 0))
            largest["attachment_type"] = "sp"
        sp_files = [str(record.get("file", "")) for record in records if record.get("attachment_type") != "permit"]
        permit_files = [str(record.get("file", "")) for record in records if record.get("attachment_type") == "permit"]
        for record in records:
            record["related_sp_files"] = [name for name in sp_files if name and name != record.get("file")]
            record["related_permit_files"] = [name for name in permit_files if name and name != record.get("file")]

    return index


def _delete_inbox_document(doc: InboxDocument) -> None:
    index_path = doc.path.parent / "_gmail_index.json"
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            record = index.get("files", {}).pop(doc.path.name, None)
            sha = record.get("sha256") if isinstance(record, dict) else None
            if sha:
                index.get("sha256", {}).pop(sha, None)
            for key, value in list(index.get("sha256", {}).items()):
                if isinstance(value, dict) and value.get("file") == doc.path.name:
                    index["sha256"].pop(key, None)
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            st.warning(f"Mail 인덱스 정리 중 오류가 있었습니다: {exc}")

    try:
        doc.path.unlink(missing_ok=True)
    except TypeError:
        if doc.path.exists():
            doc.path.unlink()
    _clear_document_caches()


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _clean_phrase(value: str, stop_words: tuple[str, ...] = ()) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip(" :：|")
    for word in stop_words:
        if word in cleaned:
            cleaned = cleaned.split(word, 1)[0].strip(" :：|")
    return cleaned[:42] or "미확인"


def _clean_company_name(value: str, stop_words: tuple[str, ...] = ()) -> str:
    cleaned = _clean_phrase(value, stop_words)
    cleaned = re.sub(r"\s*(?:㈜|\(주\))\s*$", "", cleaned).strip(" :：|")
    return cleaned[:42] or "회사명 미확인"


def _clean_identifier(value: str) -> str:
    value = re.sub(r"\s*-\s*", "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .-/")
    if not value:
        return "미확인"
    first = value.split()[0]
    if re.fullmatch(r"\d+/\d+", first):
        return "미확인"
    return first


def _ensure_selected_document(documents: list[InboxDocument]) -> InboxDocument | None:
    if not documents:
        st.session_state.pop("selected_pdf_path", None)
        return None

    selected_path = st.session_state.get("selected_pdf_path")
    if not selected_path or not Path(selected_path).exists():
        st.session_state.pop("selected_pdf_path", None)
        return None

    for doc in documents:
        if str(doc.path) == selected_path:
            return doc

    st.session_state.pop("selected_pdf_path", None)
    return None


def _references_for_document(doc: InboxDocument | None) -> list[ReferenceDocument]:
    references = _all_references()
    if doc is None:
        return references
    exact = [
        ref
        for ref in references
        if ref.company == doc.company and ref.product == doc.product
    ]
    if exact:
        return exact
    product_matches = [ref for ref in references if ref.product == doc.product]
    return product_matches or references


def _all_references() -> list[ReferenceDocument]:
    return list(REFERENCE_LIBRARY) + list(EXPANDED_REFERENCE_LIBRARY)


def _references_for_company_product(company: str, product: str) -> list[ReferenceDocument]:
    return [
        ref
        for ref in _all_references()
        if ref.company == company and ref.product == product
    ]


def _normalize_version_for_compare(version: str | None) -> str:
    version = str(version or "").strip().lower()
    version = re.sub(r"^(ver\.?|version|v)\s*", "", version)
    return version.strip()


def _version_gate(doc: InboxDocument | None, refs: list[ReferenceDocument]) -> dict:
    if doc is None:
        return {"ok": False, "reason": "제출 SP 문서를 먼저 선택하세요.", "matched": None}
    matching_refs = [ref for ref in refs if ref.company == doc.company and ref.product == doc.product]
    if not matching_refs:
        return {"ok": False, "matched": None, "status_label": "제품명 확인 필요",
                "reason": "제출 제품명과 일치하는 기준 SP가 없습니다. 제품명 원문을 유지하며, 연결된 허가서로 검수를 진행해 제품명 불일치를 확인할 수 있습니다. 버전 불일치로 단정하지 않습니다."}
    if not doc.version:
        return {
            "ok": False,
            "reason": f"제출 SP 문서에서 버전 정보를 확인하지 못했습니다. 기준 SP 버전 {TARGET_REFERENCE_VERSION} 기준으로 검수는 계속 진행합니다.",
            "matched": matching_refs[0] if matching_refs else None,
        }
    doc_version = _normalize_version_for_compare(doc.version)
    if doc_version == TARGET_REFERENCE_VERSION and matching_refs:
        return {
            "ok": True,
            "reason": f"제출 SP 버전이 기준 SP 버전 {TARGET_REFERENCE_VERSION}과 일치합니다.",
            "matched": matching_refs[0],
        }
    return {
        "ok": False,
        "reason": f"제출 SP 버전 {doc.version}와 기준 SP 버전 {TARGET_REFERENCE_VERSION}이 일치하지 않습니다. 버전 불일치로 표시하되 검수는 계속 진행합니다.",
        "matched": matching_refs[0] if matching_refs else None,
    }


def _render_workflow_gate(
    selected_doc: InboxDocument | None,
    gate: dict,
    refs: list[ReferenceDocument],
) -> None:
    selected_version = selected_doc.version if selected_doc else "-"
    matched_ref = gate.get("matched")
    reference_version = TARGET_REFERENCE_VERSION
    gate_class = "ok" if gate.get("ok") else "blocked"
    gate_label = "PASS" if gate.get("ok") else "INFO"

    st.markdown(
        f"""
        <div class="workflow-shell">
          <div class="workflow-node">
            <div class="node-icon">01</div>
            <div>
              <b>Mail SP 접수</b>
              <span>메일 제목 필터 기반 PDF 자동 저장</span>
            </div>
          </div>
          <div class="workflow-line"></div>
          <div class="workflow-node">
            <div class="node-icon">02</div>
            <div>
                <b>기준 SP 버전 확인</b>
              <span>제출 {selected_version} · 기준 {reference_version}</span>
            </div>
          </div>
          <div class="workflow-line"></div>
          <div class="workflow-node {gate_class}">
            <div class="node-icon">{gate_label}</div>
            <div>
              <b>2페이지 검수 진입</b>
              <span>{_escape(gate.get("reason", ""))}</span>
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_inbox_panel(documents: list[InboxDocument], selected_doc: InboxDocument | None) -> None:
    st.markdown(
        f"""
        <div class="inbox-panel-marker"></div>
        <div class="panel-title-row">
          <div>
            <h2>제출 SP 문서함</h2>
          </div>
          <div class="count-pill">총 {len(documents)}건</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    notice = st.session_state.pop("delete_notice", "")
    if notice:
        st.success(notice)

    if not documents:
        st.markdown(
            """
            <div class="empty-card">
              <b>아직 수신된 PDF가 없습니다.</b>
              <span>Mail 설정을 열고 [식약처] 제목의 PDF 첨부 메일을 보내면 이 영역에 자동으로 표시됩니다.</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    with st.container(height=INBOX_DOCUMENT_LIST_HEIGHT, border=False):
        for idx, doc in enumerate(documents):
            selected = bool(selected_doc and doc.path == selected_doc.path)
            refs = _references_for_document(doc)
            gate = _version_gate(doc, refs)
            _render_inbox_card(doc, selected, gate, idx)


def _render_inbox_card(doc: InboxDocument, selected: bool, gate: dict, idx: int) -> None:
    card_class = "submitted-card selected" if selected else "submitted-card"
    status_class = "match" if gate.get("ok") else "mismatch"
    status_label = _escape(gate.get("status_label") or ("버전 일치" if gate.get("ok") else "버전 확인 필요"))
    version = doc.version or "버전 없음"
    subject = doc.subject or "메일 제목 정보 없음"
    permit_summary = _permit_summary(doc)
    review_status = _judgement_status_for_doc(doc)
    review_status_html = _review_status_html(review_status)
    review_summary_html = _review_summary_html(review_status)

    st.markdown(
        f"""
        <div class="{card_class}">
          {_company_logo_html(doc.company, "logo-token")}
          <div class="submitted-main">
            <div class="doc-meta-line">
              <span>{_escape(doc.company)}</span>
              <span class="doc-pill-row"><b class="{status_class}">{status_label}</b>{review_status_html}</span>
            </div>
            <div class="submitted-title">{_escape(doc.title)}</div>
            <div class="submitted-product">{_escape(doc.product)}</div>
            <div class="field-grid">
              <span>제품/제조번호</span><b>{_escape(doc.product_number)}</b>
              <span>제출 버전</span><b>{_escape(version)}</b>
              <span>접수일</span><b>{_escape(doc.received_date)}</b>
              <span>문서 제목</span><b>{_escape(_source_document_name(doc.path))}</b>
              <span>허가서</span><b>{_escape(permit_summary)}</b>
              <span>수신 제목</span><b>{_escape(subject)}</b>
            </div>
            {review_summary_html}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="doc-actions-marker"></div>', unsafe_allow_html=True)

    select_col, open_col, permit_col, delete_col = st.columns([1.08, .72, .86, .48], gap="small")
    with select_col:
        label = "선택 취소" if selected else "이 SP 문서 선택"
        if st.button(label, key=f"select-doc-{idx}-{doc.path.name}", use_container_width=True):
            if selected:
                st.session_state.pop("selected_pdf_path", None)
                st.session_state["gate_error"] = ""
                st.session_state["run_sim"] = False
            else:
                st.session_state["selected_pdf_path"] = str(doc.path)
                st.session_state["reference_company_filter"] = doc.company
                st.session_state["reference_product_filter"] = doc.product
                if not gate.get("ok"):
                    st.session_state["gate_error"] = gate["reason"]
                else:
                    st.session_state["gate_error"] = ""
            st.rerun()
    with open_col:
        st.markdown(_file_open_link(doc.path, "SP 파일 열기"), unsafe_allow_html=True)
    with permit_col:
        if doc.permit_files:
            links = []
            for permit_idx, permit in enumerate(doc.permit_files, start=1):
                label = "허가서 열기" if len(doc.permit_files) == 1 else f"허가서 {permit_idx} 열기"
                links.append(_file_open_link(permit, label))
            st.markdown("".join(links), unsafe_allow_html=True)
        else:
            st.markdown('<span class="file-open-link disabled">허가서 없음</span>', unsafe_allow_html=True)
    with delete_col:
        if st.button("삭제", key=f"delete-doc-{idx}-{doc.path.name}", use_container_width=True):
            _delete_inbox_document(doc)
            if selected:
                st.session_state.pop("selected_pdf_path", None)
            st.session_state["delete_notice"] = f"{doc.path.name} 문서를 삭제했습니다."
            st.rerun()


def _source_document_name(path: Path) -> str:
    return str(_index_record(path).get("original_name") or re.sub(r"^\d{8}_\d{6}_", "", path.name))


def _permit_summary(doc: InboxDocument) -> str:
    if getattr(doc, "permit_link_error", ""):
        return doc.permit_link_error
    if not doc.permit_files:
        return "SP 룰 전용 · 허가서 미검증"
    names = [_source_document_name(permit) for permit in doc.permit_files]
    if len(names) == 1:
        return f"{names[0]} 있음"
    return f"{len(names)}개 있음: {', '.join(names[:2])}"


def _file_open_link(path: Path, label: str) -> str:
    href = _pdf_viewer_url(path)
    if not href:
        return f'<span class="file-open-link disabled">{_escape(label)}</span>'
    return (
        f'<a class="file-open-link" href="{_escape(href)}" target="_blank" '
        f'rel="noopener noreferrer">{_escape(label)}</a>'
    )


def _pdf_viewer_url(path: Path) -> str:
    source = Path(path)
    if not source.exists() or source.suffix.casefold() != ".pdf":
        return ""
    try:
        resolved = str(source.resolve())
    except OSError:
        resolved = str(source)
    token = base64.urlsafe_b64encode(resolved.encode("utf-8", "ignore")).decode("ascii").rstrip("=")
    return f"?view=pdf_viewer&pdf={quote(token)}"


def _decode_pdf_viewer_ref(token: str) -> Path | None:
    token = str(token or "").strip()
    if not token:
        return None
    try:
        padded = token + ("=" * (-len(token) % 4))
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", "ignore")
    except Exception:
        return None
    path = Path(decoded)
    if not _is_allowed_pdf_viewer_path(path):
        return None
    return path


def _is_allowed_pdf_viewer_path(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if resolved.suffix.casefold() != ".pdf" or not resolved.exists():
        return False

    app_dir = Path(__file__).resolve().parent
    allowed_roots = (
        app_dir / "incoming_sp_pdfs",
        STORE_DIR,
        app_dir / "static" / "pdf_view",
        app_dir / "sp_pdf_judger" / "permits",
    )
    for root in allowed_roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def _review_issue_definitions() -> list[dict[str, object]]:
    """UI용 검수 이슈. 실제 좌표 데이터가 생기면 page/highlight 값을 결과 JSON으로 교체한다."""
    return [
        {"title": "제조번호 불일치", "count": 9, "page": 3, "top": 31, "kind": "fail"},
        {"title": "제조년월일 불일치", "count": 9, "page": 3, "top": 47, "kind": "fail"},
        {"title": "제조량 불일치", "count": 3, "page": 4, "top": 58, "kind": "fail"},
        {"title": "제조수량 불일치", "count": 0, "page": 5, "top": 68, "kind": "pass"},
    ]


def _render_review_workspace(
    documents: list[InboxDocument],
    selected_doc: InboxDocument | None,
    refs: list[ReferenceDocument],
    gate: dict,
) -> None:
    """참고 시안의 3단 검수 화면. 기존 문서 선택/최종 판정 흐름은 그대로 사용한다."""
    del refs, gate
    st.markdown(
        """
        <style>
        .review-workspace { color:#172554; }
        .review-workspace, .review-workspace * { box-sizing:border-box; }
        .review-workspace .workspace-title { display:flex; align-items:center; gap:.65rem; margin:.25rem 0 1rem; }
        .review-workspace .workspace-title h1 { margin:0; font-size:1.15rem; color:#172554; font-weight:800; }
        .review-workspace .workspace-title span { color:#64748b; font-size:.88rem; }
        div[data-testid="stVerticalBlock"]:has(.review-workspace) { background:#f8fbff; border-radius:14px; padding:1rem; }
        .review-panel { background:#fff; border:1px solid #dbe4f0; border-radius:10px; padding:.72rem; min-height:590px; box-shadow:0 2px 8px rgba(15,23,42,.035); }
        .review-panel h2 { margin:0 0 .72rem; font-size:1rem; color:#172554; }
        .review-doc { border:1px solid #e1e8f0; border-left:4px solid #22a06b; border-radius:8px; padding:.55rem .62rem; margin:.48rem 0; background:#fff; }
        .review-doc.active { background:#eff8ff; border-color:#38a4ff; border-left-color:#1677e8; }
        .review-doc b { display:block; color:#1e3a5f; font-size:.86rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .review-doc small { display:block; color:#64748b; margin-top:.24rem; font-size:.73rem; }
        .pdf-toolbar { height:43px; display:flex; align-items:center; gap:.8rem; padding:0 .8rem; border:1px solid #dbe4f0; border-bottom:0; border-radius:9px 9px 0 0; color:#475569; background:#f8fafc; font-weight:700; font-size:.86rem; }
        .pdf-canvas { position:relative; min-height:535px; max-height:610px; overflow:auto; display:flex; justify-content:center; background:#eef2f7; border:1px solid #dbe4f0; border-radius:0 0 9px 9px; padding:1rem; }
        .pdf-sheet { position:relative; width:min(100%, 720px); height:max-content; background:#fff; box-shadow:0 2px 14px rgba(15,23,42,.16); }
        .pdf-sheet img { display:block; width:100%; height:auto; }
        .pdf-highlight { position:absolute; left:12%; width:74%; height:5.5%; border:3px solid #ef4444; background:rgba(254,240,138,.3); border-radius:3px; box-shadow:0 0 0 9999px rgba(255,255,255,.03); pointer-events:none; }
        .pdf-empty { display:grid; place-items:center; min-height:500px; color:#64748b; text-align:center; }
        .summary-box { border:1px solid #bfd7f0; border-radius:9px; overflow:hidden; margin-bottom:.8rem; }
        .summary-box h3 { margin:0; padding:.65rem .75rem; background:#f2f8ff; font-size:.86rem; color:#1e3a5f; }
        .summary-metrics { display:grid; grid-template-columns:repeat(4,1fr); padding:.75rem .35rem; text-align:center; }
        .summary-metrics b { display:block; font-size:1.18rem; color:#1e293b; margin-top:.2rem; }
        .summary-metrics span { font-size:.72rem; font-weight:800; color:#64748b; }
        .metric-pass span { color:#159957; } .metric-fail span { color:#e13c3c; } .metric-hold span { color:#d97706; }
        .issue-guide { border-left:4px solid #ef4444; border-radius:7px; padding:.65rem .72rem; background:#fff5f5; color:#b42318; font-size:.79rem; margin:.8rem 0; }
        .issue-guide b { display:block; margin-bottom:.2rem; }
        .progress-strip { display:flex; align-items:center; gap:.25rem; margin-top:1rem; padding:1rem 1.2rem; background:#fff; border:1px solid #dbe4f0; border-radius:10px; }
        .progress-step { flex:1; display:flex; align-items:center; gap:.5rem; color:#64748b; font-size:.76rem; font-weight:700; }
        .progress-step i { display:grid; place-items:center; width:27px; height:27px; border-radius:50%; background:#e7eef6; color:#64748b; font-style:normal; }
        .progress-step.done { color:#166534; } .progress-step.done i { background:#dcfce7; color:#16a34a; }
        .progress-step.current { color:#1769d1; } .progress-step.current i { background:#fff; border:2px solid #2684e8; color:#1769d1; }
        .progress-line { width:8%; height:1px; background:#cbd5e1; }
        @media (max-width: 1050px) { .review-panel { min-height:auto; } .progress-step { font-size:.65rem; } }
        </style>
        <div class="review-workspace"><div class="workspace-title"><h1>AI 문서 검수 진행</h1><span>제조·품질관리 문서의 AI 비교 검토</span></div></div>
        """,
        unsafe_allow_html=True,
    )

    if selected_doc is None:
        st.warning("검수할 제출 SP 문서를 먼저 선택해 주세요.")
        if st.button("문서 선택 화면으로 돌아가기", use_container_width=True):
            st.session_state["run_sim"] = False
            st.rerun()
        return

    issues = _review_issue_definitions()
    selected_issue = int(st.session_state.get("review_issue_index", 0))
    selected_issue = max(0, min(selected_issue, len(issues) - 1))
    current_page = int(st.session_state.get("review_pdf_page", issues[selected_issue]["page"]))

    left, center, right = st.columns([1.05, 2.8, 1.22], gap="small")
    with left:
        st.markdown('<div class="review-panel"><h2>문서 목록</h2>', unsafe_allow_html=True)
        for idx, doc in enumerate(documents):
            active = doc.path == selected_doc.path
            css = " active" if active else ""
            st.markdown(
                f'<div class="review-doc{css}"><b>{_escape(doc.title)}</b><small>{_escape(doc.company)} · { _escape(doc.version or "버전 미상") }</small></div>',
                unsafe_allow_html=True,
            )
            if st.button("선택됨" if active else "이 문서 검수", key=f"review-doc-{idx}", use_container_width=True, disabled=active):
                st.session_state["selected_pdf_path"] = str(doc.path)
                st.session_state["sim_pdf"] = str(doc.path)
                st.session_state["sim_product"] = doc.product
                st.session_state["review_pdf_page"] = 1
                st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    with center:
        st.markdown(
            f'<div class="pdf-toolbar">☷ &nbsp; {current_page} / PDF &nbsp; <span style="margin-left:auto">{_escape(selected_doc.path.name)}</span></div>',
            unsafe_allow_html=True,
        )
        pages = _render_pdf_pages_for_viewer(str(selected_doc.path), _file_cache_signature(selected_doc.path))
        page_items = pages.get("pages", [])
        page_image = next((p for p in page_items if int(p["page"]) == current_page), None)
        if page_image:
            top = int(issues[selected_issue]["top"])
            st.markdown(
                f'<div class="pdf-canvas"><div class="pdf-sheet"><img src="data:image/png;base64,{page_image["image"]}" alt="PDF {current_page} 페이지"><div class="pdf-highlight" style="top:{top}%" title="선택한 부적합 항목 위치"></div></div></div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown('<div class="pdf-canvas"><div class="pdf-empty">PDF 미리보기를 준비할 수 없습니다.<br>다운로드한 원본 파일에서 확인해 주세요.</div></div>', unsafe_allow_html=True)

    with right:
        st.markdown('<div class="review-panel"><h2>제조요약도 검수 결과</h2><div class="summary-box"><h3>전체 요약</h3><div class="summary-metrics"><div class="metric-pass"><span>● 충족</span><b>65</b></div><div class="metric-fail"><span>● 불충족</span><b>21</b></div><div class="metric-hold"><span>● 보류</span><b>9</b></div><div><span>전체 항목</span><b>95</b></div></div></div><div class="issue-guide"><b>▲ 불일치 항목 있음</b>아래 항목을 누르면 PDF의 해당 위치를 표시합니다.</div>', unsafe_allow_html=True)
        for idx, issue in enumerate(issues):
            label = f'{issue["title"]}  ·  {issue["count"]}건'
            if st.button(label, key=f"review-issue-{idx}", use_container_width=True):
                st.session_state["review_issue_index"] = idx
                st.session_state["review_pdf_page"] = int(issue["page"])
                st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        if st.button("최종 판정 페이지로 이동  →", type="primary", use_container_width=True):
            st.query_params["view"] = "judge_final"
            st.rerun()

    st.markdown(
        """<div class="progress-strip">
        <div class="progress-step done"><i>✓</i>문서 업로드</div><div class="progress-line"></div>
        <div class="progress-step done"><i>✓</i>OCR 인식</div><div class="progress-line"></div>
        <div class="progress-step done"><i>✓</i>정보 추출</div><div class="progress-line"></div>
        <div class="progress-step done"><i>✓</i>AI 검수</div><div class="progress-line"></div>
        <div class="progress-step current"><i>5</i>검수 결과</div><div class="progress-line"></div>
        <div class="progress-step"><i>6</i>최종 판정</div></div>""",
        unsafe_allow_html=True,
    )


def _render_pdf_viewer_page() -> None:
    raw_token = ""
    try:
        value = st.query_params.get("pdf", "")
        raw_token = str(value[0] if isinstance(value, list) and value else value or "")
    except Exception:
        raw_token = ""

    pdf_path = _decode_pdf_viewer_ref(raw_token)

    st.markdown(
        """
        <style>
        [data-testid="stHeader"],
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        [data-testid="collapsedControl"] {
            display: none !important;
        }
        .block-container {
            padding: 1.2rem 1.4rem 1.4rem !important;
            max-width: none !important;
        }
        .pdf-viewer-top {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            margin-bottom: .85rem;
            color: var(--text);
        }
        .pdf-viewer-top h1 {
            margin: 0;
            font-size: 1.35rem;
            line-height: 1.25;
            overflow-wrap: anywhere;
        }
        .pdf-viewer-top span {
            color: var(--muted);
            font-size: 1rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    if pdf_path is None:
        st.error("문서 경로를 확인할 수 없습니다. 제출 문서함에서 다시 열어주세요.")
        return

    try:
        pdf_bytes = pdf_path.read_bytes()
    except OSError:
        st.error("PDF 파일을 읽을 수 없습니다. 메일 첨부 파일이 아직 저장되어 있는지 확인해주세요.")
        return

    st.markdown(
        f"""
        <div class="pdf-viewer-top">
          <div>
            <h1>{_escape(pdf_path.name)}</h1>
            <span>등록된 원본 PDF를 앱 안에서 표시합니다.</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.download_button(
        "PDF 다운로드",
        data=pdf_bytes,
        file_name=pdf_path.name,
        mime="application/pdf",
        use_container_width=True,
    )

    requested_page = _safe_int(st.query_params.get("page", "0"))
    if requested_page > 0:
        try:
            import fitz
            with fitz.open(pdf_path) as document:
                if requested_page > len(document):
                    st.error("원본 PDF에 없는 페이지입니다.")
                    return
                pix = document[requested_page - 1].get_pixmap(matrix=fitz.Matrix(1.65, 1.65), alpha=False)
                rendered_pages = {"pages": [{"page": requested_page, "image": base64.b64encode(pix.tobytes("png")).decode()}]}
        except (OSError, RuntimeError) as error:
            st.error(f"근거 페이지를 읽을 수 없습니다: {type(error).__name__}")
            return
    else:
        rendered_pages = _render_pdf_pages_for_viewer(
            str(pdf_path),
            _file_cache_signature(pdf_path),
        )
    if rendered_pages.get("pages"):
        st.markdown(
            """
            <style>
            .pdf-page-card {
                width: min(1180px, 100%);
                margin: 1rem auto 1.2rem;
                border: 1px solid #4c5152;
                border-radius: 12px;
                background: #f7f7f7;
                box-shadow: 0 16px 42px rgba(0,0,0,.26);
                overflow: hidden;
            }
            .pdf-page-label {
                padding: .55rem .8rem;
                color: #333;
                background: #e9ecec;
                border-bottom: 1px solid #d2d7d8;
                font-size: 1rem;
                font-weight: 900;
            }
            .pdf-page-card img {
                display: block;
                width: 100%;
                height: auto;
                background: #fff;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
        for page in rendered_pages["pages"]:
            st.markdown(
                f"""
                <div class="pdf-page-card">
                  <div class="pdf-page-label">Page {page["page"]}</div>
                  <img src="data:image/png;base64,{page["image"]}" alt="PDF page {page["page"]}" />
                </div>
                """,
                unsafe_allow_html=True,
            )
        if rendered_pages.get("truncated"):
            st.info(
                f"PDF가 길어서 앞 {rendered_pages['rendered']}페이지만 미리보기로 표시했습니다. 전체 파일은 위의 다운로드 버튼으로 확인해주세요."
            )
        return

    pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")
    pdf_b64_json = json.dumps(pdf_b64)
    pdf_name_json = json.dumps(pdf_path.name, ensure_ascii=False)
    components.html(
        f"""
        <!doctype html>
        <html>
        <head>
          <meta charset="utf-8" />
          <style>
            html, body {{ margin: 0; height: 100%; background: #f8fbff; }}
            .pdf-actions {{
              display: flex;
              justify-content: flex-end;
              gap: 10px;
              margin: 0 0 10px;
            }}
            .pdf-actions a {{
              display: inline-flex;
              align-items: center;
              justify-content: center;
              min-height: 38px;
              padding: 0 14px;
              border-radius: 8px;
              border: 1px solid #93c5fd;
              background: #eff6ff;
              color: #174e9b;
              font: 800 15px/1.2 'Malgun Gothic', sans-serif;
              text-decoration: none;
            }}
            .pdf-actions a:hover {{
              border-color: #55a6ff;
              background: #3d4648;
            }}
            iframe {{
              width: 100%;
              height: calc(100vh - 58px);
              border: 1px solid #4c5152;
              border-radius: 10px;
              background: #ffffff;
            }}
            #pdf-error {{
              display: none;
              height: calc(100vh - 58px);
              border: 1px solid #4c5152;
              border-radius: 10px;
              background: #f4f4f4;
              color: #333;
              place-items: center;
              text-align: center;
              font: 800 18px/1.45 'Malgun Gothic', sans-serif;
            }}
            body.pdf-error iframe {{ display: none; }}
            body.pdf-error #pdf-error {{ display: grid; }}
          </style>
        </head>
        <body>
          <div class="pdf-actions">
            <a id="open-pdf-link" href="#" target="_blank" rel="noopener">PDF 새 탭으로 열기</a>
          </div>
          <iframe id="pdf-frame" title="PDF preview"></iframe>
          <div id="pdf-error">
            PDF 미리보기를 불러오지 못했습니다.<br />
            위의 PDF 새 탭으로 열기 또는 다운로드 버튼을 사용해주세요.
          </div>
          <script>
            const PDF_B64 = {pdf_b64_json};
            const PDF_NAME = {pdf_name_json};

            function blobUrlFromBase64(base64) {{
              const binary = atob(base64);
              const chunkSize = 32768;
              const chunks = [];
              for (let offset = 0; offset < binary.length; offset += chunkSize) {{
                const slice = binary.slice(offset, offset + chunkSize);
                const bytes = new Uint8Array(slice.length);
                for (let i = 0; i < slice.length; i += 1) {{
                  bytes[i] = slice.charCodeAt(i);
                }}
                chunks.push(bytes);
              }}
              const blob = new Blob(chunks, {{ type: 'application/pdf' }});
              return URL.createObjectURL(blob);
            }}

            try {{
              const pdfUrl = blobUrlFromBase64(PDF_B64);
              const frame = document.getElementById('pdf-frame');
              const openLink = document.getElementById('open-pdf-link');
              frame.src = pdfUrl + '#toolbar=1&navpanes=0&view=FitH';
              openLink.href = pdfUrl;
              openLink.download = PDF_NAME;
              window.addEventListener('pagehide', () => URL.revokeObjectURL(pdfUrl), {{ once: true }});
            }} catch (error) {{
              document.body.classList.add('pdf-error');
            }}
          </script>
        </body>
        </html>
        """,
        height=860,
        scrolling=False,
    )


@st.cache_data(show_spinner=False)
def _render_pdf_pages_for_viewer(pdf_str: str, signature: tuple[int, int]) -> dict:
    del signature
    max_pages = int(os.getenv("SP_PDF_VIEWER_MAX_PAGES", "40"))
    zoom = float(os.getenv("SP_PDF_VIEWER_ZOOM", "1.65"))
    try:
        import fitz  # PyMuPDF
    except Exception:
        return {"pages": [], "rendered": 0, "truncated": False}

    pages: list[dict[str, object]] = []
    try:
        with fitz.open(pdf_str) as doc:
            total_pages = len(doc)
            limit = min(total_pages, max_pages)
            matrix = fitz.Matrix(zoom, zoom)
            for page_index in range(limit):
                page = doc.load_page(page_index)
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                image = base64.b64encode(pix.tobytes("png")).decode("ascii")
                pages.append({"page": page_index + 1, "image": image})
            return {
                "pages": pages,
                "rendered": limit,
                "truncated": total_pages > limit,
            }
    except Exception:
        return {"pages": [], "rendered": 0, "truncated": False}


def _static_pdf_url(path: Path) -> str:
    source = Path(path)
    if not source.exists():
        return ""
    try:
        stat = source.stat()
    except OSError:
        return ""
    digest_source = f"{source.resolve()}::{stat.st_mtime_ns}::{stat.st_size}".encode("utf-8", "ignore")
    digest = hashlib.sha1(digest_source).hexdigest()[:12]
    safe_name = re.sub(r"[^A-Za-z0-9가-힣._-]+", "_", source.name).strip("._") or "document.pdf"
    static_name = f"{digest}_{safe_name}"
    target = STATIC_PDF_DIR / static_name
    try:
        STATIC_PDF_DIR.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.stat().st_size != stat.st_size:
            shutil.copy2(source, target)
    except OSError:
        return ""
    return f"/app/static/pdf_view/{quote(static_name)}"


def _render_reference_panel(
    selected_doc: InboxDocument | None,
    refs: list[ReferenceDocument],
    gate: dict,
) -> None:
    if selected_doc:
        target = f"선택 제출문서: {selected_doc.company} · {selected_doc.product}"
    else:
        target = "전체 기준 SP 라이브러리"

    running_sim = bool(st.session_state.get("run_sim"))
    if running_sim:
        sim_product = str(st.session_state.get("sim_product", "") or "").strip()
        header_target = sim_product
    else:
        header_target = target

    panel_title = "AI 검수 진행 화면" if running_sim else "기준 SP 문서함"
    count_pill_html = "" if running_sim else f'<div class="count-pill">{len(refs)}개 매칭</div>'
    panel_marker_html = '<div class="simulation-fullscreen-marker"></div>' if running_sim else '<div class="reference-panel-marker"></div>'

    st.markdown(
        f"""
        {panel_marker_html}
        <div class="panel-title-row {'simulation-title-row' if running_sim else ''}">
          <div>
            <h2>{panel_title}</h2>
            <p>{_escape(header_target)}</p>
          </div>
          {count_pill_html}
        </div>
        """,
        unsafe_allow_html=True,
    )

    if running_sim:
        st.session_state.pop("sim_transition_started_at", None)
        notice_slot = st.empty()
        notice_slot.markdown(
            """
            <style>
            .sim-transition-notice {
                margin: 0 0 14px;
                padding: 14px 18px;
                border: 1px solid rgba(37,99,235,.36);
                border-radius: 14px;
                background: linear-gradient(90deg, rgba(239,246,255,.98), rgba(236,253,245,.94));
                color: #0b2f66;
                font-weight: 900;
                letter-spacing: 0;
                box-shadow: 0 8px 22px rgba(15,23,42,.07);
            }
            </style>
            <div class="sim-transition-notice">
              AI 검수 시뮬레이션과 판정을 준비하고 있습니다.
            </div>
            """,
            unsafe_allow_html=True,
        )
        _sim_pdf = st.session_state.get("sim_pdf", "")
        finish_delay = _render_inline_simulation(
            st.session_state.get("sim_product", ""),
            Path(_sim_pdf) if _sim_pdf else None,
        )
        notice_slot.empty()
        st.markdown('<div class="action-bar">', unsafe_allow_html=True)
        if _sim_pdf and finish_delay > 0:
            final_href = f"./?view=judge_final&pdf={quote(str(_sim_pdf), safe='')}"
            st.markdown(
                f"""
                <style>
                .final-judge-after-sim {{
                  display:flex;
                  align-items:center;
                  justify-content:center;
                  min-height:0;
                  border-radius:12px;
                  border:1px solid #059669;
                  background:linear-gradient(135deg,#10b981,#059669);
                  color:#ffffff !important;
                  font-size:1.06rem;
                  font-weight:950;
                  text-decoration:none !important;
                  box-shadow:0 10px 22px rgba(5,150,105,.22);
                  opacity:0;
                  max-height:0;
                  margin:0;
                  padding:0;
                  pointer-events:none;
                  overflow:hidden;
                  transform:translateY(4px);
                  transition:opacity .24s ease, transform .24s ease, max-height .24s ease, margin .24s ease, padding .24s ease, min-height .24s ease;
                }}
                .final-judge-after-sim.is-visible,
                body.sp-sim-finished .final-judge-after-sim {{
                  opacity:1 !important;
                  transform:translateY(0) !important;
                  max-height:64px !important;
                  min-height:2.85rem !important;
                  margin:.2rem 0 .55rem !important;
                  padding:.72rem 1rem !important;
                  pointer-events:auto !important;
                  overflow:visible !important;
                }}
                .final-judge-after-sim:hover {{
                  background:#047857;
                  color:#ffffff !important;
                }}
                </style>
                <a class="final-judge-after-sim is-visible" href="{final_href}" target="_self">최종 판정 페이지로 이동</a>
                """,
                unsafe_allow_html=True,
            )
        if st.button("← 기존 문서 목록으로", use_container_width=True):
            st.session_state["run_sim"] = False
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)
        return

    filter_state = _render_reference_filter_controls(selected_doc)
    display_refs = _filter_reference_documents(filter_state)
    gate_context = _is_gate_filter_context(selected_doc, filter_state)

    if selected_doc is None or gate_context or _can_review_with_linked_permit(selected_doc, gate):
        _render_gate_message(gate, selected_doc)
    else:
        _render_reference_browse_notice(selected_doc, filter_state)

    with st.container(height=REFERENCE_DOCUMENT_LIST_HEIGHT, border=False):
        if not display_refs:
            st.markdown(
                """
                <div class="empty-card">
                  <b>조회 조건에 맞는 기준 SP 문서가 없습니다.</b>
                  <span>회사명 또는 제품명을 다른 조건으로 선택해보세요.</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
        for ref in display_refs:
            _render_reference_card(ref, selected_doc, gate)

    st.markdown('<div class="action-bar">', unsafe_allow_html=True)
    can_continue = _can_continue_review(selected_doc, gate_context, gate)
    if st.button("검수 진행", type="primary", use_container_width=True, disabled=not can_continue):
        if selected_doc is None:
            st.warning("검수할 제출 SP 문서를 먼저 선택해야 합니다.")
            return

        selected_pdf_text = str(selected_doc.path)
        _load_judgement_bridge()["forget_failed_judgement_attempt"](selected_doc.path)
        forget_failed_simulation(selected_doc.path)

        st.session_state["run_sim"] = True
        st.session_state["sim_product"] = selected_doc.product
        st.session_state["sim_pdf"] = selected_pdf_text
        st.session_state["selected_pdf_path"] = selected_pdf_text
        st.session_state["judge_pdf_path"] = selected_pdf_text
        st.session_state["latest_judgement_pdf_path"] = selected_pdf_text
        st.session_state["sim_transition_started_at"] = time.time()

        # 버튼 클릭 직후 Gmail 자동 polling이 같이 돌면서 멈추는 현상 방지
        st.session_state["last_gmail_sync_at"] = time.time()

        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def _can_continue_review(doc, gate_context, gate) -> bool:
    """An SP without permit/reference can still run its own deterministic rules."""
    return bool(doc is not None and not getattr(doc, "permit_link_error", "") and (
        not getattr(doc, "permit_files", ())
        or gate_context
        or _can_review_with_linked_permit(doc, gate)
    ))


def _can_review_with_linked_permit(doc, gate) -> bool:
    """A product mismatch is a review finding, not a reason to block its review."""
    return bool(doc is not None and not getattr(doc, "permit_link_error", "") and gate.get("matched") is None
                and any(Path(path).is_file() for path in getattr(doc, "permit_files", ())))


def _is_gate_filter_context(selected_doc: InboxDocument | None, filter_state: dict[str, str]) -> bool:
    if selected_doc is None:
        return False
    return (
        filter_state.get("company") == selected_doc.company
        and filter_state.get("product") == selected_doc.product
    )


def _render_reference_browse_notice(selected_doc: InboxDocument, filter_state: dict[str, str]) -> None:
    product = filter_state.get("product") or ALL_PRODUCT_LABEL
    scope = (
        f"{filter_state.get('company', '')} · {product}"
        if product != ALL_PRODUCT_LABEL
        else f"{filter_state.get('company', '')} · 전체 제품"
    )
    st.markdown(
        f"""
        <div class="browse-banner">
          <b>기준 SP 라이브러리 조회 중</b>
          <span>{_escape(scope)} 기준문서를 보고 있습니다. 버전 매칭은 선택한 SP 문서와 같은 회사/제품에서만 표시됩니다.</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _clear_selected_doc_for_reference_browse() -> None:
    st.session_state.pop("selected_pdf_path", None)
    st.session_state["gate_error"] = ""


def _render_reference_filter_controls(selected_doc: InboxDocument | None) -> dict[str, str]:
    references = _all_references()
    companies = sorted({ref.company for ref in references})
    default_company = selected_doc.company if selected_doc and selected_doc.company in companies else companies[0]

    if st.session_state.get("reference_company_filter") not in companies:
        st.session_state["reference_company_filter"] = default_company

    company_col, product_col = st.columns([1, 1.15], gap="medium")
    with company_col:
        company = st.selectbox(
            "기준 SP 회사 선택",
            companies,
            key="reference_company_filter",
            on_change=_clear_selected_doc_for_reference_browse,
        )

    products = sorted({ref.product for ref in references if ref.company == company})
    product_options = [ALL_PRODUCT_LABEL] + products
    default_product = (
        selected_doc.product
        if selected_doc and selected_doc.company == company and selected_doc.product in products
        else ALL_PRODUCT_LABEL
    )
    if st.session_state.get("reference_product_filter") not in product_options:
        st.session_state["reference_product_filter"] = default_product

    with product_col:
        product = st.selectbox(
            "기준 SP 제품 선택",
            product_options,
            key="reference_product_filter",
            on_change=_clear_selected_doc_for_reference_browse,
        )

    return {"company": company, "product": product}


def _filter_reference_documents(filter_state: dict[str, str]) -> list[ReferenceDocument]:
    references = _all_references()
    if filter_state["product"] == ALL_PRODUCT_LABEL:
        return [ref for ref in references if ref.company == filter_state["company"]]
    return _references_for_company_product(filter_state["company"], filter_state["product"])


def _render_gate_message(gate: dict, selected_doc: InboxDocument | None) -> None:
    error = st.session_state.pop("gate_error", "")
    message = error or gate.get("reason", "")
    if selected_doc is None:
        status_class = "neutral"
        title = "문서 선택 대기"
    elif gate.get("ok"):
        status_class = "success"
        title = "버전 매칭 통과"
    else:
        status_class = "error"
        title = gate.get("status_label") or "기준 SP 버전 불일치"

    st.markdown(
        f"""
        <div class="gate-banner {status_class}">
          <b>{_escape(title)}</b>
          <span>{_escape(message)}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_reference_card(ref: ReferenceDocument, selected_doc: InboxDocument | None, gate: dict) -> None:
    is_selected_product = selected_doc and ref.company == selected_doc.company and ref.product == selected_doc.product
    is_exact = bool(
        is_selected_product
        and _normalize_version_for_compare(selected_doc.version) == TARGET_REFERENCE_VERSION
    )
    card_class = "reference-card exact" if is_exact else "reference-card"
    status_class = "active" if ref.status == "적용중" else "archive"
    version_class = "version-match" if is_exact else "version-pill"
    display_version = TARGET_REFERENCE_VERSION if is_selected_product else ref.version

    st.markdown(
        f"""
        <div class="{card_class}">
          <div class="ref-head">
            <div class="ref-identity">
              {_company_logo_html(ref.company, "ref-logo")}
              <div>
                <div class="ref-company">{_escape(ref.company)}</div>
                <div class="ref-title">{_escape(ref.title)}</div>
              </div>
            </div>
            <div class="{version_class}">{_escape(display_version)}</div>
          </div>
          <div class="ref-grid">
            <span>제품명</span><b>{_escape(ref.product)}</b>
            <span>문서 ID</span><b>{_escape(ref.doc_id)}</b>
            <span>적용일</span><b>{_escape(ref.effective_date)}</b>
            <span>상태</span><b class="{status_class}">{_escape(ref.status)}</b>
            <span>페이지/검사항목</span><b>{ref.pages}p · {ref.checks}개</b>
            <span>담당부서</span><b>{_escape(ref.owner)}</b>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _organization_logo_html(name: str, domain: str) -> str:
    logo_url = _logo_file_data_uri(ORG_LOGO_FILES.get(name, "")) or _favicon_url(domain)
    return (
        f'<div class="agency-logo" title="{_escape(name)}">'
        f'<img src="{_escape(logo_url)}" alt="{_escape(name)} 로고" />'
        "</div>"
    )


def _company_logo_html(company: str, class_name: str) -> str:
    local_logo = _local_logo_data_uri(company)
    domain = _company_logo_domain(company)
    initials = _company_initials(company)
    if local_logo:
        return (
            f'<div class="{class_name} image-logo" title="{_escape(company)}">'
            f'<img src="{_escape(local_logo)}" alt="{_escape(company)} 로고" />'
            f'<span>{_escape(initials)}</span>'
            "</div>"
        )
    if not domain:
        return f'<div class="{class_name} logo-fallback"><span>{_escape(initials)}</span></div>'
    logo_url = _favicon_url(domain)
    return (
        f'<div class="{class_name} image-logo" title="{_escape(company)}">'
        f'<img src="{_escape(logo_url)}" alt="{_escape(company)} 로고" />'
        f'<span>{_escape(initials)}</span>'
        "</div>"
    )


def _local_logo_data_uri(company: str) -> str:
    logo_path = find_company_logo(company)
    if logo_path is None:
        return ""
    if not logo_path.is_absolute():
        logo_path = Path(__file__).resolve().parent / logo_path
    return _path_to_data_uri(logo_path)


def _logo_file_data_uri(file_name: str) -> str:
    if not file_name:
        return ""
    return _path_to_data_uri(Path(__file__).resolve().parent / "company_logos" / file_name)


def _path_to_data_uri(logo_path: Path) -> str:
    if not logo_path.exists():
        return ""
    try:
        raw = logo_path.read_bytes()
    except OSError:
        return ""
    mime_type = mimetypes.guess_type(str(logo_path))[0] or "image/png"
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _company_logo_domain(company: str) -> str:
    if company in COMPANY_LOGO_DOMAINS:
        return COMPANY_LOGO_DOMAINS[company]
    normalized = _normalize_company_name(company)
    for key, domain in COMPANY_LOGO_DOMAINS.items():
        key_normalized = _normalize_company_name(key)
        if key_normalized and (key_normalized in normalized or normalized in key_normalized):
            return domain
    return ""


def _favicon_url(domain: str) -> str:
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=128"


def _company_initials(company: str) -> str:
    if company.startswith("SK"):
        return "SK"
    if company.startswith("GC"):
        return "GC"
    if company.startswith("LG"):
        return "LG"
    for token in ("한국", "일양", "유바", "파마", "동국", "셀트", "대웅", "한미"):
        if company.startswith(token):
            return token[:2]
    clean = re.sub(r"(주식회사|\(주\)|㈜)", "", company).strip()
    if clean:
        return clean[:2]
    return "SP"


def _normalize_company_name(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"(주식회사|\(주\)|㈜|co\.?|ltd\.?|inc\.?)", "", value, flags=re.IGNORECASE)
    return re.sub(r"[^0-9a-z가-힣]+", "", value)


def _escape(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _inject_styles(fit_first_page: bool = True) -> None:
    first_page_fit_css = """
        /* 첫 화면 1-page fit: 브라우저/Streamlit 전체 페이지 스크롤 제거 */
        html, body, [data-testid="stAppViewContainer"] {
            height: 100vh !important;
            min-height: 100vh !important;
            overflow: hidden !important;
        }
        [data-testid="stAppViewContainer"] > .main,
        section.main {
            height: 100vh !important;
            overflow: hidden !important;
        }
        .main .block-container,
        .block-container {
            height: 100vh !important;
            max-height: 100vh !important;
            overflow: hidden !important;
            padding: 0 .95rem 1.08rem !important;
            position: relative !important;
        }

        /* 화면 하단에도 상단처럼 구분 라인을 만들어 전체 UI가 박스 안에 들어온 것처럼 보이게 한다. */
        .main .block-container::after,
        .block-container::after {
            content: "";
            position: fixed;
            left: .95rem;
            right: .95rem;
            bottom: .62rem;
            height: 1px;
            border-radius: 999px;
            background: rgba(126, 137, 140, .58);
            box-shadow:
              0 -1px 0 rgba(255,255,255,.035),
              0 -10px 26px rgba(0,0,0,.16);
            pointer-events: none;
            z-index: 2;
        }

        /* 첫 화면 좌/우 패널 높이 통일 */
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.inbox-panel-marker),
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.reference-panel-marker) {
            height: calc(100vh - 124px) !important;
            max-height: calc(100vh - 124px) !important;
            overflow: hidden !important;
            margin-bottom: .55rem !important;
            border-bottom-color: rgba(126, 137, 140, .72) !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.inbox-panel-marker) > div,
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.reference-panel-marker) > div {
            height: 100% !important;
            overflow: hidden !important;
        }

        /* 검수 시뮬레이션은 단독 전체 폭/큰 화면으로 표시 */
        html:has(.simulation-fullscreen-marker),
        body:has(.simulation-fullscreen-marker),
        [data-testid="stAppViewContainer"]:has(.simulation-fullscreen-marker),
        [data-testid="stAppViewContainer"]:has(.simulation-fullscreen-marker) > .main,
        section.main:has(.simulation-fullscreen-marker),
        .main .block-container:has(.simulation-fullscreen-marker),
        .block-container:has(.simulation-fullscreen-marker) {
            height: auto !important;
            max-height: none !important;
            min-height: 100vh !important;
            overflow-y: auto !important;
        }
        .block-container:has(.simulation-fullscreen-marker) {
            padding-top: .55rem !important;
            padding-bottom: 1.15rem !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.simulation-fullscreen-marker) {
            height: auto !important;
            max-height: none !important;
            overflow: visible !important;
            margin-bottom: .45rem !important;
            border-bottom-color: rgba(126, 137, 140, .74) !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.simulation-fullscreen-marker) > div {
            height: auto !important;
            padding: .65rem .9rem .75rem !important;
            overflow: visible !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.simulation-fullscreen-marker) iframe {
            width: 100% !important;
            min-height: 1120px !important;
            border-radius: 16px !important;
        }
        .simulation-title-row {
            min-height: 64px !important;
            margin: -.15rem 0 .42rem !important;
            padding: .62rem 1rem !important;
        }
        .simulation-title-row h2 {
            font-size: 1.46rem !important;
            line-height: 1.15 !important;
        }
        .simulation-title-row p {
            font-size: .98rem !important;
            line-height: 1.22 !important;
            color: #c7d0d2 !important;
        }

        /* 상단 브랜드 박스 안쪽 중앙 정렬 */
        div[data-testid="stHorizontalBlock"]:has(.topbar-brand-block) {
            min-height: 88px !important;
            margin: 1.05rem 0 .9rem !important;
            padding: 0 1.1rem !important;
            align-items: center !important;
            overflow: hidden !important;
        }
        .topbar-brand-block {
            min-height: 88px !important;
            align-items: center !important;
        }
        .agency-logo {
            width: 104px !important;
            height: 54px !important;
        }
        .brand-title {
            font-size: 2.15rem !important;
        }

        /* 패널/카드 간격 압축 */
        div[data-testid="stVerticalBlock"] {
            gap: .46rem !important;
        }
        div[data-testid="stHorizontalBlock"] {
            gap: .8rem !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"] > div {
            padding: .78rem .9rem .85rem !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"] > div > div[data-testid="stVerticalBlock"] {
            gap: .42rem !important;
        }
        .panel-title-row {
            margin: 0 0 .55rem !important;
        }
        .panel-title-row h2 {
            font-size: 2.2rem !important;
            font-weight: 800 !important;
            letter-spacing: -0.5px;
        }
        }
        .panel-title-row p {
            margin-top: .22rem !important;
            font-size: 1.12rem !important;
        }
        .count-pill {
            padding: .32rem .62rem !important;
            font-size: 1.06rem !important;
        }

        /* 좌측 제출 문서 카드 압축 */
        .submitted-card {
            grid-template-columns: 68px 1fr !important;
            gap: .88rem !important;
            padding: .9rem !important;
            margin: .58rem 0 .34rem !important;
        }
        .submitted-main {
            min-width: 0;
        }
        .logo-token {
            width: 62px !important;
            height: 42px !important;
        }
        .doc-meta-line span,
        .ref-company {
            font-size: 1.28rem !important;
            color: #1f2937 !important;
            font-weight: 700 !important;

        }
        .doc-meta-line b {
            font-size: .96rem !important;
            padding: .2rem .48rem !important;
        }
        .doc-meta-line .doc-pill-row {
            gap: .24rem !important;
        }
        .doc-review-summary {
            grid-template-columns: repeat(auto-fit, minmax(118px, 1fr)) !important;
            gap: .34rem !important;
            margin-top: .46rem !important;
            padding: .52rem .56rem !important;
        }
        .doc-review-summary span {
            font-size: .9rem !important;
        }
        .doc-review-summary b {
            font-size: 1.16rem !important;
        }
        .submitted-title {
            font-size: 1.24rem !important;
            margin-top: .12rem !important;
        }
        .submitted-product {
            font-size: 1.14rem !important;
        }
        .field-grid,
        .ref-grid {
            margin-top: .45rem !important;
            row-gap: .22rem !important;
            font-size: 1.1rem !important;
        }

        /* 우측 기준 문서 카드 압축 */
        .reference-card {
            padding: 1.5rem !important;
            margin-bottom: 1rem !important;
            border-radius: 16px !important;

            background: #ffffff !important;
            border: 1px solid #d9e2ec !important;

            box-shadow: 0 2px 8px rgba(0,0,0,.04) !important;
        }
        .ref-logo {
            width: 68px !important;
            height: 46px !important;
        }
        .ref-title {
            font-size: 1.4rem !important;
            font-weight: 800 !important;
            line-height: 1.35 !important;
            color: #1f2937 !important;
        }
        }
        .version-pill,
        .version-match {
            min-width: 90px !important;
            padding: .45rem .8rem !important;
            font-size: 1.3rem !important;
            font-weight: 700 !important;
        }

        /* 입력/버튼/배너 압축 */
        .stSelectbox [data-baseweb="select"] > div,
        .stTextInput input,
        .stDateInput input {
            min-height: 44px !important;
        }
        .stButton > button,
        .file-open-link {
            min-height: 2.55rem !important;
            font-size: 1.08rem !important;
        }
        .gate-banner,
        .browse-banner {
            padding: .55rem .72rem !important;
            margin-bottom: .52rem !important;
        }
        .gate-banner b,
        .browse-banner b {
            font-size: 1.2rem !important;
        }
        .gate-banner span,
        .browse-banner span {
            font-size: 1.08rem !important;
        }
        .action-bar {
            margin-top: .52rem !important;
            padding-bottom: 0 !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.simulation-fullscreen-marker) .action-bar {
            margin-top: .55rem !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.simulation-fullscreen-marker) .stButton > button {
            min-height: 2.25rem !important;
            font-size: 1.05rem !important;
            border-radius: 10px !important;
        }

        /* 긴 문구는 높이를 늘리지 않고 말줄임 처리 */
        .submitted-title,
        .ref-title {
            display: -webkit-box !important;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden !important;
        }
        .doc-meta-line span,
        .submitted-product,
        .field-grid b,
        .ref-grid b {
            overflow: hidden !important;
            text-overflow: ellipsis !important;
        }
    """ if fit_first_page else ""

    st.markdown(
        """
        <style>
        :root {
            --bg: #f5f7fb;
            --top: #ffffff;
            --panel: #ffffff;
            --panel-2: #f8fafc;
            --card: #ffffff;
            --card-soft: #f8fafc;

            --line: #d9e2ec;
            --line-strong: #c5d0db;

            --text: #1f2937;
            --muted: #64748b;
            --muted-2: #94a3b8;

            --green: #16a34a;
            --blue: #2563eb;
            --orange: #ea580c;
            --yellow: #d97706;
            --red: #dc2626;
        }
        html, body, [data-testid="stAppViewContainer"] {
            background:
              radial-gradient(circle at 1px 1px, rgba(255,255,255,.22) 1px, transparent 0) 0 0 / 24px 24px,
              var(--bg);
            color: var(--text);
        }
        [data-testid="stHeader"] {
            background: transparent;
        }
        [data-testid="stToolbar"], [data-testid="stDecoration"] {
            display: none;
        }
        [data-testid="stSpinner"],
        [data-testid="stStatusWidget"] {
            display: none !important;
        }
        div[data-testid="stElementContainer"]:has(iframe[height="0"]) {
            display: none;
        }
        .block-container {
            max-width: none;
            padding: 0 1.35rem 1.35rem;
        }
        div[data-testid="stVerticalBlockBorderWrapper"] {
            border: 1px solid rgba(126, 137, 140, .42) !important;
            border-radius: 18px !important;
            background:
              linear-gradient(180deg, rgba(45, 50, 51, .96), rgba(35, 39, 40, .94)) !important;
            box-shadow:
              0 18px 48px rgba(0,0,0,.22),
              inset 0 1px 0 rgba(255,255,255,.04);
        }
        div[data-testid="stVerticalBlockBorderWrapper"] > div {
            padding: 1.08rem 1.15rem 1.2rem;
        }
        div[data-testid="stVerticalBlockBorderWrapper"] div[data-testid="stVerticalBlockBorderWrapper"] {
            background: rgba(43,48,49,.94) !important;
            box-shadow: none;
        }
        div[data-testid="stHorizontalBlock"]:has(.topbar-brand-block) {
            min-height: 88px;
            margin: 1.05rem 0 1rem;
            padding: 0 1.1rem;
            align-items: center;
            border-bottom: 1px solid #4d5354;
            background: var(--top);
            overflow: hidden;
        }
        .topbar-brand-block {
            display: flex;
            align-items: center;
            min-height: 88px;
        }
        .brand-wrap {
            display: flex;
            align-items: center;
            gap: 1rem;
        }
        .agency-logo-strip {
            display: flex;
            align-items: center;
            gap: .7rem;
        }
        .agency-logo {
            width: 118px;
            height: 66px;
            border-radius: 12px;
            display: grid;
            place-items: center;
            overflow: hidden;
            background: #f8fbfb;
            border: 1px solid rgba(255,255,255,.24);
            box-shadow: 0 8px 24px rgba(0,0,0,.18);
            padding: 5px 7px;
        }
        .agency-logo img {
            width: 100%;
            height: 100%;
            object-fit: contain;
        }
        .brand-title {
            font-size: 2.52rem;
            font-weight: 850;
            color: var(--text);
            line-height: 1.02;
        }
        .st-key-gmail_floating_panel {
            position: fixed !important;
            top: 104px !important;
            right: 1.35rem !important;
            width: min(560px, calc(100vw - 2.7rem)) !important;
            z-index: 1000 !important;
            background: #ffffff !important;
            border-radius: 16px !important;
            box-shadow: 0 26px 70px rgba(15,23,42,.20) !important;
        }
        .st-key-gmail_floating_panel > div,
        .st-key-gmail_floating_panel div[data-testid="stElementContainer"] {
            background: #ffffff !important;
            border-radius: 16px !important;
        }
        .st-key-gmail_floating_panel div[data-testid="stVerticalBlockBorderWrapper"] {
            background: #ffffff !important;
            border: 1px solid #d7e0ea !important;
            border-radius: 16px !important;
            box-shadow: none !important;
            max-height: calc(100vh - 128px);
            overflow-y: auto;
        }
        .st-key-gmail_floating_panel div[data-testid="stVerticalBlockBorderWrapper"] > div {
            padding: 1rem 1rem 1.05rem !important;
        }
        .st-key-gmail_floating_panel label {
            color: #1e293b !important;
        }
        .st-key-gmail_floating_panel .setting-copy,
        .st-key-gmail_floating_panel .gmail-meta,
        .st-key-gmail_floating_panel .gmail-status span:last-child {
            color: #64748b !important;
        }
        .top-actions {
            display: inline-flex;
            gap: .55rem;
            align-items: center;
            border: 1px solid var(--line-strong);
            border-radius: 8px;
            padding: .5rem .7rem;
            color: var(--muted);
            background: rgba(0,0,0,.12);
            font-size: 1.02rem;
        }
        .top-actions b {
            color: var(--green);
            font-size: .9rem;
            letter-spacing: .04em;
        }
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--green);
            box-shadow: 0 0 14px var(--green);
        }
        div[data-testid="stExpander"] {
            border: 1px solid var(--line) !important;
            background: rgba(48, 52, 53, .86) !important;
            border-radius: 10px !important;
            margin-bottom: .55rem;
        }
        div[data-testid="stExpander"] summary {
            color: var(--text) !important;
            font-weight: 800;
        }
        .setting-copy {
            color: var(--muted);
            margin-bottom: .8rem;
            font-size: 1.12rem;
        }
        .setting-copy b {
            color: var(--yellow);
        }
        .button-spacer {
            height: 1.68rem;
        }
        .gmail-meta {
            display: flex;
            flex-wrap: wrap;
            gap: .55rem;
            margin-top: .85rem;
            color: var(--muted);
            font-size: 1.02rem;
        }
        .gmail-meta span {
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: .25rem .55rem;
            background: rgba(0,0,0,.12);
        }
        .gmail-status {
            display: flex;
            align-items: center;
            gap: .62rem;
            min-height: 42px;
            padding: .62rem .9rem;
            border-radius: 12px;
            border: 1px solid rgba(126,137,140,.42);
            background: linear-gradient(180deg,rgba(45,50,51,.96),rgba(35,39,40,.94));
            box-shadow: inset 0 1px 0 rgba(255,255,255,.04);
        }
        .gmail-status-dot {
            width: 9px;
            height: 9px;
            border-radius: 50%;
            flex: 0 0 auto;
            background: var(--muted-2);
        }
        .gmail-status b {
            color: var(--text);
            font-size: 1.16rem;
            white-space: nowrap;
        }
        .gmail-status span:last-child {
            color: var(--muted);
            font-size: 1.08rem;
            overflow-wrap: anywhere;
        }
        .gmail-status.connected .gmail-status-dot {
            background: var(--green);
            box-shadow: 0 0 10px var(--green);
        }
        .gmail-status.offline .gmail-status-dot {
            background: #f59e0b;
            box-shadow: 0 0 10px rgba(245,158,11,.55);
        }
        .gmail-status.pending .gmail-status-dot {
            background: var(--yellow);
            box-shadow: 0 0 10px rgba(255,200,87,.55);
        }
        .gmail-status.error .gmail-status-dot {
            background: var(--orange);
            box-shadow: 0 0 10px rgba(255,122,99,.65);
        }
        label, .stTextInput label, .stSelectbox label, .stDateInput label {
            color: #d6dddd !important;
            font-size: 1.08rem !important;
        }
        .stTextInput,
        .stTextInput *,
        .stDateInput,
        .stDateInput *,
        .stSelectbox,
        .stSelectbox * {
            pointer-events: auto !important;
        }
        .stTextInput div[data-baseweb="input"],
        .stDateInput div[data-baseweb="input"] {
            min-height: 48px !important;
            cursor: text !important;
            display: flex !important;
            align-items: center !important;
        }
        .stTextInput input, .stDateInput input {
            background: #242828 !important;
            color: var(--text) !important;
            border: 1px solid var(--line) !important;
            border-radius: 8px !important;
            min-height: 48px !important;
            height: 48px !important;
            font-size: 1.1rem !important;
            line-height: 1.25 !important;
            padding: .55rem .7rem !important;
            cursor: text !important;
        }
        .stSelectbox [data-baseweb="select"] > div {
            background: linear-gradient(135deg, #ffffff, #f8fbff) !important;
            color: #1e293b !important;
            border-color: #cbd9e8 !important;
            border-radius: 8px !important;
            min-height: 48px !important;
            font-size: 1.08rem !important;
            box-shadow: 0 2px 7px rgba(15,23,42,.04) !important;
        }
        .stSelectbox [data-baseweb="select"] > div:hover,
        .stSelectbox [data-baseweb="select"] > div:focus-within {
            border-color: #3b82f6 !important;
            box-shadow: 0 0 0 3px rgba(59,130,246,.12) !important;
        }
        .stSelectbox [data-baseweb="select"] svg {
            fill: #2563eb !important;
        }
        /* 밝은 첫 화면에서는 기준 SP 선택 라벨을 선명하게 표시한다. */
        .stSelectbox label,
        .stSelectbox label p,
        .stSelectbox [data-testid="stWidgetLabel"] p {
            color: #172554 !important;
            font-weight: 800 !important;
            opacity: 1 !important;
        }
        .reference-panel-marker + .panel-title-row p {
            color: #1e293b !important;
            font-weight: 650 !important;
            opacity: 1 !important;
        }
        /* Gmail 설정은 밝은 패널 안에서 항상 읽을 수 있게 별도 톤을 사용한다. */
        .st-key-gmail_floating_panel .stTextInput input,
        .st-key-gmail_floating_panel .stDateInput input,
        .st-key-gmail_floating_panel .stTextInput div[data-baseweb="input"],
        .st-key-gmail_floating_panel .stDateInput div[data-baseweb="input"] {
            background: #ffffff !important;
            color: #0f172a !important;
            border-color: #b8c7d9 !important;
        }
        .st-key-gmail_floating_panel .stTextInput input::placeholder,
        .st-key-gmail_floating_panel .stDateInput input::placeholder {
            color: #94a3b8 !important;
            opacity: 1 !important;
        }
        .st-key-gmail_floating_panel .stTextInput input:focus,
        .st-key-gmail_floating_panel .stDateInput input:focus {
            border-color: #2563eb !important;
            box-shadow: 0 0 0 3px rgba(37,99,235,.12) !important;
        }
        .st-key-gmail_floating_panel .gmail-status {
            background: #f8fafc !important;
            border-color: #d7e0ea !important;
            box-shadow: none !important;
        }
        .st-key-gmail_floating_panel .gmail-status b {
            color: #1e293b !important;
        }
        .st-key-gmail_floating_panel .gmail-status.offline {
            background: #fff7ed !important;
            border-color: #fed7aa !important;
        }
        .st-key-gmail_floating_panel .gmail-status.offline b {
            color: #9a3412 !important;
        }
        .st-key-gmail_floating_panel .setting-copy {
            padding: .7rem .8rem;
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 9px;
            line-height: 1.55;
        }
        .st-key-gmail_floating_panel .setting-copy b {
            color: #2563eb !important;
        }
        .st-key-gmail-disconnect > button {
            background: #ffffff !important;
            color: #b42318 !important;
            border-color: #fca5a5 !important;
        }
        .st-key-gmail-disconnect > button:not(:disabled):hover {
            background: #fff1f2 !important;
            border-color: #ef4444 !important;
            color: #991b1b !important;
        }
        .stButton > button {
            border-radius: 10px;
            border: 1px solid #d9e2ec;
            background: #ffffff;
            color: #1f2937;
            min-height: 2.8rem;
            font-weight: 700;
            font-size: 1.05rem;
            line-height: 1.16;
            white-space: normal;
            padding: .5rem .8rem;
        }
        .st-key-gmail-settings-toggle,
        .st-key-gmail-settings-toggle > div,
        .st-key-gmail-settings-toggle div[data-testid="stButton"],
        .st-key-gmail-settings-toggle button {
            width: 100% !important;
            height: 100% !important;
            position: relative !important;
            z-index: 50 !important;
            pointer-events: auto !important;
            cursor: pointer !important;
        }
        div[data-testid="stColumn"]:has(.st-key-gmail-settings-toggle) {
            position: relative !important;
            z-index: 2000 !important;
            pointer-events: auto !important;
        }
        .st-key-gmail-settings-toggle {
            position: relative !important;
            min-height: 3rem !important;
            margin-top: 0 !important;
        }
        .st-key-gmail-settings-toggle div[data-testid="stButton"] {
            position: absolute !important;
            top: -.45rem !important;
            right: 0 !important;
            bottom: 0 !important;
            left: 0 !important;
        }
        .st-key-gmail-settings-toggle button {
            position: absolute !important;
            inset: 0 !important;
            max-width: none !important;
        }
        /* 브랜드 영역은 장식 전용이다. Gmail 버튼 위의 클릭을 가로막지 않게 한다. */
        .topbar-brand-block,
        .topbar-brand-block * {
            pointer-events: none !important;
        }
        .st-key-gmail-settings-toggle div[data-testid="stButton"] {
            position: relative !important;
            inset: auto !important;
        }
        .st-key-gmail-settings-toggle button {
            position: relative !important;
            inset: auto !important;
            width: 100% !important;
            height: 3rem !important;
        }
        .st-key-gmail-settings-toggle button {
            min-height: 3rem !important;
            display: flex !important;
            align-items: center !important;
            justify-content: center !important;
        }
        .st-key-gmail-settings-toggle button * {
            pointer-events: none !important;
        }
        .stButton > button:hover {
            background: #f8fafc !important;
            border-color: #2563eb !important;
            color: #2563eb !important;
        }
        .stButton > button[kind="primary"] {
            background: var(--orange);
            border-color: var(--orange);
            color: white;
        }
        .stButton > button:disabled {
            background: #2f3334;
            color: #788386;
            border-color: #464b4c;
        }
        .workflow-shell {
            display: grid;
            grid-template-columns: minmax(230px, 1fr) 68px minmax(230px, 1fr) 68px minmax(230px, 1fr);
            gap: 0;
            align-items: center;
            margin: .35rem 0 1rem;
        }
        .workflow-node {
            min-height: 86px;
            display: flex;
            align-items: center;
            gap: .82rem;
            padding: .95rem;
            background: rgba(42, 47, 48, .94);
            border: 1px solid var(--line);
            border-radius: 12px;
            box-shadow: 0 10px 34px rgba(0,0,0,.18);
        }
        .workflow-node.ok {
            border-color: rgba(55,217,158,.72);
            box-shadow: 0 0 0 1px rgba(55,217,158,.08), 0 0 34px rgba(55,217,158,.12);
        }
        .workflow-node.blocked {
            border-color: rgba(255,122,99,.72);
        }
        .workflow-node b {
            display: block;
            color: var(--text);
            font-size: 1.16rem;
        }
        .workflow-node span {
            display: block;
            color: var(--muted);
            font-size: 1.02rem;
            margin-top: .15rem;
        }
        .node-icon {
            width: 46px;
            height: 46px;
            border-radius: 10px;
            display: grid;
            place-items: center;
            color: #ffffff;
            font-size: .9rem;
            font-weight: 900;
            border: 1px solid var(--line-strong);
            background: #3b4142;
        }
        .workflow-line {
            height: 2px;
            background: linear-gradient(90deg, var(--line), var(--line-strong), var(--line));
        }
        .panel-title-row {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 1rem;
            margin: 0 0 1rem;
        }
        .panel-title-row h2 {
            margin: .1rem 0 0;
            color: var(--text);
            font-size: 1.82rem;
            line-height: 1.15;
        }
        .panel-title-row p {
            margin: .35rem 0 0;
            color: var(--muted);
            font-size: 1.12rem;
        }
        .panel-kicker {
            color: var(--green);
            font-size: .96rem;
            font-weight: 900;
            letter-spacing: .04em;
        }
        .count-pill {
            color: var(--muted);
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: .32rem .58rem;
            font-size: 1.12rem;
            background: rgba(0,0,0,.14);
            white-space: nowrap;
        }
        .submitted-card,
        .reference-card,
        .empty-card,
        .gate-banner {
            border: 1px solid var(--line);
            background: rgba(43,48,49,.94);
            border-radius: 12px;
            box-shadow: 0 12px 34px rgba(0,0,0,.16);
        }
        .submitted-card {
            display: grid;
            grid-template-columns: 76px 1fr;
            gap: 1.08rem;
            padding: 1.08rem;
            margin: .75rem 0 .38rem;
            background: linear-gradient(145deg, #ffffff, #f7fbff) !important;
            border-color: #cfddea !important;
            box-shadow: 0 8px 22px rgba(30,64,175,.08) !important;
        }
        .doc-actions-marker {
            height: .65rem;
        }
        div[data-testid="stElementContainer"]:has(.doc-actions-marker) {
            margin: 0 !important;
        }
        .submitted-card.selected {
            border-color: #3b82f6 !important;
            box-shadow: 0 0 0 2px rgba(59,130,246,.14), 0 12px 28px rgba(37,99,235,.14) !important;
        }
        .logo-token {
            width: 68px;
            height: 46px;
            border-radius: 8px;
            display: grid;
            place-items: center;
            color: var(--text);
            border: 1px solid #aab2b5;
            background: #42494b;
            font-size: .92rem;
            font-weight: 900;
            overflow: hidden;
            position: relative;
        }
        .logo-token.image-logo {
            background: #f8fbfb;
            padding: 3px 5px;
        }
        .logo-token img {
            width: 100%;
            height: 100%;
            object-fit: contain;
            z-index: 1;
        }
        .logo-token.image-logo span {
            display: none;
        }
        .logo-token.logo-fallback span {
            display: grid;
            place-items: center;
            width: 100%;
            height: 100%;
        }
        .ref-identity {
            display: flex;
            align-items: flex-start;
            gap: .85rem;
            min-width: 0;
        }
        .ref-logo {
            flex: 0 0 auto;
            width: 76px;
            height: 50px;
            border-radius: 9px;
            display: grid;
            place-items: center;
            overflow: hidden;
            background: #f8fbfb;
            border: 1px solid #aab2b5;
            color: #283030;
            font-size: .78rem;
            font-weight: 900;
            padding: 4px 6px;
        }
        .ref-logo img {
            width: 100%;
            height: 100%;
            object-fit: contain;
        }
        .ref-logo.image-logo span {
            display: none;
        }
        .doc-meta-line,
        .ref-head {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: .7rem;
        }
        .doc-meta-line span,
        .ref-company {
            color: #1e3a5f;
            font-weight: 850;
            font-size: 1.3rem;
        }
        .doc-meta-line b {
            border-radius: 999px;
            padding: .22rem .52rem;
            font-size: 1.02rem;
            white-space: nowrap;
        }
        .doc-meta-line b.match {
            color: var(--green);
            background: rgba(55,217,158,.12);
        }
        .doc-meta-line b.mismatch {
            color: var(--orange);
            background: rgba(255,122,99,.13);
        }
        .doc-meta-line .doc-pill-row {
            display: inline-flex;
            align-items: center;
            justify-content: flex-end;
            gap: .35rem;
            flex-wrap: wrap;
            color: inherit;
            font-size: inherit;
        }
        .doc-meta-line b.review-status.completed {
            color: var(--green);
            background: rgba(55,217,158,.16);
        }
        .doc-meta-line b.review-status.running {
            color: var(--yellow);
            background: rgba(255,200,87,.16);
        }
        .doc-meta-line b.review-status.pending {
            color: #475569;
            background: #e2e8f0;
        }
        .doc-review-summary {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(126px, 1fr));
            gap: .5rem;
            margin-top: .82rem;
            padding: .62rem;
            border: 1px solid #dbe4ee;
            border-radius: 8px;
            background: #f8fafc;
            box-shadow: none !important;
        }
        .doc-review-summary div {
            display: grid;
            grid-template-columns: auto 1fr;
            grid-template-areas:
                "dot label"
                "dot value";
            align-items: center;
            column-gap: .35rem;
            min-width: 0;
            padding: .45rem .48rem;
            border-radius: 8px;
            background: #ffffff;
            border: 1px solid #e5edf5;
        }
        .doc-review-summary .dot {
            grid-area: dot;
            width: .62rem;
            height: .62rem;
            border-radius: 999px;
        }
        .doc-review-summary .dot.pass { background: var(--green); }
        .doc-review-summary .dot.fail { background: var(--red); }
        .doc-review-summary .dot.hold { background: var(--yellow); }
        .doc-review-summary span {
            grid-area: label;
            color: #64748b;
            font-size: .96rem;
            font-weight: 800;
            white-space: nowrap;
        }
        .doc-review-summary b {
            grid-area: value;
            color: #1e293b;
            font-size: 1.24rem;
            font-weight: 900;
            line-height: 1.1;
        }
        .doc-review-summary .total {
            background: #eff6ff;
            border-color: #bfdbfe;
        }
        .submitted-title {
            color: #1e293b !important;
            font-size: 1.38rem;
            font-weight: 850;
            line-height: 1.3;
            margin-top: .25rem;
        }
        .submitted-product {
            color: var(--blue);
            font-size: 1.24rem;
            font-weight: 750;
            margin-top: .12rem;
        }
        .file-open-link {
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 2.35rem;
            width: 100%;
            border-radius: 8px;
            border: 1px solid #93c5fd;
            background: linear-gradient(135deg, #eff6ff, #dbeafe);
            color: #1d4ed8 !important;
            text-decoration: none !important;
            font-weight: 800;
            font-size: 1.1rem;
            margin-bottom: .25rem;
            line-height: 1.16;
            overflow-wrap: anywhere;
            padding: .35rem .45rem;
        }
        .file-open-link:hover {
            border-color: #2563eb;
            background: #2563eb;
            color: #fff !important;
        }
        .file-open-link.disabled {
            color: #94a3b8 !important;
            background: #f1f5f9;
            border-color: #d7e0ea;
        }
        .field-grid,
        .ref-grid {
            display: grid;
            grid-template-columns: 8.2rem 1fr;
            column-gap: .65rem;
            row-gap: .34rem;
            margin-top: .85rem;
            color: #64748b;
            font-size: 1.16rem;
        }
        .field-grid b,
        .ref-grid b {
            color: #1e293b;
            font-weight: 750;
            overflow-wrap: anywhere;
        }
        .reference-card {
            padding: 1.15rem;
            margin-bottom: .75rem;
        }
        .reference-card.exact {
            border-color: var(--green);
            box-shadow: 0 0 0 1px rgba(55,217,158,.12), 0 0 36px rgba(55,217,158,.1);
        }
        .ref-title {
            color: var(--muted);
            font-size: 1.26rem;
            line-height: 1.35;
            margin-top: .2rem;
        }
        .version-pill,
        .version-match {
            min-width: 80px;
            text-align: center;
            border-radius: 999px;
            padding: .42rem .74rem;
            font-size: 1.24rem;
            font-weight: 900;
        }
        .version-pill {
            color: var(--text);
            background: #3e4445;
            border: 1px solid var(--line-strong);
        }
        .version-match {
            color: #06291d;
            background: var(--green);
        }
        .active {
            color: var(--green) !important;
        }
        .archive {
            color: var(--muted) !important;
        }
        .gate-banner {
            padding: .85rem 1rem;
            margin-bottom: .85rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
        }
        .gate-banner b {
            font-size: 1.28rem;
        }
        .gate-banner span {
            color: var(--muted);
            font-size: 1.14rem;
            text-align: right;
        }
        .gate-banner.success {
            border-color: rgba(55,217,158,.75);
            background: rgba(55,217,158,.1);
        }
        .gate-banner.error {
            border-color: rgba(255,122,99,.75);
            background: rgba(255,122,99,.1);
        }
        .gate-banner.neutral {
            background: rgba(0,0,0,.12);
        }
        .browse-banner {
            padding: .85rem 1rem;
            margin-bottom: .85rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            border: 1px solid var(--line);
            background: rgba(0,0,0,.14);
            border-radius: 12px;
            color: var(--muted);
        }
        .browse-banner b {
            color: var(--text);
            font-size: 1.22rem;
            white-space: nowrap;
        }
        .browse-banner span {
            text-align: right;
            font-size: 1.12rem;
        }
        .empty-card {
            padding: 1.05rem;
            display: grid;
            gap: .35rem;
            background: #ffffff !important;
            border: 1px solid #d7e0ea !important;
            box-shadow: 0 4px 14px rgba(15,23,42,.06) !important;
        }
        .empty-card b {
            color: #1e293b !important;
        }
        .empty-card span {
            color: #64748b !important;
            font-size: 1.08rem;
            line-height: 1.55;
        }
        /* 제출 문서함이 비어 있을 때도 안내문은 밝고 선명하게 표시한다. */
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.inbox-panel-marker) .empty-card {
            background: #ffffff !important;
            border-color: #d7e0ea !important;
            box-shadow: 0 4px 14px rgba(15,23,42,.06);
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.inbox-panel-marker) .empty-card b {
            color: #1e293b !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.inbox-panel-marker) .empty-card span {
            color: #64748b !important;
            line-height: 1.55;
        }
        .action-bar {
            margin-top: 1rem;
            padding-bottom: .1rem;
        }
        .stAlert {
            background: #ffffff !important;
            color: #1e293b !important;
            border: 1px solid #d7e0ea !important;
        }
        /* 첫 진입 화면: 참고 시안과 같은 밝은 문서 접수/선택 대시보드 */
        div[data-testid="stHorizontalBlock"]:has(.topbar-brand-block) {
            background: linear-gradient(100deg, #062761, #0b4b9f) !important;
            border-bottom: 0 !important;
            box-shadow: 0 4px 18px rgba(6,39,97,.20);
        }
        .brand-title { color: #ffffff !important; font-size: 2.15rem !important; }
        .app-nav { display:flex; align-items:stretch; align-self:stretch; margin-left:auto; gap:.1rem; }
        .app-nav span { display:flex; align-items:center; padding:0 .82rem; color:rgba(255,255,255,.76); font-size:.91rem; font-weight:800; white-space:nowrap; border-bottom:3px solid transparent; }
        .app-nav span.active { color:#ffffff; border-bottom-color:#86c6ff; background:rgba(255,255,255,.08); }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.inbox-panel-marker),
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.reference-panel-marker) {
            background:#ffffff !important;
            border-color:#d7e0ea !important;
            box-shadow:0 5px 20px rgba(15,23,42,.06) !important;
        }
        .panel-title-row h2 {
            color:#173f75 !important;
            text-shadow:none !important;
        }
        .panel-title-row p {
            color:#475569 !important;
            font-weight:750 !important;
        }
        .count-pill { color:#42678e !important; border-color:#d7e0ea !important; background:#f1f5f9 !important; }
        .reference-card { background:linear-gradient(145deg,#ffffff,#f8fbff) !important; border-color:#d7e0ea !important; box-shadow:0 5px 16px rgba(15,23,42,.05) !important; }
        .ref-title { color:#334155 !important; }
        .version-pill { color:#1e3a5f !important; background:#eef5ff !important; border-color:#b9d7f6 !important; }
        .gate-banner.neutral { background:#f1f5f9 !important; border-color:#d7e0ea !important; }
        .gate-banner b { color:#0f172a; }
        .gate-banner span, .browse-banner span { color:#334155 !important; }
        .gate-banner.success {
            background:linear-gradient(90deg,#ecfdf5,#eefaf4) !important;
            border-color:#86efac !important;
        }
        .gate-banner.success b {
            color:#166534 !important;
            text-shadow:none !important;
        }
        .gate-banner.success span {
            color:#166534 !important;
            font-weight:800 !important;
        }
        .browse-banner { background:#f8fbff !important; border-color:#d7e0ea !important; }
        .browse-banner b { color:#1e3a5f !important; }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.reference-panel-marker) .stSelectbox label,
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.reference-panel-marker) .stSelectbox label p,
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.reference-panel-marker) [data-testid="stWidgetLabel"] p {
            color:#174e9b !important;
            font-weight:850 !important;
            text-shadow:none !important;
        }
        .stButton > button[kind="primary"] { background:#1677e8 !important; border-color:#1677e8 !important; color:#ffffff !important; box-shadow:0 5px 13px rgba(22,119,232,.20); }
        .stButton > button[kind="primary"]:hover { background:#0f62c7 !important; border-color:#0f62c7 !important; }
        div[data-testid="stVerticalBlockBorderWrapper"]:not(:has(.simulation-fullscreen-marker)) {
            background:#ffffff !important;
            border-color:#d7e0ea !important;
            box-shadow:0 8px 24px rgba(15,23,42,.07) !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:not(:has(.simulation-fullscreen-marker)) > div {
            background:transparent !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:not(:has(.simulation-fullscreen-marker)) div[data-testid="stVerticalBlockBorderWrapper"] {
            background:#ffffff !important;
            border-color:#d7e0ea !important;
            box-shadow:none !important;
        }
        /* AI 검수 진행: 실제 Start/End 워크플로는 유지하고 작업 맥락만 강화한다. */
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.simulation-fullscreen-marker) {
            background:linear-gradient(180deg,#f8fbff 0%,#eef5ff 100%) !important;
            border-color:#c8d9ec !important;
            box-shadow:0 12px 30px rgba(15,58,122,.10) !important;
        }
        .simulation-title-row { position:relative; overflow:hidden; align-items:center !important; min-height:64px !important; margin:-.15rem 0 .42rem !important; padding:.62rem 1rem !important; border:1px solid #164986 !important; border-radius:14px; background:linear-gradient(112deg,#051f4b 0%,#0a4e9d 62%,#147bd9 100%); box-shadow:0 8px 18px rgba(10,62,139,.20); }
        .simulation-title-row:after { content:""; position:absolute; right:-38px; top:-88px; width:178px; height:178px; border:28px solid rgba(255,255,255,.10); border-radius:50%; }
        .simulation-title-row h2 { position:relative; z-index:1; color:#ffffff !important; font-size:1.46rem !important; line-height:1.15 !important; letter-spacing:0; text-shadow:0 2px 10px rgba(0,0,0,.18); }
        .simulation-title-row p { position:relative; z-index:1; color:#bfe1ff !important; font-size:.98rem !important; line-height:1.22 !important; font-weight:700; }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.simulation-fullscreen-marker) iframe {
            border:2px solid #173f75 !important;
            box-shadow:0 14px 30px rgba(5,25,57,.26), 0 0 0 4px rgba(36,121,220,.10) !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.simulation-fullscreen-marker) .action-bar .stButton > button {
            background:linear-gradient(135deg,#eff6ff,#dbeafe) !important;
            border-color:#93c5fd !important;
            color:#174e9b !important;
            box-shadow:none !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.simulation-fullscreen-marker) .action-bar .stButton > button:hover {
            background:#2563eb !important;
            border-color:#2563eb !important;
            color:#ffffff !important;
        }
        @media (max-width: 1200px) {
            .workflow-shell {
                grid-template-columns: 1fr;
                gap: .6rem;
            }
            .workflow-line {
                display: none;
            }
        }
        div[data-testid="stHorizontalBlock"]:has(.topbar-brand-block) {
            min-height: 88px !important;
            margin: 1.05rem 0 .95rem !important;
            padding: 0 1.1rem !important;
            align-items: center !important;
            border: 1px solid #dbe3ea !important;
            border-radius: 14px !important;
            background: #ffffff !important;
            box-shadow: 0 8px 24px rgba(15,23,42,.08) !important;
            overflow: hidden !important;
        }
        .topbar-brand-block {
            display: flex !important;
            align-items: center !important;
            min-height: 88px !important;
            transform: translateY(-4px) !important;
        }
        .brand-wrap {
            display: flex !important;
            align-items: center !important;
            gap: .75rem !important;
            transform: none !important;
        }
        .agency-logo-strip {
            display: flex !important;
            align-items: center !important;
            gap: .55rem !important;
        }
        .agency-logo {
            width: 104px !important;
            height: 52px !important;
            border-radius: 10px !important;
            box-shadow: 0 5px 14px rgba(15,23,42,.08) !important;
        }
        .topbar-brand-block .brand-title,
        .brand-title {
            color:#0b2f66 !important;
            text-shadow:none !important;
            display:inline-flex !important;
            align-items:center !important;
            min-height:2.75rem !important;
            padding:0 !important;
            border-radius:0 !important;
            background:transparent !important;
            box-shadow:none !important;
        }
        /* The existing dashboard is a light UI. Keep native widgets readable
           even when a browser session retained Streamlit's dark preference. */
        html, body, .stApp, [data-testid="stAppViewContainer"] {
            color-scheme:light !important;
            background-color:#f5f7fb !important;
            color:#1f2937 !important;
        }
        [data-testid="stWidgetLabel"], [data-testid="stWidgetLabel"] p,
        [data-testid="stCaptionContainer"] { color:#475569 !important; }
        .stButton > button:not([kind="primary"]) {
            background:#ffffff !important; color:#1f2937 !important; border-color:#cbd5e1 !important;
        }
        .stButton > button:not([kind="primary"]) p { color:inherit !important; }
        .stButton > button:disabled { color:#64748b !important; background:#f1f5f9 !important; }
        .stSelectbox [data-baseweb="select"] > div,
        .stMultiSelect [data-baseweb="select"] > div,
        [data-baseweb="input"], [data-baseweb="input"] input,
        [data-baseweb="textarea"], [data-baseweb="textarea"] textarea,
        [data-testid="stFileUploaderDropzone"] {
            background:#f8fafc !important; color:#1f2937 !important;
        }
        [data-baseweb="popover"], [data-baseweb="popover"] ul,
        [data-baseweb="popover"] li { background:#ffffff !important; color:#1f2937 !important; }
        [data-testid="stDialog"] [role="dialog"] { background:#ffffff !important; color:#1f2937 !important; }
        .gate-banner.error { background:#fff1f2 !important; border-color:#fda4af !important; }
        .gate-banner.error b, .gate-banner.error span { color:#9f1239 !important; }
        .count-help { color:#475569; background:#fff; padding:6px 10px; font-size:.82rem; line-height:1.6; }
        .count-help summary { cursor:pointer; }
        __FIRST_PAGE_FIT_CSS__
        </style>
        """.replace("__FIRST_PAGE_FIT_CSS__", first_page_fit_css),
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
