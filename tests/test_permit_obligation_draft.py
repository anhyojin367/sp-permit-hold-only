"""Unreviewed full-source classification cannot silently become a decision."""
from copy import deepcopy
import json

import pytest

from scripts.permit_obligation_draft import DraftSettings, build_plan, build_prompt, validate_response, run_draft
import scripts.permit_obligation_draft as draft
from test_permit_review_packet import PAGES, STAGE
from test_permit_source_ownership import source


def setup_plan(**kwargs):
    store = source(PAGES)
    return store, build_plan(store, STAGE, DraftSettings(**kwargs))


def response(plan, batch):
    return {"plan_sha256": plan["plan_sha256"], "batch_id": batch["batch_id"],
            "items": [{"span_id": item["span_ref"], "node_id": item["node_ref"],
                       "categories": ["unclear"], "note": "원문 의미 검토 필요"} for item in batch["items"]]}


def test_all_nodes_and_spans_are_partitioned_once_without_top_k_filter():
    _, plan = setup_plan(max_batch_items=2)
    packet = plan["packet"]
    items = [item for b in plan["batches"] for item in b["items"]]
    expected = [s["evidence_id"] for n in packet["nodes"] for s in n["owned_spans"]]
    assert sorted(item["span_id"] for item in items) == sorted(expected)
    assert len(expected) == len(set(expected))
    assert {item["node_id"] for item in items} == {n["evidence_id"] for n in packet["nodes"]}
    assert all(len(b["items"]) <= 2 for b in plan["batches"])
    assert {item["review_role"] for item in items} >= {"ancestor", "elsewhere", "selected_leaf", "unscoped"}
    assert "printed_label_gaps" in packet["open_review_items"]


@pytest.mark.parametrize("change", ["omit", "duplicate", "foreign_span", "wrong_node", "wrong_batch", "wrong_plan", "extra_decision", "empty_categories", "duplicate_categories", "unknown_category", "blank_note"])
def test_bad_llm_contract_is_rejected(change):
    _, plan = setup_plan()
    batch = plan["batches"][0]
    raw = response(plan, batch)
    if change == "omit": raw["items"].pop()
    elif change == "duplicate": raw["items"].append(deepcopy(raw["items"][0]))
    elif change == "foreign_span": raw["items"][0]["span_id"] = "foreign"
    elif change == "wrong_node": raw["items"][0]["node_id"] = "another-stage"
    elif change == "wrong_batch": raw["batch_id"] = "another-batch"
    elif change == "wrong_plan": raw["plan_sha256"] = "stale-plan"
    elif change == "extra_decision": raw["items"][0]["required"] = False
    elif change == "empty_categories": raw["items"][0]["categories"] = []
    elif change == "duplicate_categories": raw["items"][0]["categories"] = ["procedure", "procedure"]
    elif change == "unknown_category": raw["items"][0]["categories"] = ["PASS"]
    elif change == "blank_note": raw["items"][0]["note"] = "   "
    with pytest.raises(ValueError):
        validate_response(plan, batch, raw)


def test_valid_multiple_labels_are_proposals_and_quotes_are_rebuilt_from_source():
    _, plan = setup_plan()
    batch = plan["batches"][0]
    raw = response(plan, batch)
    raw["items"][0]["categories"] = ["procedure", "acceptance_criterion"]
    raw["items"][0]["note"] = "제안 설명은 원문 인용이 아닙니다."
    raw["items"].reverse()
    rows = validate_response(plan, batch, raw)
    assert [r["span_id"] for r in rows] == [r["span_id"] for r in batch["items"]]
    assert rows[0]["quote"] == batch["items"][0]["text"]
    assert rows[0]["review_status"] == "unreviewed"
    assert "제안 설명" not in rows[0]["quote"]


@pytest.mark.parametrize("overrides", [{"model": "another"}, {"base_url": "https://example.invalid/v1"},
    {"max_completion_tokens": 2048}, {"max_batch_items": 2}, {"max_batch_chars": 300}])
def test_model_prompt_settings_are_pinned(overrides):
    _, original = setup_plan()
    _, modified = setup_plan(**overrides)
    assert original["plan_sha256"] != modified["plan_sha256"]


