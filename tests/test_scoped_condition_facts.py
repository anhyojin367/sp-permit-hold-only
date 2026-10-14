"""Source-scoped facts: exact records, whole field inventory, one independent batch."""
from copy import deepcopy
import importlib
from types import SimpleNamespace

import pytest

from tests.test_permit_applicability_md import config_data, write_md, load


INFO = "제조번호\nA-01\n용기규격\n100 mL"
STERILE = "시험명: 무균시험\n시험차수: 2"
CONTENT = "시험명: 함량시험\n농도: 20 mg/mL"
PAGE = "머리말 제조번호 FIN-99\n1.1 A 정보\n" + INFO + "\n1.2 B 정보\n제조번호 B-99\n용기규격 50 mL\n" \
    "2.1 A 무균시험\n" + STERILE + "\n2.2 A 함량시험\n" + CONTENT + "\n3. 다음 단계"


def record(kind, path, raw, index, test=None):
    return SimpleNamespace(record_type=kind, section_path=[{"title": t} for t in path],
        test_name=test, raw_text=raw, content=raw if kind == "content" else None, remarks=None,
        page_start=1, page_end=1, ocr_suspect=False, order_idx=index)


def fixture():
    records = [record("content", ["원액", "정보", "A"], INFO, 1),
               record("test", ["원액", "시험", "A"], STERILE, 2, "무균시험"),
               record("test", ["원액", "시험", "A"], CONTENT, 3, "함량시험")]
    ctx = SimpleNamespace(pages=[(1, PAGE)], records=records)
    def scope(path, start, end, kind="content", test=None):
        result = dict(record_path=path, record_type=kind, start=start, end=end)
        if test: result["test_name"] = test
        return result
    info = scope(["원액", "정보", "A"], "1.1 A 정보", "1.2 B 정보")
    spec = dict(sp_stage="원액", batch_field="제조번호", batch_source=info, fields=[
        dict(name="용기규격", label="용기규격", source=deepcopy(info)),
        dict(name="시험차수", label="시험차수", source=scope(["원액", "시험", "A"], "2.1 A 무균시험", "2.2 A 함량시험", "test", "무균시험")),
        dict(name="농도", label="농도", source=scope(["원액", "시험", "A"], "2.2 A 함량시험", "3. 다음 단계", "test", "함량시험")),
    ])
    return ctx, spec


def read(ctx, specs):
    api = importlib.import_module("sp_pdf_judger.scoped_condition_facts")
    return api.read_scoped_targets(ctx, [api.ScopedStage.model_validate(s) for s in specs],
        source_file="sp.pdf", pdf_sha256="a"*64)


def test_exact_scoped_record_facts_keep_page_offsets_and_ignore_other_component():
    ctx, spec = fixture()
    targets, audit = read(ctx, [spec])
    assert len(targets) == 1 and targets[0].batch == "A-01"
    assert [(f.field, f.raw_value) for f in targets[0].facts] == [("용기규격", "100 mL"), ("시험차수", "2"), ("농도", "20 mg/mL")]
    assert audit[0]["batch"] == "A-01" and audit[0]["batch_record_orders"] == [1]
    for fact in targets[0].facts:
        assert fact.quote == PAGE[fact.char_start:fact.char_end]
        assert fact.page == 1
    assert read(ctx, [spec])[0] == targets
    changed, _ = read(ctx, [spec])
    assert changed[0].document_id == targets[0].document_id


@pytest.mark.parametrize("case", ["wrong_record_path", "missing_record", "duplicate_record", "wrong_page",
    "forged_raw", "ocr", "extra_raw_batch", "ambiguous_batch", "cross_batch", "missing_end",
    "repeated_boundary", "reversed_boundary", "raw_field_unextracted", "outside_test_scope", "wrong_test"])
