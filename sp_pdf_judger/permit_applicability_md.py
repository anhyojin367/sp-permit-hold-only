"""Opt-in reviewed condition MD. No automatic approval, API call or product activation."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, model_validator

from .permit_applicability import (
    ConditionSpec, RequirementSpec, StrictModel, build_contract, read_sp_targets,
    evaluate_reviewed_conditions,
    _conditions_for,
)
from .policy_schema import Text, UniqueKeyLoader, distinct
from .scoped_condition_facts import ScopedStage, read_scoped_targets

EvidenceId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class StageBinding(StrictModel):
    source_node_id: EvidenceId
    sp_stage: Text


class FieldBinding(StrictModel):
    name: Text
    unit: str


class ReviewedCondition(ConditionSpec):
    source_span_id: EvidenceId
    field: Text


class ReviewedRequirement(RequirementSpec):
    source_node_id: EvidenceId
    stage_node_id: EvidenceId
    sp_stage: Text
    combine: Literal["all", "any"]  # Explicit; no implicit default in authoring.


class ApplicabilitySettings(StrictModel):
    schema_version: int = Field(ge=1, le=1)
    id: Text
    company: Text
    product: Text
    review_note: Text
    input_layout: Literal["explicit_page_block_v1", "single_batch_scoped_records_v1"]
    source_scopes: list[ScopedStage] | None = None
    reviewed_document_id: EvidenceId
    reviewed_node_ids: list[EvidenceId] = Field(min_length=1)
    reviewed_condition_ids: list[EvidenceId]
    stages: list[StageBinding] = Field(min_length=1)
    fields: list[FieldBinding]
    requirements: list[ReviewedRequirement] = Field(min_length=1)
    conditions: list[ReviewedCondition]

    @model_validator(mode="after")
    def complete_settings(self):
        for values in (self.reviewed_node_ids, self.reviewed_condition_ids,
                       [s.source_node_id for s in self.stages], [s.sp_stage for s in self.stages],
                       [f.name for f in self.fields], [r.source_node_id for r in self.requirements],
                       [c.source_span_id for c in self.conditions]):
            distinct(values)
        if set(self.reviewed_condition_ids) != {c.source_span_id for c in self.conditions}:
            raise ValueError("검토된 조건 목록과 실행 조건 목록이 다릅니다.")
        fields = {f.name: f.unit for f in self.fields}
        if set(fields) != {c.field for c in self.conditions}:
            raise ValueError("선언한 입력 필드와 조건의 필드가 다릅니다.")
        if any(c.unit != fields[c.field] for c in self.conditions):
            raise ValueError("입력 필드 단위와 조건 단위가 다릅니다.")
        stages = {s.source_node_id: s.sp_stage for s in self.stages}
        if any(stages.get(r.stage_node_id) != r.sp_stage for r in self.requirements):
            raise ValueError("시험 의무의 제조단계 연결이 선언과 다릅니다.")
        if self.input_layout == "explicit_page_block_v1":
            if self.source_scopes is not None:
                raise ValueError("기존 페이지 양식에 표 원문 범위를 섞을 수 없습니다.")
        else:
            scopes = self.source_scopes or []
            distinct([s.sp_stage for s in scopes])
            if {s.sp_stage for s in scopes} != set(stages.values()):
                raise ValueError("모든 제조단계에 명시적인 표 원문 범위가 필요합니다.")
            if any(f.name not in fields for s in scopes for f in s.fields):
                raise ValueError("표 연결에 선언하지 않은 조건 필드가 있습니다.")
        return self


class MdEnvelope(StrictModel):
    applicability: ApplicabilitySettings


class NoAliasLoader(UniqueKeyLoader):
    def construct_mapping(self, node, deep=False):
        if any(key.tag == "tag:yaml.org,2002:merge" for key, _ in node.value):
            raise ValueError("조건 MD의 YAML 별칭/병합은 지원하지 않습니다.")
        return super().construct_mapping(node, deep=deep)

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("조건 MD의 YAML 별칭/병합은 지원하지 않습니다.")
        return super().compose_node(parent, index)


@dataclass(frozen=True)
class ApplicabilityMarkdown:
    _settings_json: str
    md_sha256: str
    source_path: Path

    @property
    def settings(self):
        # Return a fresh model: frozen BaseModel alone does not freeze its lists.
        # A caller cannot mutate conditions while retaining the MD's old hash.
        return ApplicabilitySettings.model_validate_json(self._settings_json)

    def bind(self, store, *, company, product):
        spec = self.settings
        if (company, product) != (spec.company, spec.product):
            raise ValueError("조건 MD의 회사/제품 범위가 제출 문서와 다릅니다.")
        contract = build_contract(store, spec.requirements, spec.conditions,
            reviewed_document_id=spec.reviewed_document_id, reviewed_node_ids=spec.reviewed_node_ids)
        nodes = {n["evidence_id"]: n for n in contract.nodes}
        selected = {s.source_node_id for s in spec.stages}
        for stage in spec.stages:
            node = nodes.get(stage.source_node_id)
            if node is None or node["source_kind"] != "section" or node["leaf"]:
                raise ValueError("검토할 제조단계의 원문이 없거나 말단 문단입니다.")
            if selected.intersection(node["ancestor_ids"]):
                raise ValueError("선택한 제조단계의 하위 범위가 겹칩니다.")
            required = {n["evidence_id"] for n in contract.nodes if n["source_kind"] == "section"
                        and n["leaf"] and stage.source_node_id in n["ancestor_ids"]}
            declared = {r.source_node_id for r in spec.requirements if r.stage_node_id == stage.source_node_id}
            if not required or required != declared:
                raise ValueError("선택한 제조단계의 전체 말단 시험 의무가 누락·추가되었습니다.")
        for scope in spec.source_scopes or []:
            expected = {c.field for r in contract.requirements if r.sp_stage == scope.sp_stage
                        for c in _conditions_for(contract, r)}
            if expected != {f.name for f in scope.fields}:
                raise ValueError("제조단계에 적용되는 모든 조건 필드와 표 연결 목록이 다릅니다.")
        # Including all MD bytes invalidates stale responses even for prose or
        # scope edits. This is provenance, NOT a signature or semantic approval.
        digest = hashlib.sha256(("applicability-md-v1\0" + contract.contract_id + "\0" + self.md_sha256).encode()).hexdigest()
        return replace(contract, contract_id=digest)

    def read_targets(self, pages, *, source_file):
        if self.settings.input_layout != "explicit_page_block_v1":
            raise ValueError("표 양식은 전체 원본 PDF를 읽는 evaluate_numeric_pdf 진입점을 사용해야 합니다.")
        return read_sp_targets(pages, source_file=source_file, fields={f.name for f in self.settings.fields})


def load_applicability_md(path):
    """Load one explicitly selected local MD; never scan or auto-enable rules."""
    path = Path(path)
    if path.suffix.lower() != ".md":
        raise ValueError("조건 설정은 명시적인 MD 파일이어야 합니다.")
    raw = path.read_bytes()
    if len(raw) > 1_000_000:
        raise ValueError("조건 MD가 허용 크기를 초과합니다.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("조건 MD는 UTF-8이어야 합니다.") from exc
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ValueError("조건 MD의 첫 줄에 YAML 시작 구분자가 필요합니다.")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "---"), None)
    if end is None:
        raise ValueError("조건 MD의 YAML 끝 구분자가 없습니다.")
    try:
        data = yaml.load("".join(lines[1:end]), Loader=NoAliasLoader)
    except yaml.YAMLError as exc:
        raise ValueError("조건 MD의 YAML을 해석할 수 없습니다.") from exc
    settings = MdEnvelope.model_validate(data).applicability
    return ApplicabilityMarkdown(settings.model_dump_json(), hashlib.sha256(raw).hexdigest(), path.resolve())


def evaluate_numeric_applicability(rules_md, store, pages, *, source_file, company, product):
    """Explicit reviewed-numeric mode, never a fallback for rejected LLM output.

    `pages` must be the complete physical-page extraction of the selected SP,
    supplied by the document extractor, not an LLM or a caller-created Target.
    Only explicit_page_block_v1 is supported; arbitrary tables remain unsupported.
    MD semantic review is an input premise, not something a hash can establish.
    Invalid bindings raise before any result; missing/ambiguous facts stay unknown.
    """
    if not isinstance(source_file, str) or not source_file.strip():
        raise ValueError("SP 원문 파일 식별자가 필요합니다.")
    loaded = load_applicability_md(rules_md)
    contract = loaded.bind(store, company=company, product=product)
    pages = tuple(pages)
    targets = loaded.read_targets(pages, source_file=source_file)
    if pages[0][0] != 1:
        raise ValueError("SP 물리1쪽부터 전체 페이지 원문을 제공해야 합니다.")
    if {t.stage for t in targets} != {r.sp_stage for r in contract.requirements}:
        raise ValueError("SP 제조단계와 검토한 MD 범위가 다릅니다. 일부 단계를 생략하지 않습니다.")
    return _numeric_result(loaded, contract, targets)


def _numeric_result(loaded, contract, targets, **audit):
    calculated = evaluate_reviewed_conditions(contract, targets).model_dump()
    result = {
        **calculated,
        "execution_mode": "reviewed_numeric_v1", "accepted": True, "errors": [],
        "evaluation_status": "held" if any(d["applicability"] == "unknown" for d in calculated["decisions"]) else "complete",
        "model_used": False, "logical_calls": 0,
        "rules_md_sha256": loaded.md_sha256,
        "permit_document_id": loaded.settings.reviewed_document_id,
        "targets": [t.model_dump() for t in targets],
        "requirements": [r.model_dump() for r in contract.requirements],
        "predicates": [c.model_dump() for c in contract.conditions],
        **audit,
    }
    result["evaluation_id"] = hashlib.sha256(json.dumps(result, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if hashlib.sha256(loaded.source_path.read_bytes()).hexdigest() != loaded.md_sha256:
        raise ValueError("판정 중 MD가 변경되었습니다. 새 설정으로 처음부터 다시 검증해야 합니다.")
    return result


def evaluate_numeric_pdf(rules_md, store, sp_pdf, records, *, company, product):
    """Read a complete immutable PDF snapshot; verify scoped extractor records.

    Records are not an authority for values: field values and positions come
    from the PDF, with full scoped-field coverage checked against the records.
    This version does not OCR scanned tables or support multiple lots per stage.
    """
    from copy import deepcopy
    from types import SimpleNamespace
    import fitz

    loaded = load_applicability_md(rules_md)
    if loaded.settings.input_layout != "single_batch_scoped_records_v1":
        raise ValueError("표 PDF 진입점에는 single_batch_scoped_records_v1 MD가 필요합니다.")
    contract = loaded.bind(store, company=company, product=product)
    sp_pdf = Path(sp_pdf)
    if sp_pdf.suffix.lower() != ".pdf":
        raise ValueError("SP 원본 PDF가 필요합니다.")
    raw = sp_pdf.read_bytes()
    fingerprint = hashlib.sha256(raw).hexdigest()
    with fitz.open(stream=raw, filetype="pdf") as document:
        pages = [(i + 1, page.get_text()) for i, page in enumerate(document)]
    ctx = SimpleNamespace(pages=pages, records=deepcopy(tuple(records)))
    targets, source_audit = read_scoped_targets(ctx, loaded.settings.source_scopes,
        source_file=sp_pdf.name, pdf_sha256=fingerprint)
    result = _numeric_result(loaded, contract, targets, source_pdf_sha256=fingerprint,
                             source_page_count=len(pages), source_scope_audit=source_audit)
    if hashlib.sha256(sp_pdf.read_bytes()).hexdigest() != fingerprint:
        raise ValueError("판정 중 SP PDF가 변경되었습니다. 처음부터 다시 추출해야 합니다.")
    return result
