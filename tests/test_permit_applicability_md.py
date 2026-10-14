"""Executable MD must bind reviewed source and reject silent partial policies."""
from copy import deepcopy
import importlib

import pytest
import yaml

from tests.test_permit_applicability import setup, evidence


def config_data():
    api, store, contract, targets = setup()
    config = {
        "schema_version": 1, "id": "TEST_CONDITIONS", "company": "합성제조사", "product": "합성시험제품",
        "review_note": "이 고정 합성 문서의 전체 문단과 아래 조건 해석을 시험용으로 검토했다.",
        "input_layout": "explicit_page_block_v1",
        "reviewed_document_id": store.source_documents[0].evidence_id,
        "reviewed_node_ids": [n["evidence_id"] for n in contract.nodes],
        "reviewed_condition_ids": [c.source_span_id for c in contract.conditions],
        "stages": [{"source_node_id": contract.requirements[0].stage_node_id, "sp_stage": "원액"}],
        "fields": [{"name": c.field, "unit": c.unit} for c in contract.conditions],
        "requirements": [r.model_dump() for r in contract.requirements],
        "conditions": [c.model_dump() for c in contract.conditions],
    }
    return api, store, contract, targets, config


def write_md(tmp_path, config, body="# 조건 규칙\n본문은 설명이며 실행 조건은 위 설정이다.\n"):
    path = tmp_path / "conditions.md"
    path.write_text("---\n" + yaml.safe_dump({"applicability": config}, allow_unicode=True, sort_keys=False)
                    + "---\n\n" + body, encoding="utf-8")
    return path


def load(path):
    return importlib.import_module("sp_pdf_judger.permit_applicability_md").load_applicability_md(path)


def test_md_load_bind_and_independent_target_reader(tmp_path):
    api, store, old, _, config = config_data()
    loaded = load(write_md(tmp_path, config))
    contract = loaded.bind(store, company="합성제조사", product="합성시험제품")
    targets = loaded.read_targets([(1, "제조단계: 원액\n제조번호: L1\n용기규격: 100 mL\n시험차수: 2")], source_file="sp.pdf")
    assert len(loaded.md_sha256) == 64 and contract.contract_id != old.contract_id
    assert [d.applicability for d in api.expected_response(contract, targets).decisions] == ["required", "required"]
    assert api.validate_evidence(evidence(api, contract, targets), contract, targets)["accepted"]


@pytest.mark.parametrize("case", ["extra", "version_bool", "version_unknown", "blank_id", "blank_review",
    "missing_layout", "unknown_layout", "duplicate_stage", "duplicate_field", "unused_field", "unknown_field",
    "unit_typo", "numeric_value", "bool_value", "bad_operator", "bad_purpose", "duplicate_condition",
    "missing_requirement", "duplicate_requirement", "extra_requirement", "wrong_stage", "wrong_sp_stage",
    "missing_node", "duplicate_node", "old_document", "foreign_span", "malformed_id", "missing_combine",
    "missing_condition", "duplicate_condition_review"])
def test_bad_md_is_rejected_before_any_model_call(tmp_path, case):
    _, store, _, _, config = config_data()
    if case == "extra": config["skip_validation"] = True
    if case == "version_bool": config["schema_version"] = True
    if case == "version_unknown": config["schema_version"] = 9
    if case == "blank_id": config["id"] = " "
    if case == "blank_review": config["review_note"] = " "
    if case == "missing_layout": config.pop("input_layout")
    if case == "unknown_layout": config["input_layout"] = "auto"
    if case == "duplicate_stage": config["stages"].append(deepcopy(config["stages"][0]))
    if case == "duplicate_field": config["fields"].append({"name": "용기 규격", "unit": "mL"})
    if case == "unused_field": config["fields"].append({"name": "없는필드", "unit": ""})
    if case == "unknown_field": config["conditions"][0]["field"] = "용기규격오타"
    if case == "unit_typo": config["conditions"][0]["unit"] = "ML"
    if case == "numeric_value": config["conditions"][0]["value"] = 100
    if case == "bool_value": config["conditions"][0]["value"] = True
    if case == "bad_operator": config["conditions"][0]["operator"] = "equals"
    if case == "bad_purpose": config["conditions"][0]["purpose"] = "exempt"
    if case == "duplicate_condition": config["conditions"].append(deepcopy(config["conditions"][0]))
    if case == "missing_requirement": config["requirements"].pop()
    if case == "duplicate_requirement": config["requirements"].append(deepcopy(config["requirements"][0]))
    if case == "extra_requirement": config["requirements"][0]["source_node_id"] = "f" * 64
    if case == "wrong_stage": config["requirements"][0]["stage_node_id"] = "f" * 64
    if case == "wrong_sp_stage": config["requirements"][0]["sp_stage"] = "완제"
    if case == "missing_node": config["reviewed_node_ids"].pop(0)
    if case == "duplicate_node": config["reviewed_node_ids"].append(config["reviewed_node_ids"][0])
    if case == "old_document": config["reviewed_document_id"] = "f" * 64
    if case == "foreign_span": config["conditions"][0]["source_span_id"] = "f" * 64
    if case == "malformed_id": config["reviewed_document_id"] = "auto"
    if case == "missing_combine": config["requirements"][0].pop("combine")
    if case == "missing_condition":
        config["conditions"].pop()
        config["fields"].pop()
    if case == "duplicate_condition_review": config["reviewed_condition_ids"].append(config["reviewed_condition_ids"][0])
    with pytest.raises(ValueError):
        load(write_md(tmp_path, config)).bind(store, company="합성제조사", product="합성시험제품")


