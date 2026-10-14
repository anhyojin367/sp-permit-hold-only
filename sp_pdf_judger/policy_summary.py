"""Project MD findings into the existing manufacturing-summary UI contract."""
from __future__ import annotations

from .config import FAIL_LABEL, HOLD_LABEL, PASS_LABEL
from .manufacturing_info_validator import ManufacturingInfoValidationResult, FieldCompareResult, TestDateCompareResult, _norm_match


def apply_policy_test_guards(evaluations, findings):
    """An explicit MD endpoint check can veto an ungrounded per-test PASS.

    Never promotes a verdict or replaces a proven failure with a hold. Preserve
    the preceding verdict in the audit so disagreements remain inspectable.
    """
    audit = []
    by_position = {
        evaluation.record_position: evaluation
        for evaluation in evaluations
        if evaluation.record_type == "test"
        and isinstance(getattr(evaluation, "record_position", None), int)
    }
    by_order = {evaluation.order_idx: evaluation for evaluation in evaluations if evaluation.record_type == "test"}
    for finding in findings:
        for guard in finding.details.get("test_guards", []):
            guard_position = guard.get("record_position")
            if isinstance(guard_position, int):
                evaluation = by_position.get(guard_position)
                # Legacy/non-target evaluations predate record-position fan-out.
                # Fall back only when the whole evaluation set is legacy; a
                # partial miss in a position-aware set must never hit a sibling
                # that happens to share the same order index.
                if evaluation is None and not by_position:
                    evaluation = by_order.get(guard["order_idx"])
            else:
                evaluation = by_order.get(guard["order_idx"])
            if evaluation is None or guard["status"] not in {FAIL_LABEL, HOLD_LABEL}:
                continue
            # Keep every proven failure, but never let a missing-evidence guard
            # downgrade one. Prior numeric PASS prose belongs in the audit only.
            if evaluation.final_status == FAIL_LABEL and guard["status"] == HOLD_LABEL:
                continue
            message = finding.rule_id + ": " + guard["reason"]
            if message in (evaluation.reason or "").splitlines():
                continue
            audit_entry = {"rule_id": finding.rule_id, "order_idx": evaluation.order_idx,
                "previous_status": evaluation.final_status, "previous_reason": evaluation.reason, "previous_source": evaluation.source}
            if isinstance(getattr(evaluation, "record_position", None), int):
                audit_entry["record_position"] = evaluation.record_position
            audit.append(audit_entry)
            preceding_failure = evaluation.reason if evaluation.final_status == FAIL_LABEL else ""
            evaluation.final_status = guard["status"]
            evaluation.reason = "\n".join(part for part in (preceding_failure, message) if part)
            evaluation.source = "md_policy_guard"
            evaluation.comparator = guard.get("comparator", "md_labelled_numeric")
            evaluation.comparison_completed = guard["status"] == FAIL_LABEL
            for row in evaluation.lot_judgements:
                row["status"] = evaluation.final_status
                row["reason"] = evaluation.reason
                row["source"] = evaluation.source
    return audit


def _presentation_stages(records):
    names = {}
    for record in records:
        for node in (record.diagram_data or {}).get("nodes", []):
            name = node.get("name", "")
            if _norm_match(name):
                names.setdefault(_norm_match(name), name)
    return names


def _card_stage(stage, check, names, records):
    """Resolve display grouping through the record's existing section ancestry.

    This does not change a selector, date comparison, or verdict. An unknown or
    ambiguous parent stays separate so no finding can disappear from the UI.
    """
    key = _norm_match(stage)
    if key in names:
        return names[key]
    if check.get("kind") != "test_date" or not check.get("section_number") or not check.get("page_number"):
        return stage
    parents = set()
    for record in records:
        if (record.record_type != "test" or record.section_number != check["section_number"]
                or record.page_start != check["page_number"]
                or _norm_match(record.test_name) != _norm_match(check.get("test_name"))):
            continue
        for part in reversed(record.section_path):
            title = _norm_match(part.get("title", ""))
            matches = {name for name in names if name in title}
            if matches:
                parents.update(matches)
                break
    return names[next(iter(parents))] if len(parents) == 1 else stage


