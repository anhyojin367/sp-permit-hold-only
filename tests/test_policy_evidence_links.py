"""Citation links resolve only to the linked document and its recorded page."""
import base64
import html
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from sp_judgement_bridge import _policy_evidence_links


def result(tmp_path, evidence, permits=None):
    permit = tmp_path / "permit.pdf"
    return SimpleNamespace(pdf_path=tmp_path / "sp.pdf", metadata={
        "permit_paths": [str(p) for p in (permits or [permit])],
        "policy_audit": [{"rule_id": "R07", "evidence": evidence}],
    })


def parsed_links(markup):
    links = []
    for encoded_url in re.findall(r'href="([^"]+)"', markup):
        query = parse_qs(urlsplit(html.unescape(encoded_url)).query)
        token = query["pdf"][0]
        path = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        links.append((Path(path), int(query["page"][0])))
    return links


def test_multiple_authority_pages_keep_separate_clickable_targets(tmp_path):
    evidence = [{"source": "sp", "file": "sp.pdf", "page": 16},
                {"source": "permit", "file": "permit.pdf", "page": 13},
                {"source": "permit", "file": "permit.pdf", "page": 14}]
    markup = _policy_evidence_links(result(tmp_path, evidence), "R07")
    assert parsed_links(markup) == [(tmp_path / "sp.pdf", 16),
                                   (tmp_path / "permit.pdf", 13),
                                   (tmp_path / "permit.pdf", 14)]
    assert "허가서 13쪽" in markup and "허가서 14쪽" in markup


def test_unlinked_file_or_invalid_page_does_not_create_misleading_link(tmp_path):
    evidence = [{"source": "permit", "file": "unlinked.pdf", "page": 13},
                {"source": "permit", "file": "permit.pdf", "page": 0},
                {"source": "permit", "file": "permit.pdf", "page": "13"}]
    assert _policy_evidence_links(result(tmp_path, evidence), "R07") == ""


def test_ambiguous_basename_does_not_guess_and_duplicate_page_is_one_link(tmp_path):
    evidence = [{"source": "permit", "file": "permit.pdf", "page": 13}]
    assert not _policy_evidence_links(result(tmp_path, evidence,
        [tmp_path / "a" / "permit.pdf", tmp_path / "b" / "permit.pdf"]), "R07")
    markup = _policy_evidence_links(result(tmp_path, evidence * 2), "R07")
    assert len(parsed_links(markup)) == 1


def test_sanitized_permit_basename_links_to_the_runtime_uploaded_pdf(tmp_path):
    runtime_permit = tmp_path / "uploaded-permits" / "permit.pdf"
    runtime_permit.parent.mkdir()
    runtime_permit.write_bytes(b"permit")
    linked = result(
        tmp_path,
        [{"source": "permit", "file": "permit.pdf", "page": 13}],
    )
    linked.metadata["permit_paths"] = ["permit.pdf"]
    linked.runtime_permit_paths = (runtime_permit,)

    markup = _policy_evidence_links(linked, "R07")

    assert parsed_links(markup) == [(runtime_permit, 13)]
