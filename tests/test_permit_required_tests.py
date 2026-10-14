"""Permit-driven presence is stage-, source-, hierarchy- and batch-scoped."""
from dataclasses import replace
from pathlib import Path

import pytest

from sp_pdf_judger.permit_catalog import PermitPolicy
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.policy_engine import PolicyContext, Rule, permit_required_tests, to_evaluations
from sp_pdf_judger.policy_required_tests import permit_scope_inventory
from sp_pdf_judger.schemas import ExtractedRecord


PERMIT_PATH = ["제조방법 및 시험기준", "원액"]
PERMIT = "1. 제조방법 및 시험기준\n1.1. 원액\n1.1.1. 무균시험\n균이 없어야 한다.\n1.1.2. 함량시험\n90 이상이어야 한다."


def store(text=PERMIT, source="permit.pdf"):
    return PermitPdfStore.from_page_texts([(1, text)],
        policy=PermitPolicy("linked", True, True, True, "auto"), source_file=source)


def record(name, batch=None, stage="원액", result="적합", parent=None):
    remarks = f"제조번호: {batch}" if batch is not None else None
    raw = f"시험명: {name}\n결과: {result}" + (f"\n{remarks}" if remarks else "")
    return ExtractedRecord(record_type="test", section_title=stage,
        section_path=[{"title": stage}], test_name=name, parent_test_group=parent,
        result=result, remarks=remarks, raw_text=raw, page_start=1, page_end=1)


def context(records=None, batches=("L1",), permit=None):
    records = records if records is not None else [record("무균시험"), record("함량시험")]
    inventory = [ExtractedRecord(record_type="content", section_title="배치목록",
        section_path=[{"title": "원액"}, {"title": "배치목록"}], content=f"제조번호: {batch}",
        page_start=1, page_end=1) for batch in batches]
    ctx = PolicyContext.__new__(PolicyContext)
    ctx.pdf_path = Path("synthetic.pdf")
    ctx.records = inventory + records
    for index, item in enumerate(ctx.records):
        item.order_idx = index
    ctx.pages = [(1, "1 원액\n" + "\n".join(r.raw_text or r.content or "" for r in ctx.records) + "\n2 다음단계")]
    ctx.permit_store = permit or store()
    return ctx


def rule(ctx, **changes):
    source = permit_scope_inventory(ctx.permit_store, PERMIT_PATH)
    params = dict(permit_stage_path=PERMIT_PATH,
        reviewed_scope_sha256=source.get("reviewed_scope_sha256", "0" * 64),
        reviewed_document_sha256=source.get("document_sha256", "0" * 64),
        requirement_policy="all_leaf_sections_required", sp_stage_path=["원액"],
        coverage_start="1 원액", coverage_end="2 다음단계",
        batch_inventory={"type": "content", "context": ["배치목록"]},
        batch_field="제조번호", batch_binding="single_batch_stage")
    params.update(changes)
    return Rule(id="REQ", title="허가 필수시험", instruction="검토한 허가 단계의 모든 필수시험을 배치마다 검사",
                operation="permit_required_tests", params=params, aliases=["A.9"], products=["합성시험제품"])


def check(ctx, **changes):
    return permit_required_tests(rule(ctx, **changes), ctx)


def test_complete_scope_pass_has_source_ids_batches_and_both_evidence_kinds():
    result = check(context())
    assert result.status == "PASS"
    assert result.details["checked"] == 2
    assert result.details["presence_only"] is True
    assert {e["source"] for e in result.evidence} == {"sp", "permit"}
    assert len({r["source_id"] for r in result.details["requirement_checks"]}) == 2
    assert {r["batch"] for r in result.details["requirement_checks"]} == {"L1"}


def test_missing_test_is_discovered_from_permit_not_sp_queries():
    result = check(context([record("무균시험")]))
    assert result.status == "FAIL" and "함량시험" in result.reason


@pytest.mark.parametrize("variant", ["other_stage", "heading", "parent_only", "split_records", "split_fields"])
def test_presence_cannot_be_borrowed_from_unrelated_identity(variant):
    rows = [record("무균시험")]
    if variant == "other_stage": rows += [record("함량시험", stage="다른 원액")]
    if variant == "heading": rows += [replace(record("함량시험"), record_type="heading")]
    if variant == "parent_only": rows += [record("하위값", parent="함량시험")]
    if variant == "split_records": rows += [record("함량"), record("시험")]
    if variant == "split_fields": rows += [record("함량", parent="시험")]
    assert check(context(rows)).status != "PASS"


