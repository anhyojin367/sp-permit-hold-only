from pathlib import Path

import pytest

from scripts import review_permit_facets as cli
from scripts.permit_facet_review import DraftSettings
from test_permit_facet_review import setup, reply


def inputs(tmp_path, monkeypatch):
    store, md, plan = setup(tmp_path)
    permit = tmp_path/'test.pdf'
    permit.write_bytes(b'test-only; store is mocked')
    monkeypatch.setattr(cli, '_load_inputs', lambda a: (store, DraftSettings(), plan))
    args = ['--permit', str(permit), '--stage-title', '시험', '--md', str(md), '--output-dir', str(tmp_path/'job')]
    return args, plan


def test_cli_default_is_api_zero_and_valid_resume_is_api_zero(tmp_path, monkeypatch):
    args, plan = inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, '_provider', lambda s: pytest.fail('dry-run initialized client'))
    assert cli.main(args) == 0
    requests = {r['request_id']: r for r in plan['requests']}
    calls = []
    def call(prompt, schema, attempt):
        calls.append(attempt['request_id'])
        return reply(requests[attempt['request_id']])
    monkeypatch.setattr(cli, '_provider', lambda s: call)
    assert cli.main(args+['--live', '--resume']) == 0
    assert len(calls) == len(plan['requests'])
    monkeypatch.setattr(cli, '_provider', lambda s: pytest.fail('replay initialized client'))
    assert cli.main(args+['--live', '--resume']) == 0


def test_cli_failure_requires_retry_flag(tmp_path, monkeypatch):
    args, plan = inputs(tmp_path, monkeypatch)
    def bad(*a): raise RuntimeError('SENSITIVE_PROVIDER_DETAIL')
    monkeypatch.setattr(cli, '_provider', lambda s: bad)
    assert cli.main(args+['--live']) == 2
    monkeypatch.setattr(cli, '_provider', lambda s: pytest.fail('unapproved failure retry'))
    assert cli.main(args+['--live', '--resume']) == 2


@pytest.mark.parametrize('flags', [['--retry-failed'], ['--live', '--retry-failed'], ['--resume', '--retry-failed']])
def test_cli_rejects_invalid_retry_flags(tmp_path, monkeypatch, flags):
    args, _ = inputs(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(args+flags)
    assert exc.value.code == 2


def test_cli_existing_output_never_overwritten(tmp_path, monkeypatch):
    args, _ = inputs(tmp_path, monkeypatch)
    assert cli.main(args) == 0
    plan = tmp_path/'job/plan.json'; before = plan.read_bytes()
    assert cli.main(args) == 2
    assert plan.read_bytes() == before


def test_cli_masks_unexpected_private_error(tmp_path, monkeypatch, capsys):
    args, _ = inputs(tmp_path, monkeypatch)
    def fail(a): raise RuntimeError('SECRET')
    monkeypatch.setattr(cli, '_load_inputs', fail)
    assert cli.main(args) == 2
    assert 'SECRET' not in capsys.readouterr().err