def test_uncertain_source_binding_is_rejected(tmp_path, case):
    ctx, spec = fixture()
    if case == "wrong_record_path": ctx.records[0].section_path[-1]["title"] = "B"
    if case == "missing_record": ctx.records.pop(0)
    if case == "duplicate_record": ctx.records.append(deepcopy(ctx.records[0]))
    if case == "wrong_page": ctx.records[0].page_start = ctx.records[0].page_end = 2
    if case == "forged_raw": ctx.records[0].raw_text = INFO.replace("100", "50")
    if case == "ocr": ctx.records[0].ocr_suspect = True
    if case == "extra_raw_batch": ctx.pages = [(1, PAGE.replace("1.2 B 정보", "제조번호 A-02\n1.2 B 정보"))]
    if case == "ambiguous_batch":
        ctx.pages = [(1, PAGE.replace("A-01", "A-01/A-02"))]
        ctx.records[0].raw_text = INFO.replace("A-01", "A-01/A-02")
    if case == "cross_batch":
        ctx.pages = [(1, PAGE.replace(STERILE, STERILE+"\n제조번호 B-99"))]
        ctx.records[1].raw_text += "\n제조번호 B-99"
    if case == "missing_end": spec["batch_source"]["end"] = "없는 경계"
    if case == "repeated_boundary": ctx.pages = [(1, PAGE + "\n1.1 A 정보")]
    if case == "reversed_boundary": spec["batch_source"]["start"], spec["batch_source"]["end"] = "1.2 B 정보", "1.1 A 정보"
    if case == "raw_field_unextracted":
        ctx.pages = [(1, PAGE.replace("2.2 A 함량시험", "시험차수: 1\n2.2 A 함량시험"))]
    if case == "outside_test_scope": spec["fields"][1]["source"]["start"] = "시험차수: 2"
    if case == "wrong_test": ctx.records[1].test_name = "다른시험"
    with pytest.raises(ValueError): read(ctx, [spec])


def test_missing_fact_never_borrows_other_section_value():
    ctx, spec = fixture()
    ctx.pages = [(1, PAGE.replace("용기규격\n100 mL", ""))]
    ctx.records[0].raw_text = INFO.replace("용기규격\n100 mL", "")
    targets, _ = read(ctx, [spec])
    assert [f.field for f in targets[0].facts] == ["시험차수", "농도"]


def test_duplicate_and_conflicting_facts_are_preserved_not_cherry_picked():
    ctx, spec = fixture()
    ctx.pages = [(1, PAGE.replace(INFO, INFO + "\n용기규격 50 mL"))]
    ctx.records[0].raw_text += "\n용기규격 50 mL"
    targets, _ = read(ctx, [spec])
    assert [f.raw_value for f in targets[0].facts if f.field == "용기규격"] == ["100 mL", "50 mL"]


@pytest.mark.parametrize("pages", [[], [(2, PAGE)], [(1, PAGE), (3, "last")], [(1, PAGE), (1, PAGE)]])
def test_complete_consecutive_physical_pages_are_required(pages):
    ctx, spec = fixture()
    ctx.pages = pages
    with pytest.raises(ValueError): read(ctx, [spec])


def test_two_stage_mappings_cannot_reuse_same_source_scope():
    ctx, spec = fixture()
    other = deepcopy(spec)
    other["sp_stage"] = "완제"
    with pytest.raises(ValueError): read(ctx, [spec, other])


def test_md_new_layout_requires_explicit_complete_scope_bindings(tmp_path):
    _, store, _, _, config = config_data()
    _, spec = fixture()
    config.update(input_layout="single_batch_scoped_records_v1", source_scopes=[spec])
    md = load(write_md(tmp_path, config))
    md.bind(store, company="합성제조사", product="합성시험제품")
    with pytest.raises(ValueError): md.read_targets([(1, PAGE)], source_file="sp.pdf")


@pytest.mark.parametrize("case", ["no_scopes", "old_with_scopes", "missing_field", "extra_field", "wrong_stage", "duplicate_stage", "missing_test_name", "test_batch_source"])
def test_invalid_md_adapter_contract_is_rejected(tmp_path, case):
    _, store, _, _, config = config_data()
    _, spec = fixture()
    config.update(input_layout="single_batch_scoped_records_v1", source_scopes=[spec])
    if case == "no_scopes": config.pop("source_scopes")
    if case == "old_with_scopes": config["input_layout"] = "explicit_page_block_v1"
    if case == "missing_field": spec["fields"].pop()
    if case == "extra_field": spec["fields"][0]["name"] = "없는필드"
    if case == "wrong_stage": spec["sp_stage"] = "완제"
    if case == "duplicate_stage": config["source_scopes"].append(deepcopy(spec))
    if case == "missing_test_name": spec["fields"][1]["source"].pop("test_name")
    if case == "test_batch_source": spec["batch_source"] = deepcopy(spec["fields"][1]["source"])
    with pytest.raises(ValueError): load(write_md(tmp_path, config)).bind(store, company="합성제조사", product="합성시험제품")
