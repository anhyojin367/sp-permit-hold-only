from dataclasses import replace

import pytest

from sp_pdf_judger.permit_catalog import PermitPolicy
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.permit_retrieval import RetrievalSelection
from sp_pdf_judger.permit_evidence_bundle import build_evidence_bundle, render_bundle_context
from sp_pdf_judger.permit_llm_protocol import parse_candidates


POLICY = PermitPolicy('test', True, True, True, 'native')
PAGES = [
    (1, '1. CHO 마스터 세포주\n공통 대조군은 적합해야 한다.\n'
        '1.1. 유정란접종시험\n검체는 각각 독립적으로 시험한다.\n'
        '1.1.1. 요막강 접종\n유정란(10~11일) 최소 10개를 사용해야 한다. '
        '36 ± 2 ℃에서 3일간 배양해야 한다. 생존률은 80% 이상이어야 한다.'),
    (2, '계대 후에도 3일 배양, 생존률 80% 이상이어야 한다. '
        '기니픽, 닭, 사람 O형 적혈구의 혈구응집반응이 없어야 한다.\n'
        '1.1.2. 난황낭 접종\n유정란(6~7일) 최소 10개를 사용해야 한다. '
        '36 ± 2 ℃에서 9일간 배양해야 한다. 1차 및 계대 생존률은 80% 이상이어야 한다.\n'
        '각주: 계대도 9일 배양한다.\n'
        '1.2. 다른시험\n이 시험에만 적용되는 99일 조건이어야 한다.'),
]


def source(pages=PAGES, name='permit.pdf'):
    return PermitPdfStore.from_page_texts(pages, source_file=name, policy=POLICY)


def bundle(store, title, *, complete=True):
    selected = tuple(c for c in store.chunks if c.title == title)
    return build_evidence_bundle(RetrievalSelection(selected, 'exact_stage_test', complete, 0, False),
                                 all_chunks=store.chunks, documents=store.source_documents,
                                 company='회사', product='제품')


def test_composite_egg_conditions_and_cross_page_footnote_are_not_cut():
    store = source()
    evidence = bundle(store, '유정란접종시험')
    assert evidence.complete, evidence.errors
    text = render_bundle_context(evidence)
    for clause in ['10~11일', '6~7일', '최소 10개', '3일', '9일', '80%', '혈구응집반응', 'O형', '각주:', '공통 대조군']:
        assert clause in text
    assert '99일' not in text
    assert {s.page_number for s in evidence.spans} == {1, 2}
    expected = {s.evidence_id for c in store.chunks if c.title != '다른시험' for s in c.owned_spans}
    assert expected == {s.evidence_id for s in evidence.spans}
    assert len(evidence.spans) == len(expected)
    assert parse_candidates(text) == list(evidence.candidates)
    for candidate in evidence.candidates:
        for clause_id in range(1, len(candidate.clauses) + 1):
            assert evidence.clause_span_ids[(candidate.candidate_id, clause_id)]


def test_child_inherits_parent_owned_common_text_but_not_sibling():
    evidence = bundle(source(), '요막강 접종')
    text = render_bundle_context(evidence)
    assert evidence.complete
    assert '공통 대조군' in text and '독립적으로' in text
    assert '6~7일' not in text and '99일' not in text


def test_same_basename_different_documents_do_not_crosslink():
    first = source()
    second = source([(1, '1. CHO 마스터 세포주\n다른 허가서 조건\n1.1. 다른시험\n50일이어야 한다.')])
    first.chunks.extend(second.chunks)
    first.source_documents.extend(second.source_documents)
    evidence = bundle(first, '유정란접종시험')
    assert evidence.complete
    assert '50일' not in render_bundle_context(evidence)
    assert len({s.document_id for s in evidence.spans}) == 1


@pytest.mark.parametrize('damage', ['broken', 'cycle', 'cross_document', 'tampered_span'])
def test_unverifiable_relationships_disable_batching(damage):
    store = source()
    idx = next(i for i,c in enumerate(store.chunks) if c.title == '요막강 접종')
    chunk = store.chunks[idx]
    if damage == 'broken':
        store.chunks[idx] = replace(chunk, parent_evidence_id='missing')
    elif damage == 'cycle':
        store.chunks[idx] = replace(chunk, parent_evidence_id=chunk.evidence_id)
    elif damage == 'cross_document':
        other = source([(1, '1. OTHER\n다른 문서 공통조건')], name='second.pdf')
        store.chunks.extend(other.chunks)
        store.source_documents.extend(other.source_documents)
        store.chunks[idx] = replace(chunk, parent_evidence_id=other.chunks[0].evidence_id)
    else:
        span = replace(chunk.owned_spans[-1], text='변조된 원문')
        store.chunks[idx] = replace(chunk, owned_spans=(*chunk.owned_spans[:-1], span))
    evidence = bundle(store, '요막강 접종')
    assert not evidence.complete and evidence.errors


@pytest.mark.parametrize('ref', ['1.2항', '1.2절', '1.2에 따른', '1.2에 따라', '1.2에 준하여', '1.2 참조'])
def test_unique_explicit_references_include_target(ref):
    store = source([(1, f'1. Component A 중간체원액\n1.1. 확인시험\n{ref} 수행한다.\n'
                        '1.2. 참조시험\n21일 이상이어야 한다.')])
    evidence = bundle(store, '확인시험')
    assert evidence.complete
    assert '21일' in render_bundle_context(evidence)


def test_unresolved_relative_reference_is_not_complete():
    store = source([(1, '1. CHO 마스터 세포주\n1.1. 확인시험\n위 항에 따라 시험한다.')])
    assert not bundle(store, '확인시험').complete


@pytest.mark.parametrize('tail', ['', '\n1.2. 참조시험\n21일이어야 한다.\n1.2. 참조시험\n28일이어야 한다.'])
def test_missing_or_ambiguous_reference_is_incomplete(tail):
    store = source([(1, '1. Component A 중간체원액\n1.1. 확인시험\n1.2항에 따른다.' + tail)])
    assert not bundle(store, '확인시험').complete


def test_query_fallback_cannot_claim_complete_evidence_and_mapping_is_read_only():
    evidence = bundle(source(), '유정란접종시험', complete=False)
    assert not evidence.complete
    with pytest.raises(TypeError):
        evidence.clause_span_ids[(1, 1)] = ()


def test_candidate_quotes_contain_only_literal_source_text():
    store = source()
    evidence = bundle(store, '유정란접종시험')
    assert all(candidate.text.startswith('1. CHO 마스터 세포주') for candidate in evidence.candidates)
