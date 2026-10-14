"""Blank layout remains in the source ledger, but is not a selectable quote."""
from copy import deepcopy
import json
import re

import pytest

from scripts.permit_evidence_units import make_units
from scripts.permit_facet_review import build_prompt, make_lines, validate_response


def request_for(text, mode='clauses'):
    raw = {'text':text, 'char_start':37}
    return {'request_id':'F01B001', 'facet_id':'unclear', 'facet_description':'문장 절단이 있는가?',
        'evidence_units':mode, 'items':[{'span_id':'S0001', 'source_span_id':'source-one',
        'source_node_id':'node-one', 'document_id':'doc-one', 'source_file':'fixture.pdf',
        'page_number':3, 'path_titles':['위치 안내'],
        'lines':make_units(raw) if mode == 'clauses' else make_lines(raw)}]}


def shown(req):
    return json.loads(build_prompt(req).split('INPUT_DATA:\n', 1)[1])['items'][0]


@pytest.mark.parametrize('text', [
    '\n용액을 혼합한 뒤,\n\n17/40\n',
    '\r\n\t \r\n시험한다.\r\n',
    '시험한다.\n\n',
    '\n\n',
    '\u3000\n차광하여 보관한다.\n\u00a0\n',
    '시험한다.\n\n2 / 9\n',
])
def test_only_nonblank_candidates_are_presented_without_mutating_source(text):
    req = request_for(text)
    before = deepcopy(req)
    view = shown(req)
    expected = [{'line_id':u['line_id'], 'text':re.sub(r'\s+', ' ', u['text']).strip()}
                for u in req['items'][0]['lines'] if u['text'].strip()]
    assert view['lines'] == expected
    assert req == before
    assert ''.join(u['text'] for u in req['items'][0]['lines']) == text
    assert view['reading_text'] == re.sub(r'\s+', ' ', text).strip()


def test_gaps_in_ids_are_not_renumbered():
    req = request_for('\n\n용액을 혼합한 뒤,\n\n17/40\n')
    source = req['items'][0]['lines']
    view = shown(req)
    assert view['lines'][0]['line_id'] != 'U001'
    assert [u['line_id'] for u in view['lines']] == [u['line_id'] for u in source if u['text'].strip()]
    assert any(u['text'] == '17/40' for u in view['lines'])


@pytest.mark.parametrize('state', ['present', 'unclear'])
def test_hidden_blank_still_rejected_if_model_invents_its_id(state):
    req = request_for('\n\n용액을 혼합한 뒤,\n\n17/40\n')
    blank = next(u for u in req['items'][0]['lines'] if not u['text'].strip())
    response = {'request_id':req['request_id'], 'facet_id':req['facet_id'], 'items':[
        {'span_id':'S0001', 'state':state, 'evidence_line_ids':[blank['line_id']], 'note':'모델의 잘못된 선택'}]}
    with pytest.raises(ValueError, match='빈 줄'):
        validate_response(req, response)


def test_restored_quote_uses_original_offsets_not_candidate_position():
    text = '\n\n용액을 혼합한 뒤,\n\n17/40\n'
    req = request_for(text)
    chosen = next(u for u in shown(req)['lines'] if '혼합' in u['text'])
    response = {'request_id':req['request_id'], 'facet_id':req['facet_id'], 'items':[
        {'span_id':'S0001', 'state':'present', 'evidence_line_ids':[chosen['line_id']], 'note':'미완성 본문'}]}
    evidence = validate_response(req, response)[0]['evidence'][0]
    assert evidence in req['items'][0]['lines']
    assert text[evidence['char_start']-37:evidence['char_end']-37] == evidence['text']
    assert ''.join(f['text'] for f in evidence['source_fragments']) == evidence['text']


def test_default_lines_mode_keeps_blank_presentation_unchanged():
    req = request_for('\n\n용액을 혼합한 뒤,\n\n17/40\n', 'lines')
    assert shown(req)['lines'] == [{'line_id':u['line_id'], 'text':u['text']} for u in req['items'][0]['lines']]
    assert 'reading_text' not in shown(req)


def test_nonblank_page_label_is_candidate_not_automatically_a_semantic_result():
    req = request_for('\n\n17/40\n')
    assert [u['text'] for u in shown(req)['lines']] == ['17/40']
    assert all(set(u) == {'line_id', 'text'} for u in shown(req)['lines'])
