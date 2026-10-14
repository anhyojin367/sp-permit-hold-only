# SkyCovione Batched Permit Judgement Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route only the existing Dongkook unknown-product `SKY-M` mapping through SkyCovione deterministic-first judgement and reduce its semantically ambiguous permit checks to at most six groups per grounded CLOVA request.

**Architecture:** Preserve the exact product-mapping provenance from PDF ingestion through permit resolution and final-cache identity. Split grouped judgement into prepare/resolve phases, partition only `ambiguous_llm` preparations into stable bounded batches, and pass every returned item through the existing per-group validator before record fan-out. Keep structural blockers local and fail-closed, preserve all unrelated legacy routes, and distinguish offline/online LLM execution in aggregate caches without invalidating the content-addressed permit index.

**Tech Stack:** Python 3, dataclasses, Pydantic v2, Streamlit, OpenAI-compatible CLOVA client, pytest.

**Spec:** `docs/superpowers/specs/2026-10-08-skycovione-batched-judgement-performance-design.md`

## Global Constraints

- Apply the optimized route only when company is `동국바이오사이언스`, the originally extracted product is `제품명 미확인`, and product/manufacturing number starts with `SKY-M`.
- Store the exact provenance ID `dongkook_unknown_sky_m_to_sky_covione`; never infer it later from the mapped product name.
- The linked permit remains authoritative; section numbers are ignored and deterministic failures cannot be promoted by CLOVA.
- Batch at most 6 groups and at most 60,000 final prompt characters; an oversized single group is sent alone without truncation.
- Missing batch identity, incompatible units or methods, unusable permit evidence, and missing required facts remain HOLD without a provider call.
- Provider 401/403/429, timeout, and connection errors remain operational failures and prevent result publication.
- Do not change any other company, product, mapping, version judgement, catalog policy, or legacy permit flow.
- Do not put prompts, raw provider bodies/errors, full local paths, or API keys in new metadata, fingerprints, or logs.
- Do not globally bump `JUDGEMENT_CACHE_VERSION`; do not change the submission policy ID or OCR mode, so an identical permit index remains warm.
- Do not push this branch to GitHub.

## Review Focus

- An oversized ambiguous group must form one untruncated batch and must not cause an empty batch or partition loop; Task 2 pins this.
- A malformed item with a recognizable requested `group_id` must HOLD only that group, while an unknown/extra/unattributable ID invalidates the whole batch framing; Task 2 pins this.
- An offline completed result must not survive adding a valid CLOVA key, including through the status-index shortcut; Task 5 pins this.
- The mapped and unmapped submission policies must produce different aggregate identities but the same raw permit-index key for identical PDF bytes; Tasks 1 and 5 pin this.
- Duplicate `order_idx` values in expanded table rows must still fan out by `record_position`, never by display order; Task 4 pins this.

---

### Task 1: Preserve exact mapping provenance and scope permit policy routing

**Files:**
- Modify: `sp_document_models.py:6-18`
- Modify: `sp_app.py:20-28, 716-726, 1463-1504, 1593-1612, 1961-1969`
- Modify: `sp_judgement_bridge.py:171-193, 1302-1418, 1482-1657, 2316-2323`
- Modify: `sp_pdf_judger/permit_catalog.py:8-15, 239-311`
- Test: `tests/test_sky_covione_mapping.py`
- Test: `tests/test_permit_catalog.py`
- Test: `tests/test_sky_covione_authoritative_permit.py`
- Test: `tests/test_submission_cache.py`

**Interfaces:**
- Consumes: existing `InboxDocument`, `resolve_submission_permits`, `_artifact_key`, `get_judgement_job`, and `ensure_judgement_artifacts` call paths.
- Produces: `DONGKOOK_UNKNOWN_SKY_M_MAPPING_ID: str`; `InboxDocument.product_mapping_id: str`; `_resolve_product_mapping(company: str, product: str, product_number: str) -> tuple[str, str]`; `resolve_submission_permits(..., package_dir: Path | None = None, *, product_mapping_id: str = "") -> ResolvedPermit`; bridge/job/cache functions with trailing `product_mapping_id: str = ""`.

