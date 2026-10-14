from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import pytest

from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.permit_source_ownership import build_permit_ownership_inventory


def test_provided_permit_ownership_does_not_change_legacy_retrieval():
    from scripts.prove_permit_ownership import POLICY
    files = list((Path(__file__).resolve().parents[1] / "더미데이터").glob("*허가문서*.pdf"))
    if len(files) != 1: pytest.skip("Provided synthetic permit required")
    store = PermitPdfStore(files, policy=POLICY)
    fields = ["source_file", "page_number", "section_number", "title", "text", "page_start",
              "page_end", "section_path_titles", "normalized_test_name", "normalized_stage_path"]
    old_fields = [{k: asdict(c)[k] for k in fields} for c in store.chunks]
    digest = hashlib.sha256(json.dumps(old_fields, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()
    # Observed before this change; not recomputed to approve changed fixtures.
    assert digest == "1af8d1fa8d0b23d6e4184533976dda831102d6e7c47b5a018833f6091e353591"
    manifest = build_permit_ownership_inventory(store)
    assert manifest["errors"] == []
    assert len(manifest["nodes"]) == 147
    assert len(manifest["unscoped_ids"]) == 22
    assert sum(len(c.owned_spans) for c in store.chunks) == 276
    assert len([c for c in store.chunks if "경우" in c.owned_text]) == 3
    assert manifest["documents"][0]["pdf_sha256"] == "0c153c31ca738dfbf6a2b752ea09fa8a8ea9badbae3466d870c6a2279e77f8d2"


def test_multpage_pdf_has_exclusive_ownership_and_rejects_changed_file(tmp_path, monkeypatch):
    from scripts.prove_permit_ownership import POLICY, run_proof
    font = Path("C:/Windows/Fonts/malgun.ttf")
    if not font.is_file(): pytest.skip("Korean font required")
    result = run_proof(tmp_path, font)
    assert result["llm_calls"] == 0 and not result["manifest"]["errors"]
    pdf = tmp_path / "permit_ownership.pdf"
    store = PermitPdfStore([pdf], policy=POLICY)
    original_read = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: b"changed" if path == pdf else original_read(path))
    assert build_permit_ownership_inventory(store)["errors"]
