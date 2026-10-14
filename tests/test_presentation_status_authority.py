"""Presentation must not re-judge an authoritative status from prose."""
from types import SimpleNamespace

import pytest

from sp_pdf_judger.config import PASS_LABEL, FAIL_LABEL, HOLD_LABEL
from sp_pdf_judger.manufacturing_stage_ui import _evaluation_status_key
from sp_pdf_judger.stage_csv_exporter import _evaluation_to_rows
from sp_pdf_judger.stage_csv_exporter import _summary_row
from sp_pdf_judger.stage_csv_exporter import _status_from_explicit_reason as csv_status
from sp_pdf_judger.ui_html import _status_from_explicit_reason as html_status


@pytest.mark.parametrize("status,key", [(PASS_LABEL, "passed"), (FAIL_LABEL, "failed"), (HOLD_LABEL, "held")])
@pytest.mark.parametrize("reason", [
    "SP 기재 기준만 비교하면 충족입니다. 최종 판정에는 허가서 기준을 우선 적용합니다.",
    "SP 단독 기준에서는 불충족입니다. 허가서 기준과 구별합니다.",
    "이전 검수합격 기록은 현재 판정의 근거가 아닙니다.",
    "이전 검수불합격 기록은 현재 판정의 근거가 아닙니다.",
    "이전 검수보류 기록은 현재 판정의 근거가 아닙니다.",
])
def test_final_status_wins_over_reason_for_every_adapter(status, key, reason):
    evaluation = SimpleNamespace(final_status=status, reason=reason)
    assert _evaluation_status_key(evaluation) == key
    assert csv_status(status, reason) == status
    assert html_status(status, reason) == status
    assert _evaluation_to_rows(evaluation)[0]["검수 결과"] == status


def test_lot_csv_keeps_explicit_hold_despite_sp_only_pass_reason():
    evaluation = SimpleNamespace(lot_judgements=[{
        "lot_no": "LOT-A", "status": HOLD_LABEL,
        "reason": "SP 기준만 비교하면 충족입니다. 허가 단위를 확인할 수 없습니다.",
    }])
    assert _evaluation_to_rows(evaluation)[0]["검수 결과"] == HOLD_LABEL
    assert _evaluation_status_key(evaluation.lot_judgements[0]) == "held"


def test_lot_summary_does_not_promote_explicit_hold_when_reason_is_empty():
    evaluation = SimpleNamespace(final_status=PASS_LABEL, lot_judgements=[{"status": HOLD_LABEL}])
    summary = _summary_row("단계", [evaluation])
    assert (summary[PASS_LABEL], summary[FAIL_LABEL], summary[HOLD_LABEL]) == (0, 0, 1)


@pytest.mark.parametrize("field", ["final_status", "status", "judgement"])
def test_legacy_status_fields_also_win_over_explanation(field):
    assert _evaluation_status_key({field: HOLD_LABEL, "reason": "SP 단독 비교는 충족입니다."}) == "held"


@pytest.mark.parametrize("reason,status,key", [
    ("검수합격", PASS_LABEL, "passed"),
    ("검수불합격", FAIL_LABEL, "failed"),
    ("검수보류", HOLD_LABEL, "held"),
])
def test_legacy_reason_only_record_is_still_supported(reason, status, key):
    assert _evaluation_status_key({"reason": reason}) == key
    assert csv_status(None, reason) == status
    assert html_status(None, reason) == status
