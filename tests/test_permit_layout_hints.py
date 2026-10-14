"""Layout observations are exact, optional, and never semantic approvals."""
from copy import deepcopy
import json

import pytest

from scripts.permit_layout_hints import layout_observations
from scripts.permit_facet_review import build_review,build_prompt,run_review
from scripts.permit_obligation_draft import DraftSettings
from scripts.permit_evidence_units import make_units
from test_permit_facet_review import setup,reply
from test_permit_review_packet import STAGE


def sample(text='혼합한 뒤,\n \n9/28 \n', quote='9/28 '):
    units=make_units({'text':text,'char_start':20})
    start=20+text.index(quote)
    item={'source_span_id':'span-one','source_node_id':'node-one','page_number':4,'lines':units}
    candidate={'page':4,'char_start':start,'char_end':start+len(quote),'quote':quote,
               'source_span_ids':['span-one'],'source_node_ids':['node-one'],'number':9,'declared_total':28}
    return item,candidate


def test_exact_candidate_to_unit_mapping_does_not_change_original():
    item,candidate=sample();before=deepcopy(item)
    hints=layout_observations(item,[candidate])
    assert item==before and len(hints)==1
    hint=hints[0]
    assert hint['kind']=='possible_page_label' and hint['review_status']=='unreviewed'
    assert hint['quote']==candidate['quote'] and hint['char_start']==candidate['char_start']
    assert hint['char_end']==candidate['char_end'] and hint['page_number']==4
    assert hint['evidence_line_ids']==[next(u['line_id'] for u in item['lines'] if '9/28' in u['text'])]
    assert 'is_page_number' not in hint and 'state' not in hint


def test_measurement_fraction_is_still_only_an_unreviewed_candidate():
    item,candidate=sample('측정 비율:\n9/28 \n전체 시료 중 검출 비율이다.')
    hints=layout_observations(item,[candidate])
    assert len(hints)==1 and hints[0]['review_status']=='unreviewed'
    assert hints[0]['kind']=='possible_page_label'
    assert ''.join(u['text'] for u in item['lines']).endswith('검출 비율이다.')


def test_other_span_same_text_and_page_is_not_attached():
    item,candidate=sample();candidate['source_span_ids']=['different-span']
    assert layout_observations(item,[candidate])==[]


@pytest.mark.parametrize('change', ['page','node','start','end','quote','number','total','bool_offset','duplicate'])
def test_inconsistent_candidate_ownership_or_quote_is_rejected(change):
    item,candidate=sample();candidates=[candidate]
    if change=='page':candidate['page']=5
    elif change=='node':candidate['source_node_ids']=['different-node']
    elif change=='start':candidate['char_start']-=1
    elif change=='end':candidate['char_end']+=1
    elif change=='quote':candidate['quote']='8/28 '
    elif change=='number':candidate['number']=8
    elif change=='total':candidate['declared_total']=29
    elif change=='bool_offset':candidate['char_start']=True
    else:candidates.append(deepcopy(candidate))
    with pytest.raises(ValueError):layout_observations(item,candidates)


def test_optional_hints_keep_source_md_ids_and_default_prompt(tmp_path):
    store,md,_=setup(tmp_path)
    plain=build_review(store,STAGE,md,DraftSettings(),evidence_units='clauses')
    hinted=build_review(store,STAGE,md,DraftSettings(),evidence_units='clauses',layout_hints=True)
    assert plain['base']==hinted['base'] and plain['md_sha256']==hinted['md_sha256']
    assert plain['plan_sha256']!=hinted['plan_sha256']
    assert plain['layout_hints'] is False and hinted['layout_hints'] is True
    count=0
    for old,new in zip(plain['requests'],hinted['requests']):
        assert 'layout_observations' not in build_prompt(old)
        payload=json.loads(build_prompt(new).split('INPUT_DATA:\n',1)[1])
        for before,after,display in zip(old['items'],new['items'],payload['items']):
            assert before=={k:v for k,v in after.items() if k!='layout_observations'}
            assert display['layout_observations']==after['layout_observations']
            count+=len(after['layout_observations'])
    assert count>0


@pytest.mark.parametrize('tamper',['toggle','quote','implementation'])
def test_hint_tampering_blocks_before_provider(tmp_path,tamper):
    store,md,_=setup(tmp_path)
    plan=build_review(store,STAGE,md,DraftSettings(),evidence_units='clauses',layout_hints=True)
    if tamper=='toggle':plan['layout_hints']=False
    elif tamper=='implementation':plan['layout_hints_implementation_sha256']='forged'
    else:
        hint=next(h for r in plan['requests'] for i in r['items'] for h in i['layout_observations'])
        hint['quote']='forged'
    result=run_review(plan,store,md,lambda *a:pytest.fail('changed source API'))
    assert not result['contract_complete'] and not result['attempts']


@pytest.mark.parametrize('invalid',[1,'true',None])
def test_hint_option_requires_boolean(tmp_path,invalid):
    store,md,_=setup(tmp_path)
    with pytest.raises(ValueError):build_review(store,STAGE,md,DraftSettings(),evidence_units='clauses',layout_hints=invalid)


def test_hints_require_explicit_unit_mode(tmp_path):
    store,md,_=setup(tmp_path)
    with pytest.raises(ValueError):build_review(store,STAGE,md,DraftSettings(),layout_hints=True)


def test_hint_enabled_checkpoint_replays_but_never_approves(tmp_path):
    from scripts.permit_facet_checkpoint import run_checkpointed
    store,md,_=setup(tmp_path)
    plan=build_review(store,STAGE,md,DraftSettings(),evidence_units='clauses',layout_hints=True)
    count=[]
    def provider(*args):
        req=plan['requests'][len(count)];count.append(True)
        return reply(req)
    one=run_checkpointed(plan,store,md,tmp_path/'job',provider)
    two=run_checkpointed(plan,store,md,tmp_path/'job',lambda *a:pytest.fail('repeat API'),resume=True)
    assert one['contract_complete'] and two['rows']==one['rows']
    assert not two['runtime_activation'] and not two['semantic_correctness_verified']
    assert all(a['checkpoint_reused'] for a in two['attempts'])


@pytest.mark.parametrize('field', ['number','declared_total','page'])
def test_numeric_candidate_fields_reject_equal_valued_floats(field):
    item,candidate=sample()
    candidate[field]=float(candidate[field])
    with pytest.raises(ValueError):layout_observations(item,[candidate])


def test_candidate_one_is_not_boolean_true():
    item,candidate=sample('1/28', '1/28')
    candidate['number']=True
    with pytest.raises(ValueError):layout_observations(item,[candidate])


@pytest.mark.parametrize('change',['hint_bool','hint_float','offset_float','page_bool'])
def test_equal_valued_type_changes_are_still_plan_changes(tmp_path,change):
    store,md,_=setup(tmp_path)
    plan=build_review(store,STAGE,md,DraftSettings(),evidence_units='clauses',layout_hints=True)
    if change.startswith('hint'):
        hint=next(h for r in plan['requests'] for i in r['items'] for h in i['layout_observations'] if h['observed_numerator']==1)
        hint['observed_numerator']=True if change=='hint_bool' else 1.0
    elif change=='offset_float':
        unit=plan['requests'][0]['items'][0]['lines'][0]
        unit['char_start']=float(unit['char_start'])
    else:
        item=next(i for r in plan['requests'] for i in r['items'] if i['page_number']==1)
        item['page_number']=True
    from scripts.permit_facet_review import _verify
    with pytest.raises(ValueError):_verify(plan,store,md)
