from __future__ import annotations

from sp_pdf_judger.validation_audit_catalog import (
    IMPLEMENTATION_STATUSES,
    build_audit_rows,
    implementation_summary,
    load_validation_rules,
)


EXPECTED_RULE_IDS = [
    *(f"A.{index}" for index in range(1, 14)),
    *(f"B.{index}" for index in range(1, 12)),
    *(f"C.{index}" for index in range(1, 6)),
]


def test_validation_rule_catalog_covers_every_requested_rule_once() -> None:
    rules = load_validation_rules()

    assert [rule.rule_id for rule in rules] == EXPECTED_RULE_IDS
    assert len({rule.rule_id for rule in rules}) == 29
    assert all(rule.category in {"A. 구조적 정합성", "B. 계산 및 수치 정합성", "C. 공정 및 시간적 순서"} for rule in rules)
    assert all(rule.requirement.strip() for rule in rules)
    assert all(rule.implementation_status in IMPLEMENTATION_STATUSES for rule in rules)
    assert all(rule.executor.strip() for rule in rules)
    assert all(rule.code_evidence for rule in rules)
    assert all(rule.audit_note.strip() for rule in rules)


def test_implementation_summary_matches_the_code_audit() -> None:
    summary = implementation_summary(load_validation_rules())

    assert summary == {"구현": 14, "부분 구현": 10, "미구현": 5, "전체": 29}


def test_audit_rows_are_ready_for_spreadsheet_export() -> None:
    rows = build_audit_rows(load_validation_rules())

    assert rows[0] == [
        "로직 ID",
        "구분",
        "검증 요구사항",
        "구현 상태",
        "현재 실행 주체",
        "코드 근거",
        "감사 메모",
    ]
    assert rows[1][0] == "A.1"
    assert rows[-1][0] == "C.5"
    assert len(rows) == 30
