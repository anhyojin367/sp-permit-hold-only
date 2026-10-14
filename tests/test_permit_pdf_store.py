from pathlib import Path

import pytest

from sp_pdf_judger.permit_catalog import PermitPolicy
from sp_pdf_judger.permit_ocr import PermitPageText
from sp_pdf_judger.permit_pdf_store import PermitChunk, PermitPdfStore, normalize_permit_heading
from sp_pdf_judger.schemas import ExtractedRecord


SKY_POLICY = PermitPolicy(
    policy_id="sky",
    authoritative=True,
    ignore_section_numbers=True,
    require_llm=True,
    ocr_mode="auto",
)


def test_repeated_stage_matching_reuses_bounded_pure_normalization(monkeypatch):
    import sp_pdf_judger.permit_pdf_store as module
    from unittest.mock import Mock
    normalizer = Mock(wraps=module.normalize_permit_heading)
    monkeypatch.setattr(module, "normalize_permit_heading", normalizer)
    stage = "처음방문회사 Component Z 중간체 원액"
    score = module._stage_match_score(stage, stage)
    calls = normalizer.call_count
    for _ in range(100):
        assert module._stage_match_score(stage, stage) == score == 100
    assert normalizer.call_count == calls
    assert module._stage_match_score(stage, "다른 단계") == 0
    assert module._stage_match_score.cache_info().maxsize == 8192


def test_moved_chunk_preserves_legacy_constructor_defaults_and_search_type():
    # Break caught: moving the dataclass drops positional defaults or splits its identity.
    from sp_pdf_judger.permit_models import PermitChunk as StableChunk
    assert StableChunk is PermitChunk
    legacy = StableChunk("permit.pdf", 2, "1", "시험", "내용")
    assert (legacy.page_start, legacy.page_end, legacy.section_path_titles,
            legacy.normalized_test_name, legacy.normalized_stage_path,
            legacy.owned_spans, legacy.evidence_id, legacy.parent_evidence_id,
            legacy.owned_text) == (None, None, (), "", "", (), "", "", "")
    store = PermitPdfStore.from_page_texts([(1, "1. 시험\n내용")])
    assert all(isinstance(c, StableChunk) for c in store.chunks)


def _component_b_record() -> ExtractedRecord:
    return ExtractedRecord(
        record_type="test",
        section_number="9.8.7",
        section_title="Component B 중간체 원액",
        section_path=["원액 시험", "Component B 중간체 원액"],
        test_name="2.1.99 확인시험",
        criteria="확인되어야 함",
        result="발색된 점 확인",
    )


def test_component_b_identity_ignores_dotted_numbers_and_keeps_page_continuation() -> None:
    store = PermitPdfStore.from_page_texts(
        [
            (
                1,
                "2.3. Component B 중간체 원액\n"
                "2.3.1. Component B 중간체원액에 대한 시험\n"
                "2.3.1.7. 확인시험\n"
                "Dot Blot 시험법에 따라 시험한다.",
            ),
            (
                2,
                "반응 후 발색된 점이 확인되어야 한다.\n"
                "2.3.1.8. 엔도톡신시험\n"
                "820 EU/mg 미만이어야 한다.",
            ),
        ],
        policy=SKY_POLICY,
        source_file="permit.pdf",
    )

    chunks = store.search(_component_b_record(), top_k=3)

    assert chunks[0].title == "확인시험"
    assert chunks[0].section_path_titles[-2:] == (
        "Component B 중간체원액에 대한 시험",
        "확인시험",
    )
    assert chunks[0].page_start == 1
    assert chunks[0].page_end == 2
    assert "Dot Blot" in chunks[0].text
    assert "발색된 점이 확인되어야 한다" in chunks[0].text
    assert "엔도톡신" not in chunks[0].text


