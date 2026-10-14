# -*- coding: utf-8 -*-
"""2페이지 검수 시뮬레이션 단독 미리보기.

1페이지(버전 매칭)를 거치지 않고 검수 시뮬레이션 화면만 바로 띄운다.

실행:
    streamlit run sp_sim2_preview.py
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from sp_flowchart_data import load_simulation_graph, simulation_graph_to_json, simulation_structure_notice
from sp_sim2_viewer import build_simulation_html

st.set_page_config(page_title="검수 시뮬레이션 미리보기", layout="wide", initial_sidebar_state="collapsed")

# 여백 최소화 (시뮬레이션이 화면을 꽉 채우도록)
st.markdown(
    """
    <style>
      header[data-testid="stHeader"]{display:none}
      .block-container{padding:0.4rem 0.6rem 0;max-width:100%}
      [data-testid="stToolbar"]{display:none}
    </style>
    """,
    unsafe_allow_html=True,
)


def _csv_dirs() -> list[Path]:
    base = Path(__file__).parent
    return sorted(d for d in base.iterdir() if d.is_dir() and d.name.endswith("_csv"))


def main() -> None:
    dirs = _csv_dirs()
    if not dirs:
        st.error("제조요약도 *_csv 폴더를 찾을 수 없습니다.")
        return

    labels = [d.name.replace("_제조요약도_csv", "").replace("_csv", "") for d in dirs]
    if len(dirs) > 1:
        choice = st.selectbox("미리볼 제품 (CSV 폴더)", range(len(dirs)), format_func=lambda i: labels[i])
    else:
        choice = 0

    csv_dir = dirs[choice]
    pdf_path = _match_pdf(labels[choice])
    if pdf_path:
        st.caption(f"제조 요약도 추출 대상: {pdf_path.name}")
    try:
        graph, after = load_simulation_graph(csv_dir, pdf_path)
    except Exception as exc:
        st.error(f"제조요약도 데이터를 읽을 수 없습니다: {exc}")
        return
    if not graph.nodes:
        st.warning(f"{csv_dir.name} 에서 제조 단계를 찾을 수 없습니다.")
        return

    graph_json = simulation_graph_to_json(graph, after)
    notice = simulation_structure_notice(graph_json)
    if notice:
        st.warning(notice)
    html = build_simulation_html(graph_json, graph.product_name or labels[choice])
    components.html(html, height=900, scrolling=False)


def _match_pdf(product_label: str):
    import re
    base = Path(__file__).parent
    norm = re.sub(r"[\s_\-./()]+", "", product_label.lower())
    pdfs = list((base / "incoming_sp_pdfs").glob("*.pdf")) + list(base.glob("*.pdf"))
    for p in pdfs:
        if norm and norm[:6] in re.sub(r"[\s_\-./()]+", "", p.stem.lower()):
            return p
    return pdfs[0] if pdfs else None


if __name__ == "__main__":
    main()
