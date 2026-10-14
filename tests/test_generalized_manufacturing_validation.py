from __future__ import annotations

from sp_pdf_judger.manufacturing_info_validator import (
    _validation_anchor_key,
    render_manufacturing_info_validation_card,
    validate_manufacturing_info_consistency,
)
from sp_pdf_judger.manufacturing_stage_ui import StageTestCard, _render_stage_test_card


def _source(*, mismatch: bool = False, out_of_range: bool = False) -> dict:
    stage_a_code = "WRONG-01" if mismatch else "A-001"
    test_date = "2026.03.01" if out_of_range else "2026.02.15"
    records = [
        {
            "record_type": "flowchart",
            "diagram_data": {
                "nodes": [
                    {
                        "node_id": "n-a",
                        "name": "중간체 A",
                        "fields": {
                            "제조번호": "A-001",
                            "제조년월일": "2026.02.10",
                            "제조량": "100L",
                        },
                    },
                    {
                        "node_id": "n-b",
                        "name": "완제품",
                        "fields": {
                            "제조번호": "B-001",
                            "제조년월일": "2026.03.01",
                            "제조수량": "200병",
                        },
                    },
                ],
                "edges": [{"from": "n-a", "to": "n-b"}],
            },
        },
        {
            "record_type": "info",
            "order_idx": 41,
            "section_number": "9.77.4",
            "section_title": "상세 정보",
            "section_path": [{"number": "9.77", "title": "중간체 A"}],
            "content": (
                f"제조번호 | {stage_a_code}\n"
                "제조년월일 | 2026.02.10\n"
                "제조량 | 100L"
            ),
        },
        {
            "record_type": "test",
            "order_idx": 42,
            "section_number": "14.2.9",
            "section_title": "중간체 A 시험",
            "section_path": [{"number": "14.2", "title": "중간체 A"}],
            "test_name": "일반 시험",
            "test_period": test_date,
        },
    ]
    evaluations = [
        {
            "order_idx": 42,
            "section_number": "14.2.9",
            "section_title": "중간체 A 시험",
            "test_name": "일반 시험",
            "test_period": test_date,
        }
    ]
    return {"records": records, "evaluations": evaluations}


def _result_for_stage(results: list, stage_name: str):
    return next(item for item in results if item.stage_name == stage_name)


def _same_lot_source(*, mismatch: bool = False) -> dict:
    second_date = "2026.02.11" if mismatch else "2026년 2월 10일"
    second_expiry = "2029.02.10" if mismatch else "2028년 2월 10일"
    return {
        "records": [
            {
                "record_type": "info",
                "section_number": "1.1",
                "section_title": "신청제품정보",
                "page_start": 2,
                "content": (
                    "제조번호 | LOT-2026-01\n"
                    "제조년월일 | 2026.02.10\n"
                    "사용(유효)기간 | 2028.02.10"
                ),
            },
            {
                "record_type": "info",
                "section_number": "4.1",
                "section_title": "완제의약품 제조정보",
                "page_start": 12,
                "content": (
                    "제조번호 | LOT-2026-01\n"
                    f"제조년월일 | {second_date}\n"
                    f"유효기간 | {second_expiry}"
                ),
            },
        ]
    }


def test_same_manufacturing_no_accepts_equivalent_date_formats() -> None:
    result = _result_for_stage(
        validate_manufacturing_info_consistency(_same_lot_source()),
        "동일 제조번호 문서 전체 정합성",
    )

    assert result.status == "합격"
    assert {field.field_name for field in result.fields} == {
        "LOT-2026-01 제조년월일",
        "LOT-2026-01 사용(유효)기간",
    }
    assert all(field.status == "합격" for field in result.fields)


def test_input_lot_reference_does_not_inherit_output_lot_manufacturing_date():
    from sp_pdf_judger.manufacturing_info_validator import collect_manufacturing_no_occurrences
    source = [{"record_type": "content", "content": (
        "사용한 세포주 제조번호 | INPUT-001\n"
        "제조번호 | OUTPUT-001\n제조년월일 | 2026.03.01\n제조량 | 100L") }]
    values = collect_manufacturing_no_occurrences(source)
    assert next(item for item in values if item.manufacturing_no == "INPUT-001").manufacturing_date == ""
    assert next(item for item in values if item.manufacturing_no == "OUTPUT-001").manufacturing_date == "2026.03.01"


def test_same_manufacturing_no_rejects_date_and_expiry_mismatches() -> None:
    result = _result_for_stage(
        validate_manufacturing_info_consistency(_same_lot_source(mismatch=True)),
        "동일 제조번호 문서 전체 정합성",
    )

    assert result.status == "불합격"
    assert all(field.status == "불합격" for field in result.fields)
    assert all("문서 위치별로 다릅니다" in field.reason for field in result.fields)


