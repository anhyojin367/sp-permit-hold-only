"""Validate executable Markdown without reading PDFs or calling CLOVA."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sp_pdf_judger.policy_engine import DEFAULT_RULE_DIR, RuleBook
from sp_pdf_judger.policy_schema import PARAM_MODELS


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules-dir", type=Path, default=DEFAULT_RULE_DIR)
    parser.add_argument("--describe", choices=sorted(PARAM_MODELS), help="Print the selected operation's parameter schema")
    args = parser.parse_args(argv)
    if args.describe:
        print(json.dumps(PARAM_MODELS[args.describe].model_json_schema(), ensure_ascii=False, indent=2))
        return 0
    try:
        book = RuleBook(args.rules_dir)
    except (OSError, ValueError) as exc:
        print(f"MD 설정 오류 — 판정을 시작하지 않았습니다.\n{exc}", file=sys.stderr)
        return 2
    print(f"MD 설정 검사 통과: 규칙 {len(book.rules)}개, 허가서 검색 명칭 묶음 {len(book.permit_search_aliases)}개")
    print(f"규칙 SHA256: {book.fingerprint}")
    print("형식/설정 검사이며, 판단 기준의 의학적 적절성이나 문서 판정의 정확성을 보증하지 않습니다. CLOVA 호출 없음.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
