# Permit Index and Grouped Judgement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 허가서를 한 번만 추출·OCR·구조화해 영구 재사용하고, 같은 제조단계·시험·배치의 SP 근거를 묶어 코드로 확정 가능한 조건은 결정론적으로 판정하며 애매한 그룹만 CLOVA를 한 번 호출한다.

**Architecture:** 허가서 해시와 처리 버전으로 주소화한 사용자별 JSON 인덱스가 페이지·문단·원문 위치·구조화 조건을 원자적으로 보존한다. 스카이코비원멀티주의 `deterministic_first` 정책만 새 그룹 판정 경로를 사용하고, 명시적 단계·배치 근거와 허가 조건의 완전성을 확인한 뒤 `deterministic`, `ambiguous_llm`, `hold_without_llm` 중 하나로 라우팅한다. 다른 정책은 기존 레코드 단위 경로를 그대로 사용한다.

**Tech Stack:** Python 3.10+, PyMuPDF, Pydantic 2, Decimal, Windows Media OCR/Tesseract, UTF-8 JSON, pytest, 기존 `artifact_process_lock`.

**Spec:** `docs/superpowers/specs/2026-09-30-permit-index-grouped-judgement-design.md`

## Global Constraints

- GitHub 푸시, 원격 브랜치 업로드, PR 생성은 하지 않는다. 작업 중 커밋은 격리된 로컬 작업공간에만 만든다.
- 영구 캐시는 기본 `%LOCALAPPDATA%\SPValidator\permit_index`, 재정의는 `SP_PERMIT_INDEX_DIR`만 사용하며 저장소와 `%TEMP%`를 기본값으로 쓰지 않는다.
- 캐시에는 허가서 원문·구조·조건만 저장하고 SP 결과, API 키, CLOVA 응답과 사용자 계정 정보는 저장하지 않는다.
- 판정 변경은 `PermitPolicy.evaluation_mode == "deterministic_first"`인 스카이코비원멀티주에만 활성화한다.
- 다른 회사·제품, 제품 매핑, 제출 버전 표시, 4.0 안내 후 진행, 제조정보, 회귀 로그와 비권위 허가 흐름은 변경하지 않는다.
- 제조단계·배치가 없거나 충돌하면 다른 레코드와 묶지 않고 `hold_without_llm`으로 보류한다.
- 결정론적 불합격을 CLOVA가 뒤집을 수 없고, 애매한 한 그룹의 provider 요청은 재시도·repair 없이 최대 한 번이다.
- 화면의 실제 제출 기준·결과·페이지와 기존 시험 카드는 그대로 유지한다.
- 신규 테스트와 전체 테스트는 네트워크/API 호출 없이 실행한다.

## Review Focus

1. 같은 PDF 바이트가 다른 파일명으로 연결돼도 인덱스는 재사용하되 화면 근거 파일명은 현재 연결 파일이어야 한다. Task 3의 이름 변경 재사용 테스트가 이를 고정한다.
2. 허가 해석 후 인덱스 게시 전 PDF 바이트가 바뀌는 TOCTOU 상황은 게시·판정을 중단하고 보류해야 한다. Task 3의 해시 재검증 테스트가 이를 고정한다.
3. 같은 단계에 제조번호 정보 레코드가 둘 이상이거나 관련 원료 번호가 섞이면 가까운 값을 추측하지 않고 충돌로 보류해야 한다. Task 5의 배치 인벤토리 테스트가 이를 고정한다.
4. 일부 수치 조건은 통과하지만 같은 허가 문단의 규범 문장이 구조화되지 않은 경우 전체 합격이 되면 안 된다. Task 6의 부분 구조화 테스트가 이를 고정한다.
5. CLOVA의 JSON 오류·429·grounding 오류가 기존 `explain()`의 재시도나 repair 경로로 흘러 그룹당 두 번 이상 호출되면 안 된다. Task 6의 provider 호출 수 테스트가 이를 고정한다.

---

### Task 1: 정책 활성화와 허가 해시 단일 계산

**Files:**
- Modify: `sp_pdf_judger/permit_catalog.py`
- Modify: `sp_pdf_judger/permits/catalog.json`
- Test: `tests/test_permit_catalog.py`
- Test: `tests/test_sky_covione_authoritative_permit.py`

