"""Reviewed numeric execution must not depend on or repair a model response."""
import importlib

import pytest

from tests.test_permit_applicability_md import config_data, write_md, load


PAGES = [(1, "제조단계: 원액\n제조번호: L1\n용기규격: 100 mL\n시험차수: 2\n농도: 20 mg/mL")]


def execute(path, store, pages=PAGES, **kwargs):
    api = importlib.import_module("sp_pdf_judger.permit_applicability_md")
    return api.evaluate_numeric_applicability(path, store, pages, source_file="sp.pdf",
        company="합성제조사", product="합성시험제품", **kwargs)


def states(result):
    return [d["applicability"] for d in result["decisions"]]


def test_direct_execution_never_calls_model_or_response_validator(tmp_path, monkeypatch):
    api, store, _, _, config = config_data()
    path = write_md(tmp_path, config)
    def forbidden(*args, **kwargs): raise AssertionError("No model response path may run")
    for name in ("build_prompt", "expected_response", "validate_response", "validate_evidence"):
        monkeypatch.setattr(api, name, forbidden)
    result = execute(path, store)
    assert states(result) == ["required", "required"]
    assert result["execution_mode"] == "reviewed_numeric_v1"
    assert result["accepted"] and result["evaluation_status"] == "complete"
    assert result["logical_calls"] == 0 and result["model_used"] is False
    assert result["rules_md_sha256"] == load(path).md_sha256
    assert result["reviewed_node_ids"] == config["reviewed_node_ids"]
    assert len(result["evaluation_id"]) == 64
    facts = {f["fact_id"]: f for t in result["targets"] for f in t["facts"]}
    for decision in result["decisions"]:
        for condition in decision["conditions"]:
            for reference in condition["facts"]:
                fact = facts[reference["fact_id"]]
                assert reference["quote"] == fact["quote"]
                assert PAGES[0][1][fact["char_start"]:fact["char_end"]] == fact["quote"]
    assert result["predicates"] == config["conditions"]


@pytest.mark.parametrize("replacement,expected", [
    ("용기규격: 50 mL", ["not_required", "not_required"]),
    ("", ["unknown", "unknown"]),
    ("용기규격: 100 L", ["unknown", "unknown"]),
    ("용기규격: 100", ["unknown", "unknown"]),
    ("용기규격: NaN mL", ["unknown", "unknown"]),
    ("용기규격: 100 mL\n용기규격: 50 mL", ["unknown", "unknown"]),
    ("용기규격: 100 mL\n용기규격: 100 mL", ["unknown", "unknown"]),
    ("용기규격=100 mL", ["unknown", "unknown"]),
])
def test_unknown_or_conflicting_fact_is_not_an_exemption(tmp_path, replacement, expected):
    _, store, _, _, config = config_data()
    result = execute(write_md(tmp_path, config), store,
        [(1, PAGES[0][1].replace("용기규격: 100 mL", replacement))])
    assert result["accepted"] and states(result) == expected
    assert result["evaluation_status"] == ("held" if "unknown" in expected else "complete")


@pytest.mark.parametrize("operator,value,expected", [
    ("eq", "100", "required"), ("eq", "100.0000000000000000001", "not_required"),
    ("ne", "100", "not_required"), ("ne", "100.0000000000000000001", "required"),
    ("lt", "100", "not_required"), ("lte", "100", "required"),
    ("gt", "100", "not_required"), ("gte", "100", "required"),
])
def test_exact_decimal_boundary_from_md(tmp_path, operator, value, expected):
    _, store, _, _, config = config_data()
    # These are execution truth-table tests, not approval of modified semantics.
    config["conditions"][0].update(operator=operator, value=value)
    assert states(execute(write_md(tmp_path, config), store)) == [expected, expected]


def test_missing_procedure_fact_does_not_exempt_or_hold_test(tmp_path):
    _, store, _, _, config = config_data()
    result = execute(write_md(tmp_path, config), store, [(1, PAGES[0][1].replace("농도: 20 mg/mL", ""))])
    assert states(result) == ["required", "required"]
    assert result["decisions"][1]["conditions"][-1]["truth"] == "unknown"
    assert result["evaluation_status"] == "complete"  # Applicability only; not procedure/result approval.


