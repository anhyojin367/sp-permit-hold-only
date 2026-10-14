"""MD-driven permit semantic draft; plan-only by default, never runtime approval."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.permit_facet_review import build_review, DraftSettings, make_audited_request
from scripts.permit_facet_checkpoint import CheckpointError, run_checkpointed


def _load_inputs(args):
    from sp_pdf_judger.config import CLOVA_BASE_URL, DEFAULT_CLOVA_MODEL, CLOVA_MAX_COMPLETION_TOKENS
    from sp_pdf_judger.permit_catalog import PermitPolicy
    from sp_pdf_judger.permit_pdf_store import PermitPdfStore
    settings = DraftSettings(model=DEFAULT_CLOVA_MODEL, base_url=CLOVA_BASE_URL,
                             max_completion_tokens=CLOVA_MAX_COMPLETION_TOKENS)
    store = PermitPdfStore([args.permit], policy=PermitPolicy('draft-only', True, True, True, 'auto'))
    plan = build_review(store, args.stage_title, args.md, settings,
                        span_refs=args.span_ref, facet_ids=args.facet, evidence_units=args.evidence_units,
                        layout_hints=args.layout_hints)
    return store, settings, plan


def _provider(settings):
    from sp_pdf_judger.config import CLOVA_API_KEY
    from sp_pdf_judger.clova_client import create_clova_client
    client = create_clova_client(CLOVA_API_KEY, settings.base_url)
    if client is None:
        raise CheckpointError('CLOVA 설정을 확인하세요.')
    return make_audited_request(client, settings)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--permit', type=Path, required=True)
    parser.add_argument('--stage-title', action='append', required=True)
    parser.add_argument('--md', type=Path, default=ROOT/'docs/examples/permit_semantic_facets.md')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--span-ref', action='append', help='Explicit partial scope, e.g. S0001')
    parser.add_argument('--facet', action='append', help='Explicit MD facet ID subset')
    parser.add_argument('--evidence-units', choices=('lines','clauses'), default='lines', help='Experimental reversible reading units; default unchanged')
    parser.add_argument('--layout-hints', action='store_true', help='Unreviewed source-position clues; requires --evidence-units clauses')
    parser.add_argument('--live', action='store_true', help='Allow paid CLOVA calls for an unapproved draft')
    parser.add_argument('--resume', action='store_true', help='Reuse same-plan valid response checkpoints')
    parser.add_argument('--retry-failed', action='store_true', help='Explicitly retry failed/uncertain requests; may incur cost again')
    args = parser.parse_args(argv)
    if args.retry_failed and not (args.live and args.resume):
        parser.error('--retry-failed requires --live --resume')
    if not args.permit.is_file() or args.permit.suffix.lower() != '.pdf':
        parser.error('단일 허가 PDF 필요')
    if not args.md.is_file(): parser.error('의미 설명 MD 필요')
    try:
        store, settings, plan = _load_inputs(args)
        print(f"PLAN: {len(plan['span_refs'])} spans / {len(plan['facet_ids'])} facets / {len(plan['requests'])} requests; partial={plan['partial_scope']}", flush=True)
        call = None
        def request(prompt, schema, attempt):
            nonlocal call
            if call is None: call = _provider(settings)
            print(f"REQUEST {attempt['request_id']} (not semantic approval)", flush=True)
            return call(prompt, schema, attempt)
        result = run_checkpointed(plan, store, args.md, args.output_dir, request if args.live else None,
                                  resume=args.resume, retry_failed=args.retry_failed, plan_only=not args.live)
        if result is None:
            print('PLAN ONLY: API 0; use the same arguments plus --live --resume to execute.', flush=True)
            return 0
        reused = sum(a.get('checkpoint_reused', False) for a in result['attempts'])
        print(f"CONTRACT={result['contract_complete']} / reused={reused} / semantic_verified=false / runtime_activation=false", flush=True)
        if result['errors']:
            blocked = [a.get('checkpoint_blocked') for a in result['attempts'] if a.get('checkpoint_blocked')]
            print(f"INCOMPLETE: checkpoint blocks={blocked}; failure records retained; no automatic retry.", flush=True)
        return 0 if result['contract_complete'] else 2
    except CheckpointError as exc:
        print(f'CHECKPOINT: {exc}', file=sys.stderr)
        return 2
    except Exception as exc:
        # CLI must not leak paths/headers/key-bearing provider exception bodies.
        print(f'ERROR: {type(exc).__name__}; private details omitted, no automatic retry.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
