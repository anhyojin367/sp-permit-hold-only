from __future__ import annotations

from pathlib import Path

from sp_pdf_judger.config import FAIL_LABEL, HOLD_LABEL, PASS_LABEL
from sp_pdf_judger.judgement import JudgeEngine, _parse_result_table, _parse_structured_result_table_from_record
from sp_pdf_judger.manufacturing_stage_ui import _build_stage_test_summary_map
from sp_pdf_judger.schemas import Evaluation, ExtractedRecord, ProcessingResult, Summary
from sp_pdf_judger.sp_table_result_generalizer import expand_table_record_to_test_records


def test_sequence_match_requirement_passes_without_gemini() -> None:
    engine = JudgeEngine(None)

    evaluation = engine.judge_record(
        ExtractedRecord(
            record_type="test",
            test_name="염기서열분석시험",
            criteria="예상되는 유전자의 염기서열과 일치해야 함",
            result="일치함",
        )
    )

    assert evaluation.final_status == PASS_LABEL
    assert evaluation.comparator == "semantic_match_text"
    assert evaluation.source == "rule_before"


def test_observed_value_confirmation_passes_without_gemini() -> None:
    engine = JudgeEngine(None)

    evaluation = engine.judge_record(
        ExtractedRecord(
            record_type="test",
            test_name="플라스미드 대장균수확인시험",
            criteria="실측치",
            result="확인됨",
        )
    )

    assert evaluation.final_status == PASS_LABEL
    assert evaluation.comparator == "observed_value_text"
    assert evaluation.source == "rule_before"


def test_pipe_result_table_keeps_all_rows_and_display_columns() -> None:
    text = (
        "세포 | 시험기간 | 시험결과\n"
        "MRC-5 | 2021.10.20 ~ 2021.11.17 | 음성\n"
        "Vero | 2021.10.20 ~ 2021.11.17 | 음성\n"
        "CHO-K1 | 2021.10.20 ~ 2021.11.17 | 음성"
    )

    rows = _parse_result_table(text)

    assert [row["item_value"] for row in rows] == ["MRC-5", "Vero", "CHO-K1"]
    assert rows[0]["display_columns"] == ["세포", "시험기간", "시험결과"]
    assert rows[0]["display_values"]["세포"] == "MRC-5"


def test_pipe_result_table_repairs_wrapped_date_cells() -> None:
    text = (
        "바이러스 | 시험기간 | 시험결과\n"
        "Bpy V | 2021.10.12 ~\n"
        "2021.10.26 | 적합\n"
        "PCV | 2021.10.12 ~\n"
        "2021.10.26 | 적합\n"
        "HEV | 2021.10.12 ~\n"
        "2021.10.26 | 적합"
    )

    rows = _parse_result_table(text)

    assert [row["item_value"] for row in rows] == ["Bpy V", "PCV", "HEV"]
    assert all(row["test_date"] == "2021.10.12 ~ 2021.10.26" for row in rows)
    assert all(row["result"] == "적합" for row in rows)


def test_pipe_result_table_with_blank_first_header_keeps_method_rows() -> None:
    text = (
        "| 시험기간 | 시험결과\n"
        "배양법 | 2022.05.17 ~ 2022.06.14 | 확인되지 않음\n"
        "NAT법 | 2022.06.29 ~ 2022.07.01 | 확인되지 않음"
    )

    rows = _parse_result_table(text)

    assert [row["item_value"] for row in rows] == ["배양법", "NAT법"]
    assert rows[0]["display_columns"] == ["시험구분", "시험기간", "시험결과"]
    assert rows[1]["test_date"] == "2022.06.29 ~ 2022.07.01"


def test_structured_result_table_with_blank_first_header_keeps_method_rows() -> None:
    record = ExtractedRecord(
        record_type="test",
        test_name="마이코플라스마부정시험",
        result_table={
            "columns": ["", "시험기간", "시험결과"],
            "rows": [
                {"": "배양법", "시험기간": "2022.05.17 ~ 2022.06.14", "시험결과": "확인되지 않음"},
                {"": "NAT법", "시험기간": "2022.06.29 ~ 2022.07.01", "시험결과": "확인되지 않음"},
            ],
        },
    )

    rows = _parse_structured_result_table_from_record(record)

    assert [row["item_value"] for row in rows] == ["배양법", "NAT법"]
    assert rows[0]["display_columns"] == ["시험구분", "시험기간", "시험결과"]
    assert rows[0]["display_values"]["시험구분"] == "배양법"