def test_parent_context_includes_three_level_children_across_pages() -> None:
    store = PermitPdfStore.from_page_texts(
        [
            (4, "2.1.2. 외래성인자부정시험(in vivo)\n상위 공통기준\n"
                "2.1.2.1. 마우스접종시험\n최소 10개를 14일 관찰하고 80% 이상 생존"),
            (5, "2.1.2.1.1. 추가 관찰\n28일 관찰\n"
                "2.1.2.2. 유정란접종시험\n요막강 및 난황낭 시험\n"
                "2.1.3. 다음 시험\n다음 시험만의 기준"),
        ], policy=SKY_POLICY, source_file="permit.pdf",
    )
    record = ExtractedRecord(test_name="외래성인자부정시험(in vivo)")

    parent = store.search(record)[0]
    child = next(chunk for chunk in store.chunks if chunk.title == "마우스접종시험")

    assert parent.page_start == 4 and parent.page_end == 5
    assert all(token in parent.text for token in ("상위 공통기준", "최소 10개", "14일", "80%", "28일", "유정란접종시험"))
    assert "다음 시험만의 기준" not in parent.text
    assert "최소 10개" in child.text and "28일" in child.text
    assert "유정란접종시험" not in child.text


def test_generic_confirmation_test_requires_component_b_stage_match_not_input_order() -> None:
    store = PermitPdfStore.from_page_texts(
        [
            (
                1,
                "2.1. CHO 마스터 세포주\n"
                "2.1.1. CHO 마스터 세포주에 대한 시험\n"
                "2.1.1.7. 확인시험\n"
                "CHO 확인 기준\n"
                "2.3. Component B 중간체 원액\n"
                "2.3.1. Component B 중간체원액에 대한 시험\n"
                "2.3.1.7. 확인시험\n"
                "Component B 확인 기준",
            )
        ],
        policy=SKY_POLICY,
        source_file="permit.pdf",
    )

    chunks = store.search(_component_b_record(), top_k=2)

    assert "Component B" in chunks[0].section_path_titles[-2]
    assert all("CHO" not in chunk.section_path_titles[-2] for chunk in chunks)


def test_default_policy_keeps_page_number_and_section_number_matching() -> None:
    store = PermitPdfStore.from_page_texts(
        [(4, "2.1.7. 확인시험\n확인 기준")], source_file="permit.pdf"
    )
    record = ExtractedRecord(section_number="2.1.7", test_name="다른 시험")

    chunks = store.search(record)

    assert chunks[0].page_number == 4
    assert chunks[0].page_start == 4
    assert chunks[0].page_end == 4
    assert chunks[0].section_number == "2.1.7"


def test_normalize_permit_heading_removes_dotted_number() -> None:
    assert normalize_permit_heading("  2.3.1.7. 확인시험  ") == "확인시험"


def test_wrapped_decimal_requirement_does_not_replace_stage_heading():
    store = PermitPdfStore.from_page_texts([(19,
        "2.2. 완제의약품\n2.2.1. 항원바이알에 대한 시험\n"
        "2.2.1.2. pH측정시험\npH는 7.5 ~\n8.5 이어야 한다.\n"
        "2.2.1.3. 무균시험\n균이 없어야 한다.")], policy=SKY_POLICY)
    assert not any(c.title == "이어야 한다." for c in store.chunks)
    record = ExtractedRecord(test_name="무균시험", section_title="항원바이알 시험")
    assert "항원바이알에 대한 시험" in store.search(record)[0].section_path_titles
    ph = next(c for c in store.chunks if c.title == "pH측정시험")
    assert "8.5 이어야 한다." in ph.text


def test_unicode_filter_size_is_not_a_section_and_keeps_later_acceptance():
    store = PermitPdfStore.from_page_texts([(21,
        "2.2. 항원바이알\n2.2.1. 입자크기측정시험\n검체는 희석 없이\n"
        "0.2 ㎛ 필터로 여과한다. 입자 크기는 27 ~ 88 nm 이어야\n한다.")], policy=SKY_POLICY)
    test = next(c for c in store.chunks if c.title == "입자크기측정시험")
    assert "27 ~ 88 nm" in test.text
    assert not any(c.section_number == "0.2" for c in store.chunks)


def test_new_top_level_section_does_not_pollute_preceding_test():
    store = PermitPdfStore.from_page_texts([
        (17, "2.4.1. 폴리소르베이트80함량시험\n함량은 0.01 ~ 0.05 % 이어야 한다."),
        (18, "별첨 문서 표지\n1. 정의\n다음 별첨의 정의\n2. 시험\n2.1. 최종원액\n2.1.1. 무균시험\n균이 없어야 한다."),
    ], policy=SKY_POLICY)
    test = next(c for c in store.chunks if c.title == "폴리소르베이트80함량시험")
    assert "별첨" not in test.text and "정의" not in test.text
    assert test.page_end == 17