- [ ] **Step 1: Write failing mapping and isolation tests**

Add tests named:

```python
def test_exact_dongkook_unknown_sky_m_mapping_records_provenance(): ...
def test_product_mapping_provenance_is_empty_for_wrong_company_known_product_or_non_sky_m(): ...
def test_inbox_document_old_constructor_defaults_mapping_id_to_empty(): ...
def test_mapped_submission_link_uses_safe_deterministic_policy_only_with_exact_id(): ...
def test_unknown_mapping_id_and_unrelated_submission_remain_legacy(): ...
def test_submission_fingerprint_covers_effective_policy_and_mapping_id(): ...
def test_bridge_propagates_mapping_id_once_and_includes_it_in_job_identity(): ...
```

Assert all six mapped policy fields exactly: `policy_id="submission-linked"`, `authoritative=True`, `ignore_section_numbers=True`, `require_llm=True`, `ocr_mode="auto"`, `evaluation_mode="deterministic_first"`. Assert the same Dongkook/product/path with empty or unknown mapping ID remains `legacy`, and an unrelated catalog policy is unchanged.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```powershell
python -m pytest tests/test_sky_covione_mapping.py tests/test_permit_catalog.py tests/test_sky_covione_authoritative_permit.py tests/test_submission_cache.py -q
```

Expected: FAIL because `product_mapping_id`, `_resolve_product_mapping`, and the resolver/bridge keyword do not exist, and the mapped explicit permit still resolves to `legacy`.

- [ ] **Step 3: Add the provenance field and exact mapping result**

Append `product_mapping_id: str = ""` to `InboxDocument`. Define the shared constant in `sp_pdf_judger/permit_catalog.py`, import it in `sp_app.py`, and add:

```python
def _resolve_product_mapping(
    company: str,
    product: str,
    product_number: str,
) -> tuple[str, str]: ...
```

Return the mapped product and exact ID only for the three-part predicate. Keep `_map_unknown_sky_covione_product(...)->str` as a compatibility wrapper, and construct `InboxDocument` with both values.

- [ ] **Step 4: Route only that provenance through submission permit resolution**

Extend `resolve_submission_permits` with the keyword-only mapping ID after positional `package_dir`. In the explicit-path branch, select `_safe_sky_covione_policy(policy_id="submission-linked")` only for the exact ID. Expand the submission fingerprint entry with company, product, mapping ID, and the effective policy projection:

```python
{
    "policy_id": policy.policy_id,
    "authoritative": policy.authoritative,
    "ignore_section_numbers": policy.ignore_section_numbers,
    "require_llm": policy.require_llm,
    "ocr_mode": policy.ocr_mode,
    "evaluation_mode": policy.evaluation_mode,
    "product_mapping_id": product_mapping_id,
}
```

Do not alter `resolve_permits` catalog matching or the policy ID/OCR mode used by the permit index.

- [ ] **Step 5: Propagate provenance through app, jobs, status, and artifacts**

Add the trailing keyword to `_artifact_key`, `get_judgement_job`, and `ensure_judgement_artifacts`; normalize it once, pass it into every resolver call and recursive/background call, and include it directly in the artifact-key raw identity. At app call sites use `getattr(doc, "product_mapping_id", "")` for backward compatibility. Pass it from `_status_record_for_doc` to `resolve_submission_permits`, and persist only the safe mapping ID in completed artifact/status metadata.

- [ ] **Step 6: Run the focused tests and confirm GREEN**

Run the Step 2 command.

Expected: all selected tests PASS; fixed-arity resolver mocks are updated to accept the new keyword.

- [ ] **Step 7: Commit the scoped routing change**

```powershell
git add sp_document_models.py sp_app.py sp_judgement_bridge.py sp_pdf_judger/permit_catalog.py tests/test_sky_covione_mapping.py tests/test_permit_catalog.py tests/test_sky_covione_authoritative_permit.py tests/test_submission_cache.py
git commit -m "feat: preserve SkyCovione mapping provenance"
```

