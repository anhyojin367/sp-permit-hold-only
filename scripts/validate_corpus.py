"""Reproducible actual-PDF regression; offline != live CLOVA coverage."""
import argparse
import json
import sys
import os
import hashlib
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sp_pdf_judger.pipeline import DocumentJudgePipeline
from sp_pdf_judger.permit_catalog import ResolvedPermit, resolve_submission_permits
from sp_pdf_judger.config import FAIL_LABEL, HOLD_LABEL, PASS_LABEL

EXPECTED = {
    "D00": [], "D01": ["R01"], "D02": ["R02", "R10"], "D03": ["R03"],
    "D04": ["R04", "R05", "R06", "R12"], "D05": ["R07", "R08"],
    "D06": ["R13", "R14"], "D07": ["R11", "R15", "R16", "R17"],
    "D08": ["R18"], "D09": ["R19", "R20"], "D10": ["R21"], "D11": ["R22"], "D12": ["R23"],
    "D13": ["R29"], "D14": ["R05", "R30"], "D15": [], "D16": [],
}

EXPECTED_TEST_FAILURES = {
    "D02": [("CHO 마스터 세포주", "① 성숙마우스접종시험")],
    "D05": [("CHO 제조용 세포주", "세포성장 및 증식확인시험"),
            ("Component B", "잔류 숙주세포 유래 DNA시험"),
            ("Component B", "박테리오파지부정시험")],
    "D06": [("CHO 제조용 세포주", "세포성장 및 증식확인시험"),
            ("E.coli 제조용 세포주", "생균수시험"),
            ("Component B", "잔류 숙주세포 유래 단백질시험"),
            ("나노파티클", "PH측정시험")],
    "D07": [("항원바이알", "성상"), ("항원바이알", "PH측정시험"), ("항원바이알", "엔도톡신시험")],
    "D09": [("CHO 마스터 세포주", "세포성장 및 증식확인시험"),
            ("Component A 본배양액", "결핵균부정시험")],
}

# D15/D16 change notation, not the observation. They are positive controls.
EXPECTED_TEST_PASSES = {
    "D06": [("Component B", "엔도톡신시험", "760 IU/mg of protein")],
    "D15": [("Component A", "무균시험", "x")],
    "D16": [("Component A", "무균시험", "불검출")],
}