**Interfaces:**
- Produces: `PermitPolicy(..., evaluation_mode: str = "legacy")`.
- Produces: `ResolvedPermit.content_hashes: tuple[tuple[Path, str], ...]`.
- Produces: `permit_hash_map(resolution: ResolvedPermit) -> dict[Path, str]`.
- Preserves: the existing five positional `PermitPolicy` arguments and all existing `ResolvedPermit` callers through trailing defaults.

- [ ] **Step 1: Write failing policy and hash-reuse tests**

Add tests asserting:

```python
def test_only_sky_covione_catalog_policy_is_deterministic_first(...):
    assert target.policy.evaluation_mode == "deterministic_first"
    assert wrong_company.policy is None

def test_resolved_permit_exposes_one_precomputed_hash_per_valid_path(...):
    assert dict(result.content_hashes)[permit.resolve()] == hashlib.sha256(b"permit").hexdigest()
    assert sha_calls[permit.resolve()] == 1
```

Also pin that a manually constructed five-argument `PermitPolicy` still has `evaluation_mode == "legacy"`.

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_permit_catalog.py tests/test_sky_covione_authoritative_permit.py -k "evaluation_mode or precomputed_hash or five_argument"`

Expected: missing `evaluation_mode`/`content_hashes` assertions fail.

- [ ] **Step 3: Implement additive policy mode and retained hashes**

Use keyword construction inside `resolve_permits()` and `resolve_submission_permits()`. Reuse the catalog validation digest instead of hashing the same catalog file again, calculate each explicit valid path once, and feed the same hash tuple into the existing fingerprint. Add `"evaluation_mode": "deterministic_first"` only to the SkyCovione catalog entry.

- [ ] **Step 4: Run focused catalog regressions**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_permit_catalog.py tests/test_sky_covione_mapping.py tests/test_sky_covione_authoritative_permit.py`

Expected: all pass without network calls.

- [ ] **Step 5: Create a local-only checkpoint**

Commit locally if the isolated worktree permits it; do not push:

```powershell
git add sp_pdf_judger/permit_catalog.py sp_pdf_judger/permits/catalog.json tests/test_permit_catalog.py tests/test_sky_covione_authoritative_permit.py
git commit -m "feat: add deterministic permit policy mode"
```

### Task 2: 안정적인 허가 모델과 조건 IR

**Files:**
- Create: `sp_pdf_judger/permit_models.py`
- Create: `sp_pdf_judger/permit_conditions.py`
- Modify: `sp_pdf_judger/permit_pdf_store.py`
- Modify: `sp_pdf_judger/permit_source_ownership.py`
- Create: `tests/test_permit_conditions.py`
- Modify: `tests/test_permit_pdf_store.py`
- Modify: `tests/test_permit_source_ownership.py`

**Interfaces:**
- Produces/re-exports: existing `PermitChunk` from `permit_models.py`; `from sp_pdf_judger.permit_pdf_store import PermitChunk` remains valid.
- Produces: `CONDITION_PARSER_SCHEMA_VERSION: str`.
- Produces: `PermitCondition(condition_id, evidence_id, clause_index, kind, operator, value, unit, lower, upper, lower_inclusive, upper_inclusive, children, quote, stage_key, test_key)` with Decimal values serialized as strings.
- Produces: `parse_permit_conditions(chunk: PermitChunk) -> tuple[PermitCondition, ...]`.
- Produces: `validate_permit_ownership_snapshot(documents, chunks) -> tuple[str, ...]` without rereading PDF bytes.

- [ ] **Step 1: Write failing IR and source-identity tests**

Cover minimum/maximum, inclusive/exclusive range, `10~11일`, `최소 10개`, `36 ± 2 ℃`, `3일`, `80% 이상`, and explicit negative qualitative clauses. For the egg-inoculation paragraph assert all conditions receive stable IDs and quotes. Add:

```python
def test_unknown_normative_clause_is_preserved_as_unparsed():
    conditions = parse_permit_conditions(chunk("별도 검증절차를 충족하여야 한다."))
    assert [item.kind for item in conditions] == ["unparsed"]

def test_hashed_document_identity_does_not_depend_on_display_filename():
    assert doc("old.pdf", sha).evidence_id == doc("renamed.pdf", sha).evidence_id
```