### Task 2: Define the bounded batch protocol and pure partition/validation layer

**Files:**
- Create: `sp_pdf_judger/permit_group_batching.py`
- Modify: `sp_pdf_judger/permit_llm_protocol.py:31-50, 202-282`
- Create: `tests/test_permit_group_batching.py`
- Modify: `tests/test_permit_llm_protocol.py`

**Interfaces:**
- Consumes: current `GroundedGroupResponse` and group/candidate/condition/fact/execution payload builders.
- Produces: `PERMIT_GROUP_BATCH_PROTOCOL_VERSION = "permit-group-batch-v1"`; `GroundedGroupBatchItem`; `GroundedGroupBatchWireItem`; `GroundedGroupBatchResponse`; `build_group_payload(...) -> dict[str, Any]`; `build_group_batch_prompt(requests: Sequence[PermitGroupBatchRequest]) -> str`; `PermitGroupBatchRequest`; `IndexedBatchItem`; `make_group_batch_request(*, group, candidates, conditions, executions) -> PermitGroupBatchRequest`; `partition_group_batch_requests(...)`; `index_group_batch_response(...)`.

- [ ] **Step 1: Write failing protocol, partition, and envelope tests**

Add tests named:

```python
def test_partition_is_stable_and_never_exceeds_six_groups(): ...
def test_partition_measures_the_final_prompt_against_60000_characters(): ...
def test_oversized_single_group_is_emitted_once_without_truncation(): ...
def test_batch_prompt_preserves_independent_group_ids_and_protocol_version(): ...
def test_batch_index_accepts_exact_unique_coverage(): ...
def test_missing_or_duplicate_known_id_invalidates_only_that_group(): ...
def test_recognizable_malformed_item_invalidates_only_that_group(): ...
def test_extra_unknown_or_unattributable_id_invalidates_the_batch(): ...
```

Assert the 7th normal request starts a second batch; every multi-item prompt is `<= 60_000` characters; an oversized singleton's `serialized_payload` is byte-for-byte unchanged. For response framing, valid siblings remain indexable after a requested item is missing, duplicated, or malformed with a recognizable ID; an extra/empty/non-string ID yields an error entry for every requested group.

- [ ] **Step 2: Run the new tests and confirm RED**

Run:

```powershell
python -m pytest tests/test_permit_group_batching.py tests/test_permit_llm_protocol.py -q
```

Expected: FAIL because the batch models, canonical group payload, and batching module do not exist.

- [ ] **Step 3: Expose one canonical group payload and batch wire models**

Refactor `build_group_prompt` to call:

```python
def build_group_payload(*, group, candidates, conditions, executions) -> dict[str, Any]: ...
```

Add a strict `GroundedGroupBatchItem(GroundedGroupResponse)` with `group_id: str`, plus this lenient known-field model so a recognizable malformed item can be rejected independently after outer parsing:

```python
class GroundedGroupBatchWireItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: Any = None
    selected_evidence_id: Any = None
    decisions: Any = None
    status: Any = None
    reason: Any = None

class GroundedGroupBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[GroundedGroupBatchWireItem]
```

Include `PERMIT_GROUP_BATCH_PROTOCOL_VERSION` literally in every batch prompt so the existing complete-request response cache changes with protocol semantics.

- [ ] **Step 4: Implement canonical requests and greedy prompt-bounded partitioning**

In `permit_group_batching.py` define:

