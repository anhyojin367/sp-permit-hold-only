"""Read-only fixture inspection. Outputs are local QA data, never runtime rules."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fitz
from sp_pdf_judger.extractor import extract_records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--extract", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="Re-extract, preserving old QA records in history")
    args = parser.parse_args()
    out = ROOT / ".local_validation" / "corpus"
    out.mkdir(parents=True, exist_ok=True)
    for pdf in sorted((ROOT / "더미데이터").glob("*.pdf")):
        key = pdf.name.split("_")[0] if pdf.name.startswith("D") else "permit"
        digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
        with fitz.open(pdf) as doc:
            pages = [{"page": i + 1, "text": page.get_text()} for i, page in enumerate(doc)]
        (out / f"{key}.pages.json").write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / f"{key}.txt").write_text("\n".join(f"\n=== PDF PAGE {p['page']} ===\n{p['text']}" for p in pages), encoding="utf-8")
        print(f"{key}: {len(pages)} pages, sha256={digest[:12]}", flush=True)
        target = out / f"{key}.records.json"
        if (args.extract or args.refresh) and key != "permit" and (args.refresh or not target.exists()):
            records = extract_records(pdf, out / f"{key}_extract")
            if target.exists():
                history = out / "history"
                history.mkdir(exist_ok=True)
                target.replace(history / f"{key}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}.records.json")
            target.write_text(json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{key}: {len(records)} records", flush=True)


if __name__ == "__main__":
    main()
