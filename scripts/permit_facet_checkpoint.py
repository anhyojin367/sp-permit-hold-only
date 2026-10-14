"""Durable, fail-closed request journal for the unapproved facet review tool."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re

from pydantic import BaseModel
from scripts.build_permit_review_packet import _digest
from scripts.permit_facet_review import _verify, run_review, validate_response


class CheckpointError(ValueError):
    pass


def write_new(path, value):
    """Exclusive creation; a partial write is retained and fails closed on resume."""
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise CheckpointError('체크포인트 파일 누락/손상: 자동 복구하지 않습니다.') from None


@contextmanager
def job_lock(directory):
    """The OS releases this lock on process exit; no stale-file deletion needed."""
    handle = (Path(directory)/'.lock').open('a+b')
    acquired = False
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise CheckpointError('같은 출력 폴더의 검토 작업이 실행 중입니다.') from None
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise CheckpointError('같은 출력 폴더의 검토 작업이 실행 중입니다.') from None
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _model_audit(attempt):
    # Do not copy arbitrary keys, headers, client objects, or exception messages.
    audit = {'provider_call_observed': attempt.get('provider_call_observed') is True}
    if isinstance(attempt.get('raw_model_output'), str):
        audit['raw_model_output'] = attempt['raw_model_output']
    return audit


def _history(directory, binding, req):
    """Validate every prior attempt, including failures, before any paid retry."""
    files = list(directory.iterdir())
    indices, starts, completed = set(), {}, {}
    for path in files:
        match = re.fullmatch(r'(\d{4,})\.(started|completed)\.json', path.name)
        if not match or not path.is_file():
            raise CheckpointError('예상하지 못한 체크포인트 파일')
        number, kind = int(match[1]), match[2]
        if path.name != f'{number:04}.{kind}.json':
            raise CheckpointError('잘못된 체크포인트 번호')
        data = read_json(path)
        if not isinstance(data, dict) or data.get('binding') != binding:
            raise CheckpointError('요청/계획 지문 불일치')
        digest = data.get('record_sha256')
        if digest != _digest({k: v for k, v in data.items() if k != 'record_sha256'}):
            raise CheckpointError('저장 응답 지문 불일치')
        indices.add(number)
        (starts if kind == 'started' else completed)[number] = data
    if indices != set(range(1, len(indices)+1)) or not set(completed) <= set(starts):
        raise CheckpointError('시작 기록 누락/비연속 체크포인트')
    for number, data in completed.items():
        if data.get('status') not in ('valid_response', 'failed'):
            raise CheckpointError('알 수 없는 완료 상태')
        if data['status'] == 'valid_response':
            try:
                validate_response(req, data['response'])
            except (KeyError, ValueError):
                raise CheckpointError('저장 응답 계약 오류') from None
    # A successful response is terminal for this request. Subsequent attempts
    # would indicate a conflicting/manual journal, not an intentional retry.
    for number in range(1, len(indices)):
        if completed.get(number, {}).get('status') == 'valid_response':
            raise CheckpointError('성공 뒤 추가 요청 기록')
    if not indices:
        return 0, None
    last = max(indices)
    return last, completed.get(last, {'status': 'incomplete'})


def _record(path, data):
    write_new(path, {**data, 'record_sha256': _digest(data)})


def run_checkpointed(plan, store, md_path, output_dir, request, *, resume=False,
                     retry_failed=False, plan_only=False):
    """Resume validated responses; never approve semantics or infer paid retry."""
    if retry_failed and (not resume or plan_only):
        raise CheckpointError('실패 재시도는 --live --resume에서만 가능합니다.')
    plan = deepcopy(plan)
    try:
        _verify(plan, store, md_path)
    except (KeyError, ValueError):
        raise CheckpointError('원문/MD/설정/계획 변경: 새 계획이 필요합니다.') from None
    out = Path(output_dir).resolve()
    if resume:
        if not out.is_dir():
            raise CheckpointError('재개할 출력 폴더가 없습니다.')
    else:
        try:
            out.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise CheckpointError('기존 출력은 덮어쓰지 않습니다. 명시적으로 재개하세요.') from None
    with job_lock(out):
        identity = {'schema': 'permit-facet-checkpoint-v1', 'plan_sha256': plan['plan_sha256'],
                    'checkpoint_implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        if resume:
            if read_json(out/'plan.json') != plan or read_json(out/'checkpoint.json') != identity:
                raise CheckpointError('기존 계획/체크포인트 구현과 다릅니다. 새 출력 폴더가 필요합니다.')
            if not (out/'requests').is_dir() or not (out/'runs').is_dir():
                raise CheckpointError('체크포인트 폴더 누락')
        else:
            write_new(out/'plan.json', plan)
            write_new(out/'checkpoint.json', identity)
            (out/'requests').mkdir()
            (out/'runs').mkdir()
        if plan_only:
            return None
        expected = {r['request_id']: r for r in plan['requests']}
        request_dirs = list((out/'requests').iterdir())
        if any(p.name not in expected or not p.is_dir() for p in request_dirs):
            raise CheckpointError('외부 요청 기록: 재개하지 않습니다.')
        saved_ids = {p.name for p in request_dirs}
        if saved_ids != set(list(expected)[:len(saved_ids)]):
            raise CheckpointError('중간 요청 폴더 누락: 자동 재호출하지 않습니다.')
        prior_runs = sorted((out/'runs').iterdir())
        for number, path in enumerate(prior_runs, 1):
            if path.name != f'run{number:06}.json':
                raise CheckpointError('실행 결과 목록 손상')
            previous = read_json(path)
            if not isinstance(previous, dict) or previous.get('plan_sha256') != plan['plan_sha256']:
                raise CheckpointError('다른 계획의 실행 결과')
            for attempt in previous.get('attempts', []):
                number = attempt.get('checkpoint_attempt')
                if number is None:
                    continue
                key = attempt.get('request_id')
                if type(number) is not int or number < 1 or key not in saved_ids:
                    raise CheckpointError('이전 실행의 요청 기록 누락: 자동 재호출하지 않습니다.')
                directory = out/'requests'/key
                if not (directory/f'{number:04}.started.json').is_file():
                    raise CheckpointError('이전 요청의 시작 영수증 누락: 자동 재호출하지 않습니다.')
                if attempt.get('accepted') and not (directory/f'{number:04}.completed.json').is_file():
                    raise CheckpointError('수락했던 응답의 완료 영수증 누락: 재시도로 우회하지 않습니다.')

        def journaled(prompt, schema, attempt):
            key = attempt['request_id']
            if not re.fullmatch(r'F\d{2}B\d{3}', key) or key not in expected:
                raise CheckpointError('유효하지 않은 요청 ID')
            req = expected[key]
            binding = {**identity, 'request_id': key,
                       'prompt_sha256': _digest(prompt), 'response_schema_sha256': _digest(schema.model_json_schema())}
            directory = out/'requests'/key
            directory.mkdir(exist_ok=True)
            try:
                number, previous = _history(directory, binding, req)
            except CheckpointError:
                attempt['checkpoint_blocked'] = 'corrupt'
                raise
            if previous and previous['status'] == 'valid_response':
                attempt.update(checkpoint_reused=True, checkpoint_attempt=number, provider_call_observed=False)
                return deepcopy(previous['response'])
            if previous and not retry_failed:
                attempt['checkpoint_blocked'] = previous['status']
                raise CheckpointError('실패/불명확 요청은 명시적 재시도 필요; 추가 비용 가능')
            if request is None:
                raise CheckpointError('새 요청에 필요한 CLOVA 호출자가 없습니다.')
            number += 1
            _record(directory/f'{number:04}.started.json', {'binding': binding, 'status': 'started'})
            attempt.update(checkpoint_reused=False, checkpoint_attempt=number)
            provider_audit = {'request_id': key}
            raw = None
            try:
                raw = request(prompt, schema, provider_audit)
                if isinstance(raw, BaseModel): raw = raw.model_dump()
                schema.model_validate(raw)
                validate_response(req, raw)
            except Exception as exc:
                safe = _model_audit(provider_audit)
                attempt.update(safe)
                _record(directory/f'{number:04}.completed.json', {'binding': binding, 'status': 'failed',
                    'provider_audit': safe, 'error_type': type(exc).__name__})
                raise CheckpointError('제공자/응답 계약 오류; 원본 모델 본문은 별도 기록') from None
            safe = _model_audit(provider_audit)
            attempt.update(safe)
            _record(directory/f'{number:04}.completed.json', {'binding': binding, 'status': 'valid_response',
                'provider_audit': safe, 'response': raw})
            return raw

        result = run_review(plan, store, md_path, journaled)
        write_new(out/'runs'/f'run{len(prior_runs)+1:06}.json', result)
        return result
