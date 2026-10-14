"""The synthetic API harness bounds rechecks; it never overrides bad evidence."""
from copy import deepcopy

import pytest

from tests.test_permit_applicability import setup, evidence
from scripts.prove_permit_applicability import request_checked_evidence


@pytest.mark.parametrize("sequence,accepted", [("good", True), ("bad-good", True), ("bad-bad", False)])
def test_recheck_is_bounded_and_preserves_invalid_attempt(sequence, accepted):
    api, _, c, t = setup()
    good = evidence(api, c, t)
    bad = deepcopy(good)
    bad["decisions"][0]["conditions"][0]["truth"] = "false"
    results = [good if word == "good" else bad for word in sequence.split("-")]
    prompts = []
    def request(prompt):
        prompts.append(prompt)
        return results[len(prompts) - 1]
    attempts = request_checked_evidence(c, t, request)
    assert len(attempts) == len(prompts) == len(results)
    assert attempts[-1]["validation"]["accepted"] == accepted
    if len(attempts) == 2:
        assert not attempts[0]["validation"]["accepted"]
        assert "정답 값은 제공되지 않는다" in prompts[1]
        # Recheck includes the incorrect proposal, never an oracle replacement.
        assert prompts[1].endswith(__import__("json").dumps(bad, ensure_ascii=False))
    if not accepted:
        assert all(d["applicability"] == "unknown" for d in attempts[-1]["validation"]["decisions"])


def test_runtime_failure_is_sanitized_and_not_retried():
    _, _, c, t = setup()
    calls = []
    def request(prompt):
        calls.append(prompt)
        raise RuntimeError("sensitive detail must not be persisted")
    attempts = request_checked_evidence(c, t, request)
    assert len(calls) == len(attempts) == 1
    assert attempts[0]["runtime_error"] == {"type": "RuntimeError", "http": None}
    assert "sensitive" not in str(attempts)
    assert not attempts[0]["validation"]["accepted"]