def test_batches_use_only_their_own_facts(tmp_path):
    _, store, _, _, config = config_data()
    pages = [*PAGES, (2, PAGES[0][1].replace("L1", "L2").replace("용기규격: 100 mL", ""))]
    result = execute(write_md(tmp_path, config), store, pages)
    assert states(result) == ["required", "required", "unknown", "unknown"]
    assert len({t["target_id"] for t in result["targets"]}) == 2


@pytest.mark.parametrize("pages", [[], [(2, PAGES[0][1])], [(1, "")],
    [*PAGES, (3, PAGES[0][1])], [*PAGES, (2, PAGES[0][1].replace("원액", "완제"))]])
def test_incomplete_pages_or_unreviewed_stage_cannot_be_silently_ignored(tmp_path, pages):
    _, store, _, _, config = config_data()
    with pytest.raises(ValueError): execute(write_md(tmp_path, config), store, pages)


def test_no_model_payload_or_caller_constructed_targets_argument(tmp_path):
    _, store, _, targets, config = config_data()
    path = write_md(tmp_path, config)
    for extra in ({"response": {}}, {"targets": targets}, {"on_failure": "pass"}):
        with pytest.raises(TypeError): execute(path, store, **extra)


def test_new_md_or_sp_cannot_reuse_old_evaluation_identity(tmp_path):
    _, store, _, _, config = config_data()
    path = write_md(tmp_path, config)
    first = execute(path, store)
    repeat = execute(path, store)
    assert first == repeat
    second = execute(path, store, [(1, PAGES[0][1].replace("L1", "L2"))])
    assert second["evaluation_id"] != first["evaluation_id"]
    assert second["contract_id"] == first["contract_id"]
    write_md(tmp_path, config, body="설명만 변경")
    third = execute(path, store)
    assert third["contract_id"] != first["contract_id"]
    assert third["evaluation_id"] != first["evaluation_id"]


def test_md_modified_during_extraction_aborts_instead_of_returning_success(tmp_path, monkeypatch):
    _, store, _, _, config = config_data()
    path = write_md(tmp_path, config)
    module = importlib.import_module("sp_pdf_judger.permit_applicability_md")
    original = module.ApplicabilityMarkdown.read_targets
    def changed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        path.write_text(path.read_text(encoding="utf-8") + "\n변경", encoding="utf-8")
        return result
    monkeypatch.setattr(module.ApplicabilityMarkdown, "read_targets", changed)
    with pytest.raises(ValueError, match="변경"): execute(path, store)


@pytest.mark.parametrize("fault", ["old_document", "missing_test", "missing_condition", "wrong_product"])
def test_direct_execution_keeps_source_and_scope_guards(tmp_path, fault):
    _, store, _, _, config = config_data()
    if fault == "old_document": config["reviewed_document_id"] = "f"*64
    if fault == "missing_test": config["requirements"].pop()
    if fault == "missing_condition": config["conditions"].pop()
    if fault == "wrong_product": config["product"] = "다른제품"
    with pytest.raises(ValueError): execute(write_md(tmp_path, config), store)


@pytest.mark.parametrize("combine,size,round_,expected", [
    ("all", "50 mL", "2", "not_required"), ("any", "50 mL", "2", "required"),
    ("any", "100 mL", "미정", "unknown"), ("all", "50 mL", "미정", "unknown"),
    ("all", "100 mL", "1", "not_required"), ("all", "100 mL", "2", "required"),
])
def test_flat_boolean_and_round_selection_keeps_conservative_unknown(tmp_path, combine, size, round_, expected):
    _, store, _, _, config = config_data()
    config["requirements"][0]["combine"] = combine
    page = PAGES[0][1].replace("100 mL", size).replace("시험차수: 2", "시험차수: " + round_)
    assert states(execute(write_md(tmp_path, config), store, [(1, page)]))[0] == expected


def test_rejected_model_evidence_remains_rejected_separately(tmp_path):
    from tests.test_permit_applicability import evidence
    api, store, _, targets, config = config_data()
    path = write_md(tmp_path, config)
    contract = load(path).bind(store, company="합성제조사", product="합성시험제품")
    rejected = evidence(api, contract, targets)
    rejected["decisions"][0]["conditions"][0]["truth"] = "unknown"
    before = api.validate_evidence(rejected, contract, targets)
    assert not before["accepted"]
    assert states(execute(path, store)) == ["required", "required"]
    assert api.validate_evidence(rejected, contract, targets) == before