def test_detects_manufacturing_mismatch_without_fixed_section_numbers() -> None:
    result = _result_for_stage(
        validate_manufacturing_info_consistency(_source(mismatch=True)),
        "중간체 A",
    )

    assert result.status == "불합격"
    assert any(
        field.field_name == "제조번호"
        and field.document_value == "WRONG-01"
        and field.status == "불합격"
        for field in result.fields
    )


def test_uses_flowchart_edges_for_test_date_window() -> None:
    passing = _result_for_stage(
        validate_manufacturing_info_consistency(_source()),
        "중간체 A",
    )
    failing = _result_for_stage(
        validate_manufacturing_info_consistency(_source(out_of_range=True)),
        "중간체 A",
    )

    assert passing.test_dates[0].status == "합격"
    assert failing.test_dates[0].status == "불합격"
    assert "2026.03.01 미만" in failing.test_dates[0].reason


def test_rendered_stage_detail_has_stable_click_anchor_and_reason() -> None:
    source = _source(mismatch=True)
    results = validate_manufacturing_info_consistency(source)
    rendered = render_manufacturing_info_validation_card(source, results)
    anchor = _validation_anchor_key("중간체 A")

    assert f'id="mfg-validation-{anchor}"' in rendered
    assert f'data-mfg-fallback-target="mfg-stage-{anchor}"' in rendered
    assert 'role="button"' in rendered
    assert "WRONG-01" in rendered
    assert "제조번호 불일치" in rendered


def test_grouped_card_can_target_first_real_stage_detail() -> None:
    card = StageTestCard(
        display_name="원료혈장",
        x_pct=0,
        y_pct=0,
        side="left",
        passed=3,
        failed=0,
        held=0,
        total=3,
        manufacturing_issues=1,
        manufacturing_failures=1,
        validation_target="원료혈장1",
    )

    rendered = _render_stage_test_card(card)
    assert f'data-mfg-target="mfg-stage-{_validation_anchor_key("원료혈장")}"' in rendered
    assert "제조정보 불일치" not in rendered
    assert "data-mfg-validation-target" not in rendered


def test_missing_required_value_is_not_treated_as_pass() -> None:
    source = _source()
    source["records"][1]["content"] = (
        "제조번호 | A-001\n"
        "제조년월일 | 2026.02.10"
    )

    result = _result_for_stage(
        validate_manufacturing_info_consistency(source),
        "중간체 A",
    )

    assert result.status == "보류"
    assert any(
        field.field_name == "제조량" and field.status == "보류"
        for field in result.fields
    )


def test_storage_period_is_never_compared_as_manufacturing_quantity() -> None:
    source = _source()
    source["records"][1]["content"] = (
        "제조번호 | A-001\n"
        "제조년월일 | 2026.02.10\n"
        "보관조건 2~8℃ 냉장보관\n"
        "허가된 저장기간 제조일로부터 24개월"
    )

    result = _result_for_stage(
        validate_manufacturing_info_consistency(source),
        "중간체 A",
    )
    quantity_fields = [
        field for field in result.fields if field.field_name == "제조량"
    ]

    assert quantity_fields[0].status == "보류"
    assert "24개월" not in quantity_fields[0].document_value


def test_explicit_stage_manufacturing_quantity_wins_over_ingredient_amount() -> None:
    source = _source()
    source["records"][0]["diagram_data"]["nodes"][0]["name"] = "나노피티클원액"
    source["records"][0]["diagram_data"]["nodes"][0]["fields"] = {
        "제조번호": "NP-2603-001",
        "제조년월일": "2025.07.01",
        "제조량": "960 L",
    }
    source["records"][1]["section_path"] = [
        {"number": "17.8", "title": "나노피티클원액"}
    ]
    source["records"][1]["content"] = (
        "제조번호 NP-2603-001\n"
        "제조년월일 2025.07.01\n"
        "보관조건 2~8℃ 냉장보관\n"
        "허가된 저장기간 12개월\n"
        "제조량 960 L\n"
        "원료명 | 제조번호 | 분량\n"
        "첨가제 | ADD-01 | 400.0 L"
    )
    source["records"][2]["section_path"] = [
        {"number": "18.1", "title": "나노피티클원액"}
    ]
    source["evaluations"][0]["section_title"] = "나노피티클원액 시험"

    result = _result_for_stage(
        validate_manufacturing_info_consistency(source),
        "나노피티클원액",
    )
    quantities = [
        field for field in result.fields if field.field_name == "제조량"
    ]

    assert result.status == "합격"
    assert any(field.document_value == "960 L" for field in quantities)
    assert all(field.document_value != "400.0 L" for field in quantities)