For synthetic documents without a hash, retain filename-based identity to avoid unrelated fixture collisions.

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_permit_conditions.py tests/test_permit_source_ownership.py tests/test_permit_pdf_store.py`

Expected: import failures for new modules/functions.

- [ ] **Step 3: Move `PermitChunk` without changing behavior**

Move only the frozen dataclass to `permit_models.py`, import/re-export it from `permit_pdf_store.py`, and keep every field/default unchanged. Change hashed `PermitSourceDocument.evidence_id` to use PDF hash plus pages rather than display filename; retain the legacy fallback when `pdf_sha256` is empty. Extract ownership validation into `validate_permit_ownership_snapshot()` and let the existing inventory wrapper call it plus its path check.

- [ ] **Step 4: Implement conservative condition parsing**

Use `Decimal`, `criteria_parser.parse_criteria_text`, `unit_normalizer.parse_number_and_unit`, and `policy_time.complete_duration_seconds`. Recognize only the kinds fixed by the tests; every remaining normative `이어야/되어야/없어야/않아야/해야/하여야` clause becomes `unparsed`. Do not silently discard a clause or decide applicability here.

- [ ] **Step 5: Run focused parsing and ownership tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_permit_conditions.py tests/test_permit_source_ownership.py tests/test_permit_pdf_store.py tests/test_permit_llm_protocol.py`

Expected: all pass; legacy permit imports and clause parsing remain compatible.

- [ ] **Step 6: Create a local-only checkpoint**

```powershell
git add sp_pdf_judger/permit_models.py sp_pdf_judger/permit_conditions.py sp_pdf_judger/permit_pdf_store.py sp_pdf_judger/permit_source_ownership.py tests/test_permit_conditions.py tests/test_permit_pdf_store.py tests/test_permit_source_ownership.py
git commit -m "feat: structure permit conditions with stable evidence"
```

Do not push.

### Task 3: 콘텐츠 주소형 영구 허가 인덱스

**Files:**
- Create: `sp_pdf_judger/permit_index.py`
- Modify: `sp_pdf_judger/permit_ocr.py`
- Modify: `sp_pdf_judger/permit_pdf_store.py`
- Modify: `sp_pdf_judger/permit_source_ownership.py`
- Create: `tests/test_permit_index.py`
- Create: `tests/helpers/permit_index_worker.py`
- Modify: `tests/test_permit_ocr.py`
- Modify: `tests/test_permit_pdf_store.py`

**Interfaces:**
- Produces: `PERMIT_INDEX_SCHEMA_VERSION`, `OCR_IMPLEMENTATION_VERSION`, `PERMIT_PARSER_SCHEMA_VERSION`.
- Produces: `PermitIndexKey(value: str, pdf_sha256: str)`.
- Produces: `PermitIndexSnapshot(pages, chunks, source_documents, conditions, extraction_errors)`.
- Produces: `PermitIndexLoad(snapshot, cache_hit, key, schema_version, elapsed_ms, ocr_page_count)`.
- Produces: `permit_index_root() -> Path` and `build_permit_index_key(...) -> PermitIndexKey`.
- Produces: `load_or_build_permit_index(*, pdf_path, pdf_sha256, policy, builder) -> PermitIndexLoad`.
- Changes: `extract_permit_page_texts(..., *, pdf_sha256: str | None = None, use_page_cache: bool = True)`; legacy callers keep defaults.
- Changes: `PermitPdfStore(..., *, pdf_sha256_by_path: Mapping[Path, str] | None = None, permit_index_loader=load_or_build_permit_index)`.

- [ ] **Step 1: Write failing cold/warm, invalidation and corruption tests**

Pin these behaviors:

```python
def test_fresh_second_store_skips_page_extractor_ocr_condition_and_chunk_parser(...):
    assert first_counts == {"extract": 1, "parse": 1, "conditions": 1}
    assert second_counts == {"extract": 0, "parse": 0, "conditions": 0}
    assert warm.snapshot == cold.snapshot

def test_same_bytes_new_name_reuses_index_and_rebinds_current_display_name(...): ...
def test_source_changed_after_resolution_is_not_published(...): ...
```

