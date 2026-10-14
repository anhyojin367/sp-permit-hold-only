"""Keep orchestration separate from retired pre-MD checks."""
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def definitions():
    tree = ast.parse((ROOT / "sp_pdf_judger/pipeline.py").read_text(encoding="utf-8"))
    nodes = {node.name: node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    return tree, nodes


def test_retired_checks_are_not_in_runtime_orchestration():
    _, nodes = definitions()
    baseline = json.loads((ROOT / "tests/fixtures/pipeline_active_v91.json").read_text(encoding="utf-8"))
    assert not set(baseline["removed"]) & nodes.keys()
    # Existing entry points, including the directly tested general-method guard,
    # keep their original import addresses.
    assert {"DocumentJudgePipeline", "_apply_general_test_method_rules"} <= nodes.keys()


def test_local_helpers_are_reachable_from_pipeline():
    _, nodes = definitions()
    reached = {"DocumentJudgePipeline"}
    while True:
        more = reached | {ref.id for name in reached for ref in ast.walk(nodes[name])
                          if isinstance(ref, ast.Name) and ref.id in nodes}
        if more == reached:
            break
        reached = more
    assert reached == nodes.keys(), f"Orphaned orchestration helpers: {nodes.keys() - reached}"


def test_pipeline_has_no_unused_imports():
    tree, _ = definitions()
    loaded = {node.id for node in ast.walk(tree)
              if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    imported = {alias.asname or alias.name.split('.')[0]
                for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
                and getattr(node, "module", None) != "__future__" for alias in node.names}
    assert imported <= loaded


def test_legacy_audit_is_explicitly_historical_not_current_coverage():
    payload = json.loads((ROOT / "sp_pdf_judger/validation_audit_catalog.json").read_text(encoding="utf-8"))
    assert payload["scope"] == "historical_snapshot"
    assert payload["code_reference_revision"] == "db045c5"
    assert (ROOT / payload["current_scope_document"]).is_file()