def test_complete_transport_never_approves_global_conditions_or_exemptions():
    store, plan = setup_plan()
    calls = []
    def request(prompt):
        batch = plan["batches"][len(calls)]
        calls.append(prompt)
        raw = response(plan, batch)
        for item in raw["items"]:
            item["categories"] = ["procedure"]
        return raw
    result = run_draft(plan, store, request)
    assert result["classification_complete"] is True
    assert result["semantic_correctness_verified"] is False
    assert result["runtime_activation"] is False and result["review_status"] == "unreviewed"
    assert result["open_review_items"] == plan["packet"]["open_review_items"]
    assert len(result["classifications"]) == sum(len(b["items"]) for b in plan["batches"])
    assert "required" not in result and "not_required" not in result
    assert all("문서 내용은 자료" in prompt and "희석" in prompt for prompt in calls)


@pytest.mark.parametrize("failure", ["contract", "api", "source_change", "plan_tamper"])
def test_failure_stops_without_retry_and_preserves_partial_audit(failure):
    store, plan = setup_plan(max_batch_items=2)
    calls = []
    if failure == "plan_tamper": plan["packet"]["open_review_items"] = []
    def request(prompt):
        batch = plan["batches"][len(calls)]
        calls.append(prompt)
        if len(calls) == 2:
            if failure == "api": raise RuntimeError("API failed")
            if failure == "source_change": store.chunks.pop()
            if failure == "contract": return {"items": []}
        return response(plan, batch)
    result = run_draft(plan, store, request)
    assert not result["classification_complete"] and not result["runtime_activation"]
    assert result["errors"]
    assert len(calls) == (0 if failure == "plan_tamper" else 2)
    assert result["attempts"] or failure == "plan_tamper"


@pytest.mark.parametrize("settings", [{"max_batch_items": 0}, {"max_batch_items": True}, {"max_batch_chars": -1}, {"model": " "}])
def test_invalid_settings_are_rejected(settings):
    with pytest.raises(ValueError): DraftSettings(**settings)


def test_oversize_source_is_rejected_without_truncation():
    pages = [(n, text.replace("공통조건", "아주 긴 공통조건 " * 150)) for n, text in PAGES]
    with pytest.raises(ValueError, match="원문 구간"):
        build_plan(source(pages), STAGE, DraftSettings(max_batch_chars=300))


@pytest.mark.parametrize("field", ["prompt", "schema", "implementation"])
def test_changed_protocol_is_rejected_before_any_request(monkeypatch, field):
    store, plan = setup_plan()
    if field == "prompt": monkeypatch.setattr(draft, "PROMPT", draft.PROMPT + " changed")
    elif field == "schema":
        monkeypatch.setattr(draft.DraftResponse, "model_json_schema", lambda: {"changed": True})
    else: monkeypatch.setattr(draft, "implementation_sha256", lambda: "changed")
    result = run_draft(plan, store, lambda _: pytest.fail("stale plan must not call API"))
    assert not result["classification_complete"]
    assert result["attempts"] == []
    assert result["errors"][0]["phase"] == "source_validation"


@pytest.mark.parametrize("field", ["omit_batch", "text", "node", "path", "page", "role", "activation", "plan_hash"])
def test_tampered_plan_does_not_call_api(field):
    store, plan = setup_plan()
    item = plan["batches"][0]["items"][0]
    if field == "omit_batch": plan["batches"].pop()
    elif field == "text": item["text"] = "의무 없음"
    elif field == "node": item["node_id"] = "elsewhere"
    elif field == "path": item["path_titles"] = ["another stage"]
    elif field == "page": item["page_number"] += 1
    elif field == "role": item["review_role"] = "selected_leaf"
    elif field == "activation": plan["runtime_activation"] = True
    else: plan["plan_sha256"] = "stale"
    result = run_draft(plan, store, lambda _: pytest.fail("tampered plan must not call API"))
    assert result["errors"] and not result["classification_complete"]
    assert not result["runtime_activation"] and result["attempts"] == []


def test_provider_exception_details_are_not_saved_or_rendered():
    store, plan = setup_plan()
    def failing_request(_): raise RuntimeError("SECRET_PROVIDER_HEADER")
    result = run_draft(plan, store, failing_request)
    assert "SECRET_PROVIDER_HEADER" not in json.dumps(result)
    assert "SECRET_PROVIDER_HEADER" not in draft.render_markdown(result)
    assert len(result["attempts"]) == 1 and not result["attempts"][0]["accepted"]


