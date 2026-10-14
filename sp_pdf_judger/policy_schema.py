"""Strict authoring contract for MD parameters; no document or LLM access."""
from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def identity(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def distinct(values: list[str]) -> list[str]:
    if len({identity(value) for value in values}) != len(values):
        raise ValueError("duplicate names after whitespace/case normalization")
    return values


Text = Annotated[str, Field(min_length=1), AfterValidator(nonblank)]
Names = Annotated[list[Text], Field(min_length=1), AfterValidator(distinct)]


def regex(value: str) -> str:
    try:
        compiled = re.compile(value)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc
    if compiled.search("") is not None:
        raise ValueError("pattern must not match empty text")
    return value


def captured_regex(value: str) -> str:
    regex(value)
    if re.compile(value).groups < 1:
        raise ValueError("pattern needs at least one capture group")
    return value


Pattern = Annotated[Text, AfterValidator(regex)]
CapturePattern = Annotated[Text, AfterValidator(captured_regex)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RuleSelector(StrictModel):
    type: Text | None = None
    context: list[Text] = Field(default_factory=list)
    test: Text | None = None


class NoParams(StrictModel):
    pass


class Requirement(StrictModel):
    """Retained user requirement, distinct from an executed verdict/count."""
    id: Annotated[Text, Field(pattern=r"^(?:[ABC]\.\d+|G\d{2})$")]
    instruction: Text
    rule_ids: list[Text] = Field(default_factory=list)
    components: list[Text] = Field(default_factory=list)
    coverage: Literal["active", "partial", "requires_reference"]
    scope: Text


class VersionParams(StrictModel):
    pattern: Pattern = r"Ver\.?\s*([\d.]+)\s*\(([^)]+)\)"


class DiagramParams(StrictModel):
    field: Text
    stage: Text | None = None
    stage_aliases: dict[Text, Names] = Field(default_factory=dict)


class RequiredTestsParams(StrictModel):
    tests: Names


TitlePath = Annotated[list[Text], Field(min_length=1)]


class RequiredTestNameMapping(StrictModel):
    permit_test_path: TitlePath
    sp_test_paths: list[TitlePath] = Field(min_length=1)
    reason: Text


class PermitTestPresenceParams(StrictModel):
    """Source and identity contract, independent of the obligation policy."""
    permit_stage_path: TitlePath
    reviewed_scope_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    reviewed_document_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    sp_stage_path: TitlePath
    coverage_start: Text
    coverage_end: Text
    batch_inventory: RuleSelector
    batch_field: Text
    batch_field_layout: Literal["delimited_line", "label_value_lines"] = "delimited_line"
    batch_inventory_path: TitlePath | None = None
    batch_coverage_start: Text | None = None
    batch_coverage_end: Text | None = None
    test_coverage_start: Text | None = None
    test_coverage_end: Text | None = None
    batch_binding: Literal["single_batch_stage", "explicit_test_field"]
    test_batch_field: Text | None = None
    name_mappings: list[RequiredTestNameMapping] = Field(default_factory=list)

    @model_validator(mode="after")
    def unambiguous_contract(self):
        distinct([self.coverage_start, self.coverage_end])
        if self.batch_inventory.type not in {"content", "info"} or not self.batch_inventory.context:
            raise ValueError("batch_inventory needs a scoped content/info selector")
        if (self.batch_binding == "explicit_test_field") != bool(self.test_batch_field):
            raise ValueError("test_batch_field is required only for explicit_test_field binding")
        scope = (self.batch_inventory_path, self.batch_coverage_start, self.batch_coverage_end)
        if any(scope) or self.batch_field_layout == "label_value_lines":
            if not all(scope):
                raise ValueError("label/value inventory needs exact path and both batch coverage boundaries")
            distinct([self.batch_coverage_start, self.batch_coverage_end])
        test_scope = (self.test_coverage_start, self.test_coverage_end)
        if any(test_scope):
            if not all(test_scope) or not all(scope):
                raise ValueError("independent test coverage needs both boundaries and a scoped batch inventory")
            distinct(list(test_scope))
        seen_permit, seen_sp = set(), set()
        for item in self.name_mappings:
            permit_key = tuple(map(identity, item.permit_test_path))
            if permit_key in seen_permit:
                raise ValueError("duplicate permit test path")
            seen_permit.add(permit_key)
            for path in item.sp_test_paths:
                key = tuple(map(identity, path))
                if key in seen_sp:
                    raise ValueError("a SP test path cannot map to multiple requirements")
                seen_sp.add(key)
        return self


class PermitRequiredTestsParams(PermitTestPresenceParams):
    """Opt-in, reviewed unconditional subtree; never an inferred obligation."""
    requirement_policy: Literal["all_leaf_sections_required"]


class PermitConditionalTestsParams(PermitTestPresenceParams):
    requirement_policy: Literal["reviewed_numeric_applicability"]
    applicability_md: Text

    @model_validator(mode="after")
    def narrow_conditional_scope(self):
        # A companion is local to the explicit RuleBook. No arbitrary file paths.
        parts = self.applicability_md.split("/")
        if (len(parts) < 2 or parts[0] != "_conditions" or any(p in {"", ".", ".."} for p in parts)
                or any(c in self.applicability_md for c in "\\:") or not parts[-1].endswith(".md")):
            raise ValueError("applicability_md must be a relative _conditions/*.md path")
        if (self.batch_binding != "single_batch_stage" or self.batch_field_layout != "label_value_lines"
                or not self.batch_inventory_path or not self.test_coverage_start):
            raise ValueError("conditional presence requires an exact independent single-batch and test scope")
        return self


class DurationParams(StrictModel):
    minimum_days: int = Field(ge=0)
    inclusive: bool = False


class ElapsedTimeParams(StrictModel):
    start_field: Text
    end_field: Text
    duration_field: Text
    tolerance_seconds: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def separate_fields(self):
        distinct([self.start_field, self.end_field, self.duration_field])
        return self


class ExpiryParams(StrictModel):
    start_field: Text
    end_field: Text
    duration_field: Text
    end_policy: Literal["not_after", "exact"] = "not_after"

    @model_validator(mode="after")
    def separate_fields(self):
        distinct([self.start_field, self.end_field, self.duration_field])
        return self


class SignatureOrderParams(StrictModel):
    tests: RuleSelector


class ProcessOrderParams(StrictModel):
    date_field: Text = "제조년월일"
    allow_same_day: bool = True


class StorageWindowParams(StrictModel):
    target: RuleSelector


class MassBalanceParams(StrictModel):
    output_field: Text
    start_marker: Text
    end_marker: Text
    amount_header: Text = "분량"
    comparison: Literal["not_exceed", "equal"] = "not_exceed"

    @model_validator(mode="after")
    def separate_markers(self):
        distinct([self.start_marker, self.end_marker])
        return self


class PermitFieldParams(StrictModel):
    sp_field: Text
    permit_pattern: CapturePattern
    sp_pattern: CapturePattern | None = None


class PermitTestParams(StrictModel):
    permit_stage: list[Text] = Field(default_factory=list)
    permit_pattern: CapturePattern
    sp_pattern: CapturePattern
    sp_attribute: Literal["method", "criteria", "result", "test_name", "content", "remarks"]

    @model_validator(mode="after")
    def matching_capture_counts(self):
        if re.compile(self.permit_pattern).groups != re.compile(self.sp_pattern).groups:
            raise ValueError("permit_pattern and sp_pattern need the same capture-group count")
        return self


class NumericParams(StrictModel):
    sp_supplement_if_no_numeric_limit: bool = False


class LabelledNumericParams(NumericParams):
    expected_labels: Names


class UnitAlias(StrictModel):
    test: Text
    from_unit: Text = Field(alias="from")
    to: Text


class UnitParams(StrictModel):
    unit_aliases: list[UnitAlias] = Field(default_factory=list)
    expected_units: dict[Text, Text] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unambiguous_aliases(self):
        seen = set()
        for alias in self.unit_aliases:
            key = (identity(alias.test), identity(alias.from_unit))
            if key in seen:
                raise ValueError("duplicate unit alias source for the same test")
            seen.add(key)
        return self


class StagePair(StrictModel):
    source: Names
    target: Names


class TestAfterManufactureParams(StrictModel):
    stages: list[StagePair] = Field(min_length=1)


class StoragePeriodParams(StrictModel):
    period_field: Text = "저장기간"
    start_field: Text = "제조년월일"
    duration_field: Text = "허가된 저장기간"

    @model_validator(mode="after")
    def separate_fields(self):
        distinct([self.period_field, self.start_field, self.duration_field])
        return self


class LotParams(StrictModel):
    fields: Names


PARAM_MODELS = {
    **{name: NoParams for name in ("page_continuity", "signature", "precision", "semantic_review", "duplicate_tests")},
    "version_consistency": VersionParams,
    "diagram_consistency": DiagramParams,
    "required_tests": RequiredTestsParams,
    "permit_required_tests": PermitRequiredTestsParams,
    "permit_conditional_tests": PermitConditionalTestsParams,
    "duration": DurationParams,
    "elapsed_time": ElapsedTimeParams,
    "expiry": ExpiryParams,
    "signature_order": SignatureOrderParams,
    "process_order": ProcessOrderParams,
    "storage_window": StorageWindowParams,
    "mass_balance": MassBalanceParams,
    "permit_field": PermitFieldParams,
    "permit_test": PermitTestParams,
    "numeric_compare": NumericParams,
    "minimum_count": NumericParams,
    "labelled_numeric_compare": LabelledNumericParams,
    "unit_consistency": UnitParams,
    "test_after_manufacture": TestAfterManufactureParams,
    "storage_period": StoragePeriodParams,
    "lot_consistency": LotParams,
}


def validate_params(rule_id, operation, params):
    if operation not in PARAM_MODELS:
        raise ValueError(f"Unknown operation: {rule_id}: {operation}")
    try:
        PARAM_MODELS[operation].model_validate(params)
    except ValueError as exc:
        raise ValueError(f"Invalid params for {rule_id} ({operation}): {exc}") from exc


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader normally overwrites duplicate keys; policies must not."""

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
                seen.add(key)
            except TypeError as exc:
                raise ValueError("MD mapping keys must be scalar values") from exc
            if duplicate:
                raise ValueError(f"Duplicate MD key '{key}' at line {key_node.start_mark.line + 1}")
        return super().construct_mapping(node, deep=deep)


def load_frontmatter(text):
    try:
        value = yaml.load(text, Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid MD YAML: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("rules"), list):
        raise ValueError("Rule list missing")
    unknown = set(value) - {"rules", "permit_search_aliases", "result_aliases", "requirements"}
    if unknown:
        raise ValueError(f"Unknown MD root fields: {sorted(map(str, unknown))}")
    return value
