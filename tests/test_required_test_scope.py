"""Independent test source scopes must not borrow or subtract other stages."""
from dataclasses import replace

import pytest

from test_permit_required_tests import check, context, record, rule


def scoped_context(names=("무균시험",), batches=("L1",)):
    ctx = context([record(name, lot if len(batches) > 1 else None)
                   for lot in batches for name in names], batches)
    for row in ctx.records[len(batches):]:
        row.section_path = [{"title": "원액"}, {"title": "대상시험"}]
        row.page_start = row.page_end = 3
    ctx.pages = [
        (1, "1 원액\n1.1 배치목록\n" + "\n".join(r.content for r in ctx.records[:len(batches)])),
        (2, "1.2 다른단계\n시험명: 함량시험\n결과: 95"),
        (3, "1.3 대상시험\n" + "\n".join(r.raw_text for r in ctx.records[len(batches):])),
        (4, "1.4 다음시험\n다른 구간\n2 다음단계")]
    return ctx


def params(**changes):
    result = dict(sp_stage_path=["원액", "대상시험"],
        batch_inventory_path=["원액", "배치목록"],
        batch_coverage_start="1.1 배치목록", batch_coverage_end="1.2 다른단계",
        test_coverage_start="1.3 대상시험", test_coverage_end="1.4 다음시험")
    result.update(changes)
    return result


def test_legacy_broad_scope_stays_conservative_but_explicit_scope_proves_absence():
    ctx = scoped_context()
    old = params()
    del old["test_coverage_start"], old["test_coverage_end"]
    assert check(ctx, **old).status == "HOLD"
    result = check(ctx, **params())
    assert result.status == "FAIL"
    assert [(r["test_path"], r["status"]) for r in result.details["requirement_checks"]] == [
        (["무균시험"], "PASS"), (["함량시험"], "FAIL")]
    assert result.details["test_coverage_audit"] == {
        "path": ["원액", "대상시험"], "first": 3, "last": 4,
        "start": "1.3 대상시험", "end": "1.4 다음시험"}


def test_complete_test_scope_and_separate_batch_scope_pass():
    result = check(scoped_context(("무균시험", "함량시험")), **params())
    assert result.status == "PASS" and result.details["checked"] == 2


def test_unextracted_mention_inside_test_scope_still_holds():
    ctx = scoped_context()
    ctx.pages[2] = (3, ctx.pages[2][1] + "\n시험명: 함량시험\n결과: 95")
    assert check(ctx, **params()).status == "HOLD"


def test_empty_target_text_stays_hold_not_a_complete_extraction_claim():
    ctx = scoped_context(())
    assert check(ctx, **params()).status == "HOLD"


def test_wrong_stage_record_inside_source_scope_is_not_borrowed():
    ctx = scoped_context()
    extra = record("함량시험", stage="다른단계")
    extra.page_start = extra.page_end = 3
    ctx.records.append(extra)
    ctx.pages[2] = (3, ctx.pages[2][1] + "\n" + extra.raw_text)
    assert check(ctx, **params()).status == "HOLD"


@pytest.mark.parametrize("variant", ["wrong_page", "outside_text", "cross_boundary", "ocr"])
def test_test_source_must_belong_to_exact_scope_and_reported_page(variant):
    ctx = scoped_context(("무균시험", "함량시험"))
    target = ctx.records[2]
    if variant == "wrong_page": target.page_start = target.page_end = 2
    if variant == "outside_text":
        ctx.pages[2] = (3, ctx.pages[2][1].replace(target.raw_text, ""))
        ctx.pages[1] = (2, ctx.pages[1][1] + "\n" + target.raw_text)
    if variant == "cross_boundary":
        target.raw_text += "\n1.4 다음시험"
        target.page_end = 4
    if variant == "ocr": target.ocr_suspect = True
    assert check(ctx, **params()).status == "HOLD"


@pytest.mark.parametrize("variant", ["missing", "duplicate", "reversed", "outside", "overlap", "gap", "blank"])
def test_test_scope_boundaries_and_text_coverage_fail_closed(variant):
    ctx = scoped_context()
    config = params()
    if variant == "missing": config["test_coverage_start"] = "없는절"
    if variant == "duplicate": ctx.pages[0] = (1, ctx.pages[0][1] + "\n1.3 대상시험")
    if variant == "reversed": config["test_coverage_start"], config["test_coverage_end"] = config["test_coverage_end"], config["test_coverage_start"]
    if variant == "outside":
        ctx.pages[3] = (4, "2 다음단계\n1.4 다음시험")
    if variant == "overlap": config["test_coverage_start"] = "1.1 배치목록"
    if variant == "gap": ctx.pages.pop(1)
    if variant == "blank": ctx.pages[1] = (2, "")
    assert check(ctx, **config).status == "HOLD"


@pytest.mark.parametrize("field", ["test_coverage_start", "test_coverage_end", "batch_inventory_path"])
def test_partial_scope_configuration_is_rejected(field):
    config = params()
    del config[field]
    with pytest.raises(ValueError): rule(scoped_context(), **config)


def test_identical_test_boundaries_are_rejected():
    with pytest.raises(ValueError):
        rule(scoped_context(), **params(test_coverage_end="1.3 대상시험"))


def test_scope_cannot_hide_selected_stage_records_to_force_a_missing_failure():
    ctx = scoped_context(("무균시험", "함량시험"))
    # The MD accidentally starts after one selected test: not evidence of absence.
    config = params(test_coverage_start="시험명: 함량시험")
    assert check(ctx, **config).status == "HOLD"


def test_multiple_batches_keep_independent_absence_after_scope_separation():
    ctx = scoped_context(("무균시험", "함량시험"), ("L1", "L2"))
    missing = ctx.records.pop()
    ctx.pages[2] = (3, ctx.pages[2][1].replace(missing.raw_text, ""))
    result = check(ctx, **params(batch_binding="explicit_test_field", test_batch_field="제조번호"))
    assert result.status == "FAIL"
    assert [(r["batch"], r["test_path"]) for r in result.details["requirement_checks"]
            if r["status"] == "FAIL"] == [("L2", ["함량시험"])]


@pytest.mark.parametrize("variant", ["duplicate", "empty_result"])
def test_record_ambiguity_remains_hold(variant):
    ctx = scoped_context(("무균시험", "함량시험"))
    if variant == "duplicate": ctx.records.append(replace(ctx.records[2]))
    else: ctx.records[2].result = ""
    assert check(ctx, **params()).status == "HOLD"


def test_confirmed_failure_is_preserved_with_unrelated_duplicate_hold():
    ctx = scoped_context()
    ctx.records.append(replace(ctx.records[1]))
    result = check(ctx, **params())
    assert result.status == "FAIL"
    assert {r["status"] for r in result.details["requirement_checks"]} == {"HOLD", "FAIL"}


def test_reviewed_test_block_can_precede_the_inventory():
    ctx = scoped_context(("무균시험", "함량시험"))
    ctx.pages = [(1, "1 원액\n1.3 대상시험\n" + "\n".join(r.raw_text for r in ctx.records[1:]) +
                  "\n1.4 다음시험\n1.1 배치목록\n제조번호: L1\n1.2 다른단계\n2 다음단계")]
    for row in ctx.records: row.page_start = row.page_end = 1
    assert check(ctx, **params()).status == "PASS"
