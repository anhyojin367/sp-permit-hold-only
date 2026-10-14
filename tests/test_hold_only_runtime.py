"""Portable runtime checks: real subprocesses, no server or provider calls."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import ast
from types import SimpleNamespace
from datetime import date, datetime

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _app_function(name, namespace):
    tree = ast.parse((ROOT / 'sp_app.py').read_text(encoding='utf-8-sig'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), 'sp_app.py', 'exec'), namespace)
    return namespace[name]


def test_gmail_defaults_never_consume_global_streamlit_secrets(tmp_path):
    # A global Gmail account must not auto-connect a new isolated app.
    function = _app_function('_local_gmail_defaults', {
        '__file__': str(tmp_path / 'sp_app.py'), 'Path': Path, 'date': date, 'datetime': datetime,
        'DEFAULT_SUBJECT_KEYWORD': 'test', 'DEFAULT_GMAIL_SINCE': date(2026, 1, 1),
        '_imap_since_term': lambda _: 'SINCE 1-Jan-2026',
        'st': SimpleNamespace(secrets={'gmail': {'address':'synthetic@example.test', 'app_password':'synthetic'}}),
    })
    result = function()
    assert result['address'] == ''
    assert result['app_password'] == ''


@pytest.mark.parametrize('change', ['scope', 'sp', 'permit'])
def test_saved_status_rejects_wrong_scope_or_changed_content_in_legacy_mode(tmp_path, monkeypatch, change):
    from sp_pdf_judger.review_presentation import file_sha256
    monkeypatch.setenv('SP_PERMIT_REVIEW_SCOPE', 'hold_only')
    monkeypatch.setenv('SP_PERMIT_SEMANTIC_MODE', 'legacy')
    sp = tmp_path / 'sp.pdf'
    permit = tmp_path / 'permit.pdf'
    sp.write_bytes(b'original-sp')
    permit.write_bytes(b'original-permit')
    record = {'cache_version':'v', 'rule_fingerprint':'r', 'extraction_fingerprint':'e',
              'llm_execution_fingerprint':'l', 'semantic_fingerprint':'', 'permit_fingerprint':'p',
              'detail_fingerprint':'d', 'permit_review_scope':'hold_only', 'sp_sha256':file_sha256(sp),
              'permit_file_hashes':{str(permit.resolve()): file_sha256(permit)}}
    function = _app_function('_status_record_for_doc', {
        'Path':Path, 'JUDGEMENT_STATUS_CACHE_VERSION':'v', '_pdf_status_key':lambda _: 'key',
        'policy_fingerprint':lambda:'r', 'extraction_fingerprint':lambda:'e',
        'llm_execution_fingerprint':lambda:'l', '_related_permit_files':lambda _:[permit],
        'resolve_submission_permits':lambda *a, **k:SimpleNamespace(fingerprint='p', paths=[permit]),
        'resolve_domain_detail_profile':lambda *a:SimpleNamespace(fingerprint='d'),
    })
    doc = SimpleNamespace(path=sp, company='test', product='test')
    assert function(doc, {'documents':{'key':record}}) == record
    if change == 'scope':
        record['permit_review_scope'] = 'all'
    elif change == 'sp':
        sp.write_bytes(b'changed--sp')
    else:
        permit.write_bytes(b'changed--permit')
    assert function(doc, {'documents':{'key':record}}) is None


def test_saved_status_accepts_hashes_of_resolved_subset(tmp_path, monkeypatch):
    from sp_pdf_judger.review_presentation import file_sha256
    monkeypatch.setenv('SP_PERMIT_REVIEW_SCOPE', 'hold_only')
    monkeypatch.setenv('SP_PERMIT_SEMANTIC_MODE', 'legacy')
    sp, selected, unrelated = [tmp_path / name for name in ('sp.pdf', 'selected.pdf', 'unrelated.pdf')]
    for path in (sp, selected, unrelated):
        path.write_bytes(path.name.encode())
    record = {'cache_version':'v', 'rule_fingerprint':'r', 'extraction_fingerprint':'e',
              'llm_execution_fingerprint':'l', 'semantic_fingerprint':'', 'permit_fingerprint':'p',
              'detail_fingerprint':'d', 'permit_review_scope':'hold_only', 'sp_sha256':file_sha256(sp),
              'permit_file_hashes':{str(selected.resolve()):file_sha256(selected)}}
    function = _app_function('_status_record_for_doc', {
        'Path':Path, 'JUDGEMENT_STATUS_CACHE_VERSION':'v', '_pdf_status_key':lambda _:'key',
        'policy_fingerprint':lambda:'r', 'extraction_fingerprint':lambda:'e',
        'llm_execution_fingerprint':lambda:'l', '_related_permit_files':lambda _:[selected, unrelated],
        'resolve_submission_permits':lambda *a, **k:SimpleNamespace(fingerprint='p', paths=[selected]),
        'resolve_domain_detail_profile':lambda *a:SimpleNamespace(fingerprint='d'),
    })
    doc = SimpleNamespace(path=sp, company='test', product='test')
    assert function(doc, {'documents':{'key':record}}) == record


def test_runtime_replaces_inherited_paths_and_scope(tmp_path):
    source = ROOT / 'sp_variant_runtime.py'
    if source.exists():
        shutil.copy2(source, tmp_path)
    env = dict(os.environ, SP_PERMIT_REVIEW_SCOPE='all', SP_PERMIT_SEMANTIC_MODE='shared_evidence')
    keys = ('TEMP', 'TMP', 'TMPDIR', 'SP_PDF_DIR', 'SP_PERMIT_INDEX_DIR', 'CLOVA_RESPONSE_CACHE_DIR')
    for key in keys:
        env[key] = str(tmp_path / 'wrong-inherited')
    code = '''import os, tempfile, json
tempfile.tempdir = 'wrong-cached-temp'
from sp_variant_runtime import bootstrap_runtime
root = bootstrap_runtime()
print(json.dumps({'root':str(root), 'cwd':os.getcwd(), 'temp':tempfile.gettempdir(),
 'scope':os.environ['SP_PERMIT_REVIEW_SCOPE'], 'mode':os.environ['SP_PERMIT_SEMANTIC_MODE'],
 'paths':{k:os.environ[k] for k in ('TEMP','TMP','TMPDIR','SP_PDF_DIR','SP_PERMIT_INDEX_DIR','CLOVA_RESPONSE_CACHE_DIR')}}))
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert Path(data['root']) == tmp_path
    assert Path(data['cwd']) == tmp_path
    assert Path(data['temp']) == tmp_path / '.runtime' / 'tmp'
    assert data['scope'] == 'hold_only'
    assert data['mode'] == 'legacy'
    for path in data['paths'].values():
        assert Path(path).is_relative_to(tmp_path)
        assert Path(path).is_dir()


def test_launcher_check_is_portable_and_does_not_start_server(tmp_path):
    for name in ('sp_variant_runtime.py', 'launch_hold_only.py'):
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, tmp_path)
    result = subprocess.run([sys.executable, str(tmp_path / 'launch_hold_only.py'), '--check'],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert Path(data['root']) == tmp_path
    assert data['scope'] == 'hold_only'
    assert data['command'][0] == sys.executable
    assert str(tmp_path / 'sp_app.py') in data['command']
    assert '--server.address=127.0.0.1' in data['command']
    assert '--server.port=8507' in data['command']


def test_credentials_do_not_discover_parent_dotenv(tmp_path):
    child = tmp_path / 'isolated'
    package = child / 'sp_pdf_judger'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('', encoding='utf-8')
    for name in ('config.py', 'llm.py'):
        shutil.copy2(ROOT / 'sp_pdf_judger' / name, package / name)
    (tmp_path / '.env').write_text('CLOVA_API_KEY=synthetic-parent\nCLOVA_MODEL=parent-model\n', encoding='utf-8')
    env = dict(os.environ, CLOVA_API_KEY='synthetic-explicit', CLOVA_MODEL='explicit-model')
    code = f'''import sp_pdf_judger
sp_pdf_judger.__path__.append({str(ROOT / 'sp_pdf_judger')!r})
from sp_pdf_judger.llm import resolve_clova_execution_settings
key, model, _ = resolve_clova_execution_settings()
assert key == 'synthetic-explicit', 'parent dotenv overrode process credential'
assert model == 'explicit-model', 'parent dotenv overrode process model'
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=child, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
