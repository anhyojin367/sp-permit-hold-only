from dataclasses import replace

from sp_pdf_judger.permit_catalog import PermitPolicy
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.schemas import ExtractedRecord


POLICY = PermitPolicy('test', True, True, True, 'native')


def source(policy=POLICY):
    return PermitPdfStore.from_page_texts([
        (1, '1. Component A 중간체원액\n1.1. 확인시험\n방법 A: 음성이어야 한다.'),
        (2, '2. Component B 중간체원액\n2.1. 확인시험\n방법 B: 양성이어야 한다.'),
    ], policy=policy)


def record(**kwargs):
    return ExtractedRecord(section_title='Component B 중간체원액', test_name='확인시험', **kwargs)


def test_exact_stage_excludes_other_stage_and_caches_query():
    store = source()
    first = store.search_precise(record(result='양성'))
    assert first.complete and first.route == 'exact_stage_test'
    assert [c.page_number for c in first.chunks] == [2]
    assert first.scored_count == 0
    assert not first.cache_hit
    assert store.search_precise(record(result='양성')).cache_hit
    assert not store.search_precise(record(result='음성')).cache_hit


def test_all_exact_conflicting_candidates_remain_even_beyond_top_k():
    store = source()
    template = store.chunks[-1]
    store.chunks.extend(replace(template, evidence_id=f'conflict-{i}', text=f'방법 {i}: 20일') for i in range(12))
    selection = store.search_precise(record(method='방법 B'))
    assert len(selection.chunks) == 13
    assert selection.complete


def test_alias_changes_rebuild_lookup_without_modifying_source():
    store = source()
    query = replace(record(), test_name='동일성시험')
    assert store.search_precise(query).route == 'legacy_fallback'
    store.configure_search_aliases([['확인시험', '동일성시험']])
    exact = store.search_precise(query)
    assert exact.complete and exact.chunks[0].title == '확인시험'
    assert not exact.cache_hit
    store.configure_search_aliases([])
    assert not store.search_precise(query).complete


def test_no_policy_and_unknown_stage_or_test_use_unchanged_legacy_search():
    for store, query in [(source(None), record()), (source(), replace(record(), section_title=None)),
                         (source(), replace(record(), test_name='새로운 시험'))]:
        expected = store.search(query, top_k=12)
        selection = store.search_precise(query)
        assert list(selection.chunks) == expected
        assert selection.route == 'legacy_fallback' and not selection.complete


def test_replacing_chunks_and_policy_invalidates_query_cache():
    store = source()
    store.search_precise(record())
    store.chunks = store.chunks[:2]
    assert not store.search_precise(record()).cache_hit
    assert not store.search_precise(record()).complete
    store.policy = None
    assert not store.search_precise(record()).cache_hit


def test_all_legacy_query_fields_participate_in_cache_key():
    store = source()
    baseline = record()
    store.search_precise(baseline)
    for field, value in [('criteria', '다른 기준'), ('method', '다른 방법'), ('raw_text', '다른 문맥'),
                         ('section_number', '9.8.7'), ('section_path', [{'title': '새 경로'}])]:
        assert not store.search_precise(replace(baseline, **{field: value})).cache_hit