def engine_fingerprint():
    digest = hashlib.sha256()
    sources = [ROOT / "json추출.py", ROOT / "scripts" / "validate_corpus.py",
               *sorted((ROOT / "sp_pdf_judger").rglob("*.py")), *sorted((ROOT / "sp_pdf_judger" / "rules").rglob("*.md"))]
    for path in sources:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def assess_result(key, result, live):
    compact = lambda value: "".join((value or "").split()).casefold()
    found = {r["rule_id"] for r in result.metadata["policy_audit"] if r["status"] == "FAIL"}
    missing_tests = [f"{stage} / {test}" for stage, test in EXPECTED_TEST_FAILURES.get(key, [])
        if not any(compact(stage) in compact(e.section_title) and compact(test) == compact(e.test_name)
                   and e.final_status == FAIL_LABEL for e in result.evaluations)]
    missing_passes = [f"{stage} / {test} / {value}" for stage, test, value in EXPECTED_TEST_PASSES.get(key, [])
        if not any(compact(stage) in compact(e.section_title) and compact(test) == compact(e.test_name)
                   and compact(value) == compact(e.result) and e.final_status == PASS_LABEL for e in result.evaluations)]
    audit = {"mode": "live" if live else "offline", "expected_rules": EXPECTED[key],
        "missing_rules": sorted(set(EXPECTED[key]) - found), "missing_test_failures": missing_tests,
        "missing_test_passes": missing_passes,
        "normal_false_positives": [e.test_name for e in result.evaluations if e.final_status == FAIL_LABEL] if key in {"D00", "D15", "D16"} else [],
        "holds": [{"test": e.test_name, "section": e.section_title, "reason": e.reason, "rule_id": e.section_number}
                  for e in result.evaluations if e.final_status == HOLD_LABEL],
        "completed_at": datetime.now(timezone.utc).isoformat()}
    audit["live_acceptance_complete"] = bool(live and result.metadata.get("llm_success_count")
        and not result.metadata.get("llm_last_error") and not audit["holds"] and not audit["missing_rules"]
        and not missing_tests and not missing_passes and not audit["normal_false_positives"])
    # D13 intentionally lacks the source of these six comparisons. Keep their
    # HOLD verdicts visible; validate the causal link rather than force a PASS.
    expected_dependencies = {"R04", "R05", "R12", "R16", "R17", "R30"} if key == "D13" else set()
    linked = {f["rule_id"] for f in result.metadata["policy_audit"]
              if f["status"] == "HOLD" and f.get("details", {}).get("blocked_by") == ["R29"] and "R29" in found}
    audit["expected_dependency_holds"] = sorted(expected_dependencies)
    audit["missing_dependency_links"] = sorted(expected_dependencies - linked)
    audit["unexpected_holds"] = [h for h in audit["holds"] if h["rule_id"] not in expected_dependencies.intersection(linked)]
    audit["live_contract_complete"] = bool(live and result.metadata.get("llm_success_count")
        and not result.metadata.get("llm_last_error") and not audit["unexpected_holds"]
        and not audit["missing_dependency_links"] and not audit["missing_rules"] and not missing_tests
        and not missing_passes and not audit["normal_false_positives"])
    return audit


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Use paid CLOVA API including authoritative permit review")
    parser.add_argument("--cases", nargs="+", choices=list(EXPECTED), default=list(EXPECTED))
    parser.add_argument("--output-dir", type=Path, help="Separate validation output; leaves earlier runs intact")
    args = parser.parse_args()
    engine_version = engine_fingerprint()
    output = args.output_dir or ROOT / ".local_validation" / ("live" if args.live else "integrated_offline")
    output.mkdir(parents=True, exist_ok=True)
    if args.live:
        os.environ["CLOVA_RESPONSE_CACHE_DIR"] = str(ROOT / ".local_validation" / "clova_response_cache")
    permits = sorted((ROOT / "더미데이터").glob("*허가문서*.pdf"))
    if not permits:
        raise SystemExit("Private permit fixture is missing")
    resolution = resolve_submission_permits(permits, "동국바이오사이언스", "스카이코비원멀티주")
    if not args.live:
        # Offline mode validates extraction, deterministic/MD rules and permit retrieval,
        # NOT the CLOVA authoritative judgement. Keep that distinction in the report.
        resolution = ResolvedPermit(None, resolution.paths, resolution.fingerprint, resolution.errors)
    failures = []
    with nullcontext() if args.live else patch("sp_pdf_judger.llm.create_clova_client", return_value=None):
        for key in args.cases:
            pdfs = sorted((ROOT / "더미데이터").glob(f"{key}_*.pdf"))
            if len(pdfs) != 1:
                raise SystemExit(f"Expected one fixture for {key}")
            print(f"START {key} mode={'live' if args.live else 'offline'}", flush=True)
            pipeline = DocumentJudgePipeline(company="동국바이오사이언스", product="스카이코비원멀티주", permit_resolution=resolution)
            count = [0]
            original_judge = pipeline.judge_engine.judge_record
            def progress(record):
                count[0] += 1
                if args.live:
                    print(f"{key} test {count[0]}: {record.test_name}", flush=True)
                return original_judge(record)
            pipeline.judge_engine.judge_record = progress
            result = pipeline.run(pdfs[0])
            payload = asdict(result)
            audit = assess_result(key, result, args.live)
            audit["engine_fingerprint"] = engine_version
            audit["source_sha256"] = hashlib.sha256(pdfs[0].read_bytes()).hexdigest()
            audit["engine_changed_during_run"] = engine_fingerprint() != engine_version
            if audit["engine_changed_during_run"]:
                audit["live_acceptance_complete"] = False
                audit["live_contract_complete"] = False
            payload["validation"] = audit
            destination = output / f"{key}.json"
            if destination.exists():
                history = output / "history"
                history.mkdir(exist_ok=True)
                destination.replace(history / f"{key}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}.json")
            destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print(key, asdict(result.summary), "missing=", audit["missing_rules"], "missing_tests=", audit["missing_test_failures"],
                  "missing_passes=", audit["missing_test_passes"],
                  "normal_false_positives=", audit["normal_false_positives"],
                  "llm_calls=", result.metadata["llm_call_count"], flush=True)
            if audit["engine_changed_during_run"] or audit["missing_rules"] or audit["missing_test_failures"] or audit["missing_test_passes"] or audit["normal_false_positives"] or audit["missing_dependency_links"] or (args.live and not audit["live_contract_complete"]):
                failures.append(key)
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