Parametrize key invalidation for PDF SHA, OCR mode/language/DPI/implementation version, chunk parser version, condition parser version and structure-affecting policy projection. Parametrize invalid JSON, wrong hash/page count, broken span/evidence/parent/condition IDs. Add a two-process test proving one builder execution and waiter reread.

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_permit_index.py tests/test_permit_ocr.py tests/test_permit_pdf_store.py`

Expected: missing index API and warm-path assertions fail.

- [ ] **Step 3: Implement root, key, lock and atomic JSON publication**

Use `SP_PERMIT_INDEX_DIR`, otherwise `%LOCALAPPDATA%\SPValidator\permit_index`; on non-Windows use the user-specific application cache directory. Keep stable locks under `<root>/.locks/<key>.lock`, outside replaceable entry directories. Under the lock perform recheck → build → validate → unique temporary write → flush/fsync → JSON reread validation → `os.replace()`.

- [ ] **Step 4: Implement complete typed serialization and warm reconstruction**

Serialize every page, source document, `PermitChunk`, `PermitSourceSpan`, condition and extraction error. Validate via `validate_permit_ownership_snapshot()` plus condition-to-chunk references. Rebind only runtime display filename when identical bytes move; content-derived evidence IDs stay stable. Recompute the current file hash immediately before publication and reject a TOCTOU mismatch.

- [ ] **Step 5: Integrate the index above OCR/parsing**

On a miss, `PermitPdfStore` builds a temporary store from page texts, parses conditions, and returns `PermitIndexSnapshot`. On a hit it restores ordinary store lists and diagnostics without calling `extract_permit_page_texts()` or `_parse_page_texts()`. Pass the precomputed hash to OCR and set `use_page_cache=False` during index builds; retain the old `%TEMP%` cache only for direct legacy callers.

- [ ] **Step 6: Run index, OCR, ownership and process-lock tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_permit_index.py tests/test_permit_ocr.py tests/test_permit_pdf_store.py tests/test_permit_source_ownership.py tests/test_review_process_lock.py`

Expected: all pass; two-process builder count is exactly one.

- [ ] **Step 7: Create a local-only checkpoint**

```powershell
git add sp_pdf_judger/permit_index.py sp_pdf_judger/permit_ocr.py sp_pdf_judger/permit_pdf_store.py sp_pdf_judger/permit_source_ownership.py tests/test_permit_index.py tests/helpers/permit_index_worker.py tests/test_permit_ocr.py tests/test_permit_pdf_store.py
git commit -m "feat: persist content-addressed permit index"
```

Do not push.

### Task 4: 파이프라인의 따뜻한 경로와 캐시 감사정보

**Files:**
- Modify: `sp_pdf_judger/pipeline.py`
- Modify: `sp_pdf_judger/policy_engine.py`
- Modify: `sp_judgement_bridge.py`
- Modify: `tests/test_submission_cache.py`
- Modify: `tests/test_sky_covione_authoritative_permit.py`
- Create: `tests/test_permit_index_pipeline.py`

**Interfaces:**
- Changes: `PolicyContext(..., permit_store: PermitPdfStore | None = None)` reuses the pipeline store and its indexed page texts.
- Produces: `permit_evaluation_fingerprint() -> str`, combining permit-index/chunk/condition schema versions for final-artifact invalidation.
- Produces: sanitized per-file `PermitPdfStore.index_diagnostics`.
- Produces metadata: `permit_index_cache_hit`, key/schema, elapsed/OCR counts and existing `permit_extraction_diagnostics` extensions.

- [ ] **Step 1: Write failing pipeline reuse and artifact invalidation tests**

Assert that two fresh pipelines using the same permit have extractor/parser call counts `1` then `0`; `PolicyContext` does not create a second store or reopen permit pages; metadata contains hit/key/schema/timing/counts without full local path or provider errors. Assert `_artifact_key()` changes when `permit_evaluation_fingerprint()` changes.

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_permit_index_pipeline.py tests/test_submission_cache.py tests/test_sky_covione_authoritative_permit.py`

Expected: duplicate store creation and missing metadata/fingerprint assertions fail.

- [ ] **Step 3: Pass resolved hashes and reuse one permit store**

Construct `PermitPdfStore` with `permit_hash_map(self.permit_resolution)`. Pass that same store into `PolicyContext`; build `permit_pages` from its indexed `source_documents` rather than reopening the permit PDF. Keep SP page extraction and all policy evaluation order unchanged.

- [ ] **Step 4: Add sanitized diagnostics and invalidate old final artifacts**

Add aggregate and per-permit metadata, using only `path.name` in new diagnostic fields. Bump `JUDGEMENT_CACHE_VERSION` and include `permit_evaluation_fingerprint()` in `_artifact_key()`.

- [ ] **Step 5: Run warm-pipeline and cache regressions**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_permit_index_pipeline.py tests/test_submission_cache.py tests/test_failed_judgement_cache.py tests/test_sky_covione_authoritative_permit.py tests/test_pipeline_structure.py`