@pytest.mark.parametrize("scope", [("다른회사", "합성시험제품"), ("합성제조사", "다른제품"), ("", "")])
def test_wrong_submission_scope_is_rejected(tmp_path, scope):
    _, store, _, _, config = config_data()
    with pytest.raises(ValueError): load(write_md(tmp_path, config)).bind(store, company=scope[0], product=scope[1])


@pytest.mark.parametrize("change", ["value", "operator", "combine", "body"])
def test_md_only_change_invalidates_old_response_and_changes_supported_logic(tmp_path, change):
    api, store, _, targets, config = config_data()
    path = write_md(tmp_path, config)
    first = load(path).bind(store, company="합성제조사", product="합성시험제품")
    prior = evidence(api, first, targets)
    if change == "value": config["conditions"][0]["value"] = "50"
    if change == "operator": config["conditions"][0]["operator"] = "ne"
    if change == "combine":
        config["conditions"][0]["value"] = "50"
        config["requirements"][0]["combine"] = "any"
    write_md(tmp_path, config, body="설명 변경" if change == "body" else "# 조건 규칙")
    changed = load(path).bind(store, company="합성제조사", product="합성시험제품")
    assert first.contract_id != changed.contract_id
    assert not api.validate_evidence(prior, changed, targets)["accepted"]
    states = [d.applicability for d in api.expected_response(changed, targets).decisions]
    assert states == {"value": ["not_required"]*2, "operator": ["not_required"]*2,
                      "combine": ["required", "not_required"], "body": ["required"]*2}[change]


@pytest.mark.parametrize("text", ["# no frontmatter", "---\napplicability: {}", "---\napplicability: {}\napplicability: {}\n---",
    "---\napplicability: &x {a: *x}\n---", "---\napplicability: !!python/object/apply:os.system [echo BAD]\n---",
    "---\napplicability: {}\nunknown: 1\n---", "---\napplicability: {<<: {schema_version: 1}}\n---"])
def test_malformed_unsafe_or_duplicate_yaml_is_rejected(tmp_path, text):
    path = tmp_path / "bad.md"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError): load(path)


def test_source_or_md_change_is_not_automatically_reapproved(tmp_path):
    _, _, _, _, config = config_data()
    from tests.test_permit_applicability import PERMIT
    _, changed_store, _, _ = setup(permit=PERMIT.replace("100 mL", "50 mL"))
    with pytest.raises(ValueError): load(write_md(tmp_path, config)).bind(changed_store, company="합성제조사", product="합성시험제품")


def test_loaded_configuration_is_a_snapshot_and_nested_mutation_cannot_change_it(tmp_path):
    api, store, _, targets, config = config_data()
    path = write_md(tmp_path, config)
    loaded = load(path)
    snapshot = loaded.bind(store, company="합성제조사", product="합성시험제품")
    mutable_copy = loaded.settings
    mutable_copy.conditions.pop()
    mutable_copy.requirements.pop()
    config["conditions"][0]["value"] = "50"
    write_md(tmp_path, config)
    assert loaded.bind(store, company="합성제조사", product="합성시험제품").contract_id == snapshot.contract_id
    assert load(path).md_sha256 != loaded.md_sha256
    assert all(d.applicability == "required" for d in api.expected_response(snapshot, targets).decisions)


@pytest.mark.parametrize("kind", ["wrong_extension", "large", "bad_encoding"])
def test_invalid_md_file_is_rejected(tmp_path, kind):
    path = tmp_path / ("settings.txt" if kind == "wrong_extension" else "settings.md")
    path.write_bytes(b"\xff\xfe" if kind == "bad_encoding" else b"x" * (1_000_001 if kind == "large" else 1))
    with pytest.raises(ValueError): load(path)


def test_unconditional_review_requires_explicit_empty_condition_manifest(tmp_path):
    api, store, _, targets, config = config_data()
    config.update(conditions=[], fields=[], reviewed_condition_ids=[])
    contract = load(write_md(tmp_path, config)).bind(store, company="합성제조사", product="합성시험제품")
    assert all(d.applicability == "required" for d in api.expected_response(contract, targets).decisions)


def test_yaml_delimiter_inside_review_text_is_not_a_frontmatter_boundary(tmp_path):
    _, store, _, _, config = config_data()
    config["review_note"] = "앞 --- 뒤"
    loaded = load(write_md(tmp_path, config))
    loaded.bind(store, company="합성제조사", product="합성시험제품")
    assert loaded.settings.review_note == "앞 --- 뒤"