def test_parent_stage_and_pooling_header_are_not_manufacturing_numbers() -> None:
    source = {
        "records": [
            {
                "record_type": "flowchart",
                "diagram_data": {
                    "nodes": [
                        {
                            "node_id": "plasma",
                            "name": "원료혈장1",
                            "fields": {
                                "제조번호": "P26-1",
                                "제조년월일": "2026.01.05",
                                "제조량": "700L",
                            },
                        },
                        {
                            "node_id": "fraction",
                            "name": "원획분",
                            "fields": {
                                "제조번호": "F26-1",
                                "제조년월일": "2026.02.10",
                                "제조량": "2100L",
                            },
                        },
                    ],
                    "edges": [{"from": "plasma", "to": "fraction"}],
                },
            },
            {
                "record_type": "info",
                "section_path": [
                    {"number": "3", "title": "원획분"},
                    {"number": "3.1", "title": "원료혈장"},
                    {"number": "3.1.1", "title": "정보"},
                ],
                "content": (
                    "제조번호 | Pooling 일자 | 제조량 (L)\n"
                    "P26-1 | 2026.01.05 | 700"
                ),
            },
        ],
        "evaluations": [],
    }

    results = validate_manufacturing_info_consistency(source)
    plasma = _result_for_stage(results, "원료혈장1")
    fraction = _result_for_stage(results, "원획분")

    assert plasma.status == "합격"
    assert all(field.document_value != "Pooling" for field in plasma.fields)
    assert fraction.status == "보류"
    assert all(field.document_value != "Pooling" for field in fraction.fields)


def _chol_date_source(test_dates: list[str]) -> dict:
    records = [
        {
            "record_type": "flowchart",
            "diagram_data": {
                "nodes": [
                    {
                        "node_id": "mcb",
                        "name": "CHOL 마스터 세포주",
                        "fields": {
                            "제조번호": "MCB-CHOL-A01",
                            "제조년월일": "2025.01.01",
                        },
                    },
                    {
                        "node_id": "wcb",
                        "name": "CHOL 제조용 세포주",
                        "fields": {
                            "제조번호": "WCB-CHOL-A01-26",
                            "제조년월일": "2025.03.01",
                        },
                    },
                ],
                "edges": [{"from": "mcb", "to": "wcb"}],
            },
        },
        {
            "record_type": "info",
            "order_idx": 10,
            "section_path": [{"number": "8.1", "title": "CHOL 마스터 세포주"}],
            "content": "제조번호 MCB-CHOL-A01\n제조년월일 2025.01.01",
        },
    ]
    evaluations = []
    for idx, raw_date in enumerate(test_dates, start=20):
        records.append(
            {
                "record_type": "test",
                "order_idx": idx,
                "section_path": [
                    {"number": f"9.{idx}", "title": "CHOL 마스터 세포주 시험"}
                ],
                "test_name": f"CHOL 시험 {idx}",
                "test_period": raw_date,
            }
        )
        evaluations.append(
            {
                "order_idx": idx,
                "section_title": "CHOL 마스터 세포주 시험",
                "test_name": f"CHOL 시험 {idx}",
                "test_period": raw_date,
            }
        )
    return {"records": records, "evaluations": evaluations}


def test_month_precision_dates_use_the_whole_month_inside_stage_window() -> None:
    results = validate_manufacturing_info_consistency(
        _chol_date_source(["2025.01", "2025.02", "2025.03"])
    )
    chol = _result_for_stage(results, "CHOL 마스터 세포주")

    assert [item.status for item in chol.test_dates] == ["합격", "합격", "불합격"]
    assert "2025.01.01 이상, 2025.03.01 미만" in chol.test_dates[0].reason


def test_related_test_without_date_is_counted_as_hold_not_dropped() -> None:
    results = validate_manufacturing_info_consistency(_chol_date_source([""]))
    chol = _result_for_stage(results, "CHOL 마스터 세포주")

    assert len(chol.test_dates) == 1
    assert chol.test_dates[0].status == "보류"
    assert chol.test_dates[0].date_text == "미확인"


