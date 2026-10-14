"""Paid requests must not be repeated implicitly after failure or interruption."""
from copy import deepcopy
import json

import pytest

from scripts.permit_facet_checkpoint import CheckpointError, run_checkpointed, job_lock
from test_permit_facet_review import setup, reply, MD


def caller(plan, calls, *, crash_at=None, invalid_at=None):
    by_id = {r['request_id']: r for r in plan['requests']}
    def request(prompt, schema, attempt):
        key = attempt['request_id']
        calls.append(key)
        if key == crash_at:
            raise KeyboardInterrupt()
        raw = reply(by_id[key])
        if key == invalid_at:
            raw['items'][0]['state'] = 'present'
        attempt['provider_call_observed'] = True
        attempt['raw_model_output'] = json.dumps(raw)
        return raw
    return request


def test_plan_only_never_calls_provider_then_live_resume(tmp_path):
    store, md, plan = setup(tmp_path)
    out, calls = tmp_path/'job', []
    assert run_checkpointed(plan, store, md, out, None, plan_only=True) is None
    result = run_checkpointed(plan, store, md, out, caller(plan, calls), resume=True)
    assert result['contract_complete'] and len(calls) == len(plan['requests'])
    assert not result['runtime_activation'] and not result['semantic_correctness_verified']


def test_complete_resume_replays_without_any_provider_call(tmp_path):
    store, md, plan = setup(tmp_path)
    out, calls = tmp_path/'job', []
    first = run_checkpointed(plan, store, md, out, caller(plan, calls))
    initial_files = {str(p.relative_to(out)): p.read_bytes() for p in out.rglob('*.json')}
    again = run_checkpointed(plan, store, md, out, lambda *a: pytest.fail('duplicate API'), resume=True)
    assert first['rows'] == again['rows'] and again['contract_complete']
    assert all(a['checkpoint_reused'] and not a['provider_call_observed'] for a in again['attempts'])
    assert all((out/name).read_bytes() == data for name, data in initial_files.items())
    assert len(list((out/'runs').glob('*.json'))) == 2


def test_existing_directory_requires_explicit_resume(tmp_path):
    store, md, plan = setup(tmp_path)
    out = tmp_path/'job'; out.mkdir()
    with pytest.raises(CheckpointError):
        run_checkpointed(plan, store, md, out, lambda *a: pytest.fail('API'))


def test_resume_requires_existing_valid_plan(tmp_path):
    store, md, plan = setup(tmp_path)
    with pytest.raises(CheckpointError):
        run_checkpointed(plan, store, md, tmp_path/'missing', None, resume=True)


def test_failed_response_retained_and_not_implicitly_retried(tmp_path):
    store, md, plan = setup(tmp_path)
    out, calls = tmp_path/'job', []
    failed = plan['requests'][1]['request_id']
    first = run_checkpointed(plan, store, md, out, caller(plan, calls, invalid_at=failed))
    assert not first['contract_complete'] and len(calls) == 2
    old_files = {p: p.read_bytes() for p in out.rglob('*.json')}
    again = run_checkpointed(plan, store, md, out, lambda *a: pytest.fail('implicit retry'), resume=True)
    assert not again['contract_complete']
    assert again['attempts'][-1]['checkpoint_blocked'] == 'failed'
    assert all(p.read_bytes() == data for p, data in old_files.items())
    retry_calls = []
    final = run_checkpointed(plan, store, md, out, caller(plan, retry_calls), resume=True, retry_failed=True)
    assert final['contract_complete']
    assert plan['requests'][0]['request_id'] not in retry_calls
    assert retry_calls[0] == failed


def test_interruption_retains_started_receipt_and_requires_explicit_retry(tmp_path):
    store, md, plan = setup(tmp_path)
    out, calls = tmp_path/'job', []
    crash = plan['requests'][1]['request_id']
    with pytest.raises(KeyboardInterrupt):
        run_checkpointed(plan, store, md, out, caller(plan, calls, crash_at=crash))
    again = run_checkpointed(plan, store, md, out, lambda *a: pytest.fail('uncertain API retry'), resume=True)
    assert not again['contract_complete']
    assert again['attempts'][-1]['checkpoint_blocked'] == 'incomplete'
    assert len(list((out/'requests'/crash).glob('*.started.json'))) == 1
    final = run_checkpointed(plan, store, md, out, caller(plan, []), resume=True, retry_failed=True)
    assert final['contract_complete']
    assert len(list((out/'requests'/crash).glob('*.started.json'))) == 2


def test_provider_error_does_not_persist_auth_or_exception_details(tmp_path):
    store, md, plan = setup(tmp_path)
    out = tmp_path/'job'
    def failed(prompt, schema, attempt):
        attempt['raw_model_output'] = '{broken json'
        attempt['api_key'] = 'SECRET_HEADER'
        raise RuntimeError('SECRET_HEADER')
    result = run_checkpointed(plan, store, md, out, failed)
    assert not result['contract_complete']
    saved = ''.join(p.read_text(encoding='utf-8') for p in out.rglob('*.json'))
    assert 'SECRET_HEADER' not in saved and '{broken json' in saved


