from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sp_pdf_judger.rule_regression import (  # noqa: E402
    set_gold_from_latest,
    workbook_summary,
    write_output_index,
)


def _output_dir(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def command_list(args: argparse.Namespace) -> int:
    output_dir = _output_dir(args.output)
    rows = [
        workbook_summary(path)
        for path in sorted(output_dir.glob("*__rule_regression.xlsx"))
    ]
    print(json.dumps({"workbooks": rows}, ensure_ascii=False, indent=2))
    return 0


def command_set_gold(args: argparse.Namespace) -> int:
    workbook_path = Path(args.workbook)
    if not workbook_path.is_absolute():
        workbook_path = PROJECT_ROOT / workbook_path
    set_gold_from_latest(workbook_path, status=args.status)
    print(json.dumps(workbook_summary(workbook_path), ensure_ascii=False, indent=2))
    return 0


def command_index(args: argparse.Namespace) -> int:
    index_path = write_output_index(_output_dir(args.output))
    print(index_path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PDF별 규칙 회귀 엑셀과 정답지를 관리합니다.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="생성된 회귀 엑셀을 조회합니다.")
    list_parser.add_argument("--output", default="OUTPUT")
    list_parser.set_defaults(handler=command_list)

    gold_parser = subparsers.add_parser(
        "set-gold",
        help="가장 최근 실행 결과를 정답지 행으로 확정합니다.",
    )
    gold_parser.add_argument("workbook")
    gold_parser.add_argument("--status", default="확정")
    gold_parser.set_defaults(handler=command_set_gold)

    index_parser = subparsers.add_parser("index", help="OUTPUT 인덱스를 다시 만듭니다.")
    index_parser.add_argument("--output", default="OUTPUT")
    index_parser.set_defaults(handler=command_index)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