Expected: all pass with zero network calls.

- [ ] **Step 6: Create a local-only checkpoint**

```powershell
git add sp_pdf_judger/pipeline.py sp_pdf_judger/policy_engine.py sp_judgement_bridge.py tests/test_permit_index_pipeline.py tests/test_submission_cache.py tests/test_sky_covione_authoritative_permit.py
git commit -m "feat: reuse indexed permit through judgement pipeline"
```

Do not push.

### Task 5: 명시적 배치 근거와 안전한 시험 그룹

**Files:**
- Create: `sp_pdf_judger/permit_grouping.py`
- Modify: `sp_pdf_judger/schemas.py`
- Modify: `sp_pdf_judger/sp_table_result_generalizer.py`
- Create: `tests/test_permit_grouping.py`
- Modify: `tests/test_table_and_semantic_judgement.py`

**Interfaces:**
- Adds to `ExtractedRecord`: optional `batch_id` and `batch_evidence` fields; existing constructors retain defaults.
- Produces: `BatchEvidence(state: Literal["explicit", "missing", "conflict", "material_lot"], batch, record_order_idx, source_kind, quote)`.
- Produces: `SPFact(fact_id, record_order_idx, row_ref, field, value, quote, page_start, page_end, source_kind)`.
- Produces: `PermitGroup(group_id, stage_key, test_key, batch, method_key, record_order_idxs, facts, candidate_evidence_ids, blocker_code)`.
- Produces: `build_stage_batch_inventory(records) -> Mapping[str, BatchEvidence]`.
- Produces: `build_permit_groups(records, permit_store) -> tuple[tuple[PermitGroup, ...], list[dict]]`.

- [ ] **Step 1: Write failing explicit-batch and isolation tests**

Cover same stage/test/batch join; different batch, stage or incompatible method split; missing batch and two explicit values stay isolated; material/control lots, filename tokens, permit examples and neighboring stages are rejected. Add a D00 fixture test asserting CHO master tests bind to `CHO-MCB-211012`, Component A tests to `SKY-CA220705`, and finished-product tests to `SKY-M220805-001`, never to positive-control or ingredient lots.

Add the Review Focus case: two same-stage information records with distinct exact `제조번호` values produce `conflict`, not nearest-value selection.

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_permit_grouping.py tests/test_table_and_semantic_judgement.py`

Expected: missing module/fields and grouping assertions fail.

- [ ] **Step 3: Preserve explicit table batch columns**

When a multi-test table has an exact batch header (`제조번호`, `배치번호`, `로트번호`, `Lot No.`), copy that row value and source description into the expanded record. Do not read `controls[*].lot_no` or arbitrary alphanumeric cells as the test batch.

- [ ] **Step 4: Build conservative stage inventory and groups**

Accept exact label-bound batch values from the same test record or a uniquely stage-matched information record with source evidence. Match normalized test-stage titles such as `CHO 마스터 세포주` ↔ `CHO 마스터 세포주에 대한 시험`, but require uniqueness. Exclude qualified labels such as `양성대조군 제조번호`, `제조용 세포주 제조번호`, ingredient/component numbers when they are not the stage's primary `제조번호`.

Group only explicit compatible records. Missing/conflicting groups receive unique group IDs and `blocker_code` so they cannot borrow values or reach CLOVA.

- [ ] **Step 5: Run grouping and existing batch-policy tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_permit_grouping.py tests/test_table_and_semantic_judgement.py tests/test_required_batch_fields.py tests/test_batch_field_policy_pdf.py tests/test_scoped_condition_facts.py tests/test_permit_required_tests.py`

Expected: all pass; existing batch policies remain unchanged.

- [ ] **Step 6: Create a local-only checkpoint**

```powershell
git add sp_pdf_judger/permit_grouping.py sp_pdf_judger/schemas.py sp_pdf_judger/sp_table_result_generalizer.py tests/test_permit_grouping.py tests/test_table_and_semantic_judgement.py
git commit -m "feat: group permit tests by explicit stage and batch"
```

Do not push.

### Task 6: 결정론 실행기와 그룹당 단일 CLOVA 게이트