def test_text_mention_without_extracted_test_is_hold_not_false_missing():
    ctx = context([record("무균시험")])
    ctx.pages = [(1, ctx.pages[0][1].replace("2 다음단계", "시험명: 함량시험\n결과: 95\n2 다음단계"))]
    result = check(ctx)
    assert result.status == "HOLD" and not result.details.get("execution_error")


def test_one_batch_does_not_fill_another():
    ctx = context([record("무균시험", "L1"), record("함량시험", "L1"), record("무균시험", "L2")], ("L1", "L2"))
    result = check(ctx, batch_binding="explicit_test_field", test_batch_field="제조번호")
    assert result.status == "FAIL"
    failures = [row for row in result.details["requirement_checks"] if row["status"] == "FAIL"]
    assert [(r["batch"], r["test_path"]) for r in failures] == [("L2", ["함량시험"])]


def test_two_complete_batches_pass():
    ctx = context([record(name, lot) for lot in ("L1", "L2") for name in ("무균시험", "함량시험")], ("L1", "L2"))
    assert check(ctx, batch_binding="explicit_test_field", test_batch_field="제조번호").details["checked"] == 4
    assert check(ctx, batch_binding="explicit_test_field", test_batch_field="제조번호").status == "PASS"


def test_multiple_batches_cannot_use_single_batch_shortcut():
    assert check(context(batches=("L1", "L2"))).status == "HOLD"


@pytest.mark.parametrize("batch", [None, "L3", "L1,L2", "", "미정"])
def test_ambiguous_batch_links_do_not_turn_absence_into_fail(batch):
    ctx = context([record("무균시험", "L1"), record("함량시험", batch)], ("L1", "L2"))
    result = check(ctx, batch_binding="explicit_test_field", test_batch_field="제조번호")
    assert result.status == "HOLD"


def test_unextracted_second_batch_mention_is_not_overlooked():
    ctx = context([record("무균시험", "L1"), record("함량시험", "L1"), record("무균시험", "L2")], ("L1", "L2"))
    ctx.pages = [(1, ctx.pages[0][1].replace("2 다음단계", "시험명: 함량시험\n결과: 99\n제조번호: L2\n2 다음단계"))]
    assert check(ctx, batch_binding="explicit_test_field", test_batch_field="제조번호").status == "HOLD"


@pytest.mark.parametrize("batches", [(), ("미정",), ("L1", "L1"), ("L1/L2", "L1/L2")])
def test_missing_or_duplicate_inventory_is_hold(batches):
    assert check(context(batches=batches)).status == "HOLD"


@pytest.mark.parametrize("mutate", ["missing_start", "missing_end", "duplicate", "reverse", "blank_page", "missing_page", "ocr", "out_of_page", "out_of_region"])
def test_source_coverage_is_required_before_absence_claim(mutate):
    ctx = context([record("무균시험")])
    text = ctx.pages[0][1]
    if mutate == "missing_start": ctx.pages = [(1, text.replace("1 원액", ""))]
    if mutate == "missing_end": ctx.pages = [(1, text.replace("2 다음단계", ""))]
    if mutate == "duplicate": ctx.pages = [(1, text + "\n1 원액")]
    if mutate == "reverse": ctx.pages = [(1, "2 다음단계\n" + text.replace("2 다음단계", ""))]
    if mutate == "blank_page": ctx.pages = [(1, text.replace("2 다음단계", "")), (2, ""), (3, "2 다음단계")]
    if mutate == "missing_page": ctx.pages = [(1, text.replace("2 다음단계", "")), (3, "2 다음단계")]
    if mutate == "ocr": ctx.records[-1].ocr_suspect = True
    if mutate == "out_of_page": ctx.records[-1].page_end = 2
    if mutate == "out_of_region": ctx.pages = [(1, "1 원액\n제조번호: L1\n2 다음단계\n" + ctx.records[-1].raw_text)]
    assert check(ctx).status == "HOLD"


def test_known_missing_is_preserved_alongside_missing_result():
    result = check(context([record("무균시험", result="")]))
    assert result.status == "FAIL" and "함량시험" in result.reason
    assert result.details["incomplete_evidence"]


def test_duplicate_performed_test_is_hold():
    assert check(context([record("무균시험"), record("무균시험"), record("함량시험")])).status == "HOLD"


