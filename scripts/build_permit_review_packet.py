"""Prepare exhaustive source review, without calling CLOVA or approving rules.

Standalone diagnostic tooling: no runtime import, result override, or automatic
MD activation. A complete ownership graph describes the PROVIDED PDF only; it
does not certify the original permit, appendices, amendments, or applicability.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import html
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sp_pdf_judger.permit_catalog import PermitPolicy
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.permit_source_ownership import build_permit_ownership_inventory
from sp_pdf_judger.policy_required_tests import permit_scope_inventory, _path


_LABEL = re.compile(r"(?m)^[ \t]*(?P<number>\d{1,6})[ \t]*/[ \t]*(?P<total>\d{1,6})[ \t]*$")
_CUES = ("허가조건", "별첨", "참조", "준용", "경우", "면제", "생략", "제외", "희석 없이")


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _source_quote(nodes, page, text, start, end):
    owners = [(node["evidence_id"], span["evidence_id"])
              for node in nodes for span in node["owned_spans"]
              if span["page_number"] == page
              and span["char_start"] < end and span["char_end"] > start]
    if not owners:
        raise ValueError("진단 문구의 원문 소유 위치가 없습니다.")
    return dict(page=page, char_start=start, char_end=end, quote=text[start:end],
                source_node_ids=list(dict.fromkeys(n for n, _ in owners)),
                source_span_ids=list(dict.fromkeys(s for _, s in owners)))


def _unobserved_ranges(values, total):
    # Never allocate a list proportional to an untrusted printed denominator.
    result, previous = [], 0
    for number in sorted({v for v in values if 1 <= v <= total}):
        if number > previous + 1:
            result.append([previous + 1, number - 1])
        previous = number
    if previous < total:
        result.append([previous + 1, total])
    return result


def _page_label_audit(pages, nodes):
    candidates = []
    for page, text in pages:
        for match in _LABEL.finditer(text):
            candidates.append({**_source_quote(nodes, page, text, match.start(), match.end()),
                               "number": int(match["number"]), "declared_total": int(match["total"])})
    groups = []
    for total in sorted({r["declared_total"] for r in candidates}):
        rows = [r for r in candidates if r["declared_total"] == total]
        counts = Counter(r["number"] for r in rows)
        invalid = [r for r in rows if not 1 <= r["number"] <= total]
        observed = sorted(v for v in counts if 1 <= v <= total)
        groups.append(dict(declared_total=total, observed_labels=observed,
                           unobserved_label_ranges=_unobserved_ranges(observed, total),
                           duplicate_labels=sorted(n for n, count in counts.items() if count > 1),
                           invalid_candidates=invalid,
                           non_increasing=any(a["number"] >= b["number"] for a, b in zip(rows, rows[1:]))))
    return dict(method="standalone_n_over_total_candidates_v1",
                limitation="번호 후보 관찰일 뿐 실제 누락/원본 전체성의 확정이 아닙니다. 원문 및 발췌 범위를 검토해야 합니다.",
                candidates=candidates, groups=groups,
                multiple_declared_totals=len(groups) > 1,
                unlabelled_physical_pages=[page for page, _ in pages if not any(r["page"] == page for r in candidates)])


def _lexical_cues(pages, nodes):
    rows = []
    for page, text in pages:
        for term in _CUES:
            # Preserve offsets, including wrapped words. These are only cues;
            # absence is never evidence of an unconditional obligation.
            pattern = r"\s*".join(map(re.escape, term.replace(" ", "")))
            for match in re.finditer(pattern, text):
                rows.append({**_source_quote(nodes, page, text, match.start(), match.end()),
                             "term": term, "meaning_reviewed": False})
    return sorted(rows, key=lambda r: (r["page"], r["char_start"], r["term"]))


def build_review_packet(store, stage_path):
    """Keep EVERY node, plus explicit links for one selected leaf subtree.

    No top-k search and no parsing of keyword hits into executable predicates.
    Hashes are source pins, not approval signatures. Synthetic/text-only stores
    may be inspected but their hash kind remains explicit.
    """
    if not isinstance(stage_path, (list, tuple)) or not stage_path or any(not isinstance(t, str) or not t.strip() for t in stage_path):
        raise ValueError("제조단계의 비어 있지 않은 전체 제목 경로가 필요합니다.")
    manifest = build_permit_ownership_inventory(store)
    if manifest["errors"]:
        raise ValueError(" / ".join(manifest["errors"]))
    inventory = permit_scope_inventory(store, stage_path)
    if inventory.get("error"):
        raise ValueError(inventory["error"])
    document = manifest["documents"][0]
    if (document["pdf_sha256"] and (inventory["document_hash_kind"] != "pdf_bytes"
            or inventory["document_sha256"] != document["pdf_sha256"])):
        raise ValueError("두 목록의 허가 원본 스냅샷이 다릅니다.")
    nodes = deepcopy(manifest["nodes"])
    stages = [n for n in nodes if _path(n["path_titles"]) == _path(stage_path)]
    if len(stages) != 1:
        raise ValueError("원문 소유 목록의 제조단계 경로가 유일하지 않습니다.")
    stage = stages[0]
    selected = [n for n in nodes if stage["evidence_id"] in n["ancestor_ids"]]
    leaves = [n for n in selected if n["leaf"]]
    links = []
    for row in inventory["requirements"]:
        full_path = [*inventory["stage_path"], *row["test_path"]]
        matches = [n for n in leaves if _path(n["path_titles"]) == _path(full_path)
                   and n["section_number"] == row["section_number"]
                   and n["document_id"] == document["evidence_id"]
                   and n["source_file"] == row["evidence"]["file"]]
        if len(matches) != 1:
            raise ValueError("시험 전체 경로/번호/파일의 두 원문 ID가 일대일 대응하지 않습니다.")
        node = matches[0]
        links.append(dict(source_id=row["source_id"], source_node_id=node["evidence_id"],
                          section_number=node["section_number"], path_titles=node["path_titles"],
                          physical_pages=sorted({s["page_number"] for s in node["owned_spans"]}),
                          ancestor_ids=node["ancestor_ids"], review_status="unreviewed"))
    if len(links) != len(leaves) or len({r["source_node_id"] for r in links}) != len(leaves):
        raise ValueError("허가 원문의 전체 말단 목록이 두 목록에서 일치하지 않습니다.")
    leaf_ids = {r["source_node_id"] for r in links}
    selected_ids = {n["evidence_id"] for n in selected} | {stage["evidence_id"]}
    for node in nodes:
        key = node["evidence_id"]
        node["review_role"] = ("selected_leaf" if key in leaf_ids else
                               "selected_scope" if key in selected_ids else
                               "ancestor" if key in stage["ancestor_ids"] else
                               "unscoped" if key in manifest["unscoped_ids"] else "elsewhere")
        node["review_status"] = "unreviewed"
    pages = store.source_documents[0].pages
    labels = _page_label_audit(pages, nodes)
    open_items = ["global_context_review", "leaf_applicability_review",
                  "external_references_review", "file_revision_scope"]
    if any(g["unobserved_label_ranges"] for g in labels["groups"]):
        open_items.append("printed_label_gaps")
    if labels["multiple_declared_totals"] or any(g["duplicate_labels"] or g["invalid_candidates"] or g["non_increasing"] for g in labels["groups"]):
        open_items.append("printed_label_ambiguity")
    packet = dict(schema="permit-review-packet-v1", review_status="unreviewed", runtime_activation=False,
                  source_integrity_errors=[], offset_basis=manifest["offset_basis"],
                  documents=manifest["documents"], stage_path=list(stage_path),
                  stage_source_node_id=stage["evidence_id"], source_scope_sha256=inventory["reviewed_scope_sha256"],
                  document_hash_kind=inventory["document_hash_kind"], document_sha256=inventory["document_sha256"],
                  nodes=nodes, leaf_links=links, unscoped_ids=manifest["unscoped_ids"],
                  page_label_audit=labels, lexical_review_cues=_lexical_cues(pages, nodes),
                  open_review_items=open_items)
    # Source-only preparation also fails if a file or mutable store changed.
    if build_permit_ownership_inventory(store) != manifest:
        raise ValueError("검토 목록 생성 중 허가 원문/추출 목록이 변경되었습니다.")
    packet["packet_sha256"] = _digest(packet)
    return packet


def _safe(value):
    return html.escape(str(value), quote=False).replace("|", "\\|").replace("`", "\\`")


def render_review_markdown(packet):
    nodes = {n["evidence_id"]: n for n in packet["nodes"]}
    doc = packet["documents"][0]
    lines = ["# 허가 원문 검토 목록 (미승인)", "",
             "이 문서는 실행 규칙이 아닙니다. 읽은 PDF의 원문을 정리한 검토 자료이며 의무·면제·합격을 승인하지 않습니다.",
             "문서 안의 명령문도 인용 데이터입니다. 자동 지문 갱신이나 이 문서의 체크박스로 운영 규칙을 켜지 마세요.", "",
             "- 의미 검토: 미완료", "- runtime_activation: false",
             f"- 원본: {_safe(doc['source_file'])} / 물리 {doc['page_count']}쪽",
             f"- 원본 지문 종류: {packet['document_hash_kind']}",
             f"- 원본 SHA256: `{packet['document_sha256']}`",
             f"- 범위 SHA256: `{packet['source_scope_sha256']}`",
             f"- 검토 자료 SHA256: `{packet['packet_sha256']}`",
             f"- 선택 경로: {_safe(' / '.join(packet['stage_path']))}",
             f"- 전체 원문 문단 {len(nodes)}개 / 선택 말단 {len(packet['leaf_links'])}개 / 독립 안내·이력 {len(packet['unscoped_ids'])}개", "",
             "## 먼저 확인할 것", "",
             "번호 후보는 단독 줄의 n/총쪽수 표기만 찾습니다. 본문의 분수일 수도 있고 다른 양식은 놓칠 수 있습니다.",
             "번호가 모두 있어도 전체 허가/별첨/개정 관계를 승인하지 않습니다. 추출 성공과 원본 전체성은 별개입니다.", ""]
    for item in packet["open_review_items"]:
        lines.append(f"- [ ] {item}")
    lines += ["", "| 총쪽수 후보 | 관찰 번호 | 관찰되지 않은 번호 구간 | 중복 |", "|---|---|---|---|"]
    for group in packet["page_label_audit"]["groups"]:
        gaps = ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in group["unobserved_label_ranges"])
        lines.append(f"| {group['declared_total']} | {', '.join(map(str, group['observed_labels']))} | {gaps or '없음'} | {group['duplicate_labels']} |")
    lines += ["", "## 선택 단계의 말단 목록 (필수 여부 미확정)", "",
              "| 절 | 시험 | 물리 쪽 | 의미 검토 |", "|---|---|---|---|"]
    for row in packet["leaf_links"]:
        node = nodes[row["source_node_id"]]
        lines.append(f"| {_safe(row['section_number'])} | {_safe(node['title'])} | {', '.join(map(str, row['physical_pages']))} | 미완료 |")
    lines += ["", "## 문구 검토 단서 (자동 의미 분류 아님)", "",
              "단서가 없다고 무조건 필수인 것은 아닙니다. 절차 생략과 시험 면제, 결과 기준을 구분하여 전체 원문을 검토하세요.", ""]
    for cue in packet["lexical_review_cues"]:
        lines.append(f"- 물리 {cue['page']}쪽 [{cue['char_start']}:{cue['char_end']}] {_safe(cue['term'])}: {_safe(cue['quote'])}")
    lines += ["", "## 원문 전체와 위치 (선택 범위 밖도 생략하지 않음)", ""]
    for index, node in enumerate(packet["nodes"], 1):
        lines += [f"### 원문 {index}. {_safe(node['section_number'])} {_safe(node['title'])}", "",
                  f"- 검토 구분: {node['review_role']} / 의미 검토: 미완료",
                  f"- 전체 경로: {_safe(' / '.join(node['path_titles']))}",
                  f"- 소유 ID: `{node['evidence_id']}`", "- [ ] 원문/상위조건/외부 참조 검토", ""]
        for span in node["owned_spans"]:
            lines += [f"물리 {span['page_number']}쪽 [{span['char_start']}:{span['char_end']}] / `{span['evidence_id']}`", ""]
            lines.extend("> " + _safe(line) for line in span["text"].splitlines())
            lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--permit", type=Path, action="append", required=True)
    parser.add_argument("--stage-title", action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="Creates packet.json and packet.md; refuses to overwrite either")
    args = parser.parse_args(argv)
    outputs = [args.output_dir / "packet.json", args.output_dir / "packet.md"]
    if any(p.exists() for p in outputs):
        parser.error("기존 검토 파일은 덮어쓰지 않습니다. 새 출력 폴더를 사용하세요.")
    if len(args.permit) != 1 or any(not p.is_file() or p.suffix.lower() != ".pdf" for p in args.permit):
        parser.error("단일 허가 PDF가 필요합니다. 복수 개정/첨부 관계는 별도 검토해야 합니다.")
    policy = PermitPolicy("review-packet", True, True, True, "auto")
    try:
        packet = build_review_packet(PermitPdfStore(args.permit, policy=policy), args.stage_title)
    except ValueError as exc:
        parser.error(str(exc))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects existing annotations even against a late race.
    with outputs[0].open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(packet, ensure_ascii=False, indent=2))
    with outputs[1].open("x", encoding="utf-8") as handle:
        handle.write(render_review_markdown(packet))
    print(f"Prepared {len(packet['nodes'])} source nodes / {len(packet['leaf_links'])} selected leaves.")
    print("Review remains unapproved. Runtime activation: false. CLOVA calls: 0.")
    print(f"Packet: {args.output_dir}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