```python
MAX_GROUPS_PER_BATCH = 6
MAX_BATCH_PROMPT_CHARS = 60_000

@dataclass(frozen=True)
class PermitGroupBatchRequest:
    group_id: str
    payload: dict[str, Any]
    serialized_payload: str

@dataclass(frozen=True)
class IndexedBatchItem:
    response: GroundedGroupResponse | None
    error_code: str | None

def make_group_batch_request(
    *,
    group: Any,
    candidates: Sequence[Any],
    conditions: Sequence[Any],
    executions: Sequence[Any],
) -> PermitGroupBatchRequest: ...

def partition_group_batch_requests(
    requests: Sequence[PermitGroupBatchRequest],
    *,
    max_groups: int = MAX_GROUPS_PER_BATCH,
    max_prompt_chars: int = MAX_BATCH_PROMPT_CHARS,
) -> tuple[tuple[PermitGroupBatchRequest, ...], ...]: ...
```

Serialize each payload with `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`. Greedily append in input order only when `len(build_group_batch_prompt(candidate_batch))` remains within the prompt limit; emit one over-budget request alone and never slice its text.

- [ ] **Step 5: Implement per-ID envelope indexing**

Define:

```python
def index_group_batch_response(
    requests: Sequence[PermitGroupBatchRequest],
    response: GroundedGroupBatchResponse | None,
) -> dict[str, IndexedBatchItem]: ...
```

Validate each recognizable wire item with `GroundedGroupBatchItem.model_validate`, then convert it to the existing `GroundedGroupResponse`. Missing/duplicate recognized requested IDs affect only those IDs. An extra, empty, non-string, or otherwise unattributable group ID is a framing error and returns no authorized response for any requested group. Do not validate evidence/fact/condition membership here; Task 3 deliberately delegates that to the existing authoritative validator.

- [ ] **Step 6: Run protocol tests and confirm GREEN**

Run the Step 2 command.

Expected: all selected tests PASS.

- [ ] **Step 7: Commit the pure batch layer**

```powershell
git add sp_pdf_judger/permit_group_batching.py sp_pdf_judger/permit_llm_protocol.py tests/test_permit_group_batching.py tests/test_permit_llm_protocol.py
git commit -m "feat: add grounded permit group batching protocol"
```

### Task 3: Split grouped judgement into prepare/resolve phases and add one-shot batch transport

**Files:**
- Modify: `sp_pdf_judger/permit_group_judgement.py:18-48, 941-1080`
- Modify: `sp_pdf_judger/judgement.py:2500-2655`
- Modify: `sp_pdf_judger/llm.py:1-25, 56-139`
- Modify: `tests/test_permit_group_judgement.py`
- Modify: `tests/test_permit_llm_protocol.py`
- Modify: `tests/test_clova_service_failure.py`

**Interfaces:**
- Consumes: Task 2's `PermitGroupBatchRequest`, batch prompt, and batch response envelope; current `execute_permit_conditions`, `classify_group`, and `validate_group_response` remain authoritative.
- Produces: `PreparedPermitGroup`; `JudgeEngine.prepare_permit_group(...)`; `JudgeEngine.resolve_prepared_permit_group(...)`; compatibility `JudgeEngine.judge_permit_group(...)`; `ClovaJudgeClient.explain_permit_group_batch_once(...)`.

- [ ] **Step 1: Write failing preparation and transport tests**

Add tests named:

```python
def test_prepare_group_performs_no_provider_call_and_returns_all_grounding_inputs(): ...
def test_prepare_returns_immediate_verdict_for_deterministic_and_structural_hold(): ...
def test_resolve_valid_batch_item_matches_single_group_validator(): ...
def test_cross_group_evidence_condition_or_fact_ids_hold_only_that_group(): ...
def test_existing_single_group_wrapper_remains_compatible(): ...
def test_batch_client_makes_one_provider_attempt_for_six_requests(): ...
def test_batch_client_generic_parse_failure_returns_none_without_retry(): ...
def test_batch_client_service_error_propagates(): ...
```

For swapped evidence, condition, and fact IDs, assert `HOLD_LABEL` and the existing validator's grounding reason codes; never accept the provider's declared status directly.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```powershell
python -m pytest tests/test_permit_group_judgement.py tests/test_permit_llm_protocol.py tests/test_clova_service_failure.py -q
```

Expected: FAIL because preparation/resolution and batch client methods do not exist.

- [ ] **Step 3: Add the immutable prepared-group contract**

