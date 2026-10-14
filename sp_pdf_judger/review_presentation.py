"""Review provenance and presentation only. Never changes a verdict or count."""
from __future__ import annotations

import hashlib
import html
from datetime import datetime, timezone
from pathlib import Path

REVIEW_RELEASE = "v108-review-clarity-20261002"
COUNT_HELP = ("건수는 수정할 곳의 수가 아니라 판정한 검사 항목 수입니다. "
              "표의 대상별 결과는 각각 1건이며, 누락된 시험 결과는 집계에서 빠지고 "
              "필수 항목 누락 여부는 별도 MD 규칙으로 검사합니다. "
              "같은 원문이 여러 규칙을 위반하면 각 검사에 집계됩니다.")


def file_sha256(path):
    path = Path(path)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_fingerprint(root=None):
    """Stable across checkout directory, CRLF/LF, and platform."""
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    paths = [root / "json추출.py", *sorted((root / "sp_pdf_judger").rglob("*.py")),
             *sorted((root / "sp_pdf_judger" / "rules").rglob("*.md"))]
    digest = hashlib.sha256()
    for path in paths:
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def review_receipt(pdf, permits, rule_fingerprint, *, llm_enabled=False, model=""):
    return {"release": REVIEW_RELEASE, "started_at": datetime.now(timezone.utc).isoformat(),
            "engine_sha256": source_fingerprint(), "rules_sha256": rule_fingerprint,
            "sp": {"name": Path(pdf).name, "sha256": file_sha256(pdf)},
            "permits": [{"name": Path(p).name, "sha256": file_sha256(p)} for p in permits],
            "mode": "CLOVA 연동" if llm_enabled else "오프라인", "model": model}


def unit_audit_reason(entry):
    label = "시험기준" if entry["field"] == "criteria" else "시험결과"
    return (f"MD 단위 표기 대응: 원문 {label} '{entry['original']}' → 비교 표기 '{entry['compared']}'. "
            f"적용 규칙 {entry['rule_id']} · {entry['product']} · {entry['test_name']}: "
            f"{entry['from']} → {entry['to']}. 해당 제품·시험의 등록 규칙만 적용하며 원문은 보존합니다.")


def render_reason(text):
    """Keep every word; collapse lengthy prose without summarizing with an LLM."""
    text = str(text or "")
    # Repeated lines may belong to separate clauses; never deduplicate evidence.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    notes = [line for line in lines if line.startswith(("MD 단위 표기 대응:", "MD 결과 표기 사전:"))]
    body = "\n".join(line for line in lines if line not in notes)
    escaped = html.escape(body).replace("\n", "<br>")
    if len(body) > 350:
        # An explicitly labelled excerpt, not a new/reinterpreted conclusion.
        preview = html.escape(body[:240]).replace("\n", "<br>")
        escaped = (f'<div class="reason-preview">{preview}… <small>(설명 일부)</small></div>'
                   f'<details class="reason-full"><summary>판정 설명 전체 보기</summary>'
                   f'<div class="reason-full-text">{escaped}</div></details>')
    return escaped + "".join(f'<div class="normalization-note" style="margin-top:8px;">{html.escape(line)}</div>' for line in notes)


def related_failure_groups(audit):
    """Group only exact shared SP evidence, not guessed common root causes."""
    groups = {}
    for finding in audit:
        if finding.get("status") != "FAIL":
            continue
        evidence = finding.get("evidence", [])
        failed_pages = {c.get("page_number") for c in finding.get("details", {}).get("manufacturing_checks", [])
                        if c.get("status") in {"불합격", "검수불합격", "FAIL"} and c.get("page_number")}
        for item in evidence:
            if item.get("source") != "sp" or not item.get("page") or not item.get("quote"):
                continue
            if failed_pages and item["page"] not in failed_pages:
                continue
            if not failed_pages and len(evidence) != 1:
                continue
            key = (item.get("file", ""), item["page"], item["quote"])
            groups.setdefault(key, set()).add(finding["rule_id"])
    return [{"page": key[1], "rules": sorted(ids)} for key, ids in groups.items() if len(ids) > 1]


def render_review_context(result):
    meta = getattr(result, "metadata", {})
    groups = related_failure_groups(meta.get("policy_audit", []))
    links = "".join(f'<li>SP {g["page"]}쪽 동일 원문 관련 검사: {html.escape(" · ".join(g["rules"]))}. '
                    '각 불충족 판정은 유지됩니다.</li>' for g in groups)
    receipt = meta.get("review_receipt", {})
    entries = []
    if receipt:
        entries.extend((label, receipt.get(key, "미기록")) for label, key in
                       [("검수 릴리스", "release"), ("검수 시작 (UTC)", "started_at"),
                        ("실행 방식", "mode"), ("모델", "model"),
                        ("엔진 SHA256", "engine_sha256"), ("MD SHA256", "rules_sha256")])
        for label, file in [("SP", receipt.get("sp", {})), *[("허가서", p) for p in receipt.get("permits", [])]]:
            entries.append((label, f"{file.get('name', '')} · SHA256 {file.get('sha256') or '미기록'}"))
    rows = "".join(f'<div><b>{html.escape(label)}</b>: {html.escape(str(value or "미기록"))}</div>' for label, value in entries)
    return (f'<details class="review-context" style="background:#f8fafc;color:#334155;border:1px solid #dbe3ea;'
            'border-radius:12px;padding:12px;margin:14px 0;line-height:1.7;overflow-wrap:anywhere;">'
            '<summary style="cursor:pointer;font-weight:700;">집계 기준 · 관련 검사 · 검수 파일/버전</summary>'
            f'<p>{COUNT_HELP}</p>{"<ul>" + links + "</ul>" if links else ""}'
            f'{rows or "<p>이전 결과에는 검수 파일/버전 기록이 없습니다. 새로 검수하면 기록됩니다.</p>"}</details>')
