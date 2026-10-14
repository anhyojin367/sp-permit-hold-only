"""Narrow, source-verified single-batch table adapter; never semantic approval."""
from collections import Counter
import re
import unicodedata
from typing import Literal

from pydantic import model_validator

from .permit_applicability import Fact, Target, StrictModel, _digest
from .policy_schema import Text, TitlePath, distinct, identity
from .policy_required_tests import _coverage, _within, _record_path, _path, _field_values, _lot


class RecordScope(StrictModel):
    record_path: TitlePath
    record_type: Literal["content", "info", "test"]
    start: Text
    end: Text
    test_name: Text | None = None

    @model_validator(mode="after")
    def exact_scope(self):
        distinct([self.start, self.end])
        if (self.record_type == "test") != bool(self.test_name):
            raise ValueError("시험 원문 범위에는 정확한 시험명이 필요합니다.")
        return self


class ScopedField(StrictModel):
    name: Text
    label: Text
    source: RecordScope


class ScopedStage(StrictModel):
    sp_stage: Text
    batch_field: Text
    batch_source: RecordScope
    fields: list[ScopedField]

    @model_validator(mode="after")
    def separate_batch(self):
        if self.batch_source.record_type == "test":
            raise ValueError("독립 배치 목록은 시험 레코드에서 가져오지 않습니다.")
        distinct([f.name for f in self.fields])
        return self


def _occurrences(text, label):
    """Keep character positions and quote both label and adjacent value line."""
    lines = [m for m in re.finditer(r"[^\n]+", text) if m[0].strip()]
    pattern = re.compile(r"^\s*" + r"[ \t]*".join(map(re.escape, identity(label))) + r"(?=$|[^\w])(.*)$", re.I)
    result = []
    for index, line in enumerate(lines):
        match = pattern.fullmatch(line[0])
        if match is None:
            continue
        tail, end = match[1].strip(), line.end()
        if tail.startswith((":", "|")):
            value = tail[1:].strip()
        elif tail:
            value = tail
        elif index + 1 < len(lines):
            value, end = lines[index + 1][0].strip(), lines[index + 1].end()
        else:
            value = ""
        result.append((value, line.start(), end, text[line.start():end]))
    return result


def _scoped_records(ctx, spec):
    coverage, error = _coverage(ctx, spec.start, spec.end)
    if error:
        raise ValueError(error)
    selected = [r for r in ctx.records if r.record_type == spec.record_type
                and _record_path(r) == _path(spec.record_path)
                and (spec.test_name is None or identity(r.test_name or "") == identity(spec.test_name))]
    if len(selected) > 1 or any(not _within(r, coverage) for r in selected):
        raise ValueError("조건 정보 레코드가 중복되었거나 원문 범위/페이지와 다릅니다.")
    if spec.test_name and any(identity(spec.test_name) not in identity(r.raw_text or "") for r in selected):
        raise ValueError("조건의 시험명이 그 시험 원문에 없습니다.")
    return coverage, selected


def _source_fields(ctx, coverage, records, label):
    fields = []
    by_page = {n: unicodedata.normalize("NFKC", t) for n, t in ctx.pages}
    for number, fragment in coverage["raw_pages"]:
        page = by_page[number]
        if not fragment:
            continue
        if page.count(fragment) != 1:
            raise ValueError("조건 원문의 페이지 문자 위치를 유일하게 연결할 수 없습니다.")
        base = page.index(fragment)
        fields.extend((number, value, base + start, base + end, quote)
                      for value, start, end, quote in _occurrences(fragment, label))
    parsed = [v for r in records for v in _field_values(r.raw_text or "", label, "label_value_lines")]
    if Counter(identity(v) for v in parsed) != Counter(identity(f[1]) for f in fields):
        raise ValueError("조건 필드의 원문 전체 목록과 해당 절의 추출 레코드가 일치하지 않습니다.")
    return fields


def read_scoped_targets(ctx, scopes, *, source_file, pdf_sha256):
    """Internal adapter for a caller that has extracted ALL pages of one PDF.

    The public PDF entry point supplies the byte hash and full page list. This
    helper alone does not authenticate a caller-provided context or PDF hash.
    """
    if (not ctx.pages or any(type(n) is not int or not isinstance(t, str) for n, t in ctx.pages)
            or [n for n, _ in ctx.pages] != list(range(1, len(ctx.pages) + 1))):
        raise ValueError("SP 물리1쪽부터 전체 연속 페이지가 필요합니다.")
    scopes = [ScopedStage.model_validate(s.model_dump()) for s in scopes]
    if not scopes or len({identity(s.sp_stage) for s in scopes}) != len(scopes):
        raise ValueError("명시적인 제조단계 범위가 없거나 중복되었습니다.")
    document_id = _digest("scoped-sp-pdf-v1", pdf_sha256, source_file, ctx.pages)
    targets, audits, ranges = [], [], []
    for scope in scopes:
        coverage, records = _scoped_records(ctx, scope.batch_source)
        batch_fields = _source_fields(ctx, coverage, records, scope.batch_field)
        if len(records) != 1 or not _lot([f[1] for f in batch_fields]):
            raise ValueError("이 양식은 독립 정보 절에서 확인된 유일한 단일 배치만 지원합니다.")
        batch = batch_fields[0][1]
        target_id = _digest("scoped-sp-target-v1", document_id, scope.sp_stage, batch)
        ranges.append((scope.sp_stage, coverage["offset_start"], coverage["offset_end"]))
        audit = {"stage": scope.sp_stage, "batch": batch, "batch_record_orders": [r.order_idx for r in records],
                 "batch_source": scope.batch_source.model_dump(), "batch_quote": batch_fields[0][4],
                 "batch_page": batch_fields[0][0], "batch_char_start": batch_fields[0][2],
                 "batch_char_end": batch_fields[0][3], "fields": []}
        facts = []
        for field in scope.fields:
            coverage, records = _scoped_records(ctx, field.source)
            ranges.append((scope.sp_stage, coverage["offset_start"], coverage["offset_end"]))
            fields = _source_fields(ctx, coverage, records, field.label)
            # An explicit conflicting/unsupported lot cannot be replaced by the
            # stage's single-batch default. Inspect the entire source scope too.
            lots = [v for _, text in coverage["raw_pages"] for v in _field_values(text, scope.batch_field, "label_value_lines")]
            if lots and (any(not _lot([v]) for v in lots) or {_lot([v]) for v in lots} != {_lot([batch])}):
                raise ValueError("조건 원문에 다른 배치 또는 불명확한 제조번호가 있습니다.")
            for page, value, start, end, quote in fields:
                facts.append(Fact(fact_id=_digest("scoped-sp-fact-v1", target_id, field.name, page, start, end, quote),
                    field=field.name, raw_value=value, quote=quote, page=page, char_start=start, char_end=end))
            audit["fields"].append({"name": field.name, "source": field.source.model_dump(),
                                    "record_orders": [r.order_idx for r in records], "raw_field_count": len(fields)})
        targets.append(Target(target_id=target_id, stage=scope.sp_stage, batch=batch, source_file=source_file,
                              document_id=document_id, facts=tuple(facts)))
        audits.append(audit)
    for i, (stage, start, end) in enumerate(ranges):
        if any(other != stage and max(start, a) < min(end, b) for other, a, b in ranges[i + 1:]):
            raise ValueError("서로 다른 제조단계의 조건 원문 범위가 겹칩니다.")
    return tuple(targets), audits
