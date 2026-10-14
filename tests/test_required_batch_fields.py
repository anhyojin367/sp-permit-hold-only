"""Batch fields must be scoped, complete and grounded, not guessed from headers."""
import pytest

from test_permit_required_tests import check, context, record, rule


def row_context(value="L1", layout="same_line"):
    ctx = context()
    text = f"제조번호 {value}" if layout == "same_line" else f"제조번호\n{value}"
    ctx.records[0].content = ctx.records[0].raw_text = text
    ctx.pages = [(1, "1 원액\n1.1 배치목록\n" + text + "\n1.2 시험\n" +
                  "\n".join(r.raw_text for r in ctx.records[1:]) + "\n2 다음단계")]
    return ctx


def row_params(**updates):
    params = dict(batch_field_layout="label_value_lines",
                  batch_inventory_path=["원액", "배치목록"],
                  batch_coverage_start="1.1 배치목록", batch_coverage_end="1.2 시험")
    params.update(updates)
    return params


@pytest.mark.parametrize("layout", ["same_line", "next_line"])
def test_scoped_label_value_lines_are_supported(layout):
    result = check(row_context(layout=layout), **row_params())
    assert result.status == "PASS"
    assert result.details["batch_inventory_audit"]["batches"] == ["L1"]


@pytest.mark.parametrize("line", ["제조번호 L2", "제조번호\nL2", "제조번호", "제조번호：L2"])
def test_unsupported_or_conflicting_explicit_lot_never_uses_default(line):
    ctx = context([record("무균시험"), record("함량시험")])
    ctx.records[1].remarks = line
    ctx.records[1].raw_text += "\n" + line
    ctx.pages = [(1, "1 원액\n" + "\n".join(r.raw_text or r.content for r in ctx.records) + "\n2 다음단계")]
    assert check(ctx).status == "HOLD"


@pytest.mark.parametrize("line", ["제조번호 L2", "제조번호\nL2", "제조번호 L1/L2", "제조번호 미정"])
def test_supported_field_conflict_is_also_hold(line):
    ctx = row_context()
    ctx.records[1].remarks = line
    ctx.records[1].raw_text += "\n" + line
    ctx.pages = [(n, text.replace("결과: 적합", "결과: 적합\n" + line, 1)) for n, text in ctx.pages]
    assert check(ctx, **row_params()).status == "HOLD"


@pytest.mark.parametrize("extra", ["제조번호 L2", "제조번호 L1", "제조번호 미정", "제조번호\n"])
def test_unextracted_inventory_rows_prevent_partial_pass(extra):
    ctx = row_context()
    ctx.pages = [(1, ctx.pages[0][1].replace("1.2 시험", extra + "\n1.2 시험"))]
    assert check(ctx, **row_params()).status == "HOLD"


def test_neighbor_stage_and_header_lots_are_not_inventory():
    ctx = row_context()
    ctx.pages = [(1, "제조번호 FIN-9\n" + ctx.pages[0][1] + "\n제조번호 OTHER-2")]
    result = check(ctx, **row_params())
    assert result.status == "PASS"
    assert result.details["batch_inventory_audit"]["batches"] == ["L1"]


def test_related_field_is_not_the_batch_field():
    ctx = row_context()
    ctx.records[0].content = ctx.records[0].raw_text = "제조용 세포주 제조번호 L1"
    ctx.pages = [(1, ctx.pages[0][1].replace("제조번호 L1", "제조용 세포주 제조번호 L1"))]
    assert check(ctx, **row_params()).status == "HOLD"


def test_exact_inventory_path_not_substring_of_neighbor():
    ctx = row_context()
    ctx.records[0].section_path[-1]["title"] = "배치목록 다른단계"
    assert check(ctx, **row_params()).status == "HOLD"


def test_invented_content_cannot_borrow_header_lot():
    ctx = context()
    ctx.records[0].raw_text = "제조번호: L2"
    ctx.pages = [(1, "제조번호: L1\n1 원액\n제조번호: L2\n" +
                  "\n".join(r.raw_text for r in ctx.records[1:]) + "\n2 다음단계")]
    assert check(ctx).status == "HOLD"


def test_test_remarks_cannot_invent_lot_not_in_its_raw_record():
    ctx = context([record("무균시험", "L2"), record("함량시험", "L1")], ("L1",))
    ctx.records[1].remarks = "제조번호: L1"
    assert check(ctx, batch_binding="explicit_test_field", test_batch_field="제조번호").status == "HOLD"


