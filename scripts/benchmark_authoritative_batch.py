"""Isolated authoritative benchmark. Offline measures plans, NOT LLM accuracy."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.benchmark_permit_index import (
    _offline_provider_guard, _raise_if_provider_attempted, _extract_and_expand_records,
)
from sp_pdf_judger.clova_transport import clova_request_context, request_diagnostics, diagnostic_delta
from sp_pdf_judger.permit_semantic_settings import SemanticSettings, semantic_fingerprint, model_budget


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')).hexdigest()


def _file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _manifest(sp, permit, mode, model, max_completion_tokens, company, product):
    from sp_pdf_judger.extraction_runtime import extraction_fingerprint
    from sp_pdf_judger.policy_engine import policy_fingerprint
    from sp_pdf_judger.permit_index import permit_evaluation_fingerprint
    sources = ('pipeline.py', 'judgement.py', 'llm.py', 'permit_semantic_batch.py', 'permit_evidence_bundle.py')
    return dict(schema='authoritative-benchmark-v1', sp_sha256=_file_hash(sp), permit_sha256=_file_hash(permit),
                model=model, max_completion_tokens=max_completion_tokens, company=company, product=product,
                mode=mode, semantic_fingerprint=semantic_fingerprint(SemanticSettings('shared_evidence')),
                extraction_fingerprint=extraction_fingerprint(), policy_fingerprint=policy_fingerprint(),
                permit_fingerprint=permit_evaluation_fingerprint(),
                implementation_fingerprint=_hash([_file_hash(PROJECT_ROOT/'sp_pdf_judger'/name) for name in sources]))


def _reject_links(root):
    for path in (root, *root.rglob('*')):
        if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('seed cache contains a link or escaped path')


def compare_records(before, after):
    old, new = ({r['position']: r for r in rows} for rows in (before, after))
    common = sorted(old.keys() & new.keys())
    return {'status_changed': [p for p in common if old[p]['status'] != new[p]['status']],
            'evidence_changed': [p for p in common if old[p]['evidence_hash'] != new[p]['evidence_hash']],
            'reason_changed_needs_review': [p for p in common if old[p]['reason_hash'] != new[p]['reason_hash']],
            'missing_positions': sorted(old.keys()-new.keys()), 'added_positions': sorted(new.keys()-old.keys())}


def _record_report(result):
    rows = []
    # A unique result-list position also covers document-wide checks; never key by order_idx.
    for position, evaluation in enumerate(result.evaluations):
        bases = result.metadata.get('comparison_bases_by_evaluation', {}).get(position, [])
        evidence = [basis.get('evidence', []) for basis in bases]
        rows.append(dict(position=position, source_position=getattr(evaluation, 'record_position', None),
                         status=evaluation.final_status, evidence_hash=_hash(evidence),
                         reason_hash=_hash(evaluation.reason)))
    return rows


def _planning_report(pipeline, records, model, max_tokens, mode):
    from sp_pdf_judger.policy_engine import RuleBook
    from sp_pdf_judger.rule_profile import resolve_rule_product
    from sp_pdf_judger.permit_grouping import build_stage_batch_inventory
    from sp_pdf_judger.policy_engine import normalize_record_units, normalize_record_result
    from sp_pdf_judger.permit_semantic_batch import partition_semantic_requests, request_size_estimates
    engine = pipeline.judge_engine
    original = engine.llm_client
    # No provider exists here: only the eligibility planner sees an enabled capability.
    engine.llm_client = SimpleNamespace(enabled=True, model=model, max_completion_tokens=max_tokens,
                                        execution_fingerprint='offline-planning-only')
    started = perf_counter()
    try:
        rules = RuleBook()
        rule_product, _ = resolve_rule_product(pipeline.product, pipeline.permit_store, rules)
        pipeline.permit_store.configure_search_aliases(rules.search_alias_groups(rule_product))
        engine.semantic_batch_inventory = build_stage_batch_inventory(records)
        requests, reasons, routes = [], Counter(), Counter()
        for position, record in enumerate(records):
            if record.record_type != 'test':
                continue
            normalized = normalize_record_units(record, rules, rule_product)
            normalized, _ = normalize_record_result(normalized, rules, rule_product)
            request = engine.prepare_authoritative_request(normalized, position=position)
            reasons[engine.semantic_preparation_reason] += 1
            routes[engine.semantic_retrieval_route] += 1
            if request is not None:
                requests.append(request)
        partition = partition_semantic_requests(requests, SemanticSettings(mode), model_budget(model, max_tokens))
        batched = [r for group in partition.batches for r in group]
        batch_sizes = [request_size_estimates(group) for group in partition.batches]
        singles = [request_size_estimates([r]) for r in batched]
        return dict(raw_tests=sum(r.record_type == 'test' for r in records), eligible_records=len(requests),
                    batched_records=len(batched), initial_batch_requests=len(partition.batches),
                    estimated_saved_after_calls=len(batched)-len(partition.batches),
                    oversized_records=len(partition.oversized_ids), reasons=dict(reasons), routes=dict(routes),
                    batch_prompt_chars=sum(s['prompt_chars'] for s in batch_sizes),
                    equivalent_single_prompt_chars=sum(s['prompt_chars'] for s in singles),
                    estimated_batch_input=sum(s['input_estimate'] for s in batch_sizes),
                    estimated_single_input=sum(s['input_estimate'] for s in singles),
                    estimate_kind='utf8-byte-estimate', seconds=perf_counter()-started)
    finally:
        engine.llm_client = original


def _execute_run(*, sp, permit, out, mode, allow_live, model, max_completion_tokens, company, product):
    import fitz
    from sp_pdf_judger.pipeline import DocumentJudgePipeline
    from sp_pdf_judger.permit_catalog import resolve_submission_permits
    from sp_pdf_judger.llm import resolve_clova_execution_settings
    from sp_pdf_judger.extraction_runtime import read_extraction_report, validate_extraction_report
    with fitz.open(sp) as document:
        sp_pages = len(document)
    with fitz.open(permit) as document:
        permit_pages = len(document)
    started = perf_counter()
    extract_dir = out/'sp_extract'
    records = _extract_and_expand_records(sp, extract_dir)
    validate_extraction_report(read_extraction_report(extract_dir))
    if not records:
        raise ValueError('no extractable records')
    extraction_seconds = perf_counter()-started
    resolution = resolve_submission_permits([permit], company, product)
    if not resolution.policy or not resolution.policy.authoritative or resolution.policy.evaluation_mode != 'legacy':
        raise ValueError('benchmark requires ordinary authoritative semantic policy')
    settings = resolve_clova_execution_settings(model=model) if allow_live else ('', model, 'offline://disabled')
    static = SimpleNamespace(pdf_path=sp, metadata={'extract_dir': str(extract_dir)}, runtime_extract_dir=extract_dir,
        preview_image_path=None, manufacturing_summary_image_paths=[], manufacturing_summary_page_numbers=[])
    phases, results, after_pipeline = {}, {}, None
    for phase in ('before', 'after'):
        started = perf_counter()
        counters = request_diagnostics()
        pipeline = DocumentJudgePipeline(company=company, product=product, permit_resolution=resolution,
            permit_enabled=phase == 'after', llm_execution_settings=settings, semantic_settings=SemanticSettings(mode))
        if allow_live and not pipeline.llm_client.enabled:
            raise ValueError('live client unavailable; not an offline accuracy result')
        pipeline.llm_client.max_completion_tokens = max_completion_tokens
        result = pipeline.run(sp, extracted_records=records, static_result=static, primary_tests_only=not allow_live)
        phases[phase] = dict(seconds=perf_counter()-started, transport=diagnostic_delta(counters, request_diagnostics()),
            logical_calls=result.metadata.get('llm_call_count'), semantic=result.metadata.get('semantic_batch_diagnostics'),
            permit_index_cache_hit=result.metadata.get('permit_index_cache_hit'),
            permit_chunks=result.metadata.get('permit_chunk_count'))
        results[phase] = result
        if phase == 'after':
            after_pipeline = pipeline
    return dict(page_counts={'sp': sp_pages, 'permit': permit_pages}, extraction_seconds=extraction_seconds,
                phases=phases, planning=_planning_report(after_pipeline, records, model, max_completion_tokens, mode),
                records=_record_report(results['after']), before_records=_record_report(results['before']),
                semantic_accuracy='not_evaluated' if not allow_live else 'requires_record_level_review')


def run_benchmark(*, sp, permit, out, mode='shared_evidence', cache_state='cold', seed_run=None,
                  allow_live=False, model='HCX-007', max_completion_tokens=4096,
                  company='동국바이오사이언스', product='스카이코비원멀티주'):
    sp, permit, out = Path(sp).resolve(), Path(permit).resolve(), Path(out).absolute()
    SemanticSettings(mode)
    model_budget(model, max_completion_tokens)
    if out.exists() or out.is_symlink():
        raise ValueError('out must be a new directory')
    if cache_state not in {'cold', 'index-only', 'responses'} or (cache_state == 'cold') != (seed_run is None):
        raise ValueError('cold rejects seed-run; index-only/responses require a seed-run')
    manifest = _manifest(sp, permit, mode, model, max_completion_tokens, company, product)
    seed = None
    if seed_run is not None:
        seed = Path(seed_run).absolute()
        _reject_links(seed)
        original = json.loads((seed/'manifest.json').read_text(encoding='utf-8'))
        keys = set(manifest) - ({'mode'} if cache_state == 'index-only' else set())
        if any(original.get(key) != manifest[key] for key in keys):
            raise ValueError('seed experiment identities do not match')
        if not (seed/'metrics.json').is_file():
            raise ValueError('seed run did not complete')
    out.mkdir(parents=True, exist_ok=False)
    for name in ('permit_index', 'responses', 'runtime'):
        (out/name).mkdir()
    if seed:
        for name in ('permit_index', 'responses') if cache_state == 'responses' else ('permit_index',):
            if (seed/name).is_dir():
                shutil.copytree(seed/name, out/name, dirs_exist_ok=True)
    (out/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    environment = {'SP_PERMIT_INDEX_DIR': str(out/'permit_index'), 'CLOVA_RESPONSE_CACHE_DIR': str(out/'responses'),
                   'TEMP': str(out/'runtime'), 'TMP': str(out/'runtime')}
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, environment))
        stack.enter_context(patch.object(tempfile, 'tempdir', str(out/'runtime')))
        guard = stack.enter_context(_offline_provider_guard()) if not allow_live else None
        stack.enter_context(clova_request_context(cache_dir=out/'responses', max_http_attempts=250))
        try:
            report = _execute_run(sp=sp, permit=permit, out=out, mode=mode, allow_live=allow_live, model=model,
                max_completion_tokens=max_completion_tokens, company=company, product=product)
            if guard is not None:
                _raise_if_provider_attempted(guard)
        except Exception as exc:
            failure = dict(completed=False, exception_type=type(exc).__name__, transport=request_diagnostics())
            (out/'incomplete.json').write_text(json.dumps(failure, indent=2), encoding='utf-8')
            raise
        report.update(execution_kind='live' if allow_live else 'offline-planning', mode=mode, cache_state=cache_state,
            cached_replay=cache_state == 'responses', transport=request_diagnostics(),
            scale_validation='unverified', live_call_ceiling=250)
        pages = report.get('page_counts', {})
        report['large_input_present'] = pages.get('sp', 0) >= 200 and pages.get('permit', 0) >= 300
        if seed:
            prior = json.loads((seed/'metrics.json').read_text(encoding='utf-8'))
            report['seed_record_delta'] = compare_records(prior['records'], report['records'])
        (out/'metrics.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sp', type=Path, required=True)
    parser.add_argument('--permit', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--mode', choices=('legacy', 'shared_evidence'), default='shared_evidence')
    parser.add_argument('--cache-state', choices=('cold', 'index-only', 'responses'), default='cold')
    parser.add_argument('--seed-run', type=Path)
    parser.add_argument('--company', default='동국바이오사이언스')
    parser.add_argument('--product', default='스카이코비원멀티주')
    parser.add_argument('--allow-live', action='store_true', help='Requires explicit paid-run and network authorization')
    args = parser.parse_args()
    report = run_benchmark(**vars(args))
    print(json.dumps({key: report.get(key) for key in ('execution_kind', 'page_counts', 'planning', 'transport')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
