"""Re-render saved reviews without API calls; assert original outcomes unchanged.

Example: python scripts/verify_text_presentation.py --input-dir <saved JSON dir>
    --output-dir .local_validation/review_20261006/presentation
This is a presentation replay, NOT a fresh CLOVA review.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import html
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sp_pdf_judger.permit_text_view import permit_view_for_result
from sp_pdf_judger.schemas import Evaluation, TreeNode
from sp_pdf_judger.ui_html import _render_reason_box, _render_test_leaf
from sp_judgement_bridge import _render_structural_validation_cards


def verify(input_dir: Path, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    pages = []
    for path in sorted(input_dir.glob("D[0-9][0-9].json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        original = deepcopy(payload)
        view = permit_view_for_result(SimpleNamespace(metadata=payload["metadata"]))
        count = 0
        sections = []
        for item in payload["evaluations"]:
            ev = Evaluation(**item)
            if ev.record_type != "test":
                continue
            before = asdict(ev)
            markup = _render_reason_box(ev, permit_view=view)
            assert before == asdict(ev)
            if view.has_permit_basis(ev) and ev.normalized_criteria:
                count += 1
                shown = view.format_quote(ev.normalized_criteria)
                assert "문서확인번호" not in shown, (path.name, ev.test_name)
                assert not re.search(r"(?m)^\s*\d+/\d+\s*$", shown), (path.name, ev.test_name)
                # Expanded display is readable too; originals remain in the data.
                if shown != ev.normalized_criteria.strip():
                    assert 'class="permit-quote"' in markup
                assert "문서확인번호" not in markup
            if path.stem == "D00" or (path.stem in {"D06", "D09"} and ev.source == "md_policy_guard"):
                node = TreeNode(key=f"test-{ev.order_idx}", title=ev.test_name or "시험", level=1,
                                node_type="test", evaluation=ev)
                sections.append(f'<section id="test-{ev.order_idx}"><p>{html.escape(ev.section_title or "")}</p>'
                                + _render_test_leaf(node, 0, permit_view=view) + '</section>')
        assert payload == original
        reports.append({"case": path.stem, "summary": payload["summary"],
                        "saved_result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "permit_cards_checked": count, "all_evaluations_unchanged": True})
        if path.stem == "D00":
            pages = sections
        elif sections:
            (output_dir / f"{path.stem}.html").write_text(
                '<!doctype html><html lang="ko"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>규칙 적용 시험의 허가서 표시 검증</title><style>body{font:16px system-ui;'
                'background:#f5f7fb;margin:16px}section{margin:24px 0}</style>'
                f'<h1>{path.stem} 저장 결과의 규칙 적용 시험</h1><p>새 API 판정이 아닙니다.</p>'
                + "\n".join(sections) + '</html>', encoding="utf-8")
        if path.stem in {"D00", "D07", "D08"}:
            result = SimpleNamespace(metadata=payload["metadata"], pdf_path=payload["pdf_path"],
                                     evaluations=[Evaluation(**item) for item in payload["evaluations"]])
            before = deepcopy(result)
            rules = "".join(_render_structural_validation_cards(result, record_type=kind,
                            section_title=title) for kind, title in [
                ("structural_validation", "문서 기본요건"),
                ("numeric_precision_validation", "계산 및 수치 정합성"),
                ("temporal_sequence_validation", "공정 및 시간 순서")])
            assert result == before
            (output_dir / f"{path.stem}.html").write_text(
                '<!doctype html><html lang="ko"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>MD 판정 이유 표시 검증</title><style>body{font:16px system-ui;'
                'background:#f5f7fb;margin:16px}</style>'
                f'<h1>{path.stem} 저장 결과 표시 검증</h1><p>새 API 판정이 아닙니다.</p>'
                + rules + '</html>', encoding="utf-8")
    assert len(reports) == 17, f"Expected D00-D16, found {len(reports)}"
    report = {"mode": "saved-review-presentation-replay", "api_calls": 0, "cases": reports}
    (output_dir / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markup = ('<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
              '<title>SP 텍스트 표시 검증</title><style>body{font:16px system-ui;background:#f5f7fb;color:#1f2937;'
              'max-width:1400px;margin:24px auto;padding:0 16px;}section{margin:24px 0}summary{cursor:pointer}</style>'
              '<h1>텍스트 표시 검증</h1><p>저장된 D00 검수 결과를 현재 UI로 다시 표시한 화면입니다. '
              '새 CLOVA 검수나 운영 앱 결과 주입이 아닙니다.</p><nav>'
              '<a href="#test-24">위첨자</a> · <a href="#test-30">페이지 사이 문장</a> · '
              '<a href="#test-31">PCR</a> · <a href="#test-37">HAP</a> · '
              '<a href="#test-38">MAP</a> · <a href="D06.html">D06 시험</a> · '
              '<a href="D09.html">D09 시험</a> · <a href="D07.html">D07 MD</a> · '
              '<a href="D08.html">D08 MD</a></nav>' + "\n".join(pages) + '</html>')
    (output_dir / "index.html").write_text(markup, encoding="utf-8")
    print(json.dumps({"cases":len(reports), "permit_cards":sum(r['permit_cards_checked'] for r in reports),
                      "api_calls":0, "output_dir":str(output_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    verify(args.input_dir, args.output_dir)