def test_lot_level_test_dates_are_each_validated() -> None:
    source = _chol_date_source([])
    source["evaluations"] = [
        {
            "order_idx": 99,
            "section_title": "CHOL 마스터 세포주 시험",
            "test_name": "세포주 확인시험",
            "lot_judgements": [
                {"lot_no": "LOT-A", "test_date": "2025.01"},
                {"lot_no": "LOT-B", "test_date": "2025.03"},
            ],
        }
    ]
    source["records"].append(
        {
            "record_type": "test",
            "order_idx": 99,
            "section_path": [{"number": "9.99", "title": "CHOL 마스터 세포주 시험"}],
            "test_name": "세포주 확인시험",
        }
    )

    chol = _result_for_stage(
        validate_manufacturing_info_consistency(source),
        "CHOL 마스터 세포주",
    )

    assert len(chol.test_dates) == 2
    assert [item.status for item in chol.test_dates] == ["합격", "불합격"]


def test_terminal_stage_requires_test_date_on_or_after_its_manufacturing_date() -> None:
    source = {
        "records": [
            {
                "record_type": "flowchart",
                "diagram_data": {
                    "nodes": [
                        {
                            "node_id": "bulk",
                            "name": "최종원액",
                            "fields": {
                                "제조번호": "FB-2603-001",
                                "제조년월일": "2025.08.01",
                            },
                        },
                        {
                            "node_id": "product",
                            "name": "완제의약품",
                            "fields": {
                                "제조번호": "JEV-DGUPI-2603-001",
                                "제조년월일": "2026.03.10",
                            },
                        },
                    ],
                    "edges": [{"from": "bulk", "to": "product"}],
                },
            },
            {
                "record_type": "test",
                "order_idx": 20,
                "section_path": [{"number": "5.2", "title": "완제의약품 시험"}],
                "test_name": "성상",
                "test_period": "2025.09",
            },
            {
                "record_type": "test",
                "order_idx": 21,
                "section_path": [{"number": "5.2", "title": "완제의약품 시험"}],
                "test_name": "확인시험",
                "test_period": "2026.04",
            },
        ],
        "evaluations": [
            {
                "order_idx": 20,
                "section_title": "완제의약품 시험",
                "test_name": "성상",
                "test_period": "2025.09",
            },
            {
                "order_idx": 21,
                "section_title": "완제의약품 시험",
                "test_name": "확인시험",
                "test_period": "2026.04",
            },
        ],
    }

    finished = _result_for_stage(
        validate_manufacturing_info_consistency(source),
        "완제의약품",
    )

    assert [item.status for item in finished.test_dates] == ["불합격", "합격"]
    assert "2026.03.10 이상" in finished.test_dates[1].reason


def test_finished_product_application_info_is_counted_as_document_source() -> None:
    source = {
        "records": [
            {
                "record_type": "flowchart",
                "diagram_data": {
                    "nodes": [
                        {
                            "node_id": "product",
                            "name": "완제의약품",
                            "fields": {
                                "제조번호": "SKY-M220805-001",
                                "제조년월일": "2022.08.05",
                                "제조수량": "112,800 바이알",
                            },
                        },
                    ],
                    "edges": [],
                },
            },
            {
                "record_type": "content",
                "section_number": "1.1",
                "section_title": "신청제품정보",
                "content": (
                    "제조사 | 동국바이오사이언스㈜\n"
                    "제품명 | 스카이코비원멀티주\n"
                    "제조번호 || SKY-M220805-001\n"
                    "신청수량 || 112,800 바이알\n"
                    "제조년월일 || 2022.08.05\n"
                ),
            },
            {
                "record_type": "content",
                "section_number": "5.1",
                "section_title": "항원바이알 정보",
                "content": (
                    "제조번호 SKY-M220805-002\n"
                    "제조년월일 2022.08.05\n"
                    "총 제조수량 112,800 바이알"
                ),
            },
        ],
        "evaluations": [],
    }

    result = _result_for_stage(
        validate_manufacturing_info_consistency(source),
        "완제의약품",
    )

    assert result.source_count >= 1
    assert any(
        field.source_label.startswith("1.1")
        and field.field_name == "제조번호"
        and field.document_value == "SKY-M220805-001"
        and field.status == "합격"
        for field in result.fields
    )


def test_full_test_period_must_fit_inside_stage_window() -> None:
    results = validate_manufacturing_info_consistency(
        _chol_date_source(
            [
                "2025.01.02~2025.02.28",
                "2024.12.31 ~ 2025.01.02",
                "2025.02.28~2025.03.01",
            ]
        )
    )
    chol = _result_for_stage(results, "CHOL 마스터 세포주")

    assert [item.status for item in chol.test_dates] == ["합격", "불합격", "불합격"]
    assert chol.test_dates[0].date_text == "2025.01.02~2025.02.28"
