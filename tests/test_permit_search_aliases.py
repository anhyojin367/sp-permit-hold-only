"""MD-controlled candidate names must not change source text or stage scope."""
import pytest

from sp_pdf_judger.permit_catalog import PermitPolicy
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.policy_engine import RuleBook
from sp_pdf_judger.schemas import ExtractedRecord


POLICY = PermitPolicy("test", True, True, True, "native")
NAMES = ["잔류 숙주세포 유래 DNA시험", "잔류 숙주세포 유래 DNA함량시험"]


def store():
    return PermitPdfStore.from_page_texts([
        (1, "2.1. Component A 중간체원액\n2.1.1. 잔류 숙주세포 유래 DNA함량시험\n10 ng/mg 미만"),
        (2, "2.2. Component B 중간체원액\n2.2.1. 잔류 숙주세포 유래 DNA함량시험\n15 ng/mg 미만"),
    ], policy=POLICY, source_file="permit.pdf")


def record(stage="Component B 중간체원액"):
    return ExtractedRecord(test_name=NAMES[0], section_title=stage)


def test_alias_finds_the_source_heading_but_never_renames_it():
    source = store()
    assert source.search(record()) == []  # reproduces the original exact-name gap
    source.configure_search_aliases([NAMES])
    hits = source.search(record())
    assert len(hits) == 1 and hits[0].page_number == 2
    assert hits[0].title == NAMES[1]
    assert "15 ng/mg" in hits[0].text
    assert record().test_name == NAMES[0]
    source.configure_search_aliases([])
    assert source.search(record()) == []  # removing MD mapping really takes effect


def test_known_stage_without_a_test_cannot_borrow_an_alias_from_another_stage():
    source = store()
    source.chunks.extend(PermitPdfStore.from_page_texts([
        (3, "2.3. 나노파티클 원액\n2.3.1. 성상\n무색")], policy=POLICY).chunks)
    source.configure_search_aliases([NAMES])
    assert source.search(record("나노파티클 원액")) == []


def test_same_stage_conflicting_alias_headings_are_both_retained():
    source = store()
    source.chunks.extend(PermitPdfStore.from_page_texts([
        (5, "2.2. Component B 중간체원액\n2.2.1. 잔류 숙주세포 유래 DNA시험\n20 ng/mg 미만")], policy=POLICY).chunks)
    source.configure_search_aliases([NAMES])
    assert {chunk.page_number for chunk in source.search(record())} == {2, 5}


def write_book(path, aliases):
    import yaml
    data = {"permit_search_aliases": aliases, "rules": [{"id": "T", "title": "테스트", "operation": "page_continuity", "instruction": "연속성"}]}
    # Test fixtures only: these are temporary MD inputs, never application files.
    path.write_text("---\n" + yaml.safe_dump(data, allow_unicode=True) + "---\n", encoding="utf-8")


def test_md_aliases_are_product_scoped_and_reloadable(tmp_path):
    path = tmp_path / "rule.md"
    entry = {"products": ["제품A"], "names": NAMES, "reason": "제공 문서의 동일 측정대상 명칭 후보"}
    write_book(path, [entry])
    book = RuleBook(tmp_path)
    assert book.search_alias_groups("제품A") == [NAMES]
    assert book.search_alias_groups("제품B") == []
    assert book.search_alias_groups("") == []
    write_book(path, [])
    assert RuleBook(tmp_path).search_alias_groups("제품A") == []


@pytest.mark.parametrize("bad", [
    {"products": [], "names": NAMES, "reason": "범위 없음"},
    {"products": ["제품A"], "names": [NAMES[0]], "reason": "쌍 없음"},
    {"products": ["제품A"], "names": ["", NAMES[0]], "reason": "빈 이름"},
    {"products": ["제품A"], "names": NAMES, "reasno": "오타"},
])
def test_malformed_search_alias_is_rejected_instead_of_silently_widening_search(tmp_path, bad):
    write_book(tmp_path / "rule.md", [bad])
    with pytest.raises(ValueError):
        RuleBook(tmp_path)


def test_actual_corpus_dna_heading_gap_is_resolved_with_local_md():
    import json
    from pathlib import Path
    from sp_pdf_judger.extractor import _record_from_dict
    root = Path(__file__).resolve().parents[1]
    pages_path = root / ".local_validation/corpus/permit.pages.json"
    record_path = root / ".local_validation/corpus/D00.records.json"
    if not pages_path.exists() or not record_path.exists():
        pytest.skip("Private extracted corpus not installed")
    pages = json.loads(pages_path.read_text(encoding="utf-8"))
    rows = json.loads(record_path.read_text(encoding="utf-8"))
    source = PermitPdfStore.from_page_texts([(p["page"], p["text"]) for p in pages], policy=POLICY)
    source.configure_search_aliases(RuleBook().search_alias_groups("스카이코비원멀티주"))
    records = [_record_from_dict(row, i) for i, row in enumerate(rows)]
    dna = [r for r in records if r.test_name == NAMES[0]]
    assert len(dna) == 2
    for item in dna:
        hits = source.search(item)
        assert len(hits) == 1 and hits[0].title == NAMES[1]
        assert "15 ng/mg of protein" in hits[0].text
        stage = "Component A" if "Component A" in item.section_title else "Component B"
        assert stage in hits[0].normalized_stage_path
    nano = next(r for r in records if r.test_name == "단백질함량시험" and "나노파티클" in r.section_title)
    assert source.search(nano) == []  # this absent permit entry must stay absent
