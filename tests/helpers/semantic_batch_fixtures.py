"""Offline-only helpers: fake only the provider, keep parsing/validation real."""
import json
import socket
from types import SimpleNamespace

def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('External network is forbidden in semantic batch tests')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket.socket, 'connect', denied)
    try:
        import httpx
    except ImportError:
        return  # Optional provider SDK is absent; socket guards still apply.
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', denied)


class FakeCompletions:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        content = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def fake_judge(monkeypatch, tmp_path, responses):
    from sp_pdf_judger.llm import ClovaJudgeClient
    from sp_pdf_judger import clova_transport
    deny_network(monkeypatch)
    monkeypatch.setenv('CLOVA_RESPONSE_CACHE_DIR', str(tmp_path / 'responses'))
    clock = [1.0]
    monkeypatch.setattr(clova_transport, '_GATES', {})
    monkeypatch.setattr(clova_transport, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    judge = ClovaJudgeClient(execution_settings=('', 'HCX-007', 'https://provider.invalid/v1'))
    provider = FakeCompletions(responses)
    judge.client = SimpleNamespace(chat=SimpleNamespace(completions=provider))
    judge.enabled = True
    return judge, provider


def semantic_request(position=0, *, result='음성', batch='BATCH-A', body='음성이어야 한다.', method='방법 A'):
    from sp_pdf_judger.permit_catalog import PermitPolicy
    from sp_pdf_judger.permit_pdf_store import PermitPdfStore
    from sp_pdf_judger.permit_retrieval import RetrievalSelection
    from sp_pdf_judger.permit_evidence_bundle import build_evidence_bundle
    from sp_pdf_judger.permit_semantic_batch import SemanticPermitRequest
    source = PermitPdfStore.from_page_texts([(1, '1. CHO 마스터 세포주\n1.1. 확인시험\n' + body)],
        source_file='permit.pdf', policy=PermitPolicy('test', True, True, True, 'native'))
    selection = RetrievalSelection((source.chunks[-1],), 'exact_stage_test', True, 0, False)
    bundle = build_evidence_bundle(selection, all_chunks=source.chunks, documents=source.source_documents,
                                   company='회사', product='제품')
    context = json.dumps({'section_title': 'CHO 마스터 세포주', 'test_name': '확인시험', 'method': method,
                          'result': result, 'batch_evidence': {'state': 'explicit' if batch else 'missing', 'batch': batch}}, ensure_ascii=False)
    return SemanticPermitRequest(f'r{position}', position, bundle, context, '음성', result, f'fp-{position}')


def semantic_decision(request, status='PASS'):
    return {'record_id': request.record_id, 'evidence_set_id': request.bundle.bundle_id,
            'decision': {'permit_match_status': 'matched', 'candidate_id': 1, 'reason': '허가서 결과 조건 비교',
                         'clauses': [{'clause_id': i + 1, 'kind': 'acceptance', 'status': status, 'reason': '조건 비교'}
                                     for i, _ in enumerate(request.bundle.candidates[0].clauses)]}}
