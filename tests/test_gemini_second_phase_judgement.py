from __future__ import annotations

from sp_pdf_judger.config import FAIL_LABEL, PASS_LABEL
from sp_pdf_judger.judgement import JudgeEngine, _judge_special_rule
from sp_pdf_judger.llm import JudgeResponse
from sp_pdf_judger.rag import UcumRagStore
from sp_pdf_judger.schemas import ExtractedRecord


class FakeGeminiClient:
    def __init__(self, response: JudgeResponse) -> None:
        self.response = response
        self.enabled = True
        self.calls = 0

    def explain(self, **kwargs):
        self.calls += 1
        return self.response


def _judge_with_fake_gemini(criteria: str, result: str, response: JudgeResponse):
    client = FakeGeminiClient(response)
    engine = JudgeEngine(UcumRagStore(), llm_client=client, permit_store=None)
    evaluation = engine.judge_record(
        ExtractedRecord(
            record_type="test",
            test_name="qualitative test",
            criteria=criteria,
            result=result,
        )
    )
    return evaluation, client


def test_compound_qualitative_requirement_is_decided_before_gemini() -> None:
    criteria = "\uc138\ud3ec\uac00 \ubb49\uce58\uc9c0 \uc54a\uace0 \uad6c\ud615\uc774\uc5b4\uc57c \ud568"
    result = "\ubb49\uce58\uc9c0 \uc54a\uc74c, \uad6c\ud615\uc784"

    assert _judge_special_rule(criteria, result)[0] == PASS_LABEL

    evaluation, client = _judge_with_fake_gemini(
        criteria,
        result,
        JudgeResponse(
            status=PASS_LABEL,
            reason="\uc2dc\ud5d8\uacb0\uacfc\uac00 \uc751\uc9d1 \uc5c6\uc74c\uacfc \uad6c\ud615 \uc870\uac74\uc744 \ubaa8\ub450 \ub9cc\uc871\ud558\uc5ec \uac80\uc218\ud569\uaca9\uc73c\ub85c \ud310\ub2e8\ud588\uc2b5\ub2c8\ub2e4.",
            normalized_criteria="\uc751\uc9d1 \uc5c6\uc74c, \uad6c\ud615",
            normalized_result="\uc751\uc9d1 \uc5c6\uc74c, \uad6c\ud615",
        ),
    )

    assert client.calls == 0
    assert evaluation.final_status == PASS_LABEL
    assert evaluation.source == "rule_before"
    assert evaluation.comparator == "compound_qualitative_text"


def test_not_confirmed_requirement_is_decided_before_gemini_without_permit() -> None:
    evaluation, client = _judge_with_fake_gemini(
        "\uade0\uc774 \ud655\uc778\ub418\uc9c0 \uc54a\uc544\uc57c \ud568",
        "\ud655\uc778\ub418\uc9c0 \uc54a\uc74c",
        JudgeResponse(
            status=PASS_LABEL,
            reason="\uc2dc\ud5d8\uacb0\uacfc\uc5d0\uc11c \uade0\uc774 \ud655\uc778\ub418\uc9c0 \uc54a\uc544 \uc2dc\ud5d8\uae30\uc900\uc744 \ub9cc\uc871\ud558\ubbc0\ub85c \uac80\uc218\ud569\uaca9\uc73c\ub85c \ud310\ub2e8\ud588\uc2b5\ub2c8\ub2e4.",
            normalized_criteria="\uade0 \ubbf8\ud655\uc778",
            normalized_result="\uade0 \ubbf8\ud655\uc778",
        ),
    )

    assert client.calls == 0
    assert evaluation.final_status == PASS_LABEL
    assert evaluation.source == "rule_before"


def test_gemini_second_phase_can_fail_compound_qualitative_requirement() -> None:
    evaluation, client = _judge_with_fake_gemini(
        "\uc138\ud3ec\uac00 \ubb49\uce58\uc9c0 \uc54a\uace0 \uad6c\ud615\uc774\uc5b4\uc57c \ud568",
        "\uc751\uc9d1\uc774 \uad00\ucc30\ub418\uace0 \ubd88\uaddc\uce59\ud568",
        JudgeResponse(
            status=FAIL_LABEL,
            reason="\uc2dc\ud5d8\uacb0\uacfc\uc5d0\uc11c \uc751\uc9d1\uacfc \ubd88\uaddc\uce59 \ud615\ud0dc\uac00 \ud655\uc778\ub418\uc5b4 \uac80\uc218\ubd88\ud569\uaca9\uc73c\ub85c \ud310\ub2e8\ud588\uc2b5\ub2c8\ub2e4.",
            normalized_criteria="\uc751\uc9d1 \uc5c6\uc74c, \uad6c\ud615",
            normalized_result="\uc751\uc9d1 \uc788\uc74c, \ubd88\uaddc\uce59",
        ),
    )

    assert client.calls == 0
    assert evaluation.final_status == FAIL_LABEL
    assert evaluation.source == "rule_before"


def test_unresolved_qualitative_hold_is_sent_to_gemini_second_phase() -> None:
    evaluation, client = _judge_with_fake_gemini(
        "\uc138\ud3ec\ub294 \uba85\ud655\ud55c \uacbd\uacc4\uc640 \uc77c\uc815\ud55c \ubc30\uc5f4\uc744 \ubcf4\uc5ec\uc57c \ud568",
        "\uc138\ud3ec \uacbd\uacc4\uac00 \uba85\ud655\ud558\uace0 \ubc30\uc5f4\uc774 \uc77c\uc815\ud568",
        JudgeResponse(
            status=PASS_LABEL,
            reason="\uc2dc\ud5d8\uacb0\uacfc\uac00 \uc138\ud3ec \uacbd\uacc4\uc640 \ubc30\uc5f4 \uc870\uac74\uc744 \ubaa8\ub450 \ub9cc\uc871\ud558\uc5ec \uac80\uc218\ud569\uaca9\uc73c\ub85c \ud310\ub2e8\ud588\uc2b5\ub2c8\ub2e4.",
            normalized_criteria="\uba85\ud655\ud55c \uacbd\uacc4, \uc77c\uc815\ud55c \ubc30\uc5f4",
            normalized_result="\uba85\ud655\ud55c \uacbd\uacc4, \uc77c\uc815\ud55c \ubc30\uc5f4",
        ),
    )

    assert client.calls == 1
    assert evaluation.final_status == PASS_LABEL
    assert evaluation.source == "gemini_second"


def test_gemini_second_phase_skips_already_decided_rule_pass() -> None:
    evaluation, client = _judge_with_fake_gemini(
        "\uc2dc\ud5d8\uae30\uc900 10 \uc774\ud558",
        "5",
        JudgeResponse(
            status=FAIL_LABEL,
            reason="Gemini should not be called for an already decided rule result.",
        ),
    )

    assert client.calls == 0
    assert evaluation.final_status == PASS_LABEL
    assert evaluation.source != "gemini_second"
