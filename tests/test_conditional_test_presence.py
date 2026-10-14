"""Opt-in conditional presence; never turns exemption into test-result approval."""
import pytest
from sp_pdf_judger.policy_engine import RuleBook, evaluate_policies, to_evaluations, policy_fingerprint

from scripts.prove_conditional_test_presence import make_fixture, execute, write_md


@pytest.fixture(scope='module')
def normal(tmp_path_factory):
    return make_fixture(tmp_path_factory.mktemp('conditional-normal'))


@pytest.mark.parametrize('size,round_,tests,concentration,states,status', [
    ('100 mL','2',('무균시험','함량시험'),'20 mg/mL',['PASS','PASS'],'PASS'),
    ('100 mL','2',('함량시험',),'20 mg/mL',['FAIL','PASS'],'FAIL'),
    ('100 mL','1',('함량시험',),'20 mg/mL',['N/A','PASS'],'PASS'),
    ('50 mL','2',(),'20 mg/mL',['N/A','N/A'],'PASS'),
    (None,'2',(),'20 mg/mL',['HOLD','HOLD'],'HOLD'),
    ('100 L','2',('무균시험','함량시험'),'20 mg/mL',['HOLD','HOLD'],'HOLD'),
    ('100 mL','2',('무균시험',),'5 mg/mL',['PASS','FAIL'],'FAIL'),
])
def test_real_pdf_presence_gate(tmp_path, size, round_, tests, concentration, states, status):
    data = make_fixture(tmp_path, size=size, round_=round_, tests=tests, concentration=concentration)
    finding, ctx, book = execute(data)
    assert finding.status == status, finding
    assert [c['status'] for c in finding.details['requirement_checks']] == states
    assert not finding.details.get('execution_error')
    assert finding.details['presence_only'] is True
    if status == 'PASS': assert '시험 결과의 합격 판정이 아닙니다' in finding.reason
    cards = to_evaluations([finding], book, 1)
    assert len(cards) == 1 and cards[0].record_type == 'structural_validation'
    assert all(c.record_type != 'test' for c in cards)


def test_source_identity_bridge_and_evidence(normal):
    finding, _, _ = execute(normal)
    assert finding.status == 'PASS'
    links = finding.details['source_identity_links']
    assert len(links) == 2 and len({r['source_id'] for r in links}) == 2
    assert all(r['source_id'] != r['source_node_id'] for r in links)
    assert {tuple(r['full_path']) for r in links} == {('제조방법','원액','무균시험'),('제조방법','원액','함량시험')}
    assert {e['source'] for e in finding.evidence} == {'sp','permit'}
    assert any('100 mL인 경우' in e['quote'] for e in finding.evidence)
    assert any('용기규격 100 mL' in e['quote'] for e in finding.evidence)


@pytest.mark.parametrize('company',['','다른회사'])
def test_company_mismatch_holds(normal, company):
    finding, _, _ = execute(normal, company=company)
    assert finding.status == 'HOLD'


def test_extracted_record_omission_is_hold_not_false_fail(normal):
    records = [r for r in normal[3] if r.test_name != '함량시험']
    finding, _, _ = execute(normal, records=records)
    assert finding.status == 'HOLD'
    assert [c['status'] for c in finding.details['requirement_checks']] == ['PASS','HOLD']


@pytest.mark.parametrize('mutation',['scope','stage','leaf','document','condition','batch_scope'])
def test_conflicting_condition_contract_rejected(tmp_path, mutation):
    data = make_fixture(tmp_path)
    root, config, rule, records = data
    if mutation == 'scope': rule['params']['permit_stage_path'] = ['제조방법']
    if mutation == 'stage': config['stages'][0]['sp_stage'] = '다른 원액'
    if mutation == 'leaf': config['requirements'].pop()
    if mutation == 'document': config['reviewed_document_id'] = '0' * 64
    if mutation == 'condition': config['conditions'].pop()
    if mutation == 'batch_scope': config['source_scopes'][0]['batch_source']['end'] = '1.2. 시험'
    write_md(root / 'rules/_conditions/conditions.md', {'applicability': config})
    write_md(root / 'rules/rules.md', {'rules':[rule]})
    try:
        finding, _, _ = execute(data)
    except ValueError:
        return  # Strict authoring rejection before execution is also safe.
    assert finding.status == 'HOLD'


def test_md_edit_invalidates_fingerprint_and_old_loaded_book(tmp_path):
    data = make_fixture(tmp_path, tests=())
    before, ctx, book = execute(data)
    assert before.status == 'FAIL'
    old = book.fingerprint
    root, config, _, _ = data
    config['conditions'][0]['operator'] = 'ne'  # Deliberate authoring mistake, not approval.
    write_md(root / 'rules/_conditions/conditions.md', {'applicability':config})
    assert policy_fingerprint(root / 'rules') != old
    assert evaluate_policies(ctx, book)[0].status == 'HOLD'
    fresh, _, _ = execute(data)
    assert [c['status'] for c in fresh.details['requirement_checks']] == ['N/A','N/A']