In `permit_group_judgement.py` add:

```python
@dataclass(frozen=True)
class PreparedPermitGroup:
    group: PermitGroup
    candidates: tuple[PermitChunk, ...]
    conditions: tuple[PermitCondition, ...]
    executions: tuple[ConditionExecution, ...]
    gate: GateDecision
    immediate_verdict: GroupVerdict | None
```

- [ ] **Step 4: Extract current JudgeEngine logic without changing verdict semantics**

Add:

```python
def prepare_permit_group(
    self,
    group: PermitGroup,
    conditions_by_evidence_id: Mapping[str, Sequence[PermitCondition]],
) -> PreparedPermitGroup: ...

def resolve_prepared_permit_group(
    self,
    prepared: PreparedPermitGroup,
    response: GroundedGroupResponse | None,
    *,
    missing_reason: str = "llm_response_unavailable",
) -> GroupVerdict: ...
```

Move store preflight, candidate lookup, candidate-page OCR checks, condition lookup, deterministic execution, gate classification, and immediate deterministic/HOLD construction into preparation. Resolution returns `immediate_verdict` unchanged or calls `validate_group_response`; malformed/cross-linked responses become a fail-closed HOLD. Rebuild `judge_permit_group` as a singleton compatibility wrapper over these methods and `explain_permit_group_once`.

- [ ] **Step 5: Add the one-provider-call batch client**

Add:

```python
def explain_permit_group_batch_once(
    self,
    *,
    requests: Sequence[PermitGroupBatchRequest],
) -> GroundedGroupBatchResponse | None: ...
```

For a nonempty batch, increment `call_count` once and call `request_structured_response` once with `use_schema=True` and `max_provider_attempts=1`. Re-raise `ClovaServiceError`; return `None` for a generic parse/schema error. Audit only each `group_id`, event, and sanitized exception type—never prompt or body. Keep the current singleton method unchanged for compatibility.

- [ ] **Step 6: Run focused judgement and transport tests and confirm GREEN**

Run the Step 2 command.

Expected: all selected tests PASS and the old singleton tests still pass.

- [ ] **Step 7: Commit the preparation and batch transport change**

```powershell
git add sp_pdf_judger/permit_group_judgement.py sp_pdf_judger/judgement.py sp_pdf_judger/llm.py tests/test_permit_group_judgement.py tests/test_permit_llm_protocol.py tests/test_clova_service_failure.py
git commit -m "refactor: prepare permit groups before provider calls"
```

### Task 4: Coordinate batches in the deterministic-first pipeline

**Files:**
- Modify: `sp_pdf_judger/pipeline.py:766-982, 984-1035, 2460-2630`
- Modify: `tests/test_deterministic_first_pipeline.py`
- Modify: `tests/test_permit_index_pipeline.py`

**Interfaces:**
- Consumes: Tasks 2-3 preparation, partitioning, client, response indexing, and resolution interfaces.
- Produces: two-pass `_judge_deterministic_first_tests`; safe metadata `permit_group_batch_count`, `permit_group_batch_sizes`, classification counts, provider calls, and estimated saved calls; stable progress text `허가서 의미 판정 묶음 n/N`.

- [ ] **Step 1: Write failing coordinator and fan-out tests**

Add tests named:

```python
def test_92_group_shape_uses_nine_batches_and_preserves_classification_counts(): ...
def test_deterministic_and_structural_hold_groups_make_zero_provider_calls(): ...
def test_disabled_client_holds_ambiguous_groups_without_attempting_batches(): ...
def test_batch_service_failure_aborts_result_publication(): ...
def test_duplicate_order_idx_rows_fan_out_by_distinct_record_position(): ...
def test_non_target_legacy_policy_does_not_use_batch_client(): ...
def test_batch_progress_and_metadata_are_safe_and_bounded(): ...
```

