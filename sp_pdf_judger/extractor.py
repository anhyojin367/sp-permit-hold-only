from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import List

from .schemas import ExtractedRecord


def _record_from_dict(d: dict, order_idx: int) -> ExtractedRecord:
    return ExtractedRecord(
        record_type=d.get("record_type") or "test",
        order_idx=d.get("order_idx") or d.get("order_index") or order_idx,
        order_index=d.get("order_index") or order_idx,

        section_number=d.get("section_number"),
        section_title=d.get("section_title"),

        test_name=d.get("test_name"),
        content_label=d.get("content_label"),
        content=d.get("content"),

        criteria=d.get("criteria"),
        result=d.get("result"),
        method=d.get("method"),
        test_date=d.get("test_date"),
        test_period=d.get("test_period"),
        remarks=d.get("remarks"),

        page_start=d.get("page_start") or 0,
        page_end=d.get("page_end") or 0,
        source_types=d.get("source_types") or [],
        raw_text=d.get("raw_text"),

        diagram_data=d.get("diagram_data"),
        section_path=d.get("section_path") or [],
        parent_test_group=d.get("parent_test_group"),
        subtest_order=d.get("subtest_order"),
        result_table=d.get("result_table"),
        tables=d.get("tables") or [],
        controls=d.get("controls") or [],
        normalized_text=d.get("normalized_text"),
        ocr_suspect=bool(d.get("ocr_suspect", False)),
        batch_id=d.get("batch_id"),
        batch_evidence=d.get("batch_evidence"),
    )


def extract_records(pdf_path: Path, output_dir: Path) -> List[ExtractedRecord]:
    """
    json추출.py를 실행해서 output_dir/05_records.json을 생성한 뒤,
    diagram_data / normalized_text / section_path까지 보존해서 반환한다.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    project_root = Path(__file__).resolve().parent.parent
    extractor_script = project_root / "json추출.py"

    if not extractor_script.exists():
        raise FileNotFoundError(f"추출기 파일을 찾을 수 없습니다: {extractor_script}")

    cmd = [
        sys.executable,
        str(extractor_script),
        "--pdf",
        str(pdf_path),
        "--out",
        str(output_dir),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode != 0:
        raise RuntimeError(
            "json추출.py 실행 실패\n"
            f"STDOUT:\n{result.stdout}\n\n"
            f"STDERR:\n{result.stderr}"
        )

    records_json_path = output_dir / "05_records.json"

    if not records_json_path.exists():
        raise FileNotFoundError(f"05_records.json이 생성되지 않았습니다: {records_json_path}")

    with open(records_json_path, "r", encoding="utf-8") as f:
        loaded = json.load(f)

    return [
        _record_from_dict(row, order_idx=i)
        for i, row in enumerate(loaded)
        if isinstance(row, dict)
    ]