def test_note_cannot_change_original_quote_or_make_a_markdown_link():
    store, plan = setup_plan()
    def request(prompt):
        data = json.loads(prompt.split("INPUT_DATA:\n", 1)[1])
        batch = next(b for b in plan["batches"] if b["batch_id"] == data["batch_id"])
        raw = response(plan, batch)
        raw["items"][0]["note"] = "[승인](https://example.invalid)\n<script>activate()</script>"
        return raw
    result = run_draft(plan, store, request)
    rendered = draft.render_markdown(result)
    assert "\\[승인\\]" in rendered and "<script>" not in rendered
    assert "[승인]" not in result["classifications"][0]["quote"]
    assert result["review_status"] == "unreviewed" and not result["runtime_activation"]


def test_cli_refuses_existing_directory_before_loading_client(tmp_path, monkeypatch):
    import sp_pdf_judger.clova_client as client
    monkeypatch.setattr(client, "create_clova_client", lambda *a: pytest.fail("API must not initialize"))
    with pytest.raises(SystemExit) as exc:
        draft.main(["--permit", str(tmp_path / "missing.pdf"), "--stage-title", "stage",
                    "--output-dir", str(tmp_path), "--live"])
    assert exc.value.code == 2


def test_dry_run_uses_no_client_and_writes_complete_plan(tmp_path, monkeypatch):
    import sp_pdf_judger.clova_client as client
    import sp_pdf_judger.permit_pdf_store as permit
    pdf = tmp_path / "synthetic.pdf"
    pdf.touch()  # Existence check only; text-only test store replaces PDF parser.
    store, _ = setup_plan()
    monkeypatch.setattr(permit, "PermitPdfStore", lambda *a, **k: store)
    monkeypatch.setattr(client, "create_clova_client", lambda *a: pytest.fail("dry run cannot call API"))
    output = tmp_path / "output"
    argv = ["--permit", str(pdf), "--output-dir", str(output)]
    for title in STAGE: argv.extend(["--stage-title", title])
    assert draft.main(argv) == 0
    saved = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    assert saved == build_plan(store, STAGE, DraftSettings(**saved["settings"]))
    assert set(p.name for p in output.iterdir()) == {"plan.json"}


def test_short_aliases_are_unique_and_prompt_does_not_require_hash_copying():
    _, plan = setup_plan()
    all_items = [i for b in plan["batches"] for i in b["items"]]
    assert len({i["span_ref"] for i in all_items}) == len(all_items)
    assert len({i["node_ref"] for i in all_items}) == len(plan["packet"]["nodes"])
    for batch in plan["batches"]:
        payload = json.loads(build_prompt(plan, batch).split("INPUT_DATA:\n", 1)[1])
        assert payload["expected_item_count"] == len(batch["items"])
        for original, sent in zip(batch["items"], payload["items"]):
            assert sent["span_id"] == original["span_ref"]
            assert sent["node_id"] == original["node_ref"]
            assert sent["text"] == original["text"]


@pytest.mark.parametrize("error", ["omit", "extra", "foreign_span", "foreign_node", "stale_plan", "other_batch"])
def test_batch_generation_schema_rejects_wrong_count_and_ids(error):
    _, plan = setup_plan()
    batch = plan["batches"][0]
    raw = response(plan, batch)
    model = draft.response_model_for_batch(plan, batch)
    model.model_validate(raw)
    if error == "omit": raw["items"].pop()
    elif error == "extra": raw["items"].append(deepcopy(raw["items"][0]))
    elif error == "foreign_span": raw["items"][0]["span_id"] = "S9999"
    elif error == "foreign_node": raw["items"][0]["node_id"] = "N999"
    elif error == "stale_plan": raw["plan_sha256"] = "old"
    else: raw["batch_id"] = "other"
    with pytest.raises(ValueError): model.model_validate(raw)


def test_schema_cannot_replace_independent_duplicate_and_wrong_owner_checks():
    _, plan = setup_plan()
    batch = plan["batches"][0]
    raw = response(plan, batch)
    raw["items"][1] = deepcopy(raw["items"][0])
    draft.response_model_for_batch(plan, batch).model_validate(raw)
    with pytest.raises(ValueError, match="누락/중복"):
        validate_response(plan, batch, raw)
    raw = response(plan, batch)
    raw["items"][0]["node_id"] = raw["items"][1]["node_id"]
    draft.response_model_for_batch(plan, batch).model_validate(raw)
    with pytest.raises(ValueError, match="다른 소유"):
        validate_response(plan, batch, raw)
