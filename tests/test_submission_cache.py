import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import sp_app as app
import sp_judgement_bridge as bridge
import sp_pdf_judger.llm as llm_module


def _configure_clova_execution(
    monkeypatch,
    *,
    api_key="",
    model="HCX-007",
    base_url="https://clova.example.test/v1",
):
    monkeypatch.setattr(llm_module, "dotenv_values", lambda *args, **kwargs: {})
    monkeypatch.setattr(llm_module, "CLOVA_API_KEY", "")
    monkeypatch.setattr(llm_module, "DEFAULT_CLOVA_MODEL", "HCX-007")
    monkeypatch.setattr(llm_module, "CLOVA_BASE_URL", "https://fallback.example.test/v1")
    if api_key:
        monkeypatch.setenv("CLOVA_API_KEY", api_key)
    else:
        monkeypatch.delenv("CLOVA_API_KEY", raising=False)
    monkeypatch.setenv("CLOVA_MODEL", model)
    monkeypatch.setenv("CLOVA_BASE_URL", base_url)


def test_llm_execution_fingerprint_changes_for_enabled_model_endpoint_or_protocol(monkeypatch):
    _configure_clova_execution(monkeypatch, api_key="enabled-key")
    monkeypatch.setattr(
        llm_module,
        "PERMIT_GROUP_BATCH_PROTOCOL_VERSION",
        "permit-group-batch-v1",
        raising=False,
    )
    baseline = llm_module.llm_execution_fingerprint()

    monkeypatch.delenv("CLOVA_API_KEY")
    assert llm_module.llm_execution_fingerprint() != baseline

    monkeypatch.setenv("CLOVA_API_KEY", "enabled-key")
    monkeypatch.setenv("CLOVA_MODEL", "HCX-009")
    assert llm_module.llm_execution_fingerprint() != baseline

    monkeypatch.setenv("CLOVA_MODEL", "HCX-007")
    monkeypatch.setenv("CLOVA_BASE_URL", "https://other-clova.example.test/v1")
    assert llm_module.llm_execution_fingerprint() != baseline

    monkeypatch.setenv("CLOVA_BASE_URL", "https://clova.example.test/v1")
    monkeypatch.setattr(
        llm_module,
        "PERMIT_GROUP_BATCH_PROTOCOL_VERSION",
        "permit-group-batch-v2",
    )
    assert llm_module.llm_execution_fingerprint() != baseline


def test_llm_execution_fingerprint_ignores_api_key_value_and_never_exposes_secret_or_url(monkeypatch):
    endpoint = "https://private-clova.example.test/secret-path"
    first_secret = "first-secret-api-key"
    second_secret = "second-secret-api-key"
    _configure_clova_execution(
        monkeypatch,
        api_key=first_secret,
        base_url=endpoint,
    )
    first = llm_module.llm_execution_fingerprint()
    monkeypatch.setenv("CLOVA_API_KEY", second_secret)
    second = llm_module.llm_execution_fingerprint()

    assert first == second
    assert len(first) == 64
    assert all(character in "0123456789abcdef" for character in first)
    assert first_secret not in first
    assert second_secret not in first
    assert endpoint not in first

    monkeypatch.delenv("CLOVA_API_KEY")
    disabled = llm_module.llm_execution_fingerprint()
    monkeypatch.setenv("CLOVA_API_KEY", "YOUR_API_KEY")
    assert llm_module.llm_execution_fingerprint() == disabled

    monkeypatch.setenv("CLOVA_API_KEY", "enabled-key")
    monkeypatch.setenv("CLOVA_BASE_URL", endpoint)
    normalized = llm_module.llm_execution_fingerprint()
    monkeypatch.setenv("CLOVA_BASE_URL", f"  {endpoint}/  ")
    assert llm_module.llm_execution_fingerprint() == normalized