def test_changed_or_conditional_permit_requires_new_review():
    ctx = context()
    original = rule(ctx)
    ctx.permit_store = store(PERMIT.replace("균이 없어야 한다.", "재시험인 경우에만 수행한다. 균이 없어야 한다."))
    assert permit_required_tests(original, ctx).status == "HOLD"


@pytest.mark.parametrize("change", ["ancestor", "preamble"])
def test_conditions_outside_the_stage_subtree_also_invalidate_review(change):
    ctx = context()
    original = rule(ctx)
    if change == "ancestor":
        text = PERMIT.replace("1. 제조방법 및 시험기준", "1. 제조방법 및 시험기준\n아래 시험들은 재시험인 경우에만 실시한다.")
    else:
        text = "적용조건: 아래 시험들은 재시험인 경우에만 실시한다.\n" + PERMIT
    ctx.permit_store = store(text)
    assert permit_required_tests(original, ctx).status == "HOLD"


@pytest.mark.parametrize("variant", ["none", "extraction_error", "duplicate_stage", "different_revision", "missing_stage", "no_children"])
def test_ambiguous_permit_is_never_silently_merged(variant):
    ctx = context()
    original = rule(ctx)
    if variant == "none": ctx.permit_store = store("")
    if variant == "extraction_error": ctx.permit_store.extraction_errors = ["page 3 OCR failed"]
    if variant == "duplicate_stage": ctx.permit_store.chunks += store(source="copy.pdf").chunks
    if variant == "different_revision": ctx.permit_store.chunks += store(PERMIT.replace("90", "95"), "revision.pdf").chunks
    if variant == "missing_stage": ctx.permit_store = store(PERMIT.replace("1.1. 원액", "1.1. 다른원액"))
    if variant == "no_children": ctx.permit_store = store("1. 제조방법 및 시험기준\n1.1. 원액")
    assert permit_required_tests(original, ctx).status == "HOLD"


def test_nested_children_are_requirements_not_parent_group_only():
    permit = store("1. 제조방법 및 시험기준\n1.1. 원액\n1.1.1. 동물시험\n1.1.1.1. 마우스\n음성\n1.1.1.2. 유정란\n음성")
    ctx = context([record("마우스", parent="동물시험")], permit=permit)
    result = check(ctx)
    assert result.status == "FAIL" and "유정란" in result.reason
    assert [r["test_path"] for r in result.details["permit_inventory"]["requirements"]] == [["동물시험", "마우스"], ["동물시험", "유정란"]]
    ctx = context([record("마우스", parent="동물시험"), record("유정란", parent="동물시험")], permit=permit)
    assert check(ctx).status == "PASS"
    ctx = context([record("동물시험")], permit=permit)
    assert check(ctx).status == "FAIL"


def test_reused_section_number_different_full_path_does_not_collide():
    original = store()
    other = store(PERMIT.replace("제조방법 및 시험기준", "다른 장"))
    original.chunks += other.chunks
    first = permit_scope_inventory(original, PERMIT_PATH)
    second = permit_scope_inventory(original, ["다른 장", "원액"])
    assert len(first["requirements"]) == len(second["requirements"]) == 2
    assert set(r["source_id"] for r in first["requirements"]).isdisjoint(r["source_id"] for r in second["requirements"])


def test_md_name_mapping_is_exact_and_search_alias_is_not_equivalence():
    ctx = context([record("무균성"), record("함량시험")])
    ctx.permit_store.configure_search_aliases([["무균성", "무균시험"]])
    assert check(ctx).status == "FAIL"
    mapping = [{"permit_test_path": ["무균시험"], "sp_test_paths": [["무균성"]], "reason": "이 합성 양식의 검토된 명칭"}]
    assert check(ctx, name_mappings=mapping).status == "PASS"
    mapping[0]["permit_test_path"] = ["없는시험"]
    assert check(ctx, name_mappings=mapping).status == "HOLD"


def test_mapping_collision_with_an_implicit_name_is_hold():
    ctx = context()
    mapping = [{"permit_test_path": ["무균시험"], "sp_test_paths": [["함량시험"]], "reason": "bad"}]
    assert check(ctx, name_mappings=mapping).status == "HOLD"


def test_missing_linked_file_alongside_parsed_permit_is_hold(tmp_path):
    ctx = context()
    original = rule(ctx)
    ctx.permit_store.permit_pdf_paths = [tmp_path / "missing.pdf"]
    assert permit_required_tests(original, ctx).status == "HOLD"


