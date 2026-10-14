"""Document-version/page tables must not become a preceding test result."""
import copy
import importlib
import json
from pathlib import Path

import pytest

m = importlib.import_module("json추출")


def test_repeated_manufacturing_sections_after_signature_keep_their_own_stage():
    pages = [m.RawPage(1, "5. 완제의약품\n5.1 항원바이알 정보\n제조번호 LOT-A\n"
                          "5.2 항원바이알 시험\n성상\n시험기준 맑음\n시험결과 맑음\n"
                          "6. 확인/서명\n서명 승인자", []),
             m.RawPage(2, "5. 완제의약품\n5.1 항원바이알 정보\n제조번호 LOT-B\n"
                          "5.2 항원바이알 시험\n성상\n시험기준 맑음\n시험결과 탁함", [])]
    items = m.Normalizer().run(pages)
    rows = m.RecordExtractor().run(m.BlockBuilder().run(items, m.SectionBuilder().run(items)))
    tests = [row for row in rows if row.record_type == "test"]
    assert len(tests) == 2
    assert [row.section_title for row in tests] == ["항원바이알 시험", "항원바이알 시험"]
    assert "탁함" in tests[1].result
    assert [row.page_start for row in tests] == [1, 2]


def test_repeated_heading_support_does_not_promote_unknown_backward_data():
    normalizer = m.Normalizer()
    items = normalizer._normalize_text("6. 확인/서명\n1.2 새로운문장\n1 mg", 1)
    assert [x.meta["number"] for x in items if x.type == "heading"] == ["6"]
HEADER = [["", "Ver. 4.0\n(2023.12.06)\n6/31 페이지", ""],
          ["16", "6", "/31 페이지"]]


@pytest.mark.parametrize("matrix", [
    HEADER,
    [["Ver. 2.1", "(2026.09.01)"], ["2/18 페이지"]],
    [["ver 12", "페이지 2/18"]],
    [["Version 2.0 (2026-09-01)"], ["Page 2 of 18"]],
    [["Ver. 2.1", "2 / 18 페이지"]],
    [["", "Ver. 4.0\n(2023.12.06)\n/31 페이지"], ["7", "/31 페이지"]],
    [["Ver. 2.0 (2026.09.01) /18 페이지"]],
])
def test_version_and_page_only_tables_are_separated(matrix):
    before = copy.deepcopy(matrix)
    page = m.RawPage(2, "", [matrix], [
        m.OrderedElement("table", m.render_table(matrix), 60,
                         {"table": matrix, "bbox": [450, 60, 540, 134]})])
    normalizer = m.Normalizer()
    assert normalizer.run([page]) == []
    assert matrix == before
    assert normalizer.page_furniture == [{
        "pdf_page": 2, "reason": "version_page_table", "text": m.render_table(matrix),
        "table": matrix, "top": 60, "bbox": [450, 60, 540, 134]}]


@pytest.mark.parametrize("matrix", [
    [["항목", "결과"], ["DNA", "9 ng/mg of protein"]],
    [["1", "2", "31"]],
    [["10", "6"]],  # Detached scientific exponent, not a page marker.
    [["Ver. 2.0", "시험방법"], ["DNA", "9 ng/mg"]],
    [["2/18 페이지", "결과", "9"]],
    [["Ver. 2.0", "2/18 페이지"], ["시료", "9"]],
    [["Ver. 2.0", "2/18 페이지"], ["9"]],
    [["Ver. 2.0", "2/18 페이지", "9 ng/mg"]],
    [["Ver. 2.0", "2/18 페이지", "적합"]],
    [["Ver. 2.0", "2/18 페이지", "2026.09.01"]],
    [["변경이력", "Ver. 2.0", "2/18 페이지"]],
    [["Ver. 2.0", "5/6"]],  # Ratios alone are insufficient evidence.
    [["(2023.12.06)", "6/31 페이지"]],
    [],
])
def test_ambiguous_tables_and_measurements_are_not_removed(matrix):
    assert not m._is_version_page_table(matrix)


def test_separating_header_does_not_break_continued_test_or_real_result_table():
    rows = [["항목", "시험결과"], ["대상 A", "9 ng/mg"], ["대상 B", "3 ng/mg"]]
    pages = [m.RawPage(1, "", [], [
        m.OrderedElement("text", "3.2 중간체 시험\nDNA시험\n시험기준 15 ng/mg 미만", 200)]),
        m.RawPage(2, "", [HEADER, rows], [
            m.OrderedElement("table", m.render_table(HEADER), 60, {"table": HEADER}),
            m.OrderedElement("text", "시험기간 2026.09.01", 200),
            m.OrderedElement("table", m.render_table(rows), 250, {"table": rows})])]
    normalizer = m.Normalizer()
    items = normalizer.run(pages)
    blocks = m.BlockBuilder().run(items, m.SectionBuilder().run(items))
    records = m.RecordExtractor().run(blocks)
    record = next(row for row in records if row.record_type == "test")
    assert "9 ng/mg" in record.result and "3 ng/mg" in record.result
    assert "Ver." not in record.result and "페이지" not in record.result
    assert record.result_table["rows"][1]["시험결과"] == "3 ng/mg"
    assert record.test_period == "2026.09.01"
    assert record.page_start == 1 and record.page_end == 2


def test_legacy_table_path_uses_the_same_separation():
    normalizer = m.Normalizer()
    assert normalizer.run([m.RawPage(2, "", [HEADER])]) == []
    assert normalizer.page_furniture[0]["pdf_page"] == 2


