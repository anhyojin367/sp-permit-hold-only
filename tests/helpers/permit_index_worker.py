from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time

from sp_pdf_judger.permit_catalog import PermitPolicy
from sp_pdf_judger.permit_conditions import parse_permit_conditions
from sp_pdf_judger.permit_index import (
    PermitIndexSnapshot,
    build_condition_manifest,
    build_permit_search_index,
    load_or_build_permit_index,
)
from sp_pdf_judger.permit_ocr import PermitPageText
from sp_pdf_judger.permit_pdf_store import PermitPdfStore


root, pdf, result = map(Path, sys.argv[1:4])
os.environ["SP_PERMIT_INDEX_DIR"] = str(root)
digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
policy = PermitPolicy("worker", True, True, True, "native")


def builder() -> PermitIndexSnapshot:
    with (root.parent / "builder-count.txt").open("a", encoding="utf-8") as stream:
        stream.write("built\n")
        stream.flush()
        os.fsync(stream.fileno())
    time.sleep(0.3)
    pages = (PermitPageText(1, "1. 원액\n1.1. 확인시험\n최소 10개이어야 한다.", "native"),)
    store = PermitPdfStore([], policy=policy)
    chunks = tuple(store._parse_page_texts(pages, pdf.name, pdf_sha256=digest))
    conditions = tuple(c for chunk in chunks for c in parse_permit_conditions(chunk))
    return PermitIndexSnapshot(
        pages=pages,
        chunks=chunks,
        source_documents=tuple(store.source_documents),
        conditions=conditions,
        extraction_errors=(),
        page_errors=(),
        search_index=build_permit_search_index(chunks),
        build_diagnostics=(("page_count", 1),),
        condition_manifest=build_condition_manifest(chunks, conditions),
    )


loaded = load_or_build_permit_index(
    pdf_path=pdf,
    pdf_sha256=digest,
    policy=policy,
    builder=builder,
)
result.write_text(
    json.dumps({"cache_hit": loaded.cache_hit, "failure_code": loaded.failure_code}),
    encoding="utf-8",
)
