from copy import deepcopy

from scripts.report_validation import CASES, render_report


def clean():
    return {case: {"summary": {"passed": 2, "failed": 0, "held": 0, "total": 2},
                  "metadata": {"llm_call_count": 2, "llm_success_count": 2, "llm_last_error": ""},
                  "validation": {"mode": "live", "engine_fingerprint": "fixed", "engine_changed_during_run": False,
                    "missing_rules": [], "missing_test_failures": [], "normal_false_positives": [], "holds": [],
                    "live_acceptance_complete": True, "completed_at": "2026-09-26T00:00:00+00:00"}}
            for case in CASES}


def test_complete_saved_fixture_report_is_not_universal_accuracy_claim():
    report = render_report(clean(), "fixed")
    assert "현재 상태: **저장된 13개 실제 회귀시험 조건 충족**" in report
    assert "무오류라는 뜻이 아니다" in report
    assert "캐시 재사용" in report


def test_incomplete_or_stale_or_offline_run_cannot_be_reported_complete():
    changes = [{"mode": "offline"}, {"engine_fingerprint": "old"},
               {"engine_changed_during_run": True}, {"live_acceptance_complete": False},
               {"missing_rules": ["R01"]}, {"missing_test_failures": ["세포성장"]}]
    for change in changes:
        data = clean()
        data["D01"]["validation"].update(change)
        assert "현재 상태: **전체 수락 미완료**" in render_report(data, "fixed")
    assert "미실행" in render_report({}, "fixed")


def test_hold_and_api_error_cannot_hide_behind_runner_complete_boolean():
    data = clean()
    data["D00"]["summary"].update(passed=1, held=1)
    data["D00"]["validation"]["holds"] = [{"section": "원액", "test": "시험", "reason": "근거 | 부족\n확인"}]
    report = render_report(data, "fixed")
    assert "전체 수락 미완료" in report and "근거 \\| 부족 확인" in report
    data = clean()
    data["D01"]["metadata"]["llm_last_error"] = "timeout"
    assert "API 성공 미확인/오류 기록" in render_report(data, "fixed")


def test_missing_audit_and_inconsistent_totals_are_not_passes():
    for container, key in [("validation", "holds"), ("summary", "total"), ("validation", "engine_changed_during_run")]:
        data = clean()
        del data["D00"][container][key]
        assert "전체 수락 미완료" in render_report(data, "fixed")
    data = clean()
    data["D00"]["summary"]["total"] = 100
    assert "합계 미확인/불일치" in render_report(data, "fixed")


def test_normal_false_failure_and_expected_error_failure_have_distinct_meaning():
    data = clean()
    data["D01"]["summary"].update(passed=1, failed=1)
    assert "현재 상태: **저장된 13개 실제 회귀시험 조건 충족**" in render_report(data, "fixed")
    data["D00"]["summary"].update(passed=1, failed=1)
    assert "정상본 불합격 존재" in render_report(data, "fixed")


def test_reporting_does_not_mutate_stored_judgements():
    data = clean()
    before = deepcopy(data)
    render_report(data, "fixed")
    assert data == before