def test_product_form_page_is_not_part_of_previous_test_or_parent():
    store = PermitPdfStore.from_page_texts([
        (21, "2.2. 항원바이알\n2.2.1. 입자크기측정시험\n입자 크기는 27 ~ 88 nm 이어야 한다."),
        (22, "문서확인번호 : DEMO\n성상\n투명한 액상\n포장단위\n10바이알/상자\n제품명\n제품A\n59/60"),
        (23, "문서확인번호 : DEMO\n제조원\n구분\n제조국\n제조원소재지\n수행공정\n전공정"),
    ], policy=SKY_POLICY)
    for chunk in store.chunks[:2]:
        assert chunk.page_end == 21
        assert "제품A" not in chunk.text and "전공정" not in chunk.text
    assert any("제품A" in chunk.text and chunk.page_start == 22 for chunk in store.chunks)
    assert any("전공정" in chunk.text and chunk.page_start == 23 for chunk in store.chunks)


def test_lone_appearance_label_does_not_discard_continued_acceptance():
    store = PermitPdfStore.from_page_texts([
        (1, "2.1. 시험항목\n시험의 첫 페이지"),
        (2, "성상\n투명하여야 한다. 제조원에서 정한 포장단위를 사용한다."),
    ], policy=SKY_POLICY)
    assert "투명하여야 한다" in store.chunks[0].text
    assert store.chunks[0].page_end == 2


def test_exact_test_and_specific_stage_excludes_other_stages_not_same_stage_conflicts():
    store = PermitPdfStore.from_page_texts([
        (1, "2.1. CHO 마스터 세포주\n2.1.1. 세포성장 및 증식확인시험\n마스터 기준"),
        (2, "2.2. CHO 제조용 세포주\n2.2.1. 세포성장 및 증식확인시험\n제조용 기준"),
        (3, "2.3. CHO 제조용 세포주\n2.3.1. 세포성장 및 증식확인시험\n충돌하는 제조용 기준"),
    ], policy=SKY_POLICY)
    record = ExtractedRecord(test_name="세포성장 및 증식확인시험", section_title="CHO 제조용 세포주에 대한 시험")
    matches = store.search(record)
    assert len(matches) == 2
    assert all("제조용" in chunk.text and "마스터" not in chunk.text for chunk in matches)
    # Without a specific stage, both stages must remain available; no blind top-1.
    record.section_title = "재료"
    assert len(store.search(record)) == 3


def test_generic_test_with_no_stage_path_is_not_a_direct_match() -> None:
    store = PermitPdfStore.from_page_texts(
        [(1, "2.3.1.7. 확인시험\n공통 확인 기준")],
        policy=SKY_POLICY,
        source_file="permit.pdf",
    )

    assert store.search(_component_b_record()) == []


@pytest.mark.parametrize("test_name", ["성상", "무균시험"])
def test_duplicate_generic_heading_without_stage_is_ambiguous(test_name: str) -> None:
    pages = [
        (index, f"2.{index}. 제조단계 {index}\n2.{index}.1. {test_name}\n{index}단계 판정기준")
        for index in range(1, 8)
    ]
    store = PermitPdfStore.from_page_texts(pages, policy=SKY_POLICY)
    record = ExtractedRecord(test_name=test_name, criteria="적합", result="적합")

    assert store.search(record) == []
    assert store.has_ambiguous_generic_test(record) is True
    assert store.has_ambiguous_generic_test(ExtractedRecord(test_name="존재하지 않는 시험")) is False


def test_specific_component_b_stage_beats_broad_stage_in_either_input_order() -> None:
    broad = (
        1,
        "2.1. Component B\n"
        "2.1.7. 확인시험\n"
        "넓은 Component B 확인 기준",
    )
    specific = (
        2,
        "2.2. Component B 중간체 원액\n"
        "2.2.7. 확인 시험\n"
        "중간체 원액 확인 기준",
    )
    record = ExtractedRecord(
        section_title="ComponentB 중간체원액",
        section_path=["Component B", "중간체 원액"],
        test_name="9.8.7. 확인시험",
    )

    for pages in ([broad, specific], [specific, broad]):
        chunks = PermitPdfStore.from_page_texts(pages, policy=SKY_POLICY).search(record)

        assert "중간체 원액" in chunks[0].section_path_titles[-2]