@pytest.mark.parametrize('changed', ['md', 'source', 'plan'])
def test_changed_source_or_plan_blocks_resume_calls(tmp_path, changed):
    store, md, plan = setup(tmp_path)
    out = tmp_path/'job'
    run_checkpointed(plan, store, md, out, None, plan_only=True)
    if changed == 'md': md.write_text(MD+'변경', encoding='utf-8')
    elif changed == 'source': store.chunks.pop()
    else: plan['span_refs'].pop()
    with pytest.raises(CheckpointError):
        run_checkpointed(plan, store, md, out, lambda *a: pytest.fail('stale request'), resume=True)


@pytest.mark.parametrize('corrupt', ['truncated', 'changed_response', 'changed_binding', 'missing_start'])
def test_corrupt_journal_not_silently_repaired_or_paid_again(tmp_path, corrupt):
    store, md, plan = setup(tmp_path)
    out = tmp_path/'job'
    run_checkpointed(plan, store, md, out, caller(plan, []))
    done = sorted(out.glob('requests/*/*.completed.json'))[0]
    if corrupt == 'truncated': done.write_text('{', encoding='utf-8')
    elif corrupt == 'missing_start': done.with_name(done.name.replace('completed', 'started')).unlink()
    else:
        data = json.loads(done.read_text(encoding='utf-8'))
        if corrupt == 'changed_response': data['response']['items'][0]['note'] = 'forged'
        else: data['binding']['plan_sha256'] = 'forged'
        done.write_text(json.dumps(data), encoding='utf-8')
    if corrupt == 'missing_start':
        with pytest.raises(CheckpointError):
            run_checkpointed(plan, store, md, out, lambda *a: pytest.fail('lost start retry'), resume=True, retry_failed=True)
        return
    result = run_checkpointed(plan, store, md, out, lambda *a: pytest.fail('corrupt paid retry'), resume=True, retry_failed=True)
    assert not result['contract_complete'] and result['attempts'][-1]['checkpoint_blocked'] == 'corrupt'


def test_lock_prevents_second_worker_and_releases_on_exception(tmp_path):
    out = tmp_path/'job'; out.mkdir()
    with pytest.raises(RuntimeError):
        with job_lock(out):
            with pytest.raises(CheckpointError):
                with job_lock(out): pytest.fail('second worker admitted')
            raise RuntimeError('simulated exit')
    with job_lock(out): pass


def test_response_source_change_remains_unaccepted_even_if_journal_valid(tmp_path):
    store, md, plan = setup(tmp_path)
    out = tmp_path/'job'
    def request(prompt, schema, attempt):
        md.write_text(MD+'changed', encoding='utf-8')
        return reply(plan['requests'][0])
    result = run_checkpointed(plan, store, md, out, request)
    assert not result['contract_complete'] and not result['rows']
    assert not result['attempts'][0]['accepted']
    assert result['errors'][0]['phase'] == 'source_validation'


def test_provider_cannot_mutate_pinned_request_or_journal_plan(tmp_path):
    store, md, plan = setup(tmp_path)
    requests = {r['request_id']: r for r in deepcopy(plan)['requests']}
    def request(prompt, schema, attempt):
        plan['requests'].clear()
        return reply(requests[attempt['request_id']])
    result = run_checkpointed(plan, store, md, tmp_path/'job', request)
    assert result['contract_complete'] and len(result['attempts']) == len(requests)


def test_retry_flag_without_resume_is_rejected_before_api(tmp_path):
    store, md, plan = setup(tmp_path)
    with pytest.raises(CheckpointError):
        run_checkpointed(plan, store, md, tmp_path/'job', None, retry_failed=True)


def test_separate_process_cannot_acquire_busy_job_lock(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    out = tmp_path/'job'; out.mkdir()
    code = '''
import sys
from scripts.permit_facet_checkpoint import job_lock, CheckpointError
try:
    with job_lock(sys.argv[1]): pass
except CheckpointError:
    raise SystemExit(23)
'''
    with job_lock(out):
        result = subprocess.run([sys.executable, '-c', code, str(out)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
    assert result.returncode == 23
    released = subprocess.run([sys.executable, '-c', code, str(out)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
    assert released.returncode == 0


@pytest.mark.parametrize('missing', ['first', 'last', 'empty_directory', 'accepted_completion'])
def test_missing_request_directory_not_retreated_as_unstarted(tmp_path, missing):
    store, md, plan = setup(tmp_path)
    out = tmp_path/'job'
    run_checkpointed(plan, store, md, out, caller(plan, []))
    key = plan['requests'][0 if missing == 'first' else -1]['request_id']
    directory = out/'requests'/key
    for path in directory.iterdir():
        if missing != 'accepted_completion' or path.name.endswith('.completed.json'): path.unlink()
    if missing in ('first', 'last'): directory.rmdir()
    with pytest.raises(CheckpointError):
        run_checkpointed(plan, store, md, out, lambda *a: pytest.fail('lost request replay'), resume=True)
