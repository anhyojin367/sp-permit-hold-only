"""Compare independently re-extracted corpus runs without hiding missing cases."""
import argparse
import json
from pathlib import Path


def compare(left: Path, right: Path):
    cases = []
    fingerprints = set()
    compact = lambda value: "".join(str(value or "").split())
    for number in range(17):
        key = f"D{number:02}"
        if not all((folder / f"{key}.json").is_file() for folder in (left, right)):
            cases.append({"case": key, "complete": False})
            continue
        a, b = [json.loads((folder / f"{key}.json").read_text(encoding="utf-8")) for folder in (left, right)]
        fingerprints.update(d["validation"].get("engine_fingerprint") for d in (a, b))
        policies = [{r["rule_id"]: r["status"] for r in d["metadata"]["policy_audit"]} for d in (a, b)]
        tests = []
        for data in (a, b):
            # Preserve multiplicity, section context, criteria, result and dates.
            tests.append(sorted(tuple(compact(r.get(f)) for f in (
                "section_number", "section_title", "test_name", "criteria", "result", "test_period"))
                for r in data["extracted_records"] if r["record_type"] == "test"))
        evaluations = [sorted(tuple(compact(r.get(f)) for f in (
            "record_type", "section_number", "section_title", "test_name", "content_label", "final_status"))
            for r in data["evaluations"]) for data in (a, b)]
        cases.append({"case": key, "complete": True,
            "policies_equal": policies[0] == policies[1],
            "policy_differences": {k: [policies[0].get(k), policies[1].get(k)]
                for k in policies[0].keys() | policies[1].keys() if policies[0].get(k) != policies[1].get(k)},
            "test_values_equal": tests[0] == tests[1],
            "evaluation_statuses_equal": evaluations[0] == evaluations[1],
            "summaries_equal": a["summary"] == b["summary"],
            "test_counts": [len(t) for t in tests], "summaries": [a["summary"], b["summary"]],
            "missing_expectations": [d["validation"][f] for d in (a, b) for f in (
                "missing_rules", "missing_test_failures", "missing_test_passes", "normal_false_positives") if d["validation"][f]],
            "engine_changed": any(d["validation"].get("engine_changed_during_run") for d in (a, b))})
    return {"cases": cases, "engine_fingerprints": sorted(str(f) for f in fingerprints),
        "all_equal": len(fingerprints) == 1 and None not in fingerprints and all(c.get("complete") and c.get("policies_equal")
        and c.get("test_values_equal") and c.get("evaluation_statuses_equal") and c.get("summaries_equal")
        and not c.get("missing_expectations") and not c.get("engine_changed") for c in cases)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare(args.left, args.right)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for case in report["cases"]:
        print(case["case"], "complete=", case["complete"], "policies_equal=", case.get("policies_equal"),
              "tests_equal=", case.get("test_values_equal"), "statuses_equal=", case.get("evaluation_statuses_equal"),
              "summaries_equal=", case.get("summaries_equal"), "differences=", case.get("policy_differences"))
    raise SystemExit(0 if report["all_equal"] else 1)
