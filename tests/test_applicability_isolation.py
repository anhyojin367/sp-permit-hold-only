"""Proof harness must separate observations without dropping required targets."""
import json

import pytest

from scripts.prove_permit_applicability import request_isolated_evidence
from tests.test_permit_applicability import evidence, setup


@pytest.mark.parametrize("bad_second", [False, True])
def test_isolation_sends_one_batch_and_revalidates_complete_coverage(bad_second):
    api, _, c, targets = setup([
        (1, "제조단계: 원액\n제조번호: L1\n용기규격: 100 mL\n시험차수: 2\n농도: 5 mg/mL"),
        (2, "제조단계: 원액\n제조번호: L2\n용기규격: 50 mL\n시험차수: 2\n농도: 20 mg/mL")])
    calls = []
    def request(prompt):
        # The optional recheck suffix is after the initial JSON payload.
        payload, _ = json.JSONDecoder().raw_decode(prompt.split("자료(JSON):\n", 1)[1])
        ids = {r["target_id"] for r in payload["decision_tasks"]}
        assert len(ids) == 1
        target = next(t for t in targets if t.target_id in ids)
        calls.append(target.target_id)
        other = next(t for t in targets if t.target_id not in ids)
        assert other.target_id not in prompt
        assert all(f.fact_id not in prompt for f in other.facts)
        response = evidence(api, c, [target])
        if bad_second and target.batch == "L2":
            response["decisions"][0]["conditions"][0]["truth"] = "true"
        return response
    result = request_isolated_evidence(c, targets, request)
    assert result["logical_calls"] == len(calls) == (3 if bad_second else 2)
    assert len(result["target_requests"]) == 2
    assert result["validation"]["accepted"] is not bad_second
    states = [r["applicability"] for r in result["validation"]["decisions"]]
    assert states == (["unknown"] * 4 if bad_second else ["required", "required", "not_required", "not_required"])