@pytest.mark.parametrize('path',['../conditions.md','C:/outside.md','_conditions/../rules.md','rules.md','_conditions/absent.md'])
def test_companion_md_must_be_inside_reserved_root(tmp_path, path):
    root, config, rule, records = make_fixture(tmp_path)
    rule['params']['applicability_md'] = path
    write_md(root / 'rules/rules.md', {'rules':[rule]})
    with pytest.raises(ValueError): RuleBook(root / 'rules')


def test_unknown_applicability_cannot_hide_a_separate_required_missing(tmp_path):
    finding, _, _ = execute(make_fixture(tmp_path, round_='미정', tests=()))
    assert finding.status == 'FAIL'
    assert [r['status'] for r in finding.details['requirement_checks']] == ['HOLD','FAIL']
    assert '추가 보류' in finding.reason


def test_duplicate_required_test_is_hold(normal):
    from copy import deepcopy
    records = deepcopy(normal[3])
    records.append(deepcopy(next(r for r in records if r.test_name == '무균시험')))
    finding, _, _ = execute(normal, records=records)
    assert finding.status == 'HOLD'


def test_missing_actual_result_is_not_test_presence_pass(normal):
    from copy import deepcopy
    records = deepcopy(normal[3])
    next(r for r in records if r.test_name == '무균시험').result = ''
    finding, _, _ = execute(normal, records=records)
    assert finding.status == 'HOLD'


@pytest.mark.parametrize('target',['md','sp','permit'])
def test_concurrent_source_change_never_returns_pass(tmp_path, monkeypatch, target):
    data = make_fixture(tmp_path)
    import sp_pdf_judger.policy_conditional_tests as module
    original = module.check_test_presence
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        path = data[0] / {'md':'rules/_conditions/conditions.md', 'sp':'sp.pdf', 'permit':'permit.pdf'}[target]
        path.write_bytes(path.read_bytes() + b'\n')
        return result
    monkeypatch.setattr(module, 'check_test_presence', changed)
    finding, _, _ = execute(data)
    assert finding.status == 'HOLD' and '변경' in finding.reason


def test_partial_cached_pages_cannot_hide_raw_test_mention(normal):
    finding, ctx, book = execute(normal)
    ctx.records = [r for r in ctx.records if r.test_name != '함량시험']
    ctx.pages = [(1,'incomplete caller pages')]
    finding = evaluate_policies(ctx, book)[0]
    assert finding.status == 'HOLD'
    assert [c['status'] for c in finding.details['requirement_checks']] == ['PASS','HOLD']


def test_empty_companion_alone_cannot_activate_a_rule(tmp_path):
    data = make_fixture(tmp_path)
    write_md(data[0] / 'rules/rules.md', {'rules':[]})
    with pytest.raises(ValueError, match='No executable'): RuleBook(data[0] / 'rules')


def test_result_from_another_field_is_not_accepted_as_presence(normal):
    from copy import deepcopy
    records = deepcopy(normal[3])
    # The criterion says '기록', but that is not the actual result ('적합').
    next(r for r in records if r.test_name == '무균시험').result = '기록'
    finding, _, _ = execute(normal, records=records)
    assert finding.status == 'HOLD'
    assert '실제 원문 값' in finding.reason


def test_md_swap_between_hash_and_load_cannot_grant_an_exemption(tmp_path, monkeypatch):
    data = make_fixture(tmp_path, tests=())
    root, config, _, _ = data
    before, ctx, book = execute(data)
    assert before.status == 'FAIL'
    import sp_pdf_judger.policy_conditional_tests as module
    path = root / 'rules/_conditions/conditions.md'
    original_bytes = path.read_bytes()
    original_hash, original_presence = module._hash, module.check_test_presence
    swapped = []
    def swap_after_read(candidate):
        value = original_hash(candidate)
        if candidate.resolve() == path.resolve() and not swapped:
            swapped.append(True)
            config['conditions'][0]['operator'] = 'ne'
            write_md(path, {'applicability':config})
        return value
    def restore_after_presence(*args, **kwargs):
        value = original_presence(*args, **kwargs)
        path.write_bytes(original_bytes)
        return value
    monkeypatch.setattr(module, '_hash', swap_after_read)
    monkeypatch.setattr(module, 'check_test_presence', restore_after_presence)
    finding = evaluate_policies(ctx, book)[0]
    # With a single validated load there is no early hash/read window to swap.
    # A FAIL from original MD or HOLD on a change is safe; N/A/PASS is not.
    assert finding.status in {'FAIL','HOLD'}, finding
