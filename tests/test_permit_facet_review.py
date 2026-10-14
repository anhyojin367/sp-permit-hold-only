"""Independent, MD-defined semantic questions never approve laboratory verdicts."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from scripts.permit_obligation_draft import DraftSettings
from scripts.permit_facet_review import build_review, parse_facets, make_lines, build_prompt, response_schema, validate_response, run_review
import scripts.permit_facet_review as review
from test_permit_source_ownership import source
from test_permit_review_packet import PAGES, STAGE

MD = "# 검토\n\n## criterion\n적합 조건이 있는가?\n\n## procedure\n절차가 있는가?\n"


def setup(tmp_path, **kwargs):
    md = tmp_path / "facets.md"
    md.write_text(MD, encoding="utf-8")
    store = source(PAGES)
    return store, md, build_review(store, STAGE, md, DraftSettings(max_batch_items=2), **kwargs)


def reply(req, state="absent"):
    return {"request_id": req["request_id"], "facet_id": req["facet_id"],
        "items": [{"span_id": i["span_id"], "state": state,
                   "evidence_line_ids": [next(l["line_id"] for l in i["lines"] if l["text"].strip())] if state == "present" else [],
                   "note": "검토 초안"} for i in req["items"]]}


def test_all_source_characters_have_exact_lines_and_offsets():
    item = {"span_ref": "S1", "char_start": 15, "text": " \n두 줄\r\n마지막"}
    lines = make_lines(item)
    assert "".join(l["text"] for l in lines) == item["text"]
    for line in lines:
        assert item["text"][line["char_start"] - 15:line["char_end"] - 15] == line["text"]


@pytest.mark.parametrize("bad", ["", "##\na", "## Bad-ID\na", "## a\n", "## a\na\n## a\nb", "## a\na\n### hidden\nb"])
def test_malformed_md_rejected(bad):
    with pytest.raises(ValueError): parse_facets(bad)


def test_every_span_is_asked_once_for_each_md_facet(tmp_path):
    _, _, plan = setup(tmp_path)
    source_ids = {s["span_id"] for b in plan["base"]["batches"] for s in b["items"]}
    for facet in ("criterion", "procedure"):
        requests = [r for r in plan["requests"] if r["facet_id"] == facet]
        ids = [i["source_span_id"] for r in requests for i in r["items"]]
        assert len(ids) == len(set(ids)) and set(ids) == source_ids
        assert all(r["facet_description"] == plan["facets"][facet] for r in requests)
    assert not plan["partial_scope"] and not plan["runtime_activation"]


def test_selected_subset_is_explicit_not_full_source_approval(tmp_path):
    _, _, plan = setup(tmp_path, span_refs=["S0001"], facet_ids=["criterion"])
    assert plan["partial_scope"] and len(plan["requests"]) == 1
    assert not plan["runtime_activation"]


@pytest.mark.parametrize("selection", [{"span_refs": []}, {"span_refs": ["unknown"]}, {"span_refs": ["S0001", "S0001"]}, {"facet_ids": []}, {"facet_ids": ["typo"]}])
def test_invalid_or_empty_subset_rejected(tmp_path, selection):
    with pytest.raises(ValueError): setup(tmp_path, **selection)


@pytest.mark.parametrize("change", ["missing", "duplicate", "foreign_span", "foreign_line", "empty_evidence", "false_with_evidence", "duplicate_line", "wrong_request", "wrong_facet", "extra_verdict", "blank_note"])
def test_invalid_model_response_rejected(tmp_path, change):
    _, _, plan = setup(tmp_path)
    req = plan["requests"][0]
    raw = reply(req, "present")
    row = raw["items"][0]
    if change == "missing": raw["items"].pop()
    elif change == "duplicate": raw["items"].append(deepcopy(row))
    elif change == "foreign_span": row["span_id"] = "S9999"
    elif change == "foreign_line": row["evidence_line_ids"] = ["L9999"]
    elif change == "empty_evidence": row["evidence_line_ids"] = []
    elif change == "false_with_evidence": row["state"] = "absent"
    elif change == "duplicate_line": row["evidence_line_ids"] *= 2
    elif change == "wrong_request": raw["request_id"] = "old"
    elif change == "wrong_facet": raw["facet_id"] = "other"
    elif change == "extra_verdict": row["approved"] = True
    else: row["note"] = " "
    with pytest.raises(ValueError): validate_response(req, raw)


def test_evidence_is_restored_from_source_never_model_quote(tmp_path):
    _, _, plan = setup(tmp_path)
    req = plan["requests"][0]
    raw = reply(req, "present")
    rows = validate_response(req, raw)
    assert rows[0]["source_span_id"] == req["items"][0]["source_span_id"]
    assert rows[0]["evidence"][0] in req["items"][0]["lines"]
    assert rows[0]["review_status"] == "unreviewed"
    assert "検査合格" not in json.dumps(rows)


def test_schema_limits_response_count_and_aliases(tmp_path):
    _, _, plan = setup(tmp_path)
    req = plan["requests"][0]
    schema = response_schema(req)
    schema.model_validate(reply(req))
    bad = reply(req)
    bad["items"].pop()
    with pytest.raises(ValueError): schema.model_validate(bad)


@pytest.mark.parametrize("change", ["md", "source", "plan"])
def test_changed_input_blocks_api(tmp_path, change):
    store, md, plan = setup(tmp_path)
    if change == "md": md.write_text(MD + "変更", encoding="utf-8")
    elif change == "source": store.chunks.pop()
    else: plan["requests"][0]["items"][0]["lines"][0]["text"] = "forged"
    result = run_review(plan, store, md, lambda *args: pytest.fail("API must not be called"))
    assert result["errors"] and not result["contract_complete"] and not result["attempts"]


def test_transport_success_does_not_validate_semantics_or_activate(tmp_path):
    store, md, plan = setup(tmp_path)
    def request(prompt, schema, attempt):
        req = plan["requests"][len(calls)]
        calls.append(prompt)
        return schema.model_validate(reply(req))
    calls = []
    result = run_review(plan, store, md, request)
    assert result["contract_complete"] and not result["runtime_activation"]
    assert not result["semantic_correctness_verified"]
    assert all("한 가지 의미" in p for p in calls)


def test_invalid_response_audit_stops_without_retry(tmp_path):
    store, md, plan = setup(tmp_path)
    def request(prompt, schema, attempt):
        attempt["raw_model_output"] = '{"not": "valid"}'
        raise RuntimeError("SECRET_HEADER")
    result = run_review(plan, store, md, request)
    assert len(result["attempts"]) == 1 and not result["contract_complete"]
    assert result["attempts"][0]["raw_model_output"] == '{"not": "valid"}'
    assert "SECRET_HEADER" not in json.dumps(result)


def test_md_only_change_changes_question_and_plan_hash(tmp_path):
    store, md, first = setup(tmp_path)
    md.write_text(MD.replace("적합 조건이 있는가?", "절차에 숨은 결과 조건도 있는가?"), encoding="utf-8")
    second = build_review(store, STAGE, md, DraftSettings(max_batch_items=2))
    assert first["plan_sha256"] != second["plan_sha256"]
    assert build_prompt(first["requests"][0]) != build_prompt(second["requests"][0])
    assert first["implementation_sha256"] == second["implementation_sha256"]


@pytest.mark.parametrize("state", ["present", "unclear"])
def test_blank_line_cannot_serve_as_evidence(tmp_path, state):
    _, _, plan = setup(tmp_path)
    req = deepcopy(plan["requests"][0])
    req["items"][0]["lines"][0]["text"] = "\n"
    raw = reply(req)
    raw["items"][0].update(state=state, evidence_line_ids=["L001"])
    with pytest.raises(ValueError, match="빈 줄"):
        validate_response(req, raw)


def test_unknown_remains_unknown_with_no_approval(tmp_path):
    _, _, plan = setup(tmp_path)
    req = plan["requests"][0]
    rows = validate_response(req, reply(req, "unclear"))
    assert all(r["state"] == "unclear" and r["review_status"] == "unreviewed" for r in rows)


def test_change_during_request_preserves_unaccepted_response(tmp_path):
    store, md, plan = setup(tmp_path)
    def request(prompt, schema, attempt):
        md.write_text(MD + "다른 조건", encoding="utf-8")
        return reply(plan["requests"][0])
    result = run_review(plan, store, md, request)
    assert result["rows"] == [] and len(result["attempts"]) == 1
    assert result["attempts"][0]["response"] and not result["attempts"][0]["accepted"]
    assert result["errors"][0]["phase"] == "source_validation"


@pytest.mark.parametrize("changed", ["prompt", "implementation"])
def test_changed_protocol_prevents_reusing_plan(tmp_path, monkeypatch, changed):
    store, md, plan = setup(tmp_path)
    if changed == "prompt": monkeypatch.setattr(review, "PROMPT", "different")
    else: plan["implementation_sha256"] = "different"
    result = run_review(plan, store, md, lambda *args: pytest.fail("stale API request"))
    assert result["errors"] and result["attempts"] == []


def test_audited_request_preserves_malformed_model_text_without_headers(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOVA_RESPONSE_CACHE_DIR", raising=False)
    import sp_pdf_judger.clova_client as clova
    client = SimpleNamespace(base_url="https://example.invalid", api_key="SECRET_HEADER",
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k:
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"bad":"shape"}'))],
                            headers={"secret": "SECRET_HEADER"}))))
    store, md, plan = setup(tmp_path)
    result = run_review(plan, store, md, review.make_audited_request(client, DraftSettings()))
    assert result["attempts"][0]["raw_model_output"] == '{"bad":"shape"}'
    assert result["attempts"][0]["provider_call_observed"]
    assert "SECRET_HEADER" not in json.dumps(result)
    assert result["errors"][0]["validation_errors"]


def test_audited_cache_hit_is_not_reported_as_observed_provider_call(tmp_path, monkeypatch):
    import sp_pdf_judger.clova_client as clova
    _, _, plan = setup(tmp_path)
    req = plan["requests"][0]
    monkeypatch.setattr(clova, "request_structured_response", lambda **k: k["response_model"].model_validate(reply(req)))
    client = SimpleNamespace(base_url="https://example.invalid")
    audit = {}
    review.make_audited_request(client, DraftSettings())("prompt", response_schema(req), audit)
    assert not audit["provider_call_observed"] and "raw_model_output" not in audit
