"""One-record live diagnosis. Saves no keys or HTTP headers."""
import json
import argparse
import sys
import hashlib
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sp_pdf_judger.extractor import _record_from_dict
from sp_pdf_judger.pipeline import DocumentJudgePipeline
from sp_pdf_judger.permit_catalog import resolve_submission_permits
from sp_pdf_judger.policy_engine import RuleBook
from dataclasses import asdict

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="D00")
    parser.add_argument("--test", default="세포주확인시험")
    parser.add_argument("--stage", default="")
    parser.add_argument("--context-only", action="store_true", help="Inspect retrieval without any API call")
    args = parser.parse_args()
    rows = json.loads((ROOT / ".local_validation/corpus" / f"{args.case}.records.json").read_text(encoding="utf-8"))
    records = [_record_from_dict(row, i) for i, row in enumerate(rows)]
    record = next(r for r in records if r.test_name == args.test and args.stage in (r.section_title or ""))
    resolution = resolve_submission_permits(list((ROOT / "더미데이터").glob("*허가문서*.pdf")), "동국바이오사이언스", "스카이코비원멀티주")
    pipeline = DocumentJudgePipeline(company="동국바이오사이언스", product="스카이코비원멀티주", permit_resolution=resolution)
    book = RuleBook()
    pipeline.permit_store.configure_search_aliases(book.search_alias_groups(pipeline.product))
    if args.context_only:
        print(pipeline.judge_engine._get_permit_context(record))
        print(json.dumps([vars(c) for c in pipeline.permit_store.chunks if args.test in c.title], ensure_ascii=False, indent=2))
        raise SystemExit(0)
    original = pipeline.llm_client.explain
    trace = []
    def capture(**kwargs):
        result = original(**kwargs)
        trace.append({"request": kwargs, "response": result.model_dump() if result else None})
        return result
    pipeline.llm_client.explain = capture
    judgement = pipeline.judge_engine.judge_record(record)
    suffix = hashlib.sha256((args.case + args.stage + args.test).encode()).hexdigest()[:12]
    path = ROOT / ".local_validation" / f"permit_probe_{args.case}_{suffix}.json"
    path.write_text(json.dumps({"trace": trace, "judgement": asdict(judgement), "permit_audit": pipeline.llm_client.permit_audit,
        "rule_fingerprint": book.fingerprint, "comparison_bases": pipeline.judge_engine.comparison_bases}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    for item in trace:
        print(json.dumps(item["response"], ensure_ascii=False))
    print(judgement.final_status, judgement.reason)
