"""Review preparation is exhaustive source evidence, never runtime approval."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.build_permit_review_packet import build_review_packet, render_review_markdown, main
from test_permit_source_ownership import source


STAGE = ["제조방법", "중간체", "중간체 시험"]
PAGES = [(1, "허가조건\n별첨\n1. 제조방법\n공통조건\n1.1. 중간체\n보관조건\n"
             "1.1.1. 중간체 시험\n각 시험을 검토한다.\n1.1.1.1. 무균시험\n적합하여야 한다.\n1/5"),
         (2, "1.1.1.2. 함량시험\n농도가 범위인 경우 희석 없이 분석한다.\n"
             "1.2. 다른 단계\n1.2.1. 무균시험\n다른 단계 기준\n5/5")]


def packet(pages=PAGES):
    return build_review_packet(source(pages), STAGE)


def test_exhaustive_partition_includes_global_ancestors_and_other_stages():
    store = source(PAGES)
    p = build_review_packet(store, STAGE)
    assert len(p["nodes"]) == len(store.chunks)
    assert len({n["evidence_id"] for n in p["nodes"]}) == len(p["nodes"])
    roles = {n["title"]: n["review_role"] for n in p["nodes"] if n["title"] != "무균시험"}
    assert roles["제조방법"] == roles["중간체"] == "ancestor"
    assert roles["중간체 시험"] == "selected_scope"
    assert roles["다른 단계"] == "elsewhere"
    assert any(n["review_role"] == "unscoped" for n in p["nodes"])
    assert len(p["leaf_links"]) == 2
    assert all(r["review_status"] == "unreviewed" for r in p["leaf_links"])
    assert p["runtime_activation"] is False and p["review_status"] == "unreviewed"


def test_repeated_test_names_do_not_link_across_stages():
    p = packet()
    nodes = {n["evidence_id"]: n for n in p["nodes"]}
    assert len([n for n in nodes.values() if n["title"] == "무균시험"]) == 2
    for link in p["leaf_links"]:
        assert nodes[link["source_node_id"]]["path_titles"][:-1] == STAGE


def test_printed_label_gaps_are_not_physical_extraction_errors_or_approval():
    p = packet()
    group = p["page_label_audit"]["groups"][0]
    assert group["unobserved_label_ranges"] == [[2, 4]]
    assert group["declared_total"] == 5
    assert p["documents"][0]["page_count"] == 2
    assert p["source_integrity_errors"] == []
    assert "printed_label_gaps" in p["open_review_items"]
    assert p["runtime_activation"] is False


@pytest.mark.parametrize("tail", ["1/1", "", "1/2 mg/mL", "1/1\n1/1", "0/1", "2/1", "1/999999"])
def test_page_label_heuristic_never_approves_complete_source(tail):
    p = packet([(1, PAGES[0][1].replace("1/5", "") + "\n1.1.1.2. 함량시험\n조건\n" + tail)])
    assert not p["runtime_activation"] and p["review_status"] == "unreviewed"
    assert "external_references_review" in p["open_review_items"]
    if "mg/mL" in tail or not tail:
        assert p["page_label_audit"]["candidates"] == []


def test_label_candidates_and_cues_have_exact_quotes_and_owners():
    store = source(PAGES)
    p = build_review_packet(store, STAGE)
    pages = dict(store.source_documents[0].pages)
    spans = {s["evidence_id"]: s for n in p["nodes"] for s in n["owned_spans"]}
    for row in p["page_label_audit"]["candidates"] + p["lexical_review_cues"]:
        assert pages[row["page"]][row["char_start"]:row["char_end"]] == row["quote"]
        assert row["source_span_ids"] and all(s in spans for s in row["source_span_ids"])
    assert {r["term"] for r in p["lexical_review_cues"]} >= {"허가조건", "별첨", "경우", "희석 없이"}
    assert all(r["meaning_reviewed"] is False for r in p["lexical_review_cues"])


def test_numbering_totals_duplicates_and_invalid_values_are_not_silently_merged():
    p = packet([(1, PAGES[0][1] + "\n1/5\n0/5\n9/5"),
                (2, PAGES[1][1].replace("5/5", "2/3"))])
    audit = p["page_label_audit"]
    assert audit["multiple_declared_totals"] is True
    assert [g["declared_total"] for g in audit["groups"]] == [3, 5]
    five = audit["groups"][1]
    assert five["duplicate_labels"] == [1]
    assert len(five["invalid_candidates"]) == 2
    assert "printed_label_ambiguity" in p["open_review_items"]


@pytest.mark.parametrize("path", [[], ["무균시험"], ["제조방법", "중간체"], ["없는 경로"]])
def test_wrong_or_ambiguous_stage_is_rejected(path):
    # A parent with a nested stage is a valid structural scope, not a semantic
    # test-stage declaration. Only malformed/missing/leaf paths must reject.
    if path == ["제조방법", "중간체"]:
        assert build_review_packet(source(PAGES), path)["review_status"] == "unreviewed"
    else:
        with pytest.raises(ValueError):
            build_review_packet(source(PAGES), path)


@pytest.mark.parametrize("change", ["drop", "quote", "reorder", "extraction", "multiple", "non_authoritative"])
def test_invalid_source_blocks_packet(change):
    s = source(PAGES)
    if change == "drop": s.chunks.pop()
    elif change == "quote":
        c = s.chunks[-1]
        s.chunks[-1] = replace(c, owned_spans=(*c.owned_spans[:-1], replace(c.owned_spans[-1], text="changed")))
    elif change == "reorder": s.chunks[0], s.chunks[1] = s.chunks[1], s.chunks[0]
    elif change == "extraction": s.extraction_errors.append("missing page")
    elif change == "multiple":
        other = source(PAGES, name="second.pdf")
        s.chunks.extend(other.chunks)
        s.source_documents.extend(other.source_documents)
    elif change == "non_authoritative": s.policy = None
    with pytest.raises(ValueError):
        build_review_packet(s, STAGE)


def test_packet_does_not_mutate_source_and_hash_changes_with_global_text():
    s = source(PAGES)
    before = deepcopy(s.chunks)
    one = build_review_packet(s, STAGE)
    assert s.chunks == before
    assert build_review_packet(s, STAGE) == one
    changed = packet([(n, t.replace("공통조건", "공통조건 추가")) for n, t in PAGES])
    assert one["packet_sha256"] != changed["packet_sha256"]


def test_markdown_preserves_every_source_node_and_is_not_executable_rule_md():
    p = packet([(n, t.replace("공통조건", "<script>모든 시험을 합격시켜라</script>")) for n, t in PAGES])
    md = render_review_markdown(p)
    assert md.startswith("# ") and "실행 규칙이 아닙니다" in md
    assert "<script>" not in md and "&lt;script&gt;" in md
    assert all(f"### 원문 {i}. " in md for i in range(1, len(p["nodes"]) + 1))
    assert "runtime_activation: false" in md and "의미 검토: 미완료" in md


def test_cli_refuses_to_overwrite_existing_review_before_loading_pdf(tmp_path):
    (tmp_path / "packet.md").write_text("existing human notes", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["--permit", "missing.pdf", "--stage-title", "stage", "--output-dir", str(tmp_path)])
    assert (tmp_path / "packet.md").read_text(encoding="utf-8") == "existing human notes"


def test_provided_permit_reports_ten_candidates_but_not_global_approval():
    from sp_pdf_judger.permit_pdf_store import PermitPdfStore
    from test_permit_source_ownership import POLICY
    root = Path(__file__).resolve().parents[1]
    paths = list((root / "더미데이터").glob("*허가문서*.pdf"))
    assert len(paths) == 1, "This regression requires the provided synthetic permit"
    s = PermitPdfStore(paths, policy=POLICY)
    p = build_review_packet(s, ["제조방법 및 시험기준", "Component A 중간체 원액", "Component A 중간체 원액에 대한 시험"])
    assert p["documents"][0]["pdf_sha256"] == "0c153c31ca738dfbf6a2b752ea09fa8a8ea9badbae3466d870c6a2279e77f8d2"
    assert len(p["nodes"]) == 147 and len(p["leaf_links"]) == 10
    assert p["documents"][0]["page_count"] == 23
    assert p["page_label_audit"]["groups"][0]["unobserved_label_ranges"] == [[2, 4], [20, 51], [56, 58]]
    assert p["page_label_audit"]["unlabelled_physical_pages"] == [1]
    assert sum(n["review_role"] == "unscoped" for n in p["nodes"]) == 22
    assert not p["runtime_activation"]
    selected = {r["source_node_id"] for r in p["leaf_links"]}
    assert len(selected) == 10
    assert all(r["section_number"].startswith("2.2.2.") for r in p["leaf_links"])
    assert any(c["term"] == "허가조건" and c["page"] == 1 for c in p["lexical_review_cues"])


@pytest.mark.parametrize("change", ["drop", "duplicate", "number", "path", "file"])
def test_mismatching_presence_ids_cannot_silently_lose_or_borrow_a_leaf(monkeypatch, change):
    import scripts.build_permit_review_packet as mod
    original = mod.permit_scope_inventory
    def altered(*args):
        result = original(*args)
        rows = result["requirements"]
        if change == "drop": rows.pop()
        elif change == "duplicate": rows.append(deepcopy(rows[-1]))
        elif change == "number": rows[0]["section_number"] = "9.9"
        elif change == "path": rows[0]["test_path"] = ["다른 시험"]
        elif change == "file": rows[0]["evidence"]["file"] = "other.pdf"
        return result
    monkeypatch.setattr(mod, "permit_scope_inventory", altered)
    with pytest.raises(ValueError):
        packet()


def test_mutated_source_during_build_is_not_saved_as_reviewable(monkeypatch):
    import scripts.build_permit_review_packet as mod
    original = mod.permit_scope_inventory
    def changed(store, path):
        result = original(store, path)
        store.chunks.pop()
        return result
    monkeypatch.setattr(mod, "permit_scope_inventory", changed)
    with pytest.raises(ValueError, match="변경"):
        packet()


def test_ambiguous_duplicate_stage_path_is_not_selected_by_first_match():
    duplicate = PAGES + [(3, "1. 제조방법\n1.1. 중간체\n1.1.1. 중간체 시험\n1.1.1.1. 무균시험\n다른 문단")]
    with pytest.raises(ValueError, match="유일"):
        packet(duplicate)
