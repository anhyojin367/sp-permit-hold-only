"""Reading units are reversible source slices, never semantic verdicts."""
from copy import deepcopy
import json

import pytest

from scripts.permit_evidence_units import make_units
from scripts.permit_facet_review import make_lines, build_review, build_prompt, run_review, validate_response
from scripts.permit_obligation_draft import DraftSettings
from test_permit_facet_review import setup, reply
from test_permit_review_packet import STAGE


def item(text, start=17):
    return {'char_start': start, 'text': text}


@pytest.mark.parametrize('text', [
    ' \n용액을 희\n석한다. 다음에는 측정한다.\n',
    '\r\n혼합한 뒤,\r\n10/30\r\n',
    '1,453 및 4,259 bp, 0.25 mL 이상이어야 한다.',
    ' \n\n제품명\n\n문서번호: X-123\n',
    '😃 Unicode\r두줄\n마지막',
    '2.1.3. E. coli 확인시험',
])
def test_every_character_and_original_line_fragment_is_preserved(text):
    original = item(text)
    units, lines = make_units(original), make_lines(original)
    assert ''.join(u['text'] for u in units) == text
    assert units[0]['char_start'] == original['char_start']
    assert units[-1]['char_end'] == original['char_start'] + len(text)
    for left, right in zip(units, units[1:]): assert left['char_end'] == right['char_start']
    for unit in units:
        assert ''.join(f['text'] for f in unit['source_fragments']) == unit['text']
        for fragment in unit['source_fragments']:
            line = next(l for l in lines if l['line_id'] == fragment['line_id'])
            assert line['char_start'] <= fragment['char_start'] < fragment['char_end'] <= line['char_end']
            assert fragment['text'] == text[fragment['char_start']-17:fragment['char_end']-17]
        assert 'state' not in unit and 'is_incomplete' not in unit


def test_soft_wrapped_word_is_not_an_independent_evidence_unit():
    units = make_units(item('원심분리, 여과 및 처리한 뒤 1차 크로\n마토그래피, 바이러스 여과,\n12/60'))
    assert any('크로\n마토그래피,' in u['text'] for u in units)
    assert not any(u['text'].rstrip().endswith('크로') for u in units)
    assert any(u['text'].strip() == '바이러스 여과,' for u in units)
    assert any(u['text'].strip() == '12/60' for u in units)


@pytest.mark.parametrize('value', ['1,453', '4,259', '1,000,000', '0.25', '2.1.3.', 'E. coli'])
def test_numeric_or_abbreviation_punctuation_not_split(value):
    units = make_units(item(value+' 확인한다.'))
    assert any(value in u['text'] for u in units)


def test_blank_lines_are_retained_but_cannot_be_used_as_evidence(tmp_path):
    store, md, _ = setup(tmp_path)
    plan = build_review(store, STAGE, md, DraftSettings(), span_refs=['S0001'], facet_ids=['criterion'], evidence_units='clauses')
    assert plan['evidence_units'] == 'clauses'
    req = deepcopy(plan['requests'][0])
    req['items'][0]['lines'] = make_units(item('\n확인한다.'))
    blank = next(u for u in req['items'][0]['lines'] if not u['text'].strip())
    response = reply(req)
    response['items'][0].update(state='present', evidence_line_ids=[blank['line_id']])
    with pytest.raises(ValueError, match='빈 줄'):
        validate_response(req, response)


def test_mode_changes_prompt_ids_and_plan_but_not_source_or_md(tmp_path):
    store, md, lines = setup(tmp_path)
    clauses = build_review(store, STAGE, md, DraftSettings(max_batch_items=2), evidence_units='clauses')
    assert lines['base'] == clauses['base'] and lines['md_sha256'] == clauses['md_sha256']
    assert lines['evidence_units'] == 'lines' and clauses['evidence_units'] == 'clauses'
    assert lines['plan_sha256'] != clauses['plan_sha256']
    assert build_prompt(lines['requests'][0]) != build_prompt(clauses['requests'][0])
    assert all(u['line_id'].startswith('U') for r in clauses['requests'] for i in r['items'] for u in i['lines'])
    assert 'source_fragments' not in build_prompt(clauses['requests'][0])


