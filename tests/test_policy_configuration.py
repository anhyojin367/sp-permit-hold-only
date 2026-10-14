"""Authoring errors must fail before extraction or paid judgement starts."""
from pathlib import Path

import pytest
import yaml

from sp_pdf_judger.policy_engine import RuleBook


def write_rule(tmp_path, operation, params, **overrides):
    rule = dict(id="CONFIG", title="설정 검증", instruction="근거대로 비교",
                operation=operation, params=params)
    rule.update(overrides)
    path = tmp_path / "rule.md"
    path.write_text("---\n" + yaml.safe_dump({"rules": [rule]}, allow_unicode=True) + "---\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("operation,params", [
    ("process_order", {"allow_same_day": "false"}),
    ("process_order", {"allow_same_dya": False}),
    ("duration", {"minimum_days": 28, "inclusive": "false"}),
    ("duration", {"minimum_days": True}),
    ("duration", {"minimum_days": -1}),
    ("duration", {"minimum_days": 1.5}),
    ("duration", {"minimum_days": "28"}),
    ("required_tests", {"tests": "무균시험"}),
    ("required_tests", {"tests": [""]}),
    ("required_tests", {"tests": []}),
    ("required_tests", {"tests": ["무균시험", "무균 시험"]}),
    ("signature_order", {"tests": {"contxt": ["완제품"]}}),
    ("storage_window", {"target": {"context": "완제품"}}),
    ("expiry", {"start_field": "제조일", "end_field": "유효일"}),
    ("expiry", {"start_field": "제조일", "end_field": "제조일", "duration_field": "기간"}),
    ("expiry", {"start_field": "제조일", "end_field": "유효일", "duration_field": "기간", "end_policy": "exactly"}),
    ("storage_period", {"period_field": "기간", "duration_field": "기간"}),
    ("mass_balance", {"output_field": "제조량", "start_marker": "조성", "end_marker": "조성"}),
    ("labelled_numeric_compare", {"expected_labels": []}),
    ("labelled_numeric_compare", {"expected_labels": ["10μm"], "sp_supplement_if_no_numeric_limit": "false"}),
    ("numeric_compare", {"threshold": 50}),
    ("semantic_review", {"minimum_days": 28}),
    ("version_consistency", {"pattern": "["}),
    ("version_consistency", {"pattern": ".*"}),
    ("permit_field", {"sp_field": "제품명", "permit_pattern": "제품명"}),
    ("permit_field", {"sp_field": "제품명", "permit_pattern": "(["}),
    ("permit_test", {"permit_pattern": "(EP)", "sp_pattern": "EP", "sp_attribute": "method"}),
    ("permit_test", {"permit_pattern": "(EP)", "sp_pattern": "(EP)", "sp_attribute": "methd"}),
    ("test_after_manufacture", {"stages": [{"source": "원액", "target": ["완제품"]}]}),
    ("test_after_manufacture", {"stages": [{"source": ["원액"], "targte": ["완제품"]}]}),
    ("unit_consistency", {"unit_aliases": [{"test": "", "from": "IU", "to": "EU"}]}),
    ("unit_consistency", {"unit_aliases": [{"test": "시험", "from": "IU", "too": "EU"}]}),
    ("unit_consistency", {"expected_units": {"": "EU"}}),
    ("page_continuity", {"skip_missing": True}),
    ("lot_consistency", {"fields": ["  "]}),
])
def test_invalid_operator_configuration_is_rejected_at_load(tmp_path, operation, params):
    write_rule(tmp_path, operation, params)
    with pytest.raises(ValueError, match="CONFIG"):
        RuleBook(tmp_path)


@pytest.mark.parametrize("text", [
    "rules: []\nrules: [{id: X, title: X, instruction: X, operation: page_continuity}]\n",
    "rules:\n- {id: X, title: X, instruction: X, operation: process_order, params: {allow_same_day: false, allow_same_day: true}}\n",
    "rules:\n- {id: X, title: X, instruction: X, operation: page_continuity}\npermit_search_aliaes: []\n",
])
def test_duplicate_yaml_keys_and_unknown_root_fields_are_rejected(tmp_path, text):
    (tmp_path / "bad.md").write_text("---\n" + text + "---\n", encoding="utf-8")
    with pytest.raises(ValueError):
        RuleBook(tmp_path)


@pytest.mark.parametrize("changes", [
    {"id": " "}, {"title": ""}, {"instruction": " "},
    {"products": [""]}, {"products": "제품A"}, {"aliases": [""]},
])
def test_blank_identity_scope_or_instruction_is_rejected(tmp_path, changes):
    write_rule(tmp_path, "page_continuity", {}, **changes)
    with pytest.raises(ValueError):
        RuleBook(tmp_path)


def test_existing_rules_keep_exact_authored_params():
    root = Path(__file__).resolve().parents[1] / "sp_pdf_judger" / "rules"
    raw_rules = [item for path in sorted(root.glob("*.md"))
                 for item in yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])["rules"]]
    loaded = RuleBook(root)
    assert len(loaded.rules) == len(raw_rules) == 29
    for original, checked in zip(raw_rules, loaded.rules):
        assert checked.params == original.get("params", {})


def test_zero_days_and_false_are_valid_without_coercion(tmp_path):
    write_rule(tmp_path, "duration", {"minimum_days": 0, "inclusive": False})
    assert RuleBook(tmp_path).rules[0].params == {"minimum_days": 0, "inclusive": False}


def test_every_executable_operation_has_a_strict_param_contract():
    from sp_pdf_judger.policy_engine import OPERATIONS
    from sp_pdf_judger.policy_schema import PARAM_MODELS
    assert set(OPERATIONS) == set(PARAM_MODELS)


def test_invalid_rule_stops_pipeline_before_pdf_or_judgement(monkeypatch, tmp_path):
    import sp_pdf_judger.pipeline as pipeline_module
    write_rule(tmp_path, "process_order", {"allow_same_day": "false"})
    pipeline = pipeline_module.DocumentJudgePipeline.__new__(pipeline_module.DocumentJudgePipeline)
    pipeline.rule_dir = tmp_path
    monkeypatch.setattr(pipeline, "_activate_detail_profile", lambda _: None)
    monkeypatch.setattr(pipeline_module, "render_first_page", lambda *a: pytest.fail("Must not render a PDF"))
    monkeypatch.setattr(pipeline_module, "extract_records", lambda *a: pytest.fail("Must not extract a PDF"))
    with pytest.raises(ValueError, match="CONFIG"):
        pipeline.run(tmp_path / "not-read.pdf")


def test_validation_cli_reports_error_without_starting_judgement(tmp_path, capsys):
    from scripts.validate_rules import main
    write_rule(tmp_path, "duration", {"minimum_days": "28"})
    assert main(["--rules-dir", str(tmp_path)]) == 2
    assert "rule.md" in capsys.readouterr().err
    write_rule(tmp_path, "duration", {"minimum_days": 28})
    assert main(["--rules-dir", str(tmp_path)]) == 0
    assert "CLOVA 호출 없음" in capsys.readouterr().out


def test_validation_cli_can_describe_editable_parameters(capsys):
    import json
    from scripts.validate_rules import main
    assert main(["--describe", "duration"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["minimum_days"]
    assert schema["properties"]["inclusive"]["type"] == "boolean"