Build the measured synthetic shape: 92 groups = 11 deterministic, 53 ambiguous, 27 structural HOLD, and 1 genuine no-candidate primary fallback. Assert exactly 9 calls and sizes `[6, 6, 6, 6, 6, 6, 6, 6, 5]`; classification counts sum to 92; the legacy path output remains unchanged.

- [ ] **Step 2: Run the focused pipeline tests and confirm RED**

Run:

```powershell
python -m pytest tests/test_deterministic_first_pipeline.py tests/test_permit_index_pipeline.py -q
```

Expected: FAIL because the current loop calls the singleton provider once per ambiguous group and emits no batch metadata/progress.

- [ ] **Step 3: Implement the preparation pass**

Keep normalization, primary judgements, grouping, and genuine no-candidate fallback unchanged. For every remaining group, call `prepare_permit_group`; immediately fan out deterministic/HOLD verdicts and queue only preparations whose `gate.path == "ambiguous_llm"`. Count classification paths at preparation time so provider unavailability cannot hide how many groups were genuinely semantic.

- [ ] **Step 4: Implement the batch execution and resolution pass**

Convert queued preparations to requests in original group order, partition them, and emit `허가서 의미 판정 묶음 {index}/{total}` before each provider call. For each indexed response, call `resolve_prepared_permit_group`, passing its `error_code` as `missing_reason` when no valid item exists. If the client is unavailable, resolve every queued preparation with `llm_unavailable` and make zero calls. Let `ClovaServiceError` escape so the bridge preserves the existing retry/no-publication contract.

- [ ] **Step 5: Preserve record fan-out and add safe aggregate metadata**

Reuse `_group_evaluation` and `record_position`; do not fan out by `order_idx`. Add:

```python
{
    "permit_group_batch_count": attempted_batch_count,
    "permit_group_batch_sizes": attempted_batch_sizes,
    "permit_group_path_counts": {
        "deterministic": ...,
        "ambiguous_llm": ...,
        "hold_without_llm": ...,
        "primary_fallback": ...,
    },
}
```

Retain provider-call delta and estimated-saved-call fields. Extend `_safe_group_provider_audit` only with numeric batch index/size if needed; do not persist prompts, bodies, local paths, or raw error messages.

- [ ] **Step 6: Run the focused pipeline tests and confirm GREEN**

Run the Step 2 command.

Expected: all selected tests PASS, including zero calls for deterministic/HOLD paths and unchanged legacy behavior.

- [ ] **Step 7: Commit the pipeline coordinator**

```powershell
git add sp_pdf_judger/pipeline.py tests/test_deterministic_first_pipeline.py tests/test_permit_index_pipeline.py
git commit -m "feat: batch ambiguous permit group judgements"
```

### Task 5: Make final-result caches sensitive to LLM execution semantics and verify end to end

**Files:**
- Modify: `sp_pdf_judger/llm.py:1-90`
- Modify: `sp_judgement_bridge.py:23-31, 171-193, 603-623, 1340-1657`
- Modify: `sp_app.py:20-33, 1593-1612`
- Modify: `tests/test_submission_cache.py`
- Modify: `tests/test_failed_judgement_cache.py`
- Modify: `tests/test_sky_covione_authoritative_permit.py`
- Modify: `tests/test_permit_index.py`
- Modify: `tests/test_simulation_input_identity.py`

**Interfaces:**
- Consumes: Task 2's `PERMIT_GROUP_BATCH_PROTOCOL_VERSION`, Task 1's mapping-aware permit fingerprint and bridge identity, and existing environment resolution used by `ClovaJudgeClient`.
- Produces: `llm_execution_fingerprint() -> str`; completed artifact/status field `llm_execution_fingerprint`; status-index rejection when current execution semantics differ.

- [ ] **Step 1: Write failing execution-fingerprint and cache-boundary tests**

Add tests named:

```python
def test_llm_execution_fingerprint_changes_for_enabled_model_endpoint_or_protocol(): ...
def test_llm_execution_fingerprint_ignores_api_key_value_and_never_exposes_secret_or_url(): ...
def test_offline_artifact_and_status_are_rejected_after_clova_becomes_enabled(): ...
def test_artifact_key_changes_for_mapping_or_llm_execution_but_not_secret_rotation(): ...
def test_mapped_and_legacy_submission_policy_share_identical_permit_index_key(): ...
def test_completed_artifact_metadata_contains_only_safe_execution_fingerprint(): ...
```

Use two different nonempty fake keys and assert equal fingerprints; change empty/nonempty key, model, endpoint, and protocol one at a time and assert inequality. Assert neither key nor plain endpoint occurs in the fingerprint, status record, artifacts, or metadata. For identical permit bytes, `submission-linked/legacy` and mapped `submission-linked/deterministic_first` must have the same permit-index key.

- [ ] **Step 2: Run cache tests and confirm RED**

Run:

```powershell
python -m pytest tests/test_submission_cache.py tests/test_failed_judgement_cache.py tests/test_sky_covione_authoritative_permit.py tests/test_permit_index.py tests/test_simulation_input_identity.py -q
```

Expected: FAIL because LLM execution identity is absent from artifact/status reuse checks.

- [ ] **Step 3: Implement a non-secret LLM execution fingerprint**

Add:

```python
def llm_execution_fingerprint() -> str: ...
```

Resolve key/model/base URL with the same dotenv, placeholder-key, and defaults semantics as `ClovaJudgeClient`. Hash canonical JSON containing only `enabled: bool`, model name, `sha256(normalized_base_url)`, and `PERMIT_GROUP_BATCH_PROTOCOL_VERSION`; return a digest, never the canonical payload. Refactor shared environment normalization so the client and fingerprint cannot drift. Key rotation between two nonempty keys must not alter the digest.

- [ ] **Step 4: Apply the fingerprint at every aggregate-cache shortcut**

Include `llm_execution_fingerprint()` in `_artifact_key` without bumping the global cache version. Persist the digest in completed artifacts/status and service-failure status. In `_status_record_for_doc`, compare the stored digest to the current digest before reusing `summary_after.csv`; keep failed-attempt explicit-retry behavior unchanged.

- [ ] **Step 5: Run focused cache tests and the whole scoped regression matrix**

Run:

```powershell
python -m pytest tests/test_sky_covione_mapping.py tests/test_permit_catalog.py tests/test_permit_group_batching.py tests/test_permit_llm_protocol.py tests/test_permit_group_judgement.py tests/test_deterministic_first_pipeline.py tests/test_clova_service_failure.py tests/test_submission_cache.py tests/test_failed_judgement_cache.py tests/test_sky_covione_authoritative_permit.py tests/test_permit_index.py tests/test_permit_index_pipeline.py tests/test_final_review_progress.py tests/test_simulation_input_identity.py -q
```

Expected: all selected tests PASS; the synthetic 53-ambiguous-group run reports no more than 9 batch provider calls.

- [ ] **Step 6: Run the full offline suite and repository checks**

Run:

```powershell
python -m pytest -q
git diff --check
git status --short --branch
```

Expected: full suite PASS with only documented skips; `git diff --check` is clean; only the intended tracked changes plus the pre-existing `.pytest-tmp-*` untracked directories are present.

- [ ] **Step 7: Commit cache correctness and final tests**

```powershell
git add sp_pdf_judger/llm.py sp_judgement_bridge.py sp_app.py tests/test_submission_cache.py tests/test_failed_judgement_cache.py tests/test_sky_covione_authoritative_permit.py tests/test_permit_index.py tests/test_simulation_input_identity.py
git commit -m "fix: separate offline and batched judgement caches"
```

- [ ] **Step 8: Perform one optional live smoke run only after offline verification**

Run the user's same SP/permit through the local Streamlit flow once with CLOVA enabled. Record elapsed time, `permit_group_provider_calls`, `permit_group_batch_count`, batch sizes, and final PASS/FAIL/HOLD counts without saving prompts, raw responses, API keys, or full paths. Acceptance is at most 10 after-phase calls, not a guaranteed wall-clock duration. Do not push.