def test_source_ids_change_with_document_not_with_section_number_alone():
    original = permit_scope_inventory(store(), PERMIT_PATH)
    changed = permit_scope_inventory(store(PERMIT.replace("90", "91")), PERMIT_PATH)
    assert original["document_sha256"] != changed["document_sha256"]
    assert original["requirements"][0]["source_id"] != changed["requirements"][0]["source_id"]


def test_same_leaf_name_in_two_parent_groups_requires_correct_parent():
    permit = store("1. 제조방법 및 시험기준\n1.1. 원액\n1.1.1. 항원\n1.1.1.1. 확인시험\n적합\n1.1.2. 용제\n1.1.2.1. 확인시험\n적합")
    ctx = context([record("확인시험", parent="항원")], permit=permit)
    result = check(ctx)
    assert result.status != "PASS"
    assert [r["status"] for r in result.details["requirement_checks"]] == ["PASS", "HOLD"]


def test_one_child_record_cannot_also_fill_a_standalone_parentless_test():
    permit = store("1. 제조방법 및 시험기준\n1.1. 원액\n1.1.1. 확인시험\n적합\n1.1.2. 항원\n1.1.2.1. 확인시험\n적합")
    ctx = context([record("확인시험", parent="항원")], permit=permit)
    assert check(ctx).status == "HOLD"


def test_slash_separated_lots_need_explicit_inventory_rows():
    assert check(context(batches=("L1/L2",))).status == "HOLD"


@pytest.mark.parametrize("lot", ["L2", "L-1", "L1/L2"])
def test_single_batch_does_not_override_conflicting_explicit_lot(lot):
    assert check(context([record("무균시험", lot), record("함량시험")])).status == "HOLD"


def test_duplicate_mapping_is_rejected():
    mapping = {"permit_test_path": ["무균시험"], "sp_test_paths": [["무균성"]], "reason": "검토"}
    with pytest.raises(ValueError): rule(context(), name_mappings=[mapping, mapping])


@pytest.mark.parametrize("changes", [
    {"reviewed_scope_sha256": "yes"}, {"permit_stage_path": []},
    {"requirement_policy": "probably_required"}, {"coverage_end": "1 원액"},
    {"batch_binding": "explicit_test_field"}, {"test_batch_field": "제조번호"},
    {"batch_inventory": {"type": "test", "context": ["배치목록"]}},
    {"batch_inventory": {"type": "content"}},
    {"unknown": True},
])
def test_unsafe_md_contract_rejected(changes):
    with pytest.raises(ValueError): rule(context(), **changes)


def test_partial_rule_selector_cannot_hide_stage_records():
    data = rule(context()).model_dump()
    data["selector"] = {"test": "무균시험"}
    with pytest.raises(ValueError): Rule.model_validate(data)


def test_explicit_product_scope_is_required():
    data = rule(context()).model_dump()
    data["products"] = []
    with pytest.raises(ValueError): Rule.model_validate(data)


def test_provided_permit_full_stage_inventory_is_not_top_k_search():
    root = Path(__file__).resolve().parents[1]
    files = list((root / "더미데이터").glob("*허가문서*.pdf"))
    if not files:
        pytest.skip("Private synthetic corpus is not in the source distribution")
    source = PermitPdfStore(files, policy=PermitPolicy("linked", True, True, True, "auto"))
    path = ["제조방법 및 시험기준", "Component A 중간체 원액", "Component A 본배양액에 대한 시험"]
    inventory = permit_scope_inventory(source, path)
    assert [r["test_path"] for r in inventory["requirements"]] == [
        ["마이코플라스마부정시험"], ["외래성인자부정시험(in vitro)"],
        ["MMV(Mouse Minute Virus)부정시험"], ["결핵균부정시험"]]
    assert {r["evidence"]["page"] for r in inventory["requirements"]} == {11}
    assert inventory["document_hash_kind"] == "pdf_bytes"
    assert len({r["source_id"] for r in inventory["requirements"]}) == 4


def test_existing_evaluation_shape_keeps_presence_semantics_and_source_page():
    ctx = context([record("무균시험")])
    policy = rule(ctx)
    result = permit_required_tests(policy, ctx)
    book = type("Book", (), {"rules": [policy]})()
    row = to_evaluations([result], book, 7)[0]
    assert row.source == "md_policy" and row.final_status == "검수불합격"
    assert row.page_start == row.page_end == 1
    assert "permit p.1" in row.reason