@pytest.mark.parametrize("top,protocol,expected", [
    (120, True, False), (230, True, True), (120, False, True),
])
def test_only_geometrically_linked_protocol_lot_line_is_separated(top, protocol, expected):
    elements = [m.OrderedElement("table", m.render_table(HEADER), 60,
                                 {"table": HEADER, "bbox": [450, 60, 540, 134]}),
                m.OrderedElement("text", "제조번호 : LOT-001", top)]
    if protocol:
        elements.append(m.OrderedElement("text", "Summary Protocol for Production and Quality control:", 80))
    elements.sort(key=lambda e: e.top)
    normalizer = m.Normalizer()
    items = normalizer.run([m.RawPage(2, "", [HEADER], elements)])
    assert any(it.meta.get("key") == "제조번호" for it in items) is expected


def test_header_separation_does_not_drop_unlabelled_continuation():
    pages = [m.RawPage(1, "3.2 시험\nDNA시험\n시험기준 15 ng/mg 미만\n시험결과 A: 9 ng/mg", []),
             m.RawPage(2, "", [HEADER], [
                 m.OrderedElement("table", m.render_table(HEADER), 60, {"table": HEADER}),
                 m.OrderedElement("text", "B: 3 ng/mg", 220)])]
    items = m.Normalizer().run(pages)
    rows = m.RecordExtractor().run(m.BlockBuilder().run(items, m.SectionBuilder().run(items)))
    record = next(row for row in rows if row.record_type == "test")
    assert "9 ng/mg" in record.result and "3 ng/mg" in record.result


def test_repeated_protocol_header_without_extractable_version_table():
    pages = [m.RawPage(n, "", [], [
        m.OrderedElement("text", "SummaryProtocolforProductionandQualitycontrol:", 82),
        m.OrderedElement("text", "SKYCovione", 98),
        m.OrderedElement("text", "제조번호 : LOT-001", 120),
        m.OrderedElement("text", "3.2 제조정보", 180),
        m.OrderedElement("text", "제조번호 : BODY-001", 230),
    ]) for n in (2, 3)]
    normalizer = m.Normalizer()
    items = normalizer.run(pages)
    assert not any("LOT-001" in it.meta.get("value", "") for it in items)
    assert sum("BODY-001" in it.meta.get("value", "") for it in items) == 2
    assert len(normalizer.page_furniture) == 2
    assert all(row["reason"] == "repeated_protocol_header" for row in normalizer.page_furniture)


@pytest.mark.parametrize("body", [False, True])
def test_single_or_body_protocol_mention_does_not_remove_lot(body):
    pages = [m.RawPage(n, "", [], [
        m.OrderedElement("text", "SummaryProtocolforProductionandQualitycontrol:", 82),
        *([m.OrderedElement("text", "3.2 제조정보", 100)] if body else []),
        m.OrderedElement("text", "제조번호 : BODY-001", 120),
    ]) for n in ((2, 3) if body else (2,))]
    items = m.Normalizer().run(pages)
    assert sum("BODY-001" in it.meta.get("value", "") for it in items) == len(pages)


def test_real_d02_dna_has_no_header_but_source_and_page_error_survive(tmp_path):
    from sp_pdf_judger.extractor import extract_records

    fixture = Path(__file__).resolve().parents[1] / "더미데이터" / "D02_페이지_시험기간_R02_R10.pdf"
    if not fixture.exists():
        pytest.skip("Private synthetic D02 fixture is unavailable")
    records = extract_records(fixture, tmp_path)
    dna = next(row for row in records if row.section_number == "3.2.2"
               and row.test_name == "잔류 숙주세포 유래 DNA시험")
    assert dna.result == "9 ng/mg of protein"
    assert dna.result_table is None
    assert dna.criteria == "15 ng/mg of protein 미만"
    assert dna.test_period == "2025.04.23"
    cell_test = next(row for row in records if row.section_title == "Component A 본배양액에 대한 시험"
                     and row.test_name == "외래성인자부정시험(invitro)")
    assert [row["세포"] for row in cell_test.result_table["rows"]] == ["MRC-5", "Vero", "CHO-K1"]
    assert all(row["시험결과"] == "음성, 음성" for row in cell_test.result_table["rows"])
    assert "Ver." not in cell_test.result and "/31" not in cell_test.result
    assert not [row.test_name for row in records if "Ver." in (row.result or "")]
    assert not [row.test_name for row in records if "제조번호" in (row.result or "")]
    evidence = json.loads((tmp_path / "06_extraction_result.json").read_text(encoding="utf-8"))
    cleanup = next(row for row in evidence["extraction_cleanup"]
                   if row["pdf_page"] == 17 and row["reason"] == "version_page_table")
    assert cleanup["table"] == HEADER
    assert evidence["pages"][16]["tables"] == [HEADER]
    # Never rewrite the document's intentional 31-page error to the actual length.
    assert any("/31" in el["text"] for page in evidence["pages"] for el in page["ordered_elements"])
    assert evidence["summary"]["total_pages"] == 26
    from sp_pdf_judger.policy_engine import PolicyContext, RuleBook, evaluate_policies
    findings = evaluate_policies(PolicyContext(fixture, records, product="스카이코비원멀티주"), RuleBook())
    assert next(row for row in findings if row.rule_id == "R02").status == "FAIL"
