"""Resolve a Markdown numeric check's source from an already grounded decision.

This module never retrieves another permit, calls an LLM, or infers a missing
criterion. Document-criterion consistency remains an independent MD operation.
"""
from __future__ import annotations

import re


def resolve_criterion(rule, ctx, record):
    sp_evidence = [ctx.evidence(record)]
    base = {"order_idx": record.order_idx, "source": "sp", "criteria": record.criteria or "",
            "reason": "SP 기재 기준", "evidence": sp_evidence}
    if rule.criterion_source == "sp" or not getattr(ctx, "permit_authoritative", False):
        return base
    by_position = getattr(ctx, 'comparison_bases_by_position', None)
    if by_position is not None:
        decisions = by_position.get(ctx.record_position(record), [])
    else:
        decisions = getattr(ctx, "comparison_bases", {}).get(record.order_idx, [])
    unresolved = {**base, "source": "unresolved", "criteria": None,
                  "reason": "허가서 적용 기준이 없거나 여러 판정 행의 기준이 서로 다릅니다."}
    if not decisions or any(item.get("source") not in {"sp", "permit"} for item in decisions):
        return unresolved
    compact = lambda text: re.sub(r"\s+", "", text or "")
    identities = {(item["source"], compact(item.get("criteria")) if item["source"] == "permit" else "") for item in decisions}
    if len(identities) != 1:
        return unresolved
    chosen = decisions[0]
    if chosen["source"] == "sp":
        return {**base, "reason": chosen.get("reason", "허가 결과 기준이 없어 SP 기준 적용")}
    if not chosen.get("criteria"):
        return unresolved
    result = {**base, **chosen, "order_idx": record.order_idx,
              "evidence": [*sp_evidence, *chosen.get("evidence", [])]}
    if rule.params.get("sp_supplement_if_no_numeric_limit") is True:
        from .judgement import _extract_numeric_requirements
        # A parser not recognizing a novel numeric syntax is NOT evidence that
        # no limit exists. Only a bare method reference may use this supplement.
        without_method_number = re.sub(r"제\s*\d+\s*법", "", chosen["criteria"])
        has_limit_signal = re.search(r"\d|이상|이하|미만|초과|최소|최대|한도|범위|%|NMT|NLT", without_method_number, re.I)
        if not has_limit_signal and not _extract_numeric_requirements(chosen["criteria"]):
            result.update(source="sp_supplement", criteria=record.criteria or "",
                          reason="허가서는 수치 한도 없는 시험법을 참조하므로 MD에 명시된 SP 수치 보완 검사")
    return result