def test_table_expansion_carries_only_exact_explicit_batch_column() -> None:
    parent = {
        "record_type": "content",
        "order_idx": 10,
        "section_title": "Component A 중간체원액에 대한 시험",
        "controls": [{"lot_no": "CONTROL-LOT"}],
        "result_table": {
            "columns": ["시험명", "시험방법", "Lot No.", "시험결과"],
            "rows": [["확인시험", "Dot Blot", "ROW-LOT-1", "적합"]],
        },
    }

    expanded = expand_table_record_to_test_records(parent, has_permit=True)

    assert len(expanded) == 1
    assert expanded[0]["batch_id"] == "ROW-LOT-1"
    assert expanded[0]["batch_evidence"] == {
        "source_kind": "explicit_table_column",
        "label": "Lot No.",
        "quote": "Lot No.: ROW-LOT-1",
        "row_index": 1,
    }


def test_table_expansion_never_promotes_control_or_qualified_batch_column() -> None:
    for columns, row in [
        (["시험명", "시험결과"], ["확인시험", "적합"]),
        (["시험명", "양성대조군 제조번호", "시험결과"], ["확인시험", "CONTROL-COL", "적합"]),
    ]:
        parent = {
            "record_type": "content",
            "order_idx": 10,
            "section_title": "Component A 중간체원액에 대한 시험",
            "controls": [{"lot_no": "CONTROL-LOT"}],
            "result_table": {"columns": columns, "rows": [row]},
        }
        expanded = expand_table_record_to_test_records(parent, has_permit=True)
        assert expanded[0].get("batch_id") is None
        assert expanded[0].get("batch_evidence") is None


def test_external_adventitious_agent_table_judges_all_rows() -> None:
    engine = JudgeEngine(None)

    evaluation = engine.judge_record(
        ExtractedRecord(
            record_type="test",
            test_name="외래성인자부정시험(invitro)",
            criteria="혈구흡착 및 적혈구응집반응 검사 시\n음성이어야함,\n세포병변 관찰 시 음성이어야 함",
            result=(
                "세포 | 시험기간 | 시험결과\n"
                "MRC-5 | 2021.10.20 ~ 2021.11.17 | 음성\n"
                "Vero | 2021.10.20 ~ 2021.11.17 | 음성\n"
                "CHO-K1 | 2021.10.20 ~ 2021.11.17 | 음성"
            ),
        )
    )

    assert evaluation.final_status == PASS_LABEL
    assert [row["item_value"] for row in evaluation.lot_judgements] == ["MRC-5", "Vero", "CHO-K1"]
    assert all(row["display_columns"] == ["세포", "시험기간", "시험결과"] for row in evaluation.lot_judgements)


def test_limit_expression_result_passes_as_limit_expression_not_raw_number() -> None:
    engine = JudgeEngine(None)

    evaluation = engine.judge_record(
        ExtractedRecord(
            record_type="test",
            test_name="엔도톡신시험",
            method="유럽약전(비탁법)",
            criteria="20 EU/mL 미만",
            result="20 EU/mL 미만",
        )
    )

    assert evaluation.final_status == PASS_LABEL
    assert evaluation.comparator == "limit_expression"
    assert evaluation.source == "rule_before"


def test_gene_loq_count_requirement_judges_whole_table() -> None:
    engine = JudgeEngine(None)

    evaluation = engine.judge_record(
        ExtractedRecord(
            record_type="test",
            test_name="잔류 숙주세포 DNA 시험",
            criteria="유전자 중 1종 이상이 정량한계 미만이어야 함",
            result=(
                "유전자 | 시험결과\n"
                "A 유전자 | 정량한계 이상\n"
                "B 유전자 | 정량한계 이상\n"
                "C 유전자 | 정량한계 미만"
            ),
        )
    )

    assert evaluation.final_status == PASS_LABEL
    assert evaluation.comparator == "loq_count_requirement"
    assert len(evaluation.lot_judgements) == 3
    assert all(row["status"] == PASS_LABEL for row in evaluation.lot_judgements)


def test_manufacturing_stage_cards_count_table_rows_like_summary() -> None:
    result = ProcessingResult(
        pdf_path=Path("dummy.pdf"),
        preview_image_path=Path("dummy.png"),
        extracted_records=[
            ExtractedRecord(
                record_type="flowchart",
                diagram_data={"nodes": [{"name": "CHO 마스터 세포주"}]},
            )
        ],
        evaluations=[
            Evaluation(
                order_idx=1,
                section_number="2.1.2.1.1",
                test_name="외래성인자부정시험(PCR)",
                final_status=PASS_LABEL,
                lot_judgements=[
                    {"status": PASS_LABEL},
                    {"status": FAIL_LABEL},
                    {"status": HOLD_LABEL},
                ],
            )
        ],
        tree=[],
        summary=Summary(passed=1, failed=1, held=1, total=3, comparable_total=3),
    )

    summary_map = _build_stage_test_summary_map(result)
    stage_summary = summary_map["cho마스터세포주"]

    assert stage_summary["passed"] == 1
    assert stage_summary["failed"] == 1
    assert stage_summary["held"] == 1
    assert stage_summary["total"] == 3