def test_default_lines_prompt_not_rewritten(tmp_path):
    _, _, plan = setup(tmp_path)
    req = plan['requests'][0]
    from scripts.permit_facet_review import PROMPT
    payload = {'request_id':req['request_id'], 'facet_id':req['facet_id'], 'question':req['facet_description'],
        'items':[{'span_id':i['span_id'], 'path_titles':i['path_titles'],
                  'lines':[{'line_id':l['line_id'], 'text':l['text']} for l in i['lines']]} for i in req['items']]}
    assert build_prompt(req) == PROMPT+'\nINPUT_DATA:\n'+json.dumps(payload,ensure_ascii=False)


@pytest.mark.parametrize('tamper', ['mode','text','fragment','implementation'])
def test_unit_plan_tampering_blocks_provider(tmp_path, tamper):
    store, md, _ = setup(tmp_path)
    plan = build_review(store, STAGE, md, DraftSettings(), evidence_units='clauses')
    if tamper == 'mode': plan['evidence_units'] = 'lines'
    elif tamper == 'implementation': plan['evidence_units_implementation_sha256'] = 'forged'
    elif tamper == 'text': plan['requests'][0]['items'][0]['lines'][0]['text'] = 'forged'
    else: plan['requests'][0]['items'][0]['lines'][0]['source_fragments'][0]['char_start'] += 1
    result = run_review(plan, store, md, lambda *a: pytest.fail('stale API'))
    assert not result['contract_complete'] and not result['attempts']


def test_units_restore_exact_original_evidence_not_display_normalization(tmp_path):
    store, md, _ = setup(tmp_path)
    plan = build_review(store, STAGE, md, DraftSettings(), span_refs=['S0001'], facet_ids=['criterion'], evidence_units='clauses')
    req = plan['requests'][0]
    result = run_review(plan, store, md, lambda *a: reply(req,'present'))
    assert result['contract_complete'] and not result['semantic_correctness_verified']
    evidence = result['rows'][0]['evidence'][0]
    assert evidence in req['items'][0]['lines']
    assert evidence['source_fragments'] and not result['runtime_activation']


def test_unknown_mode_rejected(tmp_path):
    store, md, _ = setup(tmp_path)
    with pytest.raises(ValueError): build_review(store, STAGE, md, DraftSettings(), evidence_units='guess')


def test_unit_mode_checkpoints_replay_with_exact_original_fragments(tmp_path):
    from scripts.permit_facet_checkpoint import run_checkpointed
    store, md, _ = setup(tmp_path)
    plan = build_review(store, STAGE, md, DraftSettings(), span_refs=['S0001'], facet_ids=['criterion'], evidence_units='clauses')
    req = plan['requests'][0]
    first = run_checkpointed(plan, store, md, tmp_path/'job', lambda *a: reply(req, 'present'))
    second = run_checkpointed(plan, store, md, tmp_path/'job', lambda *a: pytest.fail('repeat API'), resume=True)
    assert first['contract_complete'] and second['contract_complete'] and first['rows'] == second['rows']
    assert second['attempts'][0]['checkpoint_reused']


def test_single_unit_may_end_in_comma_without_claiming_whole_span_is_incomplete():
    units = make_units(item('준비하고, 측정한다.'))
    assert units[0]['text'].endswith(',') and units[-1]['text'].endswith('한다.')
    assert all(set(u) == {'line_id','char_start','char_end','text','source_fragments'} for u in units)


def test_full_reading_context_precedes_address_candidates(tmp_path):
    import re
    store, md, _ = setup(tmp_path)
    plan = build_review(store, STAGE, md, DraftSettings(), evidence_units='clauses')
    req = plan['requests'][0]
    prompt = build_prompt(req)
    payload = json.loads(prompt.split('INPUT_DATA:\n',1)[1])
    for source, shown in zip(req['items'],payload['items']):
        assert shown['reading_text'] == re.sub(r'\s+',' ',''.join(u['text'] for u in source['lines'])).strip()
        assert list(shown).index('reading_text') < list(shown).index('lines')