def test_execution_settings_change_during_review_cannot_relabel_or_publish_result(monkeypatch, tmp_path):
    pdf = tmp_path / "sp.pdf"
    pdf.write_bytes(b"sample")
    permit = tmp_path / "permit.pdf"
    permit.write_bytes(b"permit")
    live = {"fingerprint": "offline-fingerprint"}
    snapshotted_settings = ("", "HCX-007", "https://clova.example.test/v1")
    received_settings = []
    cache_publications = []
    status_publications = []

    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        bridge,
        "resolve_clova_execution_settings",
        lambda: snapshotted_settings,
        raising=False,
    )
    monkeypatch.setattr(
        bridge,
        "llm_execution_fingerprint",
        lambda settings=None: (
            "offline-fingerprint" if settings is not None else live["fingerprint"]
        ),
    )
    monkeypatch.setattr(
        bridge,
        "resolve_domain_detail_profile",
        lambda *_args: SimpleNamespace(fingerprint="detail", sources=[]),
    )
    monkeypatch.setattr(
        bridge,
        "resolve_permits",
        lambda *_args, **_kwargs: SimpleNamespace(
            paths=[permit], fingerprint="permit", policy=None, errors=[]
        ),
    )
    monkeypatch.setattr(
        bridge,
        "_write_summary_csv_from_result",
        lambda _result, path: path.write_text("summary", encoding="utf-8"),
    )
    monkeypatch.setattr(bridge, "make_stage_csv_zip", lambda **_kwargs: None)
    monkeypatch.setattr(bridge, "_attach_rule_regression_log", lambda artifacts: artifacts)
    monkeypatch.setattr(
        bridge,
        "_write_judgement_cache",
        lambda *_args: cache_publications.append(_args),
    )
    monkeypatch.setattr(
        bridge,
        "_remember_judgement_artifacts",
        lambda artifacts: status_publications.append(artifacts),
    )

    class Pipeline:
        def __init__(self, **kwargs):
            received_settings.append(kwargs.get("llm_execution_settings"))

        def run(self, *_args, **_kwargs):
            live["fingerprint"] = "online-fingerprint"
            return SimpleNamespace(
                extracted_records=[],
                metadata={
                    "llm_enabled": False,
                    "permit_extraction_diagnostics": [],
                },
                summary=SimpleNamespace(held=0),
            )

    monkeypatch.setattr(bridge, "DocumentJudgePipeline", Pipeline)

    with pytest.raises(bridge.JudgementCacheError, match="실행 설정"):
        bridge.ensure_judgement_artifacts(
            pdf_path=pdf,
            permit_paths=[permit],
            original_csv_dir=None,
            _background=True,
        )

    assert received_settings == [snapshotted_settings, snapshotted_settings]
    assert cache_publications == []
    assert status_publications == []


def test_topbar_exposes_release_marker_without_changing_visible_title(monkeypatch):
    from contextlib import nullcontext

    rendered = []
    monkeypatch.setattr(app, "_init_gmail_state", lambda: None)
    monkeypatch.setattr(app, "ORG_LOGO_DOMAINS", {})
    monkeypatch.setattr(app, "st", SimpleNamespace(
        session_state={}, container=lambda: nullcontext(),
        markdown=lambda value, **kwargs: rendered.append(value)))
    app._render_topbar(show_gmail=False)
    assert f'data-app-version="{app.APP_CACHE_VERSION}"' in rendered[0]
    assert '<div class="brand-title">SP 문서 AI 자동검토 시스템</div>' in rendered[0]


