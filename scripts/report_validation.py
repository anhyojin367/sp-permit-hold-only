"""Summarize saved corpus evidence without calling CLOVA or judging documents."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


CASES = tuple(f"D{i:02d}" for i in range(13))


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render_report(payloads: dict, expected_engine: str) -> str:
    """A missing/stale/error result cannot become a successful test report."""
    rows, details, complete = [], [], True
    for case in CASES:
        payload = payloads.get(case)
        if payload is None:
            complete = False
            rows.append(f"| {case} | 미실행 | — | — | — | — | — |")
            continue
        audit = payload.get("validation", {})
        summary = payload.get("summary", {})
        meta = payload.get("metadata", {})
        issues = []
        if audit.get("mode") != "live":
            issues.append("실제 API 시험 아님")
        if not expected_engine or audit.get("engine_fingerprint") != expected_engine:
            issues.append("코드 버전 불일치/미확인")
        if audit.get("engine_changed_during_run") is not False:
            issues.append("실행 중 코드 변경 여부 미확인/변경")
        missing = audit.get("missing_rules")
        missing_tests = audit.get("missing_test_failures")
        false_positives = audit.get("normal_false_positives")
        holds = audit.get("holds")
        if any(not isinstance(value, list) for value in (missing, missing_tests, false_positives, holds)):
            issues.append("검증 감사 필드 누락")
        missing = missing if isinstance(missing, list) else []
        missing_tests = missing_tests if isinstance(missing_tests, list) else []
        false_positives = false_positives if isinstance(false_positives, list) else []
        holds = holds if isinstance(holds, list) else []
        if missing or missing_tests:
            issues.append("지정 오류 검출 미충족")
        if false_positives or (case == "D00" and summary.get("failed", 0)):
            issues.append("정상본 불합격 존재")
        counts = [summary.get(key) for key in ("passed", "failed", "held", "total")]
        if any(type(n) is not int or n < 0 for n in counts) or (
            all(type(n) is int for n in counts) and sum(counts[:3]) != counts[3]
        ):
            issues.append("합계 미확인/불일치")
        if summary.get("held") != len(holds):
            issues.append("보류 합계/감사 불일치")
        if holds or summary.get("held"):
            issues.append("보류 근거 확인 필요")
        if not meta.get("llm_success_count") or meta.get("llm_last_error"):
            issues.append("API 성공 미확인/오류 기록")
        if not audit.get("live_acceptance_complete"):
            issues.append("runner 수락 미완료")
        if issues:
            complete = False
        state = "; ".join(dict.fromkeys(issues)) or "저장된 실제 회귀 조건 충족"
        rows.append("| " + " | ".join(map(cell, [case, state, summary.get("passed", "—"),
                    summary.get("failed", "—"), summary.get("held", "—"),
                    len(missing) + len(missing_tests), audit.get("completed_at", "미확인")])) + " |")
        details.extend([f"### {case}", "", f"- 상태: {state}",
                        f"- 코드: `{cell(audit.get('engine_fingerprint', '미확인'))}`",
                        f"- 논리 요청/성공: {meta.get('llm_call_count', '미확인')}/{meta.get('llm_success_count', '미확인')}"])
        for item in missing + missing_tests:
            details.append(f"- 미충족 기대값: {cell(item)}")
        for item in false_positives:
            details.append(f"- 정상본 불합격: {cell(item)}")
        for item in holds:
            details.append(f"- 보류: {cell(item.get('section', ''))} / {cell(item.get('test', ''))} — {cell(item.get('reason', ''))}")
        details.append("")
    lead = "저장된 13개 실제 회귀시험 조건 충족" if complete else "전체 수락 미완료"
    return "\n".join([
        "# SP 더미 PDF 실제 회귀시험 보고서", "", f"현재 상태: **{lead}**", "",
        f"비교 코드 SHA256: `{expected_engine or '미확인'}`", "",
        "이 보고서는 저장된 결과를 요약한다. API를 다시 호출하거나 문서를 재판정하지 않는다.",
        "지정 오류 누락 0은 제공 정답표의 검증 대상에 한정된다. 새로운 문서에서 무오류라는 뜻이 아니다.",
        "논리 요청에는 캐시 재사용이 포함될 수 있으므로 실제 유료 API 호출 수/비용으로 해석하지 않는다.",
        "표의 불합격 개수에는 의도적으로 삽입한 오류가 포함된다. 오류 PDF의 불합격 자체는 테스트 실패가 아니다.", "",
        "| 사례 | 검증 상태 | 충족 | 불충족 | 보류 | 미충족 기대값 수 | 완료 UTC |",
        "|---|---|---:|---:|---:|---:|---|", *rows, "", *details,
        "UI 동작 및 전체 A/B/C 요구사항의 완료 여부는 UI_VERIFICATION.md와 VALIDATION_SCOPE.md에서 별도로 확인한다.", "",
    ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payloads = {case: json.loads((args.results / f"{case}.json").read_text(encoding="utf-8"))
                for case in CASES if (args.results / f"{case}.json").exists()}
    report = render_report(payloads, args.engine)
    if args.output:
        args.output.write_text(report, encoding="utf-8")
        print(f"Report saved: {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
