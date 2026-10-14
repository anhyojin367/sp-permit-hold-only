"""Apply linked authoritative evidence only to an already completed SP HOLD.

The SP result is immutable input.  This boundary never reruns primary judgement,
document policies, permit groups, or legacy top-k semantic fallback.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .config import FAIL_LABEL, HOLD_LABEL, PASS_LABEL
from .hierarchy import build_document_tree
from .judgement import (
    _looks_like_unparsed_table,
    _parse_result_table,
    _permit_has_fatal_extraction_error,
)
from .numeric_safety import result_is_requirement
from .permit_evidence_bundle import build_evidence_bundle, render_bundle_context
from .permit_llm_protocol import build_prompt, parse_candidates
from .permit_semantic_settings import SemanticSettings, model_budget
from .policy_engine import RuleBook, normalize_record_result, normalize_record_units
from . import review_presentation
from .schemas import Summary
from .utils import clean_text


_SKIP_REASONS = {
    'extraction_quality': '추출 품질 문제는 허가서로 보완할 수 없어 원문 확인까지 검수보류를 유지합니다.',
    'ocr_suspect': 'OCR 의심 결과는 허가서 판단에 사용하지 않고 원문 확인까지 검수보류를 유지합니다.',
    'missing_result': 'SP의 실제 관측 결과가 없어 허가서 대조 없이 검수보류를 유지합니다.',
    'result_is_requirement': '실제 결과 대신 요구 문장이 기재되어 허가서 대조 없이 검수보류를 유지합니다.',
    'sp_policy_guard': 'SP 문서 정합성 점검의 보류 사유는 허가서로 해소하지 않고 유지합니다.',
    'unexpanded_table': '독립 시험 행으로 확정되지 않은 표는 다른 행의 결과를 빌리지 않고 검수보류를 유지합니다.',
    'permit_not_authoritative': '권위 있는 연결 허가서 정책이 확인되지 않아 검수보류를 유지합니다.',
    'permit_unavailable': '연결 허가서의 사용 가능한 근거가 확인되지 않아 검수보류를 유지합니다.',
    'permit_catalog_invalid': '연결 허가서의 확인 정보가 유효하지 않아 검수보류를 유지합니다.',
    'permit_extraction_error': '허가서 추출 오류가 있어 근거를 완전하게 확인할 때까지 검수보류를 유지합니다.',
    'provider_unavailable': '허가서 의미 비교 서비스를 사용할 수 없어 검수보류를 유지합니다.',
    'retrieval_incomplete': '시험에 대응하는 허가서 근거 전체를 확정하지 못해 검수보류를 유지합니다.',
    'bundle_incomplete': '참조 조건을 포함한 허가서 근거가 완전하지 않아 검수보류를 유지합니다.',
    'evidence_oversized': '전체 허가서 근거가 안전한 요청 범위를 초과하여 생략 없이 검수보류를 유지합니다.',
    'unknown_model_budget': '모델의 안전한 요청 범위를 확인할 수 없어 검수보류를 유지합니다.',
    'permit_not_found': '대응하는 허가서 조건을 확정하지 못해 기존 SP 검수보류를 유지합니다.',
}


def _positions(result):
    """Require one baseline evaluation for each original test position."""
    mapping = {}
    records = result.extracted_records
    for index, evaluation in enumerate(result.evaluations):
        if evaluation.record_type != 'test':
            continue
        position = evaluation.record_position
        if type(position) is not int or not 0 <= position < len(records) or position in mapping:
            raise ValueError('SP baseline record position identity is missing or duplicated')
        record = records[position]
        if record.record_type != 'test':
            raise ValueError('SP baseline position does not identify a test')
        for field in ('order_idx', 'section_number', 'section_title', 'criteria', 'result', 'page_start', 'page_end'):
            if getattr(evaluation, field) != getattr(record, field):
                raise ValueError('SP baseline record identity changed: ' + field)
        if record.test_name and clean_text(evaluation.test_name) != clean_text(record.test_name):
            raise ValueError('SP baseline record identity changed: test_name')
        mapping[position] = index
    if set(mapping) != {i for i, record in enumerate(records) if record.record_type == 'test'}:
        raise ValueError('SP baseline test position coverage is incomplete')
    return mapping


def _hard_hold_reason(record, evaluation):
    if evaluation.source == 'extraction_quality':
        return 'extraction_quality'
    if record.ocr_suspect:
        return 'ocr_suspect'
    if not clean_text(record.result):
        return 'missing_result'
    if result_is_requirement(record.criteria, record.result):
        return 'result_is_requirement'
    if evaluation.source == 'md_policy_guard' or (evaluation.comparator or '').startswith('md_'):
        return 'sp_policy_guard'
    if (evaluation.lot_judgements or record.result_table or record.tables
            or _parse_result_table(record.result) or _parse_result_table(record.raw_text)
            or _looks_like_unparsed_table(record.result)):
        return 'unexpanded_table'
    return None


def _keep_hold(evaluation, audit, reason):
    evaluation.reason = '\n'.join(filter(None, (evaluation.reason, _SKIP_REASONS[reason])))
    audit.update(reason_code=reason, final_status=HOLD_LABEL, reviewed=False)


def _baseline_integrity(result):
    receipt = result.metadata.get('review_receipt')
    if not isinstance(receipt, dict) or not receipt.get('sp', {}).get('sha256'):
        raise ValueError('SP baseline receipt fingerprint is missing')
    if review_presentation.file_sha256(result.pdf_path) != receipt['sp']['sha256']:
        raise RuntimeError('SP file changed after the baseline review')
    if review_presentation.source_fingerprint() != receipt.get('engine_sha256'):
        raise RuntimeError('Review engine fingerprint changed after the baseline review')
    if receipt.get('rules_sha256') != result.metadata.get('rule_fingerprint'):
        raise RuntimeError('SP baseline rules fingerprint changed')
    return receipt


def _rules(pipeline):
    return RuleBook(pipeline.rule_dir) if pipeline.rule_dir else RuleBook()


def _verify_loaded_integrity(result, pipeline, receipt, rules_fingerprint):
    _baseline_integrity(result)
    if _rules(pipeline).fingerprint != rules_fingerprint:
        raise RuntimeError('Markdown rules fingerprint changed during permit review')
    for path, item in zip(pipeline.permit_store.permit_pdf_paths, receipt['permits']):
        if not item['sha256'] or review_presentation.file_sha256(path) != item['sha256']:
            raise RuntimeError('Linked permit file changed during review')


def _finish(result, audit):
    # This is the same UI/export counting contract, including lot rows.
    from .pipeline import _count_evaluation_statuses
    passed, failed, held = _count_evaluation_statuses(result.evaluations)
    result.summary = Summary(passed, failed, held, passed + failed + held, passed + failed + held)
    result.tree = build_document_tree(result.extracted_records, result.evaluations)
    audit['reviewed_count'] = sum(item['reviewed'] for item in audit['records'])
    audit['skipped_count'] = audit['held_test_count'] - audit['reviewed_count']
    result.metadata['permit_review_scope'] = 'hold_only'
    result.metadata['hold_only_review'] = audit
    return result


def apply_held_permit_review(before_result, *, pipeline_factory, progress_callback=None):
    """Clone a complete SP baseline and resolve only safe, independent HOLDs.

    ``pipeline_factory`` is called at most once and only after eligible HOLDs
    exist. It supplies a permit-enabled pipeline; its ``run`` is never called.
    A service error propagates, preventing publication of partial final results.
    """
    baseline_receipt = _baseline_integrity(before_result)
    positions = _positions(before_result)
    result = deepcopy(before_result)
    progress = progress_callback or (lambda message: None)
    audit = {'scope': 'hold_only', 'version': 1, 'held_test_count': 0,
             'eligible_count': 0, 'reviewed_count': 0, 'skipped_count': 0, 'records': []}
    eligible = []
    for position, index in positions.items():
        evaluation = result.evaluations[index]
        if evaluation.final_status != HOLD_LABEL:
            continue
        record = result.extracted_records[position]
        entry = {'record_position': position, 'baseline_status': HOLD_LABEL,
                 'baseline_source': evaluation.source, 'final_status': HOLD_LABEL,
                 'reviewed': False, 'reason_code': ''}
        audit['records'].append(entry)
        audit['held_test_count'] += 1
        reason = _hard_hold_reason(record, evaluation)
        if reason:
            _keep_hold(evaluation, entry, reason)
        else:
            eligible.append((position, index, entry))
    audit['eligible_count'] = len(eligible)
    if not eligible:
        _baseline_integrity(before_result)
        return _finish(result, audit)

    pipeline = pipeline_factory()
    store, engine, client = pipeline.permit_store, pipeline.judge_engine, pipeline.llm_client
    if engine.permit_store is not store or engine.llm_client is not client:
        raise ValueError('Permit pipeline source identity mismatch')
    book = _rules(pipeline)
    if book.fingerprint != baseline_receipt['rules_sha256']:
        raise RuntimeError('Markdown rules fingerprint changed after the baseline review')
    receipt = review_presentation.review_receipt(result.pdf_path, store.permit_pdf_paths,
        book.fingerprint, llm_enabled=client.enabled, model=client.model)
    _verify_loaded_integrity(before_result, pipeline, receipt, book.fingerprint)
    expected_hashes = {Path(path).resolve(): digest for path, digest in
                       getattr(pipeline.permit_resolution, 'content_hashes', ())}
    expected_hashes.update(getattr(store, 'pdf_sha256_by_path', {}))
    for path, item in zip(store.permit_pdf_paths, receipt['permits']):
        expected = expected_hashes.get(Path(path).resolve())
        if expected and expected.casefold() != item['sha256'].casefold():
            raise RuntimeError('Linked permit file changed after resolution')

    start_calls, start_success = client.call_count, client.success_count
    start_execution = getattr(client, 'execution_fingerprint', '')
    rule_product = result.metadata.get('rule_profile', {}).get('rule_product', pipeline.product)
    store.configure_search_aliases(book.search_alias_groups(rule_product))
    settings = getattr(pipeline, 'semantic_settings', SemanticSettings())
    budget = model_budget(client.model, client.max_completion_tokens)
    by_position = deepcopy(result.metadata.get('comparison_bases_by_position', {}))
    by_evaluation = deepcopy(result.metadata.get('comparison_bases_by_evaluation', {}))
    provider_audit = []
    for sequence, (position, index, entry) in enumerate(eligible, 1):
        evaluation, record = result.evaluations[index], result.extracted_records[position]
        progress(f'보류 시험 허가서 대조 {sequence}/{len(eligible)} · {evaluation.test_name}')
        policy = engine.permit_policy
        reason = None
        if not policy or not policy.authoritative:
            reason = 'permit_not_authoritative'
        elif engine.permit_catalog_valid is False:
            reason = 'permit_catalog_invalid'
        elif _permit_has_fatal_extraction_error(store.extraction_errors):
            reason = 'permit_extraction_error'
        elif not store.enabled or engine.permit_group_preflight_reason():
            reason = 'permit_unavailable'
        elif not client.enabled:
            reason = 'provider_unavailable'
        elif budget is None:
            reason = 'unknown_model_budget'
        if reason:
            _keep_hold(evaluation, entry, reason)
            continue

        normalized = normalize_record_units(record, book, rule_product)
        normalized, _ = normalize_record_result(normalized, book, rule_product)
        selection = store.search_precise(normalized)
        if not selection.complete:
            _keep_hold(evaluation, entry, 'retrieval_incomplete')
            continue
        bundle = build_evidence_bundle(selection, all_chunks=store.chunks,
            documents=store.source_documents, company=pipeline.company, product=pipeline.product)
        if not bundle.complete:
            _keep_hold(evaluation, entry, 'bundle_incomplete')
            continue
        context = render_bundle_context(bundle)
        candidates = parse_candidates(context)
        if not candidates or len(candidates) != len(bundle.candidates):
            _keep_hold(evaluation, entry, 'bundle_incomplete')
            continue
        record_context = engine._record_context(normalized)
        prompt = build_prompt(candidates, record_context,
            '[SP 문서의 시험기준]\n' + clean_text(normalized.criteria), clean_text(normalized.result))
        output_estimate = 512 + 128 * max(len(candidate.clauses) for candidate in candidates)
        if (len(prompt) > settings.max_prompt_chars or output_estimate > budget.output_tokens
                or len(prompt.encode('utf-8')) + output_estimate + 8192 > budget.context_tokens):
            _keep_hold(evaluation, entry, 'evidence_oversized')
            continue

        # Never repeat primary judgement or retrieve another reference source.
        # Capture duplicate order_idx records independently before the next call.
        engine.comparison_bases.pop(record.order_idx, None)
        saved_rag = engine.rag_store
        engine.rag_store = None
        try:
            status, reason, comparator, norm_criteria, norm_result, source = engine._judge_with_authoritative_permit(
                record=normalized, title=evaluation.test_name, permit_context=context,
                first_status=HOLD_LABEL, first_reason=evaluation.reason,
                norm_criteria=normalized.criteria, norm_result=normalized.result)
        finally:
            engine.rag_store = saved_rag
        bases = deepcopy(engine.comparison_bases.get(record.order_idx, []))
        by_position[position] = bases
        by_evaluation[index] = bases
        entry.update(reviewed=True, reason_code='permit_reviewed', candidate_count=len(candidates),
                     clause_count=sum(len(candidate.clauses) for candidate in candidates))
        if status in {PASS_LABEL, FAIL_LABEL, HOLD_LABEL}:
            notes = [line for line in (evaluation.reason or '').splitlines()
                     if line.startswith(('MD 단위 표기 대응:', 'MD 결과 표기 사전:'))]
            evaluation.final_status = status
            evaluation.reason = '\n'.join([reason or evaluation.reason or '', *notes])
            evaluation.source = source or evaluation.source
            evaluation.comparator = comparator or evaluation.comparator
            evaluation.normalized_criteria = norm_criteria
            evaluation.normalized_result = norm_result
            evaluation.comparison_completed = status in {PASS_LABEL, FAIL_LABEL}
        else:
            evaluation.reason = '\n'.join(filter(None, (evaluation.reason, _SKIP_REASONS['permit_not_found'])))
            entry['reason_code'] = 'permit_not_found'
        entry['final_status'] = evaluation.final_status
        provider_audit.append({key: entry[key] for key in
            ('record_position', 'candidate_count', 'clause_count', 'final_status', 'reason_code')})

    _verify_loaded_integrity(before_result, pipeline, receipt, book.fingerprint)
    if getattr(client, 'execution_fingerprint', '') != start_execution:
        raise RuntimeError('Provider execution fingerprint changed during permit review')
    result.runtime_permit_paths = tuple(store.permit_pdf_paths)
    result.metadata.update({
        'sp_baseline_review_receipt': deepcopy(baseline_receipt), 'review_receipt': receipt,
        'comparison_bases_by_position': by_position, 'comparison_bases_by_evaluation': by_evaluation,
        'permit_llm_audit': provider_audit,
        'llm_call_count': int(before_result.metadata.get('llm_call_count', 0)) + client.call_count - start_calls,
        'llm_success_count': int(before_result.metadata.get('llm_success_count', 0)) + client.success_count - start_success,
        'llm_last_error': 'permit_review_failed' if client.last_error else '',
        'permit_pdf_count': len(store.permit_pdf_paths), 'permit_chunk_count': len(store.chunks),
        'permit_paths': [Path(path).name for path in store.permit_pdf_paths],
        'resolved_permit_paths': [Path(path).name for path in pipeline.permit_resolution.paths],
        'permit_policy_id': getattr(engine.permit_policy, 'policy_id', None),
        'permit_fingerprint': pipeline.permit_resolution.fingerprint,
    })
    audit['permit_call_count'] = client.call_count - start_calls
    audit['permit_success_count'] = client.success_count - start_success
    return _finish(result, audit)
