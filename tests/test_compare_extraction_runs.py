"""Do not certify portability from matching totals alone."""
import json

import pytest

from scripts.compare_extraction_runs import compare


@pytest.fixture
def corpus_pair(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    data = {
        "summary": {"passed": 1, "failed": 0, "held": 0},
        "metadata": {"policy_audit": [{"rule_id": "R01", "status": "PASS"}]},
        "extracted_records": [{"record_type": "test", "test_name": "pH", "result": "7.0"}],
        "evaluations": [{"record_type": "test", "test_name": "pH", "final_status": "PASS"}],
        "validation": {"engine_fingerprint": "frozen", "engine_changed_during_run": False,
                       "missing_rules": [], "missing_test_failures": [],
                       "missing_test_passes": [], "normal_false_positives": []},
    }
    for folder in (left, right):
        folder.mkdir()
        for number in range(17):
            (folder / f"D{number:02}.json").write_text(json.dumps(data), encoding="utf-8")
    return left, right


def test_equal_complete_corpora(corpus_pair):
    assert compare(*corpus_pair)["all_equal"]


@pytest.mark.parametrize("change", ["summary", "evaluation", "missing_case", "engine"])
def test_incomplete_or_different_results_are_not_certified(corpus_pair, change):
    left, right = corpus_pair
    path = right / "D16.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if change == "missing_case":
        path.rename(right / "not_a_case.json")
    else:
        if change == "summary":
            data["summary"]["passed"] = 2
        elif change == "evaluation":
            # Same totals/policy/input must not hide a different individual status.
            data["evaluations"][0]["final_status"] = "HOLD"
        elif change == "engine":
            data["validation"]["engine_fingerprint"] = "different"
        path.write_text(json.dumps(data), encoding="utf-8")
    assert not compare(left, right)["all_equal"]
