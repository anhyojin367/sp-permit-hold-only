"""Exhaustive permit requirements, without top-k search or cross-batch borrowing.

This first contract accepts only a human-reviewed, unconditional leaf subtree.
The MD pins both the subtree content and the whole document: changes to global
conditions, ancestors or images also require review, not a guessed mandatory
list. It checks test presence, not result limits.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter

from .policy_schema import PermitRequiredTestsParams, identity


def _path(values):
    return tuple(identity(value) for value in values)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def permit_scope_inventory(store, stage_path):
    """Return one exact stage's leaves and audit IDs, or an explicit blocker.

    Review hash excludes filename/page relocation but includes every subtree
    paragraph and its hierarchy. Source IDs also bind document content, page and
    ordinal, so reused section numbers are never global identities.
    """
    if store.extraction_errors:
        return {"error": "허가서 추출 오류가 있어 전체 필수시험 범위를 확인할 수 없습니다."}
    for path in store.permit_pdf_paths:
        if not path.is_file() or path.suffix.lower() != ".pdf" or not any(c.source_file == path.name for c in store.chunks):
            return {"error": "연결된 허가서 중 읽을 수 없거나 문단이 추출되지 않은 파일이 있습니다."}
    requested = _path(stage_path)
    matches = [(i, c) for i, c in enumerate(store.chunks) if _path(c.section_path_titles) == requested]
    if len(matches) != 1:
        return {"error": f"허가 제조단계의 전체 경로가 유일하지 않습니다: {len(matches)}건."}
    index, stage = matches[0]
    subtree = [(index, stage)]
    for position, chunk in enumerate(store.chunks[index + 1:], index + 1):
        path = _path(chunk.section_path_titles)
        if chunk.source_file != stage.source_file or len(path) <= len(requested) or path[:len(requested)] != requested:
            break
        subtree.append((position, chunk))
    if len(subtree) == 1:
        return {"error": "허가 제조단계의 하위 시험 목록이 없습니다."}
    paths = [_path(chunk.section_path_titles) for _, chunk in subtree]
    if len(paths) != len(set(paths)):
        return {"error": "허가 제조단계 안에 중복된 시험 경로가 있습니다."}
    signature = [[c.section_number, list(c.section_path_titles), c.text] for _, c in subtree]
    review_hash = _digest(signature)
    source_paths = [p for p in store.permit_pdf_paths if p.name == stage.source_file]
    if len(source_paths) > 1:
        return {"error": "같은 파일명의 허가서가 여러 경로에 연결되어 있습니다."}
    if source_paths:
        # Read only the linked permit, not an arbitrary path supplied by a chunk.
        document_hash = hashlib.sha256(source_paths[0].read_bytes()).hexdigest()
        hash_kind = "pdf_bytes"
    else:
        document_hash = _digest([[c.page_start, c.page_end, c.section_number,
                                  list(c.section_path_titles), c.text]
                                 for c in store.chunks if c.source_file == stage.source_file])
        hash_kind = "extracted_document_text"
    leaves = []
    for offset, (position, chunk) in enumerate(subtree[1:], 1):
        path = paths[offset]
        if offset + 1 < len(paths) and paths[offset + 1][:len(path)] == path:
            continue
        relative = list(chunk.section_path_titles[len(requested):])
        source_id = _digest([document_hash, list(chunk.section_path_titles),
                             chunk.page_start, chunk.page_end, position])
        leaves.append({"source_id": source_id, "test_path": relative,
                       "section_number": chunk.section_number,
                       "evidence": {"source": "permit", "file": chunk.source_file,
                                    "page": chunk.page_start or chunk.page_number,
                                    "page_end": chunk.page_end, "quote": chunk.text,
                                    "source_id": source_id}})
    return {"reviewed_scope_sha256": review_hash, "document_sha256": document_hash,
            "document_hash_kind": hash_kind, "stage_path": list(stage.section_path_titles),
            "requirements": leaves}


def _coverage(ctx, start, end):
    pages = [(number, identity(text)) for number, text in ctx.pages]
    text = "".join(value for _, value in pages)
    begin, finish = identity(start), identity(end)
    if text.count(begin) != 1 or text.count(finish) != 1:
        return None, "SP 시작/끝 경계가 없거나 중복되어 누락 검사 범위를 확정할 수 없습니다."
    left, right = text.index(begin), text.index(finish)
    if left + len(begin) >= right:
        return None, "SP 시작/끝 경계의 순서가 잘못되었습니다."
    # Include blank intervening pages: text offsets alone would hide a scan loss.
    offsets, total = [], 0
    for number, value in pages:
        offsets.append((number, total, total + len(value), value))
        total += len(value)
    first = next(number for number, a, b, _ in offsets if a <= left < b)
    last = next(number for number, a, b, _ in offsets if a <= right < b)
    scoped = [(number, value) for number, _, _, value in offsets if first <= number <= last]
    if [n for n, _ in scoped] != list(range(first, last + 1)) or any(not value for _, value in scoped):
        return None, "SP 검사 범위에 텍스트가 없거나 누락된 페이지가 있습니다."
    # Preserve lines and page breaks for label/value extraction. Never join a
    # dangling table label on one page to an unrelated value on the next.
    raw = unicodedata.normalize("NFKC", "\f".join(value for _, value in ctx.pages))
    boundaries = []
    for token in (begin, finish):
        matches = list(re.finditer(r"\s*".join(map(re.escape, token)), raw, re.I))
        if len(matches) != 1:
            return None, "SP 원문 경계의 문자 위치를 유일하게 연결할 수 없습니다."
        boundaries.append(matches[0].start())
    raw_pages = list(zip(range(first, last + 1), raw[boundaries[0]:boundaries[1]].split("\f")))
    return {"text": text[left:right], "first": first, "last": last,
            "raw_pages": raw_pages, "offset_start": left, "offset_end": right}, ""


def _record_path(record):
    return _path([part.get("title", "") for part in record.section_path if isinstance(part, dict)])


def _field_values(text, label, layout):
    """Exact line labels only. A present but unsupported field returns '' (HOLD).

    This is a reviewed adjacent-line layout, not a generic table/OCR solver.
    Related names such as '제조용 세포주 제조번호' cannot match '제조번호'.
    """
    lines = [line.strip() for line in unicodedata.normalize("NFKC", text or "").splitlines() if line.strip()]
    pattern = re.compile(r"^" + r"[ \t]*".join(map(re.escape, identity(label))) +
                         r"(?=$|[^\w])(.*)$", re.I)
    values = []
    for index, line in enumerate(lines):
        match = pattern.fullmatch(line)
        if not match:
            continue
        tail = match[1].strip()
        if tail.startswith((":", "|")):
            values.append(tail[1:].strip())
        elif layout == "label_value_lines":
            values.append(tail or (lines[index + 1] if index + 1 < len(lines) else ""))
        else:
            values.append("")
    return values


def _label_values(record, label, layout):
    values = [value for text in (record.content, record.remarks) if text
              for value in _field_values(text, label, layout)]
    # Raw source must confirm the same field, not a header elsewhere on a page.
    raw = unicodedata.normalize("NFKC", record.raw_text or record.content or "")
    # The extractor's remarks field omits its '비고:' wrapper; compare the
    # contained label/value, not an unrelated occurrence elsewhere in raw text.
    raw = re.sub(r"(?m)^[ \t]*비고[ \t]*[:|][ \t]*", "", raw)
    raw_values = _field_values(raw, label, layout)
    if [identity(v) for v in values] != [identity(v) for v in raw_values]:
        return [""]
    return values


def _lot(values):
    if len(values) != 1:
        return ""
    value = values[0].strip()
    # Keep hyphens/punctuation significant; multi-lot lists are not one lot.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value) or not re.search(r"\d", value):
        return ""
    return identity(value)


def _within(record, coverage):
    # The extractor inserts ':' after these field labels in records. Ignore
    # only that formatting, never punctuation inside values (ratios, time, etc.).
    labels = r"시험명|시험방법|시험기준|시험기간|시험일자|시험결과|비고"
    def canonical(text):
        text = unicodedata.normalize("NFKC", text)
        text = re.sub(r"(?m)^(\s*(?:" + labels + r"))[ \t]*[:|][ \t]*", r"\1", text)
        return identity(text)
    source = canonical(record.raw_text or record.content or "")
    target = canonical("\n".join(t for n, t in coverage["raw_pages"]
                                  if record.page_start <= n <= record.page_end))
    return (not record.ocr_suspect and bool(source) and source in target and
            coverage["first"] <= record.page_start <= record.page_end <= coverage["last"])


def _test_paths(record, stage_path):
    name = identity(record.test_name or "")
    if not name:
        return set()
    relative = _record_path(record)[len(stage_path):]
    if record.parent_test_group:
        # Never concatenate fragments of names. This is one exact parent node.
        # A child cannot simultaneously be an independent, parentless test.
        parent = identity(record.parent_test_group)
        if not relative or relative[-1] != parent:
            relative = (*relative, parent)
    return {(*relative, name)}


def check_permit_required_tests(raw_params, ctx):
    params = PermitRequiredTestsParams.model_validate(raw_params)
    return check_test_presence(params, ctx)


def _conditional_result_linked(record):
    """Narrow reviewed text layout: a result must come from its result field.

    This is source linkage, not evaluation of a criterion or an LLM judgement.
    Unsupported table layouts are incomplete evidence, never inferred presence.
    """
    raw = unicodedata.normalize("NFKC", record.raw_text or "")
    fields = list(re.finditer(r"(?m)^\s*(시험명|시험방법|시험기준|시험기간|시험일자|시험결과|비고)\s*[:|]\s*", raw))
    results = [raw[m.end():(fields[i + 1].start() if i + 1 < len(fields) else len(raw))]
               for i, m in enumerate(fields) if m[1] == "시험결과"]
    return len(results) == 1 and bool(identity(results[0])) and identity(results[0]) == identity(record.result)


def check_test_presence(params, ctx, *, applicability=None):
    """Neutral presence engine. Optional gates come only from a source verifier.

    Not a public LLM response interface. Unconditional callers pass no gates;
    conditional callers must provide every requirement/batch, including unknowns.
    """
    result = {"violations": [], "missing": [], "evidence": [], "checked": 0,
              "requirement_checks": [], "presence_only": True}
    inventory = permit_scope_inventory(ctx.permit_store, params.permit_stage_path)
    result["permit_inventory"] = inventory
    if inventory.get("error"):
        result["missing"].append(inventory["error"])
        return result
    requirements = inventory["requirements"]
    result["evidence"].extend(r["evidence"] for r in requirements)
    if (params.reviewed_scope_sha256 != inventory["reviewed_scope_sha256"] or
            params.reviewed_document_sha256 != inventory["document_sha256"]):
        result["missing"].append("허가 문서/시험 목록/조건이 MD에서 검토한 자료와 다릅니다. 상위 장·서문의 공통조건과 조건부 요구사항을 포함하여 재검토해야 합니다.")
        return result
    valid_paths = {_path(r["test_path"]) for r in requirements}
    mappings = {_path(item.permit_test_path): {_path(p) for p in item.sp_test_paths} for item in params.name_mappings}
    if set(mappings) - valid_paths:
        result["missing"].append("MD 명칭 대응에 허가 목록에 없는 시험 경로가 있습니다.")
        return result
    accepted = {path: {path} | mappings.get(path, set()) for path in valid_paths}
    owners = {}
    for permit_path, sp_paths in accepted.items():
        for path in sp_paths:
            if path in owners and owners[path] != permit_path:
                result["missing"].append("동일 SP 시험명이 두 허가 요구사항에 대응합니다.")
                return result
            owners[path] = permit_path
    coverage, error = _coverage(ctx, params.coverage_start, params.coverage_end)
    if error:
        result["missing"].append(error)
        return result
    result["coverage"] = {k: coverage[k] for k in ("first", "last")}
    result["evidence"].append({"source": "sp", "file": str(ctx.pdf_path), "page": coverage["first"],
                               "page_end": coverage["last"], "quote": f"{params.coverage_start} → {params.coverage_end}"})
    batches = {}
    batch_coverage = coverage
    if params.batch_inventory_path:
        batch_coverage, error = _coverage(ctx, params.batch_coverage_start, params.batch_coverage_end)
        if error or not (coverage["offset_start"] <= batch_coverage["offset_start"] <
                         batch_coverage["offset_end"] <= coverage["offset_end"]):
            result["missing"].append(error or "독립 배치 목록 범위가 전체 SP 검사 범위 밖에 있습니다.")
            return result
    test_coverage = coverage
    if params.test_coverage_start:
        test_coverage, error = _coverage(ctx, params.test_coverage_start, params.test_coverage_end)
        if error or not (coverage["offset_start"] <= test_coverage["offset_start"] <
                         test_coverage["offset_end"] <= coverage["offset_end"]):
            result["missing"].append(error or "독립 시험 원문 범위가 전체 SP 검사 범위 밖에 있습니다.")
            return result
        if not (test_coverage["offset_end"] <= batch_coverage["offset_start"] or
                batch_coverage["offset_end"] <= test_coverage["offset_start"]):
            result["missing"].append("독립 시험 원문 범위와 배치 정보 범위가 겹칩니다.")
            return result
        result["test_coverage_audit"] = {"path": params.sp_stage_path,
            "first": test_coverage["first"], "last": test_coverage["last"],
            "start": params.test_coverage_start, "end": params.test_coverage_end}
        result["evidence"].append({"source": "sp", "file": str(ctx.pdf_path),
            "page": test_coverage["first"], "page_end": test_coverage["last"],
            "quote": f"{params.test_coverage_start} → {params.test_coverage_end}"})
    source_records = ctx.select(params.batch_inventory.model_dump(exclude_unset=True))
    if params.batch_inventory_path:
        source_records = [r for r in source_records if _record_path(r) == _path(params.batch_inventory_path)]
    for record in source_records:
        values = _label_values(record, params.batch_field, params.batch_field_layout)
        batch = _lot(values)
        if not batch or batch in batches or not _within(record, batch_coverage):
            result["missing"].append("배치 목록의 제조번호/범위가 누락·중복·불명확합니다.")
            continue
        source_values = [v for n, text in batch_coverage["raw_pages"]
                         if record.page_start <= n <= record.page_end
                         for v in _field_values(text, params.batch_field, params.batch_field_layout)]
        if batch not in [_lot([v]) for v in source_values]:
            result["missing"].append("배치 목록의 제조번호가 원문 페이지에서 확인되지 않습니다.")
            continue
        batches[batch] = values[0]
        result["evidence"].append(ctx.evidence(record))
    if not source_records:
        result["missing"].append("독립된 배치 목록이 추출되지 않았습니다.")
    if params.batch_inventory_path:
        raw_batches = [_lot([value]) for _, text in batch_coverage["raw_pages"]
                       for value in _field_values(text, params.batch_field, params.batch_field_layout)]
        if not raw_batches or "" in raw_batches or Counter(raw_batches) != Counter(batches.keys()):
            result["missing"].append("독립 배치 범위의 원문 제조번호 전체와 추출 목록이 일치하지 않습니다.")
        result["batch_inventory_audit"] = {"path": params.batch_inventory_path,
            "first": batch_coverage["first"], "last": batch_coverage["last"],
            "layout": params.batch_field_layout, "raw_field_count": len(raw_batches),
            "batches": list(batches.values())}
    if result["missing"]:
        return result
    if params.batch_binding == "single_batch_stage" and len(batches) != 1:
        result["missing"].append("복수 배치를 하나의 단계로 묶어 시험 수행을 인정할 수 없습니다.")
        return result
    if applicability is not None:
        expected = {(r["source_id"], batch) for r in requirements for batch in batches}
        if (set(applicability) != expected or any(d.get("applicability") not in
                {"required", "not_required", "unknown"} for d in applicability.values())):
            result["missing"].append("수행 조건의 전체 시험·배치 목록이 존재 검사 범위와 다릅니다.")
            return result
    stage_path = _path(params.sp_stage_path)
    records = [r for r in ctx.records if r.record_type == "test" and
               _record_path(r)[:len(stage_path)] == stage_path]
    assigned = []
    uncertain_binding = False
    for record in records:
        if params.batch_binding == "single_batch_stage":
            explicit = _label_values(record, params.batch_field, params.batch_field_layout)
            batch = _lot(explicit) if explicit else next(iter(batches))
        else:
            batch = _lot(_label_values(record, params.test_batch_field, params.batch_field_layout))
        if (batch not in batches or not _within(record, test_coverage) or not identity(record.test_name) or
                identity(record.test_name) not in identity(record.raw_text or "")):
            uncertain_binding = True
            result["missing"].append(f"{record.test_name or '(시험명 없음)'}: 제조단계/배치/원문 연결이 불명확합니다.")
            continue
        assigned.append((record, batch, _test_paths(record, stage_path)))
    for requirement in requirements:
        path = _path(requirement["test_path"])
        for batch, display in batches.items():
            matches = [r for r, b, paths in assigned if b == batch and paths & accepted[path]]
            check = {"source_id": requirement["source_id"], "test_path": requirement["test_path"],
                     "batch": display, "record_orders": [r.order_idx for r in matches]}
            label = f"{display} / {' > '.join(requirement['test_path'])}"
            if applicability is not None:
                decision = applicability[(requirement["source_id"], batch)]
                check["applicability"] = decision["applicability"]
                check["applicability_evidence"] = decision
                if decision["applicability"] != "required":
                    if decision["applicability"] == "not_required":
                        check.update(status="N/A", reason="검토된 수행 조건 미해당; 시험 결과 합격을 뜻하지 않음")
                    else:
                        check.update(status="HOLD", reason="수행 필요 여부의 원문 근거 부족; 면제 불가")
                        result["missing"].append(f"{label}: {check['reason']}")
                    result["requirement_checks"].append(check)
                    continue
            if len(matches) == 1 and applicability is not None and not _conditional_result_linked(matches[0]):
                check.update(status="HOLD", reason="시험 결과 필드와 실제 원문 값의 연결이 불명확함")
                result["missing"].append(f"{label}: {check['reason']}")
                result["evidence"].append(ctx.evidence(matches[0]))
            elif len(matches) == 1 and (matches[0].result or "").strip():
                check.update(status="PASS", reason="같은 제조단계·배치의 시험 결과 기록 존재")
                result["checked"] += 1
                result["evidence"].append(ctx.evidence(matches[0]))
            elif matches:
                check.update(status="HOLD", reason="시험 기록 중복 또는 실제 결과 누락")
                result["missing"].append(f"{label}: {check['reason']}")
                result["evidence"].extend(ctx.evidence(r) for r in matches)
            else:
                # A raw mention with no linked record is extraction/link uncertainty,
                # NOT evidence of absence. Other correctly linked batches' records
                # account for their mentions; they still cannot fill this batch.
                names = {p[-1] for p in accepted[path]}
                linked_counts = {name: sum(identity(r.test_name) == name for r, _, ps in assigned
                                           if ps & accepted[path]) for name in names}
                raw_unlinked = any(test_coverage["text"].count(name) > linked_counts[name] for name in names)
                if uncertain_binding or raw_unlinked:
                    check.update(status="HOLD", reason="원문 언급/배치 연결을 완전히 확인할 수 없음")
                    result["missing"].append(f"{label}: {check['reason']}")
                else:
                    check.update(status="FAIL", reason="확인된 검사 범위에서 해당 배치의 필수시험 누락")
                    result["violations"].append(f"{label}: {check['reason']}")
                    result["checked"] += 1
            result["requirement_checks"].append(check)
    return result
