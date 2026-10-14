"""Independent semantic decisions sharing complete, identical permit evidence."""
from dataclasses import dataclass
import json
from collections import OrderedDict
from pydantic import BaseModel, ConfigDict, ValidationError
from .permit_evidence_bundle import EvidenceBundle
from .permit_llm_protocol import GroundedPermitResponse, PERMIT_SEMANTIC_INSTRUCTIONS
from .permit_semantic_settings import SEMANTIC_BATCH_VERSION


@dataclass(frozen=True)
class SemanticPermitRequest:
    record_id: str
    position: int
    bundle: EvidenceBundle
    record_context: str
    criteria: str
    result: str
    request_fingerprint: str


@dataclass(frozen=True)
class PreparedAuthoritativeResponse:
    request_fingerprint: str
    record_id: str
    response: object
    request: SemanticPermitRequest


class SemanticBatchItem(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    record_id: str
    evidence_set_id: str
    decision: GroundedPermitResponse


class SemanticBatchResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    items: list[SemanticBatchItem]


class SemanticBatchWireResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    items: list[dict[str, object]]


class SemanticProtocolError(ValueError):
    def __init__(self, *, framing=False, item_errors=None, response=None):
        self.framing = framing
        self.item_errors = dict(item_errors or {})
        self.response = response
        super().__init__('semantic_envelope_invalid' if framing else 'semantic_items_invalid')


@dataclass(frozen=True)
class SemanticPartition:
    batches: tuple[tuple[SemanticPermitRequest, ...], ...]
    singles: tuple[SemanticPermitRequest, ...]
    oversized_ids: tuple[str, ...]


def build_semantic_batch_prompt(requests):
    evidence_sets = {}
    records = []
    for request in requests:
        evidence_sets.setdefault(request.bundle.bundle_id, {
            'evidence_set_id': request.bundle.bundle_id,
            'candidates': [{'candidate_id': c.candidate_id, 'title': c.title, 'stage': c.stage,
                            'clauses': [{'clause_id': i + 1, 'text': clause} for i, clause in enumerate(c.clauses)]}
                           for c in request.bundle.candidates]})
        records.append({'record_id': request.record_id, 'evidence_set_id': request.bundle.bundle_id,
                        'record_context': request.record_context, 'criteria': request.criteria,
                        'result': request.result, 'request_fingerprint': request.request_fingerprint})
    instructions = (PERMIT_SEMANTIC_INSTRUCTIONS + '\n위 판단 규칙을 각 record에 독립적으로 적용한다. '
        '다른 record의 시험결과, 배치, 기간, 대조군을 빌리거나 판정을 복사하지 않는다. '
        '동일 evidence_set_id만 허가 원문을 공유한다. candidate_id와 clause_id는 그 집합 안에서만 유효하다. '
        '출력은 items 배열을 가진 JSON 객체이다. 각 항목은 record_id, evidence_set_id, decision을 가진다. '
        'decision에는 위의 permit_match_status, candidate_id, reason, clauses를 포함한다. '
        '모든 요청 record_id를 정확히 한 번씩 반환한다.\n')
    return instructions + '\nPAYLOAD\n' + json.dumps({'protocol': SEMANTIC_BATCH_VERSION,
        'evidence_sets': list(evidence_sets.values()), 'records': records}, ensure_ascii=False, separators=(',', ':'))


def request_size_estimates(requests):
    prompt = build_semantic_batch_prompt(requests)
    schema = json.dumps(SemanticBatchResponse.model_json_schema(), ensure_ascii=False)
    return {'prompt_chars': len(prompt), 'input_estimate': len((prompt + schema).encode('utf-8')) + 512,
            'output_estimate': 256 + sum(256 + 128 * max((len(c.clauses) for c in r.bundle.candidates), default=0) for r in requests),
            'estimate_kind': 'utf8-byte-estimate'}


def _sharing_key(request):
    if not request.bundle.complete:
        return None
    try:
        context = json.loads(request.record_context)
        batch = context.get('batch_evidence', {})
        if not isinstance(batch, dict) or batch.get('state') != 'explicit' or not batch.get('batch'):
            return None
        return (request.bundle.bundle_id, context.get('section_title'), context.get('test_name'), context.get('method'))
    except (ValueError, AttributeError, TypeError):
        return None


def partition_semantic_requests(requests, settings, budget):
    requests = tuple(requests)
    if settings.mode != 'shared_evidence' or budget is None:
        return SemanticPartition((), requests, ())
    def fits(group):
        size = request_size_estimates(group)
        return (len(group) <= settings.max_records and size['prompt_chars'] <= settings.max_prompt_chars
                and size['input_estimate'] + budget.output_tokens <= budget.context_tokens
                and size['output_estimate'] <= budget.output_tokens)
    groups, singles, oversized, batches = OrderedDict(), [], [], []
    for request in requests:
        if not fits([request]):
            oversized.append(request.record_id)
            continue
        key = _sharing_key(request)
        if key is None:
            singles.append(request)
        else:
            groups.setdefault(key, []).append(request)
    def emit(group):
        if len(group) > 1:
            batches.append(tuple(group))
        else:
            singles.extend(group)
    for requests_in_group in groups.values():
        current = []
        for request in requests_in_group:
            if current and not fits([*current, request]):
                emit(current)
                current = []
            current.append(request)
        emit(current)
    singleton_ids = {r.record_id for r in singles}
    return SemanticPartition(tuple(batches), tuple(r for r in requests if r.record_id in singleton_ids), tuple(oversized))


def index_semantic_response(requests, response):
    expected = {r.record_id: r for r in requests}
    if len(expected) != len(requests):
        raise SemanticProtocolError(framing=True)
    received = [item.get('record_id') for item in response.items]
    if (any(type(key) is not str for key in received) or len(received) != len(expected)
        or len(set(received)) != len(received) or set(received) != set(expected)):
        raise SemanticProtocolError(framing=True)
    indexed, errors = {}, {}
    for item in response.items:
        key = item['record_id']
        if item.get('evidence_set_id') != expected[key].bundle.bundle_id:
            raise SemanticProtocolError(framing=True)
        try:
            indexed[key] = SemanticBatchItem.model_validate(item, strict=True).decision
        except ValidationError:
            errors[key] = 'invalid_item_schema'
    if errors:
        raise SemanticProtocolError(item_errors=errors, response=response)
    return indexed