def manufacturing_policy_cards(findings, rule_book, *, records=()):
    """Presentation adapter only: all comparison statuses come from MD findings."""
    ids = {rule.id for rule in rule_book.rules if rule.report_group == "manufacturing_info"}
    stage_names = _presentation_stages(records)
    groups = {}
    for finding in findings:
        if finding.rule_id not in ids or finding.status == "N/A":
            continue
        checks = finding.details.get("manufacturing_checks") or [{
            "stage_name": finding.title, "kind": "field", "field_name": finding.rule_id,
            "summary_value": "", "document_value": "",
            "status": {"PASS": "합격", "FAIL": "불합격", "HOLD": "보류"}[finding.status],
            "reason": finding.reason, "source_label": finding.rule_id}]
        for check in checks:
            row = dict(check)
            stage = _card_stage(row.pop("stage_name"), check, stage_names, records)
            kind = row.pop("kind")
            group = groups.setdefault(_norm_match(stage), ManufacturingInfoValidationResult(stage, "보류", []))
            if kind == "test_date":
                group.test_dates.append(TestDateCompareResult(**row))
            else:
                group.fields.append(FieldCompareResult(**row))
    for group in groups.values():
        statuses = {item.status for item in [*group.fields, *group.test_dates]}
        group.status = "불합격" if "불합격" in statuses else "합격" if statuses == {"합격"} else "보류"
        group.source_count = len({(item.section_number, item.page_number) for item in group.fields})
    # Existing UI follows the manufacturing diagram, with document-wide cards
    # afterwards. Markdown file/rule ordering must not reorder that layout.
    return [groups[key] for key in stage_names if key in groups] + [
        group for key, group in groups.items() if key not in stage_names]


def summarize_manufacturing_policies(findings, rule_book, page_numbers):
    """Use the same findings as detail cards; never run a second rule set.

    The Markdown report_group is explicit so a semantic-review rule can replace
    a deterministic rule without changing this adapter or inventing a new UI.
    Missing/disabled rules and missing document evidence are not a pass.
    """
    ids = {rule.id for rule in rule_book.rules
           if getattr(rule, "report_group", None) == "manufacturing_dates"}
    applicable = [finding for finding in findings if finding.rule_id in ids and finding.status != "N/A"]
    meta = {
        "source": "md_policy",
        "rule_fingerprint": rule_book.fingerprint,
        "rule_ids": [finding.rule_id for finding in applicable],
        "manufacturing_summary_found": bool(page_numbers),
        "date_nodes": [],
        "violations": [finding.reason for finding in applicable if finding.status == "FAIL"],
        "readable_summary": "",
        "merge_checks": [],
    }
    if not page_numbers:
        return HOLD_LABEL, "제조요약도 페이지를 찾지 못해 사람의 확인이 필요합니다.", meta
    if not applicable:
        return HOLD_LABEL, "적용 가능한 제조요약도 날짜 MD 규칙이 없어 검수보류입니다.", meta
    present_ids = {finding.rule_id for finding in findings}
    missing_ids = ids - present_ids
    statuses = {finding.status for finding in applicable}
    status = FAIL_LABEL if "FAIL" in statuses else (
        HOLD_LABEL if missing_ids or statuses - {"PASS"} else PASS_LABEL)
    lead = {PASS_LABEL: "제조요약도 날짜 MD 규칙을 충족합니다.",
            FAIL_LABEL: "제조요약도 날짜 MD 규칙의 위반이 확인되었습니다.",
            HOLD_LABEL: "제조요약도 날짜 MD 규칙을 확정할 근거가 부족합니다."}[status]
    lines = [lead, *[f"- {finding.rule_id}: {finding.reason}" for finding in applicable]]
    if missing_ids:
        lines.append("- 실행 결과가 없는 규칙: " + ", ".join(sorted(missing_ids)))
    return status, "\n".join(lines), meta
