"""Opt-in source-bound numeric applicability plus test presence, not test approval."""
from copy import copy, deepcopy
import hashlib

import fitz

from .permit_applicability_md import load_applicability_md, evaluate_numeric_pdf
from .policy_required_tests import permit_scope_inventory, check_test_presence, _path, _lot
from .policy_schema import PermitConditionalTestsParams, identity


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_conditional_tests(rule, ctx):
    """Only RuleBook-bound companions and current PDF source enter this mode.

    Narrow v1: one reviewed permit, one stage, one independent batch. No model
    response/fallback, arbitrary Target, or unconditional-then-delete shortcut.
    """
    params = PermitConditionalTestsParams.model_validate(rule.params)
    path = rule._applicability_path
    if path is None:
        raise ValueError("조건 MD를 새 RuleBook에서 다시 읽어야 합니다.")
    loaded = load_applicability_md(path)
    # Validate the bytes actually parsed, not a separate earlier file read.
    if loaded.md_sha256 != rule._applicability_sha256:
        raise ValueError("조건 MD를 새 RuleBook에서 다시 읽어야 합니다.")
    spec = loaded.settings
    if (spec.input_layout != "single_batch_scoped_records_v1" or len(spec.stages) != 1
            or len(spec.source_scopes or []) != 1):
        raise ValueError("조건부 존재 검사 v1은 단일 제조단계/독립 단일 배치 표만 지원합니다.")
    contract = loaded.bind(ctx.permit_store, company=getattr(ctx, "company", ""), product=ctx.product)
    nodes = {n["evidence_id"]: n for n in contract.nodes}
    stage = nodes[spec.stages[0].source_node_id]
    if _path(stage["path_titles"]) != _path(params.permit_stage_path):
        raise ValueError("수행 조건과 존재 검사의 허가 제조단계 전체 경로가 다릅니다.")
    batch_source = spec.source_scopes[0].batch_source
    if (batch_source.record_type != params.batch_inventory.type
            or _path(batch_source.record_path) != _path(params.batch_inventory_path)
            or identity(batch_source.start) != identity(params.batch_coverage_start)
            or identity(batch_source.end) != identity(params.batch_coverage_end)
            or identity(spec.source_scopes[0].batch_field) != identity(params.batch_field)):
        raise ValueError("수행 조건과 존재 검사의 독립 배치 원문 범위가 다릅니다.")
    inventory = permit_scope_inventory(ctx.permit_store, params.permit_stage_path)
    if (inventory.get("error") or inventory["document_hash_kind"] != "pdf_bytes"
            or inventory["document_sha256"] != params.reviewed_document_sha256
            or inventory["reviewed_scope_sha256"] != params.reviewed_scope_sha256):
        raise ValueError("허가 PDF/전체 시험 목록을 MD와 다시 대조해야 합니다.")
    source_doc = ctx.permit_store.source_documents[0]
    if (source_doc.pdf_sha256 != inventory["document_sha256"]
            or source_doc.evidence_id != spec.reviewed_document_id):
        raise ValueError("두 검사에 연결된 허가 원본 문서가 다릅니다.")
    by_path = {_path(nodes[r.source_node_id]["path_titles"]): r.source_node_id for r in contract.requirements}
    expected_paths = {_path([*inventory["stage_path"], *r["test_path"]]) for r in inventory["requirements"]}
    if len(by_path) != len(contract.requirements) or set(by_path) != expected_paths:
        raise ValueError("허가 원문 소유 ID와 존재 검사의 전체 말단 시험 경로가 다릅니다.")
    links = []
    for requirement in inventory["requirements"]:
        full_path = [*inventory["stage_path"], *requirement["test_path"]]
        node_id = by_path[_path(full_path)]
        node = nodes[node_id]
        if (node["document_id"] != source_doc.evidence_id
                or node["source_file"] != requirement["evidence"]["file"]
                or node["section_number"] != requirement["section_number"]):
            raise ValueError("시험 경로의 허가 파일/절 번호가 다릅니다.")
        links.append(dict(source_id=requirement["source_id"], source_node_id=node_id,
                          full_path=full_path, document_sha256=source_doc.pdf_sha256))

    # Do not let an old/partial ctx.pages hide a mention in the current PDF.
    shadow = copy(ctx)
    shadow.records = deepcopy(ctx.records)
    raw = ctx.pdf_path.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    with fitz.open(stream=raw, filetype="pdf") as pdf:
        shadow.pages = [(i + 1, page.get_text()) for i, page in enumerate(pdf)]
    calculated = evaluate_numeric_pdf(path, ctx.permit_store, ctx.pdf_path, shadow.records,
        company=getattr(ctx, "company", ""), product=ctx.product)
    if (calculated["source_pdf_sha256"] != source_hash
            or calculated["rules_md_sha256"] != loaded.md_sha256):
        raise ValueError("수행 조건과 존재 검사의 원문 스냅샷이 다릅니다.")
    targets = calculated["targets"]
    if len(targets) != 1 or targets[0]["stage"] != spec.stages[0].sp_stage:
        raise ValueError("수행 조건의 독립 단일 배치를 확인할 수 없습니다.")
    target = targets[0]
    batch = _lot([target["batch"]])
    decisions = {d["requirement_id"]: d for d in calculated["decisions"]}
    if (len(decisions) != len(calculated["decisions"]) or set(decisions) != set(by_path.values())
            or any(d["target_id"] != target["target_id"] for d in decisions.values())):
        raise ValueError("수행 조건 결과의 전체 시험/배치 ID가 일치하지 않습니다.")
    gates = {(link["source_id"], batch): decisions[link["source_node_id"]] for link in links}
    result = check_test_presence(params, shadow, applicability=gates)
    result.update(source_identity_links=links, applicability_audit=calculated)

    # Include parent conditions and their exact independent SP facts in the
    # existing evidence link format. No paths or page numbers generated by LLM.
    used_spans = {d["condition_id"] for row in calculated["decisions"] for d in row["conditions"]}
    for node in contract.nodes:
        for span in node["owned_spans"]:
            if span["evidence_id"] in used_spans:
                result["evidence"].append(dict(source="permit", file=node["source_file"], page=span["page_number"],
                    quote=span["text"], source_id=span["evidence_id"], char_start=span["char_start"], char_end=span["char_end"]))
    for fact in target["facts"]:
        result["evidence"].append(dict(source="sp", file=str(ctx.pdf_path), page=fact["page"], quote=fact["quote"],
            source_id=fact["fact_id"], char_start=fact["char_start"], char_end=fact["char_end"]))
    if (_hash(path) != rule._applicability_sha256 or _hash(ctx.pdf_path) != source_hash
            or any(_hash(p) != source_doc.pdf_sha256 for p in ctx.permit_store.permit_pdf_paths)):
        raise ValueError("판정 중 조건 MD/SP/허가 PDF가 변경되었습니다.")
    return result