def test_upload_selection_survives_relative_path_callback(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    pdf = tmp_path / "sp.pdf"
    pdf.write_bytes(b"sample")
    doc = SimpleNamespace(path=pdf, company="회사", product="제품")
    state = {"sp_direct_judgement_artifacts::old": "old", "sim_signature::old": "old",
             "latest_judgement_artifact_key": "old", "unrelated": "preserve"}
    monkeypatch.setattr(app, "st", SimpleNamespace(session_state=state))
    monkeypatch.setattr(app, "_clear_document_caches", lambda: None)
    seen = []
    monkeypatch.setattr(app, "_document_from_pdf", lambda path: seen.append(path) or doc)

    app._on_upload_saved(Path("sp.pdf"))

    assert app._ensure_selected_document([doc]) is doc
    assert state["selected_pdf_path"] == str(pdf)
    assert seen == [pdf]
    assert state["reference_company_filter"] == "회사"
    assert state["reference_product_filter"] == "제품"
    assert state["unrelated"] == "preserve"
    assert not any(key.startswith(("sp_direct_judgement_artifacts::", "sim_signature")) for key in state)
    assert "latest_judgement_artifact_key" not in state


def test_status_requires_current_rules_permits_and_details(monkeypatch, tmp_path):
    pdf = tmp_path / "sp.pdf"
    pdf.write_bytes(b"sample")
    doc = SimpleNamespace(path=pdf, company="회사", product="제품")
    monkeypatch.setattr(app, "policy_fingerprint", lambda: "rules-new")
    monkeypatch.setattr(app, "_related_permit_files", lambda p: [])
    monkeypatch.setattr(
        app,
        "resolve_submission_permits",
        lambda *a, **kw: SimpleNamespace(fingerprint="permit-new"),
    )
    monkeypatch.setattr(app, "resolve_domain_detail_profile", lambda *a: SimpleNamespace(fingerprint="detail-new"))
    monkeypatch.setattr(app, "llm_execution_fingerprint", lambda: "llm-new", raising=False)
    entry = {"cache_version": app.JUDGEMENT_STATUS_CACHE_VERSION, "rule_fingerprint": "rules-new",
             "permit_fingerprint": "permit-new", "detail_fingerprint": "detail-new",
             "extraction_fingerprint": app.extraction_fingerprint(),
             "llm_execution_fingerprint": "llm-new"}
    index = {"documents": {app._pdf_status_key(pdf): entry}}
    assert app._status_record_for_doc(doc, index) is entry
    for field in ("rule_fingerprint", "permit_fingerprint", "detail_fingerprint", "extraction_fingerprint",
                  "llm_execution_fingerprint"):
        original = entry[field]
        entry[field] = "old"
        assert app._status_record_for_doc(doc, index) is None
        entry[field] = original
    assert app._status_record_for_doc(doc, {"documents": {"different-pdf": entry}}) is None


def test_identical_names_in_separate_submissions_have_distinct_cache_keys(tmp_path):
    a, b = tmp_path / "one" / "sp.pdf", tmp_path / "two" / "sp.pdf"
    a.parent.mkdir(); b.parent.mkdir()
    a.write_bytes(b"same"); b.write_bytes(b"same")
    assert bridge._artifact_key(a, [], None) != bridge._artifact_key(b, [], None)


def test_artifact_key_changes_with_product_mapping_provenance(tmp_path):
    pdf = tmp_path / "sp.pdf"
    pdf.write_bytes(b"same submission")

    legacy = bridge._artifact_key(
        pdf, [], None, "동국바이오사이언스", "스카이코비원멀티주"
    )
    mapped = bridge._artifact_key(
        pdf,
        [],
        None,
        "동국바이오사이언스",
        "스카이코비원멀티주",
        product_mapping_id="dongkook_unknown_sky_m_to_sky_covione",
    )

    assert legacy != mapped


def test_artifact_key_changes_for_mapping_or_llm_execution_but_not_secret_rotation(monkeypatch, tmp_path):
    pdf = tmp_path / "sp.pdf"
    pdf.write_bytes(b"same submission")
    _configure_clova_execution(monkeypatch, api_key="first-secret")

    baseline = bridge._artifact_key(
        pdf, [], None, "동국바이오사이언스", "스카이코비원멀티주"
    )
    monkeypatch.setenv("CLOVA_API_KEY", "rotated-secret")
    assert bridge._artifact_key(
        pdf, [], None, "동국바이오사이언스", "스카이코비원멀티주"
    ) == baseline

    assert bridge._artifact_key(
        pdf,
        [],
        None,
        "동국바이오사이언스",
        "스카이코비원멀티주",
        product_mapping_id="dongkook_unknown_sky_m_to_sky_covione",
    ) != baseline

    monkeypatch.setenv("CLOVA_MODEL", "HCX-009")
    assert bridge._artifact_key(
        pdf, [], None, "동국바이오사이언스", "스카이코비원멀티주"
    ) != baseline


def test_offline_artifact_and_status_are_rejected_after_clova_becomes_enabled(monkeypatch, tmp_path):
    pdf = tmp_path / "sp.pdf"
    pdf.write_bytes(b"same submission")
    _configure_clova_execution(monkeypatch, api_key="")
    offline_fingerprint = llm_module.llm_execution_fingerprint()
    offline_key = bridge._artifact_key(pdf, [], None, "회사", "제품")
    entry = {
        "cache_version": app.JUDGEMENT_STATUS_CACHE_VERSION,
        "rule_fingerprint": "rules-current",
        "permit_fingerprint": "permit-current",
        "detail_fingerprint": "detail-current",
        "extraction_fingerprint": app.extraction_fingerprint(),
        "llm_execution_fingerprint": offline_fingerprint,
    }

    monkeypatch.setenv("CLOVA_API_KEY", "now-enabled")
    monkeypatch.setattr(app, "llm_execution_fingerprint", llm_module.llm_execution_fingerprint, raising=False)
    monkeypatch.setattr(app, "policy_fingerprint", lambda: "rules-current")
    monkeypatch.setattr(app, "_related_permit_files", lambda _path: [])
    monkeypatch.setattr(
        app,
        "resolve_submission_permits",
        lambda *_args, **_kwargs: SimpleNamespace(fingerprint="permit-current"),
    )
    monkeypatch.setattr(
        app,
        "resolve_domain_detail_profile",
        lambda *_args: SimpleNamespace(fingerprint="detail-current"),
    )

    assert bridge._artifact_key(pdf, [], None, "회사", "제품") != offline_key
    doc = SimpleNamespace(path=pdf, company="회사", product="제품")
    assert app._status_record_for_doc(
        doc, {"documents": {app._pdf_status_key(pdf): entry}}
    ) is None


def test_status_resolution_passes_product_mapping_provenance(monkeypatch, tmp_path):
    pdf = tmp_path / "sp.pdf"
    pdf.write_bytes(b"sample")
    doc = SimpleNamespace(
        path=pdf,
        company="동국바이오사이언스",
        product="스카이코비원멀티주",
        product_mapping_id="dongkook_unknown_sky_m_to_sky_covione",
    )
    captured = []

    def resolve(*args, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(fingerprint="permit-current")

    monkeypatch.setattr(app, "policy_fingerprint", lambda: "rules-current")
    monkeypatch.setattr(app, "_related_permit_files", lambda _path: [])
    monkeypatch.setattr(app, "resolve_submission_permits", resolve)
    monkeypatch.setattr(
        app,
        "resolve_domain_detail_profile",
        lambda *_args: SimpleNamespace(fingerprint="detail-current"),
    )
    monkeypatch.setattr(app, "llm_execution_fingerprint", lambda: "llm-current", raising=False)
    entry = {
        "cache_version": app.JUDGEMENT_STATUS_CACHE_VERSION,
        "rule_fingerprint": "rules-current",
        "permit_fingerprint": "permit-current",
        "detail_fingerprint": "detail-current",
        "extraction_fingerprint": app.extraction_fingerprint(),
        "llm_execution_fingerprint": "llm-current",
    }

    assert app._status_record_for_doc(
        doc, {"documents": {app._pdf_status_key(pdf): entry}}
    ) is entry
    assert captured == [
        {"product_mapping_id": "dongkook_unknown_sky_m_to_sky_covione"}
    ]


def test_api_failure_does_not_become_completed_from_nonempty_counts(monkeypatch, tmp_path):
    pdf = tmp_path / "sp.pdf"
    pdf.write_bytes(b"dummy")
    stat = pdf.stat()
    entry = {"status": "failed", "summary_counts": {"pass": 26, "fail": 1, "hold": 104, "total": 131},
             "pdf_sig": {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}}
    monkeypatch.setattr(app, "st", SimpleNamespace(session_state={}))
    monkeypatch.setattr(app, "_load_judgement_status_index", lambda: {})
    monkeypatch.setattr(app, "_status_record_for_doc", lambda *args: entry)
    status = app._judgement_status_for_doc(SimpleNamespace(path=pdf))
    assert status["state"] == "failed" and status["summary"] is None
    assert "검수 오류" in app._review_status_html(status)
    assert "검수 완료" not in app._review_status_html(status)
    assert app._review_summary_html(status) == ""


@pytest.mark.parametrize("metadata", [
    {"llm_call_count": 104, "llm_success_count": 0, "llm_last_error": "Connection error."},
    {"llm_call_count": 104, "llm_success_count": 103},
])
def test_final_page_cannot_present_api_failure_as_document_holds(monkeypatch, tmp_path, metadata):
    errors = []
    slot = SimpleNamespace(markdown=lambda *a, **k: None, empty=lambda: None)
    monkeypatch.setattr(bridge, "st", SimpleNamespace(empty=lambda: slot, error=errors.append, button=lambda *a, **k: False))
    artifacts = {"after_result": SimpleNamespace(metadata=metadata)}
    monkeypatch.setattr(bridge, "ensure_judgement_artifacts", lambda **kwargs: artifacts)
    bridge.render_final_judgement_page(selected_doc=SimpleNamespace(path=tmp_path / "sp.pdf"), original_csv_dir=None)
    assert len(errors) == 1 and "CLOVA" in errors[0] and "재시도" in errors[0]


def test_aggregate_cache_key_changes_with_permit_evaluation_schema(monkeypatch, tmp_path):
    pdf = tmp_path / "sp.pdf"
    pdf.write_bytes(b"sample")
    monkeypatch.setattr(bridge, "permit_evaluation_fingerprint", lambda: "permit-schema-one", raising=False)
    before = bridge._artifact_key(pdf, [], None, "company", "product", "details", "resolved")
    assert bridge._artifact_key(pdf, [], None, "company", "product", "details", "resolved") == before
    monkeypatch.setattr(bridge, "permit_evaluation_fingerprint", lambda: "permit-schema-two")
    assert bridge._artifact_key(pdf, [], None, "company", "product", "details", "resolved") != before


def test_previous_aggregate_cache_version_cannot_be_reused(tmp_path):
    import pickle

    cache = tmp_path / "old.pickle"
    cache.write_bytes(pickle.dumps({
        "cache_version": "sp-app-direct-bridge-v104-20260929-negative-result",
        "after_result": SimpleNamespace(metadata={}),
    }))
    with pytest.raises(bridge.JudgementCacheError):
        bridge._read_judgement_cache(cache)


def test_pre_grouped_permit_cache_version_cannot_be_reused(tmp_path):
    import pickle

    cache = tmp_path / "pre-grouped.pickle"
    cache.write_bytes(pickle.dumps({
        "cache_version": "sp-app-direct-bridge-v105-20261005-permit-index",
        "after_result": SimpleNamespace(metadata={}),
    }))
    with pytest.raises(bridge.JudgementCacheError):
        bridge._read_judgement_cache(cache)


def test_bridge_written_status_is_accepted_by_app_reader(monkeypatch, tmp_path):
    status_dir = tmp_path / "status"
    status_index = status_dir / "status_index.json"
    monkeypatch.setattr(bridge, "JUDGEMENT_STATUS_DIR", status_dir)
    monkeypatch.setattr(bridge, "JUDGEMENT_STATUS_INDEX", status_index)
    monkeypatch.setattr(bridge, "llm_execution_fingerprint", lambda: "llm-current", raising=False)

    pdf = tmp_path / "sp.pdf"
    pdf.write_bytes(b"sample")
    bridge._remember_judgement_artifacts({
        "pdf_path": pdf,
        "artifact_key": "artifact-key",
        "summary_before_path": tmp_path / "missing-before.csv",
        "summary_after_path": tmp_path / "missing-after.csv",
        "rule_fingerprint": "rules-current",
        "permit_fingerprint": "permit-current",
        "detail_fingerprint": "detail-current",
        "extraction_fingerprint": bridge.extraction_fingerprint(),
        "llm_execution_fingerprint": "llm-current",
        "company": "회사",
        "product": "제품",
    })

    index = json.loads(status_index.read_text(encoding="utf-8"))
    doc = SimpleNamespace(path=pdf, company="회사", product="제품")
    monkeypatch.setattr(app, "policy_fingerprint", lambda: "rules-current")
    monkeypatch.setattr(app, "_related_permit_files", lambda _path: [])
    monkeypatch.setattr(
        app,
        "resolve_submission_permits",
        lambda *_args, **_kwargs: SimpleNamespace(fingerprint="permit-current"),
    )
    monkeypatch.setattr(
        app,
        "resolve_domain_detail_profile",
        lambda *_args: SimpleNamespace(fingerprint="detail-current"),
    )
    monkeypatch.setattr(app, "llm_execution_fingerprint", lambda: "llm-current", raising=False)

    entry = app._status_record_for_doc(doc, index)

    assert entry is not None
    assert entry["cache_version"] == bridge.JUDGEMENT_CACHE_VERSION


def test_pdf_product_field_takes_precedence_over_filename():
    assert app._extract_product("제 품 명\n다른제품주\n제조번호\n123", "스카이코비원.pdf") == "다른제품주"


@pytest.mark.parametrize("failure", ["exception", "missing_summary", "unknown_document", "llm_failure"])
def test_simulation_never_substitutes_static_results_on_error(monkeypatch, tmp_path, failure):
    pdf = tmp_path / "sp.pdf"
    pdf.write_bytes(b"test")
    missing_permit = tmp_path / "missing-permit.pdf"
    doc = SimpleNamespace(path=pdf, permit_files=[missing_permit], company="회사", product="제품")
    errors, received = [], []
    monkeypatch.setattr(app, "st", SimpleNamespace(session_state={}, error=errors.append))
    monkeypatch.setattr(app, "_find_simulation_csv_dir", lambda _: tmp_path)
    monkeypatch.setattr(app, "_document_from_pdf", lambda _: None if failure == "unknown_document" else doc)
    monkeypatch.setattr(app, "_cached_simulation_artifacts_for_doc", lambda *a, **k: None)

    def generate(**kwargs):
        received.extend(kwargs["permit_paths"])
        if failure == "exception":
            raise ValueError("Invalid MD")
        if failure == "llm_failure":
            return {"after_result": SimpleNamespace(metadata={"llm_last_error": "Connection error."})}
        return {"runtime_csv_dir": tmp_path}

    from concurrent.futures import Future
    def generate_job(**kwargs):
        future = Future()
        try:
            future.set_result(generate(**kwargs))
        except Exception as exc:
            future.set_exception(exc)
        return SimpleNamespace(future=future)
    monkeypatch.setattr(app, "_load_judgement_bridge", lambda: {
        "get_judgement_job": generate_job, "has_runtime_llm_failure": bridge._has_runtime_llm_failure})
    monkeypatch.setattr(app, "get_simulation_job", lambda *a, **k: pytest.fail("Static CSV must not be rendered"))
    assert app._render_inline_simulation("제품", pdf) == 0.0
    assert errors
    if failure == "llm_failure":
        assert "CLOVA" in errors[0]
    if failure != "unknown_document":
        assert received == [missing_permit], "Missing linked permits must not silently become no permit"


def test_final_page_stops_cleanly_when_judgement_fails(monkeypatch, tmp_path):
    errors = []
    slot = SimpleNamespace(markdown=lambda *a, **k: None, empty=lambda: None)
    monkeypatch.setattr(bridge, "st", SimpleNamespace(empty=lambda: slot, error=errors.append, button=lambda *a, **k: False))

    def fail(**kwargs):
        raise ValueError("Invalid MD")

    monkeypatch.setattr(bridge, "ensure_judgement_artifacts", fail)
    bridge.render_final_judgement_page(selected_doc=SimpleNamespace(path=tmp_path / "sp.pdf"), original_csv_dir=None)
    assert len(errors) == 1
    assert "Invalid MD" in errors[0]


@pytest.mark.parametrize("has_document", [True, False])
def test_final_back_navigation_never_rejudges_or_reads_pdf(monkeypatch, tmp_path, has_document):
    class RerunRequested(Exception):
        pass

    state = {"run_sim": True, "selected_pdf_path": "keep-selected", "gmail_logged_in": False}
    query = {"view": "judge_final", "pdf": "expired-cache.pdf"}
    def rerun():
        raise RerunRequested()
    def forbidden(*args, **kwargs):
        pytest.fail("Returning to inbox must not generate judgement or read the PDF")
    monkeypatch.setattr(bridge, "st", SimpleNamespace(session_state=state, query_params=query,
        button=lambda *a, **k: True, rerun=rerun, empty=forbidden, error=forbidden))
    monkeypatch.setattr(bridge, "ensure_judgement_artifacts", forbidden)
    document = SimpleNamespace(path=tmp_path / "not-opened.pdf") if has_document else None
    with pytest.raises(RerunRequested):
        bridge.render_final_judgement_page(selected_doc=document, original_csv_dir=None)
    assert not query
    assert state["run_sim"] is False
    assert state["selected_pdf_path"] == "keep-selected"


def test_overall_csv_includes_md_failure_and_unmapped_tests(monkeypatch, tmp_path):
    from sp_pdf_judger.schemas import Summary
    result = SimpleNamespace(metadata={"rule_fingerprint": "MD-v1"},
                             summary=Summary(passed=3, failed=1, held=1, total=5, comparable_total=5))
    monkeypatch.setattr(bridge, "_is_albumin_document", lambda _: False)
    monkeypatch.setattr(bridge, "build_stage_info_cards", lambda _: [
        SimpleNamespace(display_name="제조단계", passed=2, failed=0, held=0, total=2)])
    path = bridge._write_summary_csv_from_result(result, tmp_path / "summary.csv")
    rows = bridge._read_summary_rows_by_position(path)
    assert bridge._overall_from_rows(rows) == {"pass": 3, "fail": 1, "hold": 1, "total": 5}
    assert next(row for row in rows if row["name"] == "문서 공통·기타 검증")["fail"] == "1"


def test_inconsistent_summary_counts_stop_instead_of_showing_false_totals():
    result = SimpleNamespace(summary=SimpleNamespace(passed=1, failed=0, held=0, total=1))
    with pytest.raises(ValueError, match="초과"):
        bridge._include_document_summary_counts(result, [{"제조명": "중복", "검수합격": 2, "검수불합격": 0, "검수보류": 0, "총 계": 2}])
