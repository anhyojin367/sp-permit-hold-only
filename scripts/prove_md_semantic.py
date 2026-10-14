"""Live proof: change only an MD instruction and observe the same evidence again."""
import json
import sys
from dataclasses import asdict
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sp_pdf_judger.policy_engine import RuleBook, PolicyContext, evaluate_policies
from sp_pdf_judger.llm import ClovaJudgeClient
from sp_pdf_judger.extractor import _record_from_dict


if __name__ == "__main__":
    corpus = ROOT / ".local_validation" / "corpus" / "D00.records.json"
    rows = json.loads(corpus.read_text(encoding="utf-8"))
    records = [_record_from_dict(r, i) for i, r in enumerate(rows)]
    pdf = next((ROOT / "더미데이터").glob("D00_*.pdf"))
    target = ROOT / ".local_validation" / "semantic_proof"
    target.mkdir(parents=True, exist_ok=True)
    llm = ClovaJudgeClient()
    if not llm.enabled:
        raise SystemExit("CLOVA is unavailable")
    ctx = PolicyContext(pdf, records, product="스카이코비원멀티주", llm=llm)
    results = []
    # These are isolated demonstration policies, NOT the active medical criteria.
    for threshold, expected in [(2, "PASS"), (4, "FAIL")]:
        folder = ROOT / "tests" / "fixtures" / "semantic_proof" / str(threshold)
        book = RuleBook(folder)
        finding = evaluate_policies(ctx, book)[0]
        results.append({"fingerprint": book.fingerprint, "expected": expected, "actual": asdict(finding)})
        print(threshold, finding.status, finding.reason, flush=True)
    (target / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(any(r["expected"] != r["actual"]["status"] for r in results))
