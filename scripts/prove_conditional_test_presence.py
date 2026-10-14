"""Synthetic opt-in condition-to-presence proof. No product activation or API."""
from dataclasses import asdict
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml
from scripts.prove_scoped_condition_facts import source_scopes
from scripts.prove_permit_applicability import draw_pages, fixture_contract, POLICY
from scripts.validate_corpus import engine_fingerprint
from sp_pdf_judger.extractor import extract_records
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.policy_engine import PolicyContext, RuleBook, evaluate_policies, to_evaluations
from sp_pdf_judger.policy_required_tests import permit_scope_inventory

FONT = Path('C:/Windows/Fonts/malgun.ttf')


def write_md(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Eliminate aliases; repeated scope objects are separate reviewed statements.
    data = json.loads(json.dumps(data, ensure_ascii=False))
    path.write_text('---\n' + yaml.safe_dump(data, allow_unicode=True, sort_keys=False) + '---\n', encoding='utf-8')


def make_fixture(root, *, size='100 mL', round_='2', tests=('무균시험', '함량시험'), concentration='20 mg/mL'):
    root.mkdir(parents=True, exist_ok=True)
    permit, pdf = root / 'permit.pdf', root / 'sp.pdf'
    draw_pages(permit, [['1. 제조방법', '1.1. 원액', '용기규격 100 mL인 경우에만 다음 시험을 수행한다.',
        '1.1.1. 무균시험', '시험차수 2 이상인 경우에만 수행한다.', '1.1.2. 함량시험',
        '농도 10 mg/mL 이상이면 희석 절차를 생략한다.']], FONT)
    lines = ['1. 원액', '1.1. 정보', '1.1.1 A 정보', '제조번호 A-01']
    if size is not None: lines += ['용기규격 ' + size]
    lines += ['시험차수 ' + round_, '1.1.2 B 정보', '제조번호 B-99', '용기규격 50 mL',
        '1.2. 시험', '1.2.1 A 시험']
    for name, value in [('농도확인시험', concentration), ('다음시험', '기록'), *[(t, '적합') for t in tests]]:
        lines += ['시험명 ' + name, '시험방법 기록', '시험기준 기록', '시험기간 2025.01.01', '시험결과 ' + value]
    lines += ['2. 다음 단계']
    # Keep complete test records on page 2; never draw beyond the page footer.
    draw_pages(pdf, [lines[:21], lines[21:]] if len(lines) > 26 else [lines], FONT)
    store = PermitPdfStore([permit], policy=POLICY)
    contract = fixture_contract(store)
    config = dict(schema_version=1, id='SYNTHETIC_CONDITIONAL', company='합성제조사', product='합성시험제품',
        review_note='합성 fixture 전용, 실제 허가의 승인이 아님', input_layout='single_batch_scoped_records_v1',
        source_scopes=source_scopes(), reviewed_document_id=store.source_documents[0].evidence_id,
        reviewed_node_ids=[n['evidence_id'] for n in contract.nodes],
        reviewed_condition_ids=[c.source_span_id for c in contract.conditions],
        stages=[dict(source_node_id=contract.requirements[0].stage_node_id, sp_stage='원액')],
        fields=[dict(name=c.field, unit=c.unit) for c in contract.conditions],
        requirements=[r.model_dump() for r in contract.requirements], conditions=[c.model_dump() for c in contract.conditions])
    rules_root = root / 'rules'
    write_md(rules_root / '_conditions/conditions.md', {'applicability': config})
    inv = permit_scope_inventory(store, ['제조방법', '원액'])
    params = dict(permit_stage_path=['제조방법', '원액'], reviewed_scope_sha256=inv['reviewed_scope_sha256'],
        reviewed_document_sha256=inv['document_sha256'], requirement_policy='reviewed_numeric_applicability',
        applicability_md='_conditions/conditions.md', sp_stage_path=['원액', '시험', 'A 시험'],
        coverage_start='1. 원액', coverage_end='2. 다음 단계', batch_inventory={'type':'content', 'context':['A 정보']},
        batch_field='제조번호', batch_field_layout='label_value_lines', batch_inventory_path=['원액', '정보', 'A 정보'],
        batch_coverage_start='1.1.1 A 정보', batch_coverage_end='1.1.2 B 정보',
        test_coverage_start='1.2.1 A 시험', test_coverage_end='2. 다음 단계', batch_binding='single_batch_stage')
    rule = dict(id='SYNTH-REQ', title='조건부 필수시험 기록 확인 (시험 결과 적합 판정 아님)',
        instruction='검토된 수치 수행 조건과 같은 배치의 필수시험 기록을 확인한다.',
        operation='permit_conditional_tests', products=['합성시험제품'], aliases=['A.9'], params=params)
    write_md(rules_root / 'rules.md', {'rules':[rule]})
    records = extract_records(pdf, root / 'extract')
    return root, config, rule, records


def execute(data, *, company='합성제조사', product='합성시험제품', records=None, rules_root=None):
    root, _, _, original_records = data
    ctx = PolicyContext(root / 'sp.pdf', original_records if records is None else records,
        [root / 'permit.pdf'], product=product, company=company)
    book = RuleBook(rules_root or root / 'rules')
    return evaluate_policies(ctx, book)[0], ctx, book


CASES = {
    "complete": ({}, ["PASS", "PASS"], "PASS"),
    "required_missing": ({"tests": ("함량시험",)}, ["FAIL", "PASS"], "FAIL"),
    "round_one": ({"round_": "1", "tests": ("함량시험",)}, ["N/A", "PASS"], "PASS"),
    "not_applicable": ({"size": "50 mL", "tests": ()}, ["N/A", "N/A"], "PASS"),
    "unknown": ({"size": None, "tests": ()}, ["HOLD", "HOLD"], "HOLD"),
    "unit_mismatch": ({"size": "100 L"}, ["HOLD", "HOLD"], "HOLD"),
    "procedure_not_exemption": ({"concentration": "5 mg/mL", "tests": ("무균시험",)}, ["PASS", "FAIL"], "FAIL"),
}


def run_proof(output, rules_root=None):
    from sp_judgement_bridge import _render_structural_validation_cards
    output = Path(output)
    report = dict(engine_fingerprint=engine_fingerprint(), logical_calls=0, cases={})
    html = []
    for name, (options, states, status) in CASES.items():
        data = make_fixture(output / name, **options)
        finding, ctx, book = execute(data, rules_root=rules_root)
        actual = [row["status"] for row in finding.details.get("requirement_checks", [])]
        matches = actual == states and finding.status == status and not finding.details.get("execution_error")
        result = SimpleNamespace(pdf_path=ctx.pdf_path, evaluations=to_evaluations([finding], book, 1),
            metadata={"policy_audit":[asdict(finding)], "permit_paths":[str(data[0] / "permit.pdf")]})
        # Unchanged production adapter/CSS, not a new UI implementation.
        markup = _render_structural_validation_cards(result, section_title="합성 QA: " + name)
        (data[0] / "card.html").write_text(markup, encoding="utf-8")
        html.append(markup)
        report["cases"][name] = dict(expected=states, actual=actual, matches_expected=matches,
            rule_fingerprint=book.fingerprint, finding=asdict(finding))
        print(name, finding.status, actual, "matches", matches, flush=True)
    report["engine_changed_during_run"] = engine_fingerprint() != report["engine_fingerprint"]
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "cards.html").write_text('<!doctype html><meta charset="utf-8"><title>Conditional presence QA</title>'
        '<h1>합성 문서 전용 검증 · 운영 제품 미활성화</h1>' + "".join(html), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".local_validation/conditional_presence_proof")
    parser.add_argument("--rules-dir", type=Path, help="Explicit companion/rule MD root, never overwritten")
    args = parser.parse_args()
    report = run_proof(args.output, args.rules_dir)
    raise SystemExit(0 if not report["engine_changed_during_run"]
        and all(row["matches_expected"] for row in report["cases"].values()) else 1)