@pytest.mark.parametrize("broken", ["missing", "duplicate", "outside", "reverse"])
def test_batch_boundary_must_be_unique_and_inside_overall_coverage(broken):
    ctx = row_context()
    params = row_params()
    if broken == "missing": params["batch_coverage_start"] = "없는절"
    if broken == "duplicate": ctx.pages[0] = (1, ctx.pages[0][1] + "\n1.1 배치목록")
    if broken == "outside": ctx.pages[0] = (1, ctx.pages[0][1].replace("1 원액\n", "").replace("1.2 시험", "1.2 시험\n1 원액"))
    if broken == "reverse": params["batch_coverage_start"], params["batch_coverage_end"] = params["batch_coverage_end"], params["batch_coverage_start"]
    assert check(ctx, **params).status == "HOLD"


@pytest.mark.parametrize("missing", ["batch_inventory_path", "batch_coverage_start", "batch_coverage_end"])
def test_table_mode_requires_complete_md_source_contract(missing):
    params = row_params()
    del params[missing]
    with pytest.raises(ValueError): rule(row_context(), **params)


def test_label_only_line_must_not_join_across_pages():
    ctx = row_context(layout="next_line")
    before, after = ctx.pages[0][1].split("제조번호\n", 1)
    ctx.pages = [(1, before + "제조번호\n"), (2, after)]
    for row in ctx.records: row.page_end = 2
    assert check(ctx, **row_params()).status == "HOLD"


def test_extractor_field_separators_do_not_break_exact_source_match():
    ctx = context()
    for row in ctx.records[1:]:
        row.raw_text = f"{row.test_name}\n시험결과: 적합"
    ctx.pages = [(1, "1 원액\n제조번호: L1\n" +
                  "\n".join(r.raw_text.replace("시험결과: ", "시험결과\n") for r in ctx.records[1:]) + "\n2 다음단계")]
    assert check(ctx).status == "PASS"


def test_changed_value_is_not_normalized_away_with_field_separators():
    ctx = context()
    ctx.records[1].raw_text = "무균시험\n시험결과: 음성"
    ctx.pages = [(1, "1 원액\n제조번호: L1\n무균시험\n시험결과\n양성\n" + ctx.records[2].raw_text + "\n2 다음단계")]
    assert check(ctx).status == "HOLD"


def test_record_page_number_cannot_borrow_identical_source_on_another_page():
    ctx = context()
    ctx.pages = [(1, "1 원액\n제조번호: L1"),
                 (2, "\n".join(r.raw_text for r in ctx.records[1:]) + "\n2 다음단계")]
    assert check(ctx).status == "HOLD"


def test_test_name_must_exist_in_its_own_raw_record():
    ctx = context([record("무균시험"), record("다른시험")])
    ctx.records[2].test_name = "함량시험"
    ctx.pages = [(1, ctx.pages[0][1].replace("2 다음단계", "함량시험\n2 다음단계"))]
    assert check(ctx).status == "HOLD"


@pytest.mark.parametrize("layout,line", [("delimited_line", "제조번호: L1"),
                                        ("label_value_lines", "제조번호 L1")])
def test_raw_remarks_wrapper_is_preserved_and_verified(layout, line):
    ctx = row_context() if layout == "label_value_lines" else context()
    for r in ctx.records[1:]:
        r.remarks = line
        r.raw_text += "\n비고: " + line
    ctx.pages = [(n, text.replace("결과: 적합", "결과: 적합\n비고: " + line)) for n,text in ctx.pages]
    params = row_params() if layout == "label_value_lines" else {}
    assert check(ctx, **params).status == "PASS"


@pytest.mark.parametrize("separator", ["=", "-", "/", "("])
@pytest.mark.parametrize("table", [False, True])
def test_unknown_punctuation_after_exact_batch_label_is_not_absence(separator, table):
    ctx = row_context() if table else context()
    line = "제조번호" + separator + "L2" + (")" if separator == "(" else "")
    ctx.records[1].remarks = line
    ctx.records[1].raw_text += "\n" + line
    ctx.pages = [(n, text.replace("결과: 적합", "결과: 적합\n" + line, 1)) for n,text in ctx.pages]
    assert check(ctx, **(row_params() if table else {})).status == "HOLD"
