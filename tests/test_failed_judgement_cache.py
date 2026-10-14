"""Runtime API failures are reviewable history, not reusable completed reviews."""
from contextlib import nullcontext
import pickle
from types import SimpleNamespace

import pytest

import sp_judgement_bridge as bridge
from sp_pdf_judger.clova_transport import ClovaServiceError


def result(error="", calls=104, success=104):
    return SimpleNamespace(metadata={"llm_enabled": True, "llm_last_error": error,
        "llm_call_count": calls, "llm_success_count": success})


@pytest.mark.parametrize("stage", ["before_result", "after_result"])
@pytest.mark.parametrize("failure", ["error", "short_success"])
def test_runtime_error_or_incomplete_requests_make_attempt_failed(stage, failure):
    artifacts = {"before_result": result(), "after_result": result()}
    artifacts[stage] = result(error="Connection error." if failure == "error" else "",
                              success=0 if failure == "short_success" else 104)
    assert bridge._has_runtime_llm_failure(artifacts)


@pytest.mark.parametrize("metadata", [{}, {"llm_enabled": False, "llm_call_count": 0},
    {"llm_enabled": True, "llm_call_count": 104, "llm_success_count": 104, "llm_last_error": ""}])
def test_completed_request_can_still_have_genuine_holds(metadata):
    artifacts = {"after_result": SimpleNamespace(metadata=metadata, summary=SimpleNamespace(held=2))}
    assert not bridge._has_runtime_llm_failure(artifacts)


def test_explicit_retry_clears_only_failed_attempt_for_selected_document(monkeypatch,tmp_path):
    selected=tmp_path/'sp.pdf'; other=tmp_path/'other.pdf'
    failed={"pdf_path":selected,"after_result":result("Connection error.",202,0)}
    healthy={"pdf_path":selected,"after_result":result()}
    another={"pdf_path":other,"after_result":result("Connection error.",202,0)}
    state={"sp_direct_judgement_artifacts::bad":failed,
           "sp_direct_judgement_artifacts::ok":healthy,
           "sp_direct_judgement_artifacts::other":another,"unrelated":"keep"}
    monkeypatch.setattr(bridge,'st',SimpleNamespace(session_state=state))
    bridge.forget_failed_judgement_attempt(selected)
    assert 'sp_direct_judgement_artifacts::bad' not in state
    assert state['sp_direct_judgement_artifacts::ok'] is healthy
    assert state['sp_direct_judgement_artifacts::other'] is another
    assert state['unrelated']=='keep'


def test_failed_disk_cache_is_reviewed_without_an_implicit_retry(monkeypatch,tmp_path):
    pdf=tmp_path/'sp.pdf'; pdf.write_bytes(b'fixture')
    root=tmp_path/'sp_app_direct_judgement'/'key';root.mkdir(parents=True)
    cache=root/'judgement_artifacts.pkl'
    cache.write_bytes(pickle.dumps({'cache_version':bridge.JUDGEMENT_CACHE_VERSION,
        'after_result':result('Connection error.',202,0)}))
    original=cache.read_bytes()
    monkeypatch.setattr(bridge,'st',SimpleNamespace(session_state={},spinner=lambda *a: nullcontext()))
    monkeypatch.setattr(bridge.tempfile,'gettempdir',lambda:str(tmp_path))
    monkeypatch.setattr(bridge,'_artifact_key',lambda *a:'key')
    monkeypatch.setattr(bridge,'resolve_domain_detail_profile',lambda *a:SimpleNamespace(fingerprint='detail'))
    monkeypatch.setattr(bridge,'resolve_permits',lambda *a,**kw:SimpleNamespace(paths=[],fingerprint='permit',policy=None,errors=[]))
    monkeypatch.setattr(bridge,'_attach_rule_regression_log',lambda *a:None)
    monkeypatch.setattr(bridge,'_remember_judgement_artifacts',lambda *a:None)
    def pipeline(**kwargs): pytest.fail('Opening saved failure must not call the API')
    monkeypatch.setattr(bridge,'DocumentJudgePipeline',pipeline)
    saved = bridge.ensure_judgement_artifacts(pdf_path=pdf,permit_paths=[],original_csv_dir=None,
                                             _background=True)
    assert bridge._has_runtime_llm_failure(saved)
    assert cache.read_bytes()==original


@pytest.mark.parametrize('failed',[False,True])
def test_status_index_does_not_label_runtime_failure_completed(monkeypatch,tmp_path,failed):
    pdf=tmp_path/'sp.pdf';pdf.write_bytes(b'fixture')
    monkeypatch.setattr(bridge,'JUDGEMENT_STATUS_DIR',tmp_path/'status')
    monkeypatch.setattr(bridge,'_read_status_index',lambda:{'documents':{}})
    saved=[];monkeypatch.setattr(bridge,'_write_status_index',saved.append)
    bridge._remember_judgement_artifacts({'pdf_path':pdf,'artifact_key':'key',
        'summary_before_path':tmp_path/'absent_before.csv','summary_after_path':tmp_path/'absent_after.csv',
        'llm_execution_fingerprint':'llm-current',
        'after_result':result('Connection error.' if failed else '',202 if failed else 104,0 if failed else 104)})
    status=next(iter(saved[0]['documents'].values()))
    assert status['status']==('failed' if failed else 'completed')
    assert status['llm_execution_fingerprint']=='llm-current'


def test_service_failure_status_persists_execution_fingerprint(monkeypatch,tmp_path):
    pdf=tmp_path/'sp.pdf';pdf.write_bytes(b'fixture')
    root=tmp_path/'failure';root.mkdir()
    saved=[];fingerprint='f'*64
    monkeypatch.setattr(bridge,'llm_execution_fingerprint',lambda:fingerprint,raising=False)
    monkeypatch.setattr(bridge,'_store_status_entry',lambda path,entry:saved.append((path,entry)))

    bridge._remember_service_failure(
        root,'artifact-key',pdf,ClovaServiceError('timeout'),
        SimpleNamespace(fingerprint='permit'),SimpleNamespace(fingerprint='detail'))

    assert saved[0][0]==pdf
    assert saved[0][1]['llm_execution_fingerprint']==fingerprint


def test_reviewing_failed_current_attempt_does_not_automatically_resubmit(monkeypatch,tmp_path):
    pdf=tmp_path/'sp.pdf'
    failed={'pdf_path':pdf,'after_result':result('Connection error.',202,0)}
    monkeypatch.setattr(bridge,'st',SimpleNamespace(session_state={'sp_direct_judgement_artifacts::key':failed}))
    monkeypatch.setattr(bridge,'_artifact_key',lambda *a:'key')
    monkeypatch.setattr(bridge,'resolve_domain_detail_profile',lambda *a:SimpleNamespace(fingerprint='detail'))
    monkeypatch.setattr(bridge,'resolve_permits',lambda *a,**kw:SimpleNamespace(paths=[],fingerprint='permit',policy=None,errors=[]))
    monkeypatch.setattr(bridge,'_attach_rule_regression_log',lambda *a:None)
    monkeypatch.setattr(bridge,'_remember_judgement_artifacts',lambda *a:None)
    monkeypatch.setattr(bridge,'DocumentJudgePipeline',lambda **k:pytest.fail('Unrequested retry'))
    assert bridge.ensure_judgement_artifacts(pdf_path=pdf,permit_paths=[],original_csv_dir=None) is failed