def test_no_policy_keeps_headingless_later_page_as_its_own_page_chunk() -> None:
    store = PermitPdfStore.from_page_texts(
        [
            (1, "2.1.7. 확인시험\n첫 페이지 기준"),
            (2, "둘째 페이지의 독립 본문"),
        ],
        source_file="permit.pdf",
    )

    assert [(chunk.title, chunk.page_start, chunk.page_end) for chunk in store.chunks] == [
        ("확인시험", 1, 1),
        ("허가서 본문", 2, 2),
    ]
    assert "둘째 페이지" not in store.chunks[0].text


def test_format_context_uses_page_number_fallback_and_real_span() -> None:
    legacy_chunk = PermitChunk("permit.pdf", 3, "2.1.7", "확인시험", "기준")
    spanning_chunk = PermitChunk(
        "permit.pdf", 1, "2.1.8", "엔도톡신시험", "기준", page_start=1, page_end=2
    )

    context = PermitPdfStore().format_context([legacy_chunk, spanning_chunk])

    assert "- 페이지: 3" in context
    assert "- 페이지: 1-2" in context
    assert "None" not in context


def test_from_page_texts_rejects_unsupported_source_tuple() -> None:
    with pytest.raises(ValueError, match="page_number, text"):
        PermitPdfStore.from_page_texts([(1, "2.1.7. 확인시험", "native")])


def test_missing_test_in_known_stage_does_not_borrow_another_stages_threshold():
    store = PermitPdfStore.from_page_texts([(1,
        "2.1. Component A 중간체 원액\n2.1.1. 단백질함량시험\n6,200 ㎍/mL 이상\n"
        "2.2. 나노파티클 원액\n2.2.1. pH측정시험\n7.6 ~ 8.2 이어야 한다.")], policy=SKY_POLICY)
    assert store.search(ExtractedRecord(section_title="나노파티클 원액에 대한 시험", test_name="단백질함량시험")) == []
    assert store.search(ExtractedRecord(section_title="Component A 중간체 원액에 대한 시험", test_name="단백질함량시험"))[0].title == "단백질함량시험"


def test_injected_extractor_receives_policy_mode_and_aggregates_errors(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    calls: list[tuple[str, str]] = []
    policy = PermitPolicy("sky", True, True, True, "native")

    def extractor(path: Path, ocr_mode: str) -> tuple[list[PermitPageText], list[str]]:
        calls.append((path.name, ocr_mode))
        if path == first:
            return [PermitPageText(1, "2.1.7. 확인시험\n기준", "native")], ["first warning"]
        raise RuntimeError("unavailable")

    store = PermitPdfStore([first, second], policy=policy, page_text_extractor=extractor)

    assert calls == [("first.pdf", "native"), ("second.pdf", "native")]
    assert store.enabled is True
    assert store.extraction_errors[0] == "first warning"
    assert "second.pdf" in store.extraction_errors[1]
    assert "unavailable" in store.extraction_errors[1]


def test_component_b_normalizes_mixed_language_spacing_and_number_variants() -> None:
    store = PermitPdfStore.from_page_texts(
        [
            (
                1,
                "2.3. Component B 중간체 원액\n"
                "2.3.1.7. 확인 시험\n"
                "Component B 확인 기준",
            )
        ],
        policy=SKY_POLICY,
    )
    record = ExtractedRecord(
        section_title="ComponentB 중간체원액",
        test_name="9.8.7. 확인시험",
    )

    chunks = store.search(record)

    assert chunks[0].title == "확인 시험"


@pytest.mark.parametrize("stage", ["시험", "기준", "test"])
def test_generic_test_does_not_use_generic_only_stage_overlap(stage: str) -> None:
    store = PermitPdfStore.from_page_texts(
        [(1, f"2.3. {stage}\n2.3.1.7. 확인시험\n공통 확인 기준")],
        policy=SKY_POLICY,
    )
    record = ExtractedRecord(section_title=stage, test_name="9.8.7. 확인시험")

    assert store.search(record) == []


@pytest.mark.parametrize(
    "page",
    [
        (None, "text"),
        (True, "text"),
        (0, "text"),
        (-1, "text"),
        ("1", "text"),
        (1, None),
        (1, 7),
    ],
)
def test_from_page_texts_rejects_invalid_two_tuple_metadata(page: tuple[object, object]) -> None:
    with pytest.raises(ValueError, match="positive non-bool int page number and str text"):
        PermitPdfStore.from_page_texts([page])