**Files:**
- Create: `sp_pdf_judger/permit_group_judgement.py`
- Modify: `sp_pdf_judger/permit_conditions.py`
- Modify: `sp_pdf_judger/permit_llm_protocol.py`
- Modify: `sp_pdf_judger/llm.py`
- Modify: `sp_pdf_judger/judgement.py`
- Create: `tests/test_permit_group_judgement.py`
- Modify: `tests/test_permit_llm_protocol.py`
- Modify: `tests/test_numeric_safety.py`

**Interfaces:**
- Produces: `ConditionExecution(condition_id, status: Literal["pass", "fail", "hold"], fact_ids, reason_code)`.
- Produces: `execute_permit_conditions(conditions, facts) -> tuple[ConditionExecution, ...]`.
- Produces: `GateDecision(path: Literal["deterministic", "ambiguous_llm", "hold_without_llm"], reason_code)`.
- Produces: `GroupVerdict(status, reason, source, group_id, gate_path, condition_ids, fact_ids, evidence_ids)`.
- Produces: `classify_group(group, candidates, conditions, executions) -> GateDecision`.
- Produces: `GroundedGroupResponse` with selected evidence ID and one decision for every required condition ID using only presented fact IDs.
- Produces: `ClovaJudgeClient.explain_permit_group_once(...) -> GroundedGroupResponse | None`, exactly one `request_structured_response` call and no retry/repair.
- Produces: `JudgeEngine.judge_permit_group(group, conditions_by_evidence_id) -> GroupVerdict`.

- [ ] **Step 1: Write failing deterministic execution tests**

Table-drive boundary values, incompatible units, day/hour/minute equivalence, `36 ± 2 ℃`, count, survival percentage and qualitative absence. The complete egg-inoculation fixture must require age, count, temperature, duration, survival, passage and haemagglutination conditions. Removing any required fact yields hold; violating any structured condition yields fail.

Pin the partial-structure rule: structured PASS plus one `unparsed` mandatory condition is never overall PASS. A definite structured FAIL may close the group as FAIL without spending a model call.

- [ ] **Step 2: Write failing gate and one-request protocol tests**

Assert deterministic complete groups call the fake client zero times; missing/conflicting batch, missing result, OCR/index fatal error call zero and hold; N records in one semantic ambiguity group call once. Invalid/duplicate/missing evidence, condition or fact IDs, 429, malformed JSON and grounding mismatch return hold with provider call count exactly one. A model PASS cannot reverse numeric/duration/unit FAIL.

- [ ] **Step 3: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_permit_group_judgement.py tests/test_permit_llm_protocol.py tests/test_numeric_safety.py`

Expected: missing executor/gate/protocol APIs fail.

- [ ] **Step 4: Implement condition execution and explicit gate classification**

Use Decimal and a small isolated conversion table for time and supported units; do not change the legacy float parser's semantics. Aggregate condition states `fail > hold > pass`. Route complete mappings to `deterministic`, sufficient but semantically ambiguous mappings to `ambiguous_llm`, and missing/corrupt evidence to `hold_without_llm`.

- [ ] **Step 5: Implement grounded one-shot group protocol**

Require exact evidence ID, all required condition IDs exactly once, only known fact IDs, and matching stage/batch. Derive final status from validated condition decisions and trusted source quotes. `explain_permit_group_once()` must bypass the existing `explain()` retry and `_ground_permit_result()` repair paths.

- [ ] **Step 6: Integrate group judgement into `JudgeEngine`**

Keep `judge_record()` unchanged for legacy policies. For deterministic-first groups, apply primary deterministic SP rules and permit executions, call the one-shot protocol only for `ambiguous_llm`, and re-run all structured numeric/time/unit conditions after the response. Preserve existing fail-closed catalog/OCR checks and comparison-basis audit.

- [ ] **Step 7: Run deterministic, protocol and authoritative regressions**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_permit_group_judgement.py tests/test_permit_conditions.py tests/test_permit_llm_protocol.py tests/test_numeric_safety.py tests/test_sky_covione_authoritative_permit.py tests/test_gemini_second_phase_judgement.py`

Expected: all pass; legacy per-record client retry behavior remains unchanged outside the new group method.

- [ ] **Step 8: Create a local-only checkpoint**

```powershell
git add sp_pdf_judger/permit_group_judgement.py sp_pdf_judger/permit_conditions.py sp_pdf_judger/permit_llm_protocol.py sp_pdf_judger/llm.py sp_pdf_judger/judgement.py tests/test_permit_group_judgement.py tests/test_permit_llm_protocol.py tests/test_numeric_safety.py
git commit -m "feat: judge clear permit groups without llm"
```

