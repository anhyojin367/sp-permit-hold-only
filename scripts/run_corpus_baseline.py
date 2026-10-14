"""Offline legacy baseline; does not claim LLM integration coverage."""
import json
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sp_pdf_judger.pipeline import DocumentJudgePipeline
from sp_pdf_judger.extractor import _record_from_dict

if __name__ == "__main__":
    out = ROOT / ".local_validation" / "baseline"
    out.mkdir(parents=True, exist_ok=True)
    with patch("sp_pdf_judger.llm.create_clova_client", return_value=None):
        for pdf in sorted((ROOT / "더미데이터").glob("D*.pdf")):
            key = pdf.name.split("_")[0]
            if (out / f"{key}.json").exists():
                continue
            raw = json.loads((ROOT / ".local_validation" / "corpus" / f"{key}.records.json").read_text(encoding="utf-8"))
            records = [_record_from_dict(row, i) for i, row in enumerate(raw)]
            pipe = DocumentJudgePipeline(company="동국바이오사이언스", product="스카이코비원멀티주", permit_enabled=False)
            result = pipe.run(pdf, extracted_records=records)
            payload = {"summary": asdict(result.summary), "evaluations": [asdict(e) for e in result.evaluations],
                       "manufacturing_status": result.manufacturing_summary_status,
                       "manufacturing_reason": result.manufacturing_summary_reason}
            (out / f"{key}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(key, payload["summary"], flush=True)
