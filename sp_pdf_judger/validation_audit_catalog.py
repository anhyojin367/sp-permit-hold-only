from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


IMPLEMENTATION_STATUSES = ("구현", "부분 구현", "미구현")
CATALOG_PATH = Path(__file__).with_name("validation_audit_catalog.json")


@dataclass(frozen=True)
class ValidationRuleAudit:
    rule_id: str
    category: str
    requirement: str
    implementation_status: str
    executor: str
    code_evidence: tuple[str, ...]
    audit_note: str


def load_validation_rules(path: Path | None = None) -> tuple[ValidationRuleAudit, ...]:
    source = Path(path or CATALOG_PATH)
    payload = json.loads(source.read_text(encoding="utf-8"))
    rows = payload.get("rules", [])
    rules = tuple(
        ValidationRuleAudit(
            rule_id=str(row["rule_id"]).strip(),
            category=str(row["category"]).strip(),
            requirement=str(row["requirement"]).strip(),
            implementation_status=str(row["implementation_status"]).strip(),
            executor=str(row["executor"]).strip(),
            code_evidence=tuple(str(item).strip() for item in row.get("code_evidence", []) if str(item).strip()),
            audit_note=str(row["audit_note"]).strip(),
        )
        for row in rows
    )
    _validate_rules(rules)
    return rules


def _validate_rules(rules: tuple[ValidationRuleAudit, ...]) -> None:
    if not rules:
        raise ValueError("검증 로직 카탈로그가 비어 있습니다.")

    identifiers = [rule.rule_id for rule in rules]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("검증 로직 ID가 중복되었습니다.")

    invalid_statuses = sorted(
        {rule.implementation_status for rule in rules if rule.implementation_status not in IMPLEMENTATION_STATUSES}
    )
    if invalid_statuses:
        raise ValueError(f"알 수 없는 구현 상태입니다: {', '.join(invalid_statuses)}")

    for rule in rules:
        if not all((rule.rule_id, rule.category, rule.requirement, rule.executor, rule.code_evidence, rule.audit_note)):
            raise ValueError(f"필수 감사 정보가 누락되었습니다: {rule.rule_id}")


def implementation_summary(rules: Iterable[ValidationRuleAudit]) -> dict[str, int]:
    rule_list = list(rules)
    counts = Counter(rule.implementation_status for rule in rule_list)
    return {
        "구현": counts["구현"],
        "부분 구현": counts["부분 구현"],
        "미구현": counts["미구현"],
        "전체": len(rule_list),
    }


def build_audit_rows(rules: Iterable[ValidationRuleAudit]) -> list[list[str]]:
    rows = [[
        "로직 ID",
        "구분",
        "검증 요구사항",
        "구현 상태",
        "현재 실행 주체",
        "코드 근거",
        "감사 메모",
    ]]
    rows.extend(
        [
            rule.rule_id,
            rule.category,
            rule.requirement,
            rule.implementation_status,
            rule.executor,
            "\n".join(rule.code_evidence),
            rule.audit_note,
        ]
        for rule in rules
    )
    return rows