Do not push.

### Task 7: 결정론 우선 파이프라인, 원본 표시 보존과 전체 회귀

**Files:**
- Modify: `sp_pdf_judger/pipeline.py`
- Modify: `sp_pdf_judger/schemas.py`
- Modify: `sp_judgement_bridge.py`
- Modify: `sp_pdf_judger/ui_html.py` only if additive audit fields need rendering; do not change existing visible values/layout.
- Create: `tests/test_deterministic_first_pipeline.py`
- Modify: `tests/test_sky_covione_authoritative_permit.py`
- Modify: `tests/test_pipeline_structure.py`
- Modify: `tests/test_submission_cache.py`
- Create: `scripts/benchmark_permit_index.py`
- Modify: `README.md`

**Interfaces:**
- Adds to `Evaluation`: optional `permit_group_id`, `permit_group_path`, `permit_batch`, `permit_group_evidence`; existing constructors keep defaults.
- Produces: deterministic-first pipeline metadata for raw test/group counts, gate counts, actual/saved CLOVA calls and sanitized group audit.
- Preserves: one `Evaluation` per original displayed test record/row in original order.

- [ ] **Step 1: Write failing end-to-end branch and fan-out tests**

For the SkyCovione target, normalize all tests, build groups, judge each group once, then assert one evaluation per original record with unchanged `criteria`, `result`, pages and lot rows. Assert one failing member makes the group summary fail while a passing member cannot fill another member's missing result. Assert deterministic groups make zero provider calls and one ambiguous multi-record group makes one.

Run the same fixture under a wrong company/product and assert the legacy statuses, reasons, sources and call ordering are byte-for-byte equal to the pre-branch result.

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_deterministic_first_pipeline.py tests/test_sky_covione_authoritative_permit.py tests/test_pipeline_structure.py tests/test_submission_cache.py`

Expected: no deterministic-first branch/group audit exists yet.

- [ ] **Step 3: Implement the policy-gated pipeline branch**

Before the existing per-record loop, normalize target records, build and evaluate groups, and fan the verdict back to original records. Leave the entire existing loop active for every other policy. Run the existing general-method rules, MD policies, tree construction, summary and displayed-value restoration after fan-out in the same order as before.

- [ ] **Step 4: Add safe audit metadata and preserved UI fields**

Record raw/group counts, deterministic/LLM/hold counts, provider calls and estimated saved calls. Store only stable group/batch/evidence IDs and reason codes. Render group audit only if needed without changing existing criteria/result/version text or layout.

- [ ] **Step 5: Add the offline benchmark/report script**

`scripts/benchmark_permit_index.py` runs the same local SP+permit twice with CLOVA disabled and prints JSON containing cold/warm elapsed times, cache hit, extractor/OCR/parser counts, group path counts and provider calls. It must never enable live API mode or write into tracked output paths.

- [ ] **Step 6: Run focused integration and supplied-permit checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_deterministic_first_pipeline.py tests/test_permit_index_pipeline.py tests/test_permit_group_judgement.py tests/test_sky_covione_authoritative_permit.py
.\.venv\Scripts\python.exe scripts/benchmark_permit_index.py --sp "더미데이터/D00_정상기준본.pdf" --permit "sp_pdf_judger/permits/documents/sky_covione_multidose.pdf"
```

Expected: second pass reports cache hit and zero page extraction/OCR/chunk/condition parsing; clear groups have zero CLOVA calls.

- [ ] **Step 7: Run all rule, cache, permit and UI regressions**

Run:

```powershell
.\.venv\Scripts\python.exe scripts/validate_rules.py
.\.venv\Scripts\python.exe -m pytest -q -rs
.\.venv\Scripts\python.exe scripts/validate_corpus.py --output-dir .local_validation/permit_index_grouped_offline
```

Expected: no failures; only the repository's documented skips; no network calls; D00~D16 designated-error misses remain zero.

- [ ] **Step 8: Review the complete diff and local state**

Run `git diff --check`, inspect every changed file, verify no cache, PDF copy, `.env`, secret, runtime log or benchmark output is staged, and compare non-target regression snapshots.

- [ ] **Step 9: Create the final local-only checkpoint**

Stage the exact reviewed paths, never `git add -A`, and commit locally with message `feat: cache permit evidence and limit llm judgement`.

Do not push, publish, or open a PR.
