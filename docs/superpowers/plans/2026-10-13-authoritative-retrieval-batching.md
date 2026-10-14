# Authoritative Retrieval and Evidence-Sharing Batching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 새 SP/허가서의 시험별 근거 검색과 반복 입력을 줄이되 허가서 우선 LLM 의미 판정과 원문 추적을 보존한다.

**Architecture:** 기존 단건 경로를 기준 구현으로 유지하고, 명시적인 실험 모드에서 검색된 원문 묶음을 여러 독립 레코드가 공유한다. 검색·원문 연결·전송·레코드별 결과 적용을 분리하며 기존 deterministic-first 정책은 바꾸지 않는다. 새 결과는 원문/ID 검증을 통과해야 게시한다.

**Tech Stack:** 기존 Python, dataclasses, Pydantic 2, pytest, PyMuPDF, Streamlit, CLOVA OpenAI-compatible client. 신규 DB/임베딩 서비스/패키지를 도입하지 않는다.

**Spec:** `docs/superpowers/specs/2026-10-13-authoritative-retrieval-batching-design.md` (2026-10-13 사용자 승인).

**Status:** Native 구현 Task 1–7 검증 완료; Task 8 오프라인 측정/최종 검토 진행. live API 정확성·속도 및 기본 활성화 승인은 미완료. 실행 판단/차이는 ignored progress ledger에 기록.

## Global Constraints

- 작업 경로: `C:\Users\User\Documents\Codex\2026-08-26\c-users-user-desktop-sp-main\worktrees\permit-index-grouped-judgement`.
- Desktop 메인에 복사·병합하지 않는다. GitHub에 푸시하거나 웹 앱을 재배포하지 않는다.
- 브랜치 `feat/permit-index-grouped-judgement`, 시작 커밋 `d084d50e8910818511c23e6c6fce28214e085c79`.
- 초기 묶음 상한은 4개 독립 레코드다. 기존 60,000자 제한은 보조 안전 제한으로만 사용한다.
- 개발 중 새 경로는 명시적인 실험 모드로만 켠다. 기본 기존 모드와 대조할 수 있어야 한다.
- 서로 다른 배치의 SP 결과를 합치거나 판정을 복제하지 않는다. 배치 문맥이 모호하면 묶지 않는다.
- 최종 판단의 의미는 LLM이 담당하고, 코드는 원문 연결·조건 누락·응답 ID·전송 예산을 검증한다.
- 일반 업로드를 `deterministic_first`로 변경하지 않는다. 제품/회사 매핑 및 버전 로직 변경 금지.
- 보정은 실행당 각 항목 최대 1회이며, 최초 묶음과 보정을 모두 실제 호출 수로 기록한다.
- 401/403/429/timeout/연결 실패는 서비스 오류로 전파하고 최종 결과 게시를 중단한다.
- 원문·프롬프트·키를 진단 로그에 덤프하지 않는다. 캐시 삭제 대신 격리된 실험 디렉터리를 사용한다.
- 실제 provider 비교가 미실행이면 기본 모드를 전환하지 않고 남은 검증을 명시한다.
- 원본 제출 표기, 기존 비교용 SP 판정, 문서 공통 규칙을 유지한다. 무관한 리팩터링 금지.

## Review Focus

1. 동일 `order_idx`/표 하위행에서 다른 배치 결과가 뒤섞이지 않아야 한다 — Task 6.
2. 다른 PDF가 같은 파일명과 section 번호를 가져도 parent/reference가 연결되지 않아야 한다 — Task 3.
3. 부모가 여러 형제 시험을 포함할 때 공통 조건은 보존하고 다른 형제의 조건은 끌어오지 않아야 한다 — Task 3.
4. 긴 한국어 복합조건은 입력이 작아도 출력 clause 수가 많아질 수 있다 — Task 4/5.
5. 검수 중 실험 모드가 바뀌어도 이미 시작한 작업의 설정/완료 캐시가 섞이지 않아야 한다 — Task 7.

---

## 작업 단위와 인터페이스 지도

새 파일은 아래 책임에만 한정한다. `judgement.py`와 `pipeline.py`의 대규모 재배치는 하지 않는다.

| 파일 | 책임 | 선행 작업 |
|---|---|---|
| `sp_pdf_judger/permit_semantic_settings.py` | 실험 모드/모델 예산/비밀정보 없는 설정 fingerprint | 1 |
| `sp_pdf_judger/permit_retrieval.py` | immutable chunk lookup, 토큰/질의 캐시, 후보 선택 메타데이터 | 2 |
| `sp_pdf_judger/permit_evidence_bundle.py` | 원문 span/부모/명시적 참조의 검증된 연결 | 3 |
| `sp_pdf_judger/permit_semantic_batch.py` | 레코드 ID/공유 근거 요청/응답 schema/크기 분할 | 4 |
| `sp_pdf_judger/llm.py` | 기존 client/transport로 묶음 실행, 단건 보정, 원문 grounding | 5 |
| `sp_pdf_judger/judgement.py`, `pipeline.py` | 준비된 응답을 기존 의미 판정 위치에서 적용 | 6 |
| `sp_judgement_bridge.py`, `sp_review_jobs.py` | 작업 시작 설정 snapshot/캐시 및 실행 정보 전달 | 7 |
| `scripts/benchmark_authoritative_batch.py` | 최초/인덱스 재사용/응답 재사용 비교 및 안전한 보고 | 8 |

작업 2/3/4는 인터페이스가 확정된 뒤 읽기 전용 검토를 병행할 수 있다. 공유 생산 파일의 동시 편집은 하지 않는다.
Task 6/7은 반드시 순차로 통합한다. 실행 방법 승인 전에는 제품 코드를 수정하지 않는다.

## 실행 및 테스트 규칙

- Python은 작업 폴더의 `.venv\Scripts\python.exe`를 쓴다. 의존성이 이미 있으므로 자동 재설치하지 않는다.
- PowerShell에서 각 테스트 실행마다 새로운 짧은 임시 경로를 지정한다:

```powershell
$batchTestTemp = Join-Path $env:TEMP ('psb-' + [guid]::NewGuid().ToString('N').Substring(0,8))
.\.venv\Scripts\python.exe -m pytest <해당 테스트 경로> -q --basetemp "$batchTestTemp"
```

- 기존 pytest temp/캐시를 재사용하거나 삭제하지 않는다. 새 경로의 prefix와 절대 경로를 확인한다.
- 각 Task는 실패 테스트 작성 → 해당 기능 부족으로 FAIL 확인 → 최소 구현 → 해당 테스트와 기존 회귀 PASS 순서다.
- 없는 모듈의 import 에러만으로 RED를 끝내지 않는다. 선언만 먼저 만들고 동작 assertion 실패를 확인한다.
- 새 테스트의 provider는 `SimpleNamespace` completions 대역으로 주입한다. 실제 parser, schema,
  `request_structured_response`, validator는 유지한다. 테스트 fixtures에서 socket 연결을 차단한다.
- 전역 네트워크 차단 conftest가 현재 없다는 점에 유의한다. 새 helper의 차단 fixture를 새 테스트 모듈이 명시적으로 사용한다.
- 단계별 `git diff --check`와 지정 파일 diff를 확인한다. 이 계획의 로컬 실행에서는 자동 commit/merge/push를 하지 않는다.

### Task 1: 안전한 실험 모드와 테스트 기반

**Files:** Create `sp_pdf_judger/permit_semantic_settings.py`, `tests/test_permit_semantic_settings.py`, `tests/helpers/semantic_batch_fixtures.py`.

**Interfaces:**
- `SemanticSettings(mode: Literal['legacy','shared_evidence'], max_records: int = 4, max_prompt_chars: int = 60000)` frozen dataclass.
- `read_semantic_settings(environ: Mapping[str,str] | None = None) -> SemanticSettings` reads only `SP_PERMIT_SEMANTIC_MODE`; absent is `legacy`, invalid is a clear configuration error.
- `semantic_fingerprint(settings: SemanticSettings) -> str` hashes mode + retrieval/bundle/batch version constants, never secrets.
- `model_budget(model: str, max_completion_tokens: int) -> ModelBudget | None`.
  `ModelBudget(context_tokens: int, output_tokens: int, estimate_kind: str='utf8-byte-estimate')`.
  Initially allow only exact `HCX-007`, context 128000, output min(configured,32768); invalid/nonpositive output is an error. Unknown models return None and use legacy single requests.
- Test helper `deny_network(monkeypatch)` blocks `socket.create_connection`, `socket.socket.connect`, and httpx transport connections, but not the fake completion method.
  Fixtures build synthetic chunks/records and a fake client with explicit test execution settings; never load keys from local files.

- [x] Before product edits, verify existing `.venv` imports and run the unchanged full suite with a fresh short basetemp. If baseline fails, name the failures and resolve whether they are environment or pre-existing defects before attributing them to this work.
- [x] Write tests: `test_default_is_legacy`, `test_shared_evidence_is_explicit`, `test_unknown_mode_fails`, `test_settings_have_no_credentials`, `test_unknown_model_has_no_batch_budget`.

```python
assert read_semantic_settings({}).mode == 'legacy'
assert read_semantic_settings({'SP_PERMIT_SEMANTIC_MODE':'shared_evidence'}).max_records == 4
assert model_budget('unknown-model',4096) is None
assert model_budget('HCX-007',4096).output_tokens == 4096
```

- [x] Run `tests/test_permit_semantic_settings.py`; verify RED on missing behavior, then implement only these interfaces.
- [x] Run that file and `tests/test_submission_cache.py`; expect no failures and no network attempt.
- [x] Run `git diff --check`; inspect only Task 1 files and record pass counts.

Model limits are from [NAVER CLOVA Studio concepts](https://guide.ncloud-docs.com/docs/en/clovastudio-info), checked 2026-10-13. Recheck at implementation time; limits are not an account throughput guarantee. UTF-8 size is a conservative scheduling estimate, **not measured token usage or a proven tokenizer bound**.

### Task 2: 동일 제조단계·시험 lookup과 검색 캐시

**Files:** Create `permit_retrieval.py`, `tests/test_permit_precise_retrieval.py`; modify `permit_pdf_store.py` search integration only.

**Interfaces:**
- `RetrievalSelection(chunks: tuple[PermitChunk,...], route: str, complete: bool, scored_count: int, cache_hit: bool)` frozen dataclass.
- `PermitRetrievalIndex(chunks: Sequence[PermitChunk], *, source_fingerprint: str, aliases: Mapping[str,Sequence[str]])` owns immutable lookup/token sets; recreated after store policy/chunks/aliases change.
- `select(record: ExtractedRecord, *, fallback: Callable[[ExtractedRecord,int],list[PermitChunk]], top_k: int=12) -> RetrievalSelection`.
- `PermitPdfStore.search_precise(record: ExtractedRecord) -> RetrievalSelection`; existing `search()` behavior stays unchanged for legacy mode.

- [x] Add failing tests for exact-stage identity, method-conflicting duplicates, renamed/unknown test fallback, policy-less fallback, different result/query key, aliases/store replacement cache invalidation and 13 exact candidates.

```python
assert len(selection_of_13_exact.chunks) == 13  # do not silently truncate conflicts
assert other_stage_id not in {c.evidence_id for c in exact.chunks}
assert unknown.route == 'legacy_fallback'
assert repeated_query.cache_hit is True
assert changed_result.cache_hit is False
```

- [x] Run `tests/test_permit_precise_retrieval.py`; observe RED before implementing lookup and immutable query identity.
- [x] Implement token precomputation and stage/test lookup using current formatting normalization/approved aliases. Do not introduce fuzzy synonyms. Return all exact same-stage candidates; mark fallback selection as not proven complete.
- [x] Run new tests plus `tests/test_permit_pdf_store.py tests/test_permit_search_aliases.py tests/test_permit_index_pipeline.py`; expect PASS.
- [x] Review diff for unchanged legacy search results and no PDF/OCR work in `select()`.

### Task 3: 완결된 원문 근거 묶음

**Files:** Create `permit_evidence_bundle.py`, `tests/test_permit_evidence_bundle.py`; use existing `permit_models.py`, `permit_source_ownership.py` without changing source ownership semantics.

**Interfaces:**
- `EvidenceBundle(bundle_id: str, candidates: tuple[PermitCandidate,...], spans: tuple[PermitSourceSpan,...], complete: bool, errors: tuple[str,...], clause_span_ids: Mapping[tuple[int,int],tuple[str,...]])` frozen dataclass with read-only mapping values.
- `build_evidence_bundle(selection: RetrievalSelection, *, all_chunks: Sequence[PermitChunk], documents: Sequence[PermitSourceDocument], company: str, product: str) -> EvidenceBundle`.
- `render_bundle_context(bundle: EvidenceBundle) -> str` preserves the existing `[허가서 근거 n]` source layout for singleton grounding.

- [x] Write failing tests for egg-inoculation all conditions, parent common requirement, sibling exclusion, cross-page table/footnote, same-basename two PDFs, cycles, broken parent and ambiguous explicit reference.

```python
assert expected_source_span_ids <= {s.evidence_id for s in egg_bundle.spans}
assert unrelated_sibling_span_id not in {s.evidence_id for s in child_bundle.spans}
assert cyclic_bundle.complete is False
assert same_name_other_document_span not in {s.evidence_id for s in child_bundle.spans}
```

- [x] Verify RED with `tests/test_permit_evidence_bundle.py`.
- [x] Implement source-document-scoped parent walking and exact span deduplication. Include selected test descendants when the selected node is itself a composite test; include ancestor **owned** text, never ancestor `.text` with unrelated descendants. Ambiguous ancestry is ineligible for batching.
- [x] Resolve explicit numeric reference only within the same source document when target is unique. Recognize section numbers followed by `항`, `에 따른`, `에 따라`, `에 준` and `참조`; unresolved reference-like phrases make completeness false. Do not guess references from arbitrary numbers or borrow another permit.
- [x] Preserve candidate boundaries and every source clause; build candidate/clause-to-span mapping so final quotes retain correct pages. Hash source identity, stage/test/method/context and source relationships, not filename alone.
- [x] Run new tests plus `tests/test_permit_source_ownership.py tests/test_permit_ownership_pdf.py tests/test_permit_llm_protocol.py`; expect PASS. Inspect the egg fixture's actual transmitted clauses, not just counts.
- [x] Review/record diff and test counts.

### Task 4: 근거 공유 envelope와 크기 분할

**Files:** Create `permit_semantic_batch.py`, `tests/test_authoritative_semantic_batch_protocol.py`, `tests/test_authoritative_semantic_batch_partition.py`.

**Interfaces:**
- `SemanticPermitRequest(record_id: str, position: int, bundle: EvidenceBundle, record_context: str, criteria: str, result: str, request_fingerprint: str)` frozen dataclass. `record_id='r'+str(position)` uses unique post-expansion source position, not `order_idx`.
- `SemanticBatchItem(record_id: str, evidence_set_id: str, decision: GroundedPermitResponse)` and strict Pydantic `SemanticBatchResponse(items: list[SemanticBatchItem])`, extra fields forbidden.
- Receive-only `SemanticBatchWireResponse(items: list[dict[str,object]])` preserves attributable item shape errors. This tolerant model must never be sent as the generation schema. Top-level non-object/non-list framing remains invalid.
- `build_semantic_batch_prompt(requests: Sequence[SemanticPermitRequest]) -> str` writes one evidence set per exact bundle ID and one independent record per ID.
- `partition_semantic_requests(requests: Sequence[SemanticPermitRequest], settings: SemanticSettings, budget: ModelBudget | None) -> SemanticPartition` where `SemanticPartition(batches: tuple[tuple[SemanticPermitRequest,...],...], singles: tuple[SemanticPermitRequest,...], oversized_ids: tuple[str,...])`.
- `index_semantic_response(requests, response: SemanticBatchWireResponse) -> dict[str,GroundedPermitResponse]` validates IDs/ownership and converts each item to `SemanticBatchItem`. `SemanticProtocolError(ValueError)` holds `framing: bool`, `item_errors: dict[str,str]`, and an optional parsed wire response for local bounded repair; its string contains safe codes only. Per-item clause grounding remains Task 5.

- [x] Add RED tests for 8 small requests/2 batches, one shared evidence serialization per batch, different bundle isolation, reordered results, missing/duplicate/unknown IDs, cross-set candidate, input too large, output-heavy Korean and unknown model.

```python
assert [len(b) for b in small_eight.batches] == [4,4]
assert len(json_payload['evidence_sets']) == 1
assert indexed['r0'].clauses[0].status == 'FAIL'  # reversed provider result order
assert unknown_model_partition.batches == ()
assert huge_single_id in huge_partition.oversized_ids
```

- [x] Observe RED; implement stable greedy grouping only for identical, complete bundles and known explicit batch identity. Singletons or no savings return to `singles`; semantic SP deduplication is not implemented.
- [x] Measure the final prompt plus JSON schema estimate with `len(text.encode('utf-8'))`, add 512 scheduling reserve, label estimate. Require estimated input + configured output <= known context, chars <=60000, records <=4.
- [x] Output planning estimate is `256 + sum(256 + 128 * max_candidate_clause_count(item))`; compare with configured output budget and split as needed. It is an estimate, not permission to truncate clauses/reasons or silently accept an incomplete response. Never raise output settings automatically.
- [x] Run new protocol/partition files plus `tests/test_permit_batch_contract.py tests/test_permit_group_batching.py`; expect PASS with old group protocol unchanged.
- [x] Review no ID based on filename/order_idx and no original text deletion.

### Task 5: 실제 클라이언트 경계와 제한적 보정

**Files:** Modify `llm.py`, `clova_client.py` only for safe metrics hook if needed; create `tests/test_authoritative_semantic_batch_client.py`.

**Interfaces:**
- `ClovaJudgeClient.explain_authoritative_semantic_batch(*, requests: Sequence[SemanticPermitRequest]) -> dict[str,JudgeResponse] | None`.
- `None` only for disabled client/empty request. Transport/JSON/schema/framing failures raise `ClovaServiceError`; never fabricate HOLD results.
- Existing `_ground_permit_result()` gains optional `allow_repair: bool=True`; singleton callers keep default, batch repair calls use false to prevent nested repairs.

- [x] Test under real `request_structured_response()` with fake completions: valid mixed PASS/FAIL/HOLD, disabled/no call, malformed response, one attributable bad item, unknown ID, omitted clause, 429 and exhausted repair. Block sockets and isolate `.sp_judgement_status`/response cache.

```python
assert result['r0'].status == PASS_LABEL
assert result['r1'].status == FAIL_LABEL
assert provider.calls == 2  # initial batch + exactly one damaged-item repair
assert good_item_repair_calls == 0
```

- [x] Observe RED. Implement strict schema requests through existing transport, preserving interval/retry limits.
- [x] Send `SemanticBatchResponse` as `response_schema_model`, receive `SemanticBatchWireResponse`, and use a response validator that checks envelope/item/clause contracts **before cache write**. Preserve attributable errors locally through `SemanticProtocolError`; do not print its response data. A rejected batch is not written to the response cache; repaired singleton requests retain their own ordinary validated caches.
- [x] Validate record/set framing first; feed each decision through existing `grounded_verdict` using its own candidate set. Build quotes via `clause_span_ids`, not LLM-generated text. Inherit existing clause classification/coverage rules.
- [x] Repair only attributable malformed/grounding-failed items at most once with full singleton context. A frame-level failure may use one schema-format correction for the envelope and counts as the one repair opportunity for every item; do not then repair individual items again.
- [x] Do not publish or cache a batch as validated until envelope and all item contracts pass. Valid semantic ambiguous/not_found/insufficient-evidence results may remain HOLD; invalid JSON/ID/schema after repair raises service response error. Attach source metadata through `JudgeResponse` private attributes.
- [x] Run new file plus `tests/test_clova_client.py tests/test_clova_transport.py tests/test_permit_llm_protocol.py tests/test_clova_service_failure.py`; expect PASS, old semantics unchanged.
- [x] Review actual outbound schema, cached-response revalidation and secret-safe errors; record provider/correction counts separately.

### Task 6: 파이프라인 연결과 원래 행 복원

**Files:** Modify `judgement.py`, `pipeline.py`; create `tests/test_authoritative_semantic_batch_pipeline.py`.

**Interfaces:**
- `PreparedAuthoritativeResponse(request_fingerprint: str, record_id: str, response: JudgeResponse)` in `permit_semantic_batch.py`.
- `JudgeEngine.prepare_authoritative_request(record: ExtractedRecord, *, position: int) -> SemanticPermitRequest | None`: no provider calls; uses Tasks 2/3 and current policy gates.
- `JudgeEngine.judge_record(record: ExtractedRecord, *, prepared_authoritative: PreparedAuthoritativeResponse | None=None) -> Evaluation`: optional keyword preserves all old callers. Revalidate full record/context fingerprint before consuming a prepared answer at `_judge_with_authoritative_permit()`.
- `DocumentJudgePipeline.__init__(..., semantic_settings: SemanticSettings | None=None)` snapshots legacy when omitted; no implicit environment reread inside a running pipeline.

- [x] Add RED tests using real `resolve_submission_permits`, parser, normalization and pipeline: 8 compatible records/2 initial after-phase permit calls; different batches/results; duplicate order_idx; unchanged submitted decimal text; wrong fingerprint; unavailable evidence; unknown batch; preexisting table rows; no changes in legacy/deterministic-first modes.

```python
assert [e.result for e in evaluations] == [r.result for r in original_records]
assert [e.final_status for e in duplicate_order_idx_results] == [PASS_LABEL,FAIL_LABEL]
assert after_phase_permit_calls == 2
assert legacy_group_method_calls == 0
```

- [x] Observe RED. Prepare only normalized authoritative requests whose existing guards would permit LLM review. Pure requirement text in result, broken extraction/catalog, ambiguous batch or unsupported table expansion remain on existing safe path without speculative prefetch.
- [x] Group eligible requests, obtain validated replies, then call existing record judgement sequentially in original order with immutable per-position response. Keep comparison-basis, domain context, unit/result normalization, lot details and common policies intact. No mutable global response map keyed by order_idx.
- [x] Table records not yet expanded into unique independent positions remain single requests; do not infer a shared lot or coalesce table subrows. Keep before-phase SP semantic calls unchanged and count them separately from after-phase savings.
- [x] If model/budget/evidence eligibility fails, use the existing single authoritative path. Do not fall back to old completed artifacts or SP-only results because a batch API failed.
- [x] Run new tests plus `tests/test_direct_upload_semantic_regression.py tests/test_sky_covione_authoritative_permit.py tests/test_table_and_semantic_judgement.py tests/test_deterministic_first_pipeline.py tests/test_comparison_basis_capture.py`; expect PASS.
- [x] Inspect actual transmitted egg/in-vitro/foreign-matter fixtures; mock outputs are contract tests, not claims of model accuracy. Record remaining table fallback counts.

### Task 7: 앱 설정 snapshot, 캐시 분리, 안전한 실행 진단

**Files:** Modify `sp_judgement_bridge.py`, `sp_review_jobs.py`, `clova_transport.py`, `clova_client.py`; create `tests/test_semantic_mode_cache.py`, `tests/test_semantic_batch_diagnostics.py`; update `.env.example` and README (never actual `.env`/secrets).

**Interfaces:**
- Snapshot `SemanticSettings` at job creation and pass it through `get_judgement_job`, `ensure_judgement_artifacts`, `_ensure_disk_artifacts`, both pipelines. Existing prepared tuples/jobs must keep legacy compatibility.
- Extend `_artifact_key(..., semantic_fingerprint: str='')` and job dedup identity for shared mode; legacy empty suffix preserves old key behavior. Recheck attempt settings before publication exactly as existing LLM fingerprint checks do.
- `clova_transport.request_diagnostics() -> dict[str,int|float|str]` reports additional timing/token counters, without changing existing `request_metrics()` keys/contract.
- Add `semantic_batch_diagnostics` to result metadata with mode, phase timings, retrieval route/candidate counts, request/record/shared-set counts, before/after chars, estimates, actual usage when available, singleton/fallback reasons, attempts/retries/repairs/cache hits.

- [x] RED tests for legacy/shared cache separation, input/criteria/permit/policy change, identical filenames/different PDFs, settings changed during job, API-disabled-to-enabled, unknown response usage, and absence of keys/prompts/absolute paths in diagnostics.

```python
assert legacy_key != shared_key
assert changed_permit_key != shared_key
assert secret not in json.dumps(diagnostics)
assert diagnostics['cache_hits'] == 0  # fresh fake-provider execution
```

- [x] Observe RED. Include retrieval/bundle/protocol versions in shared-mode fingerprint and response request signature. A batch whose settings changed cannot publish under the old key.
- [x] Add monotonic phase timings; measure HTTP wait and gate/backoff separately without adding overlapping totals. Aggregate response usage only when reported by provider; null/unavailable is not zero usage. Cache hits must not add historic token usage to this run's paid token total.
- [x] Document launching the existing local Streamlit app with `$env:SP_PERMIT_SEMANTIC_MODE='shared_evidence'` and restoring `legacy`; do not edit user secret files or restart/kill their current server automatically.
- [x] Run new tests plus `tests/test_submission_cache.py tests/test_failed_judgement_cache.py tests/test_review_jobs.py tests/test_clova_service_failure.py tests/test_final_review_progress.py`; expect PASS and no completed pickle/summary on operational failure.
- [x] Review no main-branch/ref changes and no new upload/network destination.

### Task 8: 최초 실행 벤치마크와 전체 회귀

**Files:** Create `scripts/benchmark_authoritative_batch.py`, `tests/test_benchmark_authoritative_batch.py`; update README with measured results only after execution.

**Interfaces:** CLI `--sp PATH --permit PATH --out DIR --mode legacy|shared_evidence --cache-state cold|index-only|responses --seed-run DIR --allow-live`.
Default is offline: construct no real provider; socket/HTTP/provider guard must raise on attempted external traffic. Live execution requires explicit flag and environment network authority.
`--out` must be a new directory. All response/index/aggregate cache roots are explicitly scoped to the run; no existing user cache deletion or mutation. For index-only/responses, `--seed-run` is required: validate its experiment manifest/model/document identities and clone only the permitted cache kinds from that experiment. Never discover unrelated caches. `cold` rejects `--seed-run`.
Report safe `metrics.json` plus record-level status/evidence comparison without copying uploaded PDFs or raw prompts.

- [x] RED tests verify cold uses zero old artifacts, index-only permits no response hits, responses labels cached replay, offline blocks real calls before construction, existing output directory is rejected, and missing large corpus reports unverified rather than pass.
- [x] Implement CLI orchestration and a report schema separating before/after calls, actual HTTP/retries/cache hits, estimates/actual token usage, page counts and phase times. Document status delta/evidence delta and manually reviewed reason changes separately; not only aggregate counts.
- [x] Run `tests/test_benchmark_authoritative_batch.py tests/test_benchmark_permit_index.py`; expect PASS without paid calls.
- [x] Run complete suite with a new short basetemp: `.\.venv\Scripts\python.exe -m pytest -q --basetemp "$batchTestTemp"`. Report every failure/skip and original/new count. The prior 2221/47 is historical, not the expected current count.
- [x] Run offline D00/D07 pipeline/parity checks and synthetic independent variations of egg conditions, 21/28-day boundaries, decimal notation, stage identity, missing pages and lot identity. Do not call fake-provider parity actual LLM accuracy.
- [ ] After network authority and a bounded live-run cost/call ceiling are confirmed, compare D00/D07 legacy vs shared in cold/index-only states. Use a conservative initial ceiling of 250 HTTP attempts per run, stop when reached, and label incomplete runs. Do not auto-repeat a rate-limited run.
- [ ] If actual permitted SP >=200/300-page permit input exists, perform the same comparison; otherwise report scale validation pending. Duplicated-page stress is only engineering stress, not unseen-company proof.
- [x] Final independent whole-diff review. Correctness regressions, exhausted protocol repair, or inability to verify source completeness block default activation. Actual performance must show call/input reduction without unexplained new status changes; otherwise keep experimental-only.
- [x] `git diff --check`, working-tree review and main/ref comparison. Final handoff explicitly says local-only/no push, actual results, remaining cold/live/scale limitations, and exact local launch instructions.

## Self-review record and execution handoff

- Coverage: spec search→Task 2; evidence completeness→3; batching→4/5; row isolation→6; mode/cache→1/7; diagnostics/performance→7/8; unchanged unrelated policy→6/8.
- Review Focus items are pinned to Tasks 3/4/5/6/7 tests; no production use of historical cached replay as cold benchmark.
- Interfaces use `evidence_set_id` on wire and `bundle_id` internally with direct one-to-one mapping. `record_id` always comes from unique source position; clause IDs are bundle-scoped.
- Model budget estimate is explicitly not actual tokenizer usage. Unknown models use singleton fallback; live acceptance remains required.
- No implementation checkbox is checked at plan-writing time. Scope changes discovered during implementation return to design review.

Recommended execution method: **Native** (the main agent implements this plan in order, with one independent whole-diff review at the end). Evidence types and request/response hooks are tightly coupled; parallel production edits would increase integration risk. Read-only audits can still be delegated.
Alternative: **Subagent-driven**, with a separate implementer/reviewer gate per Task, at higher coordination cost.
User plan review and execution-method selection are required before Task 1.

## 2026-10-14 review amendments

Native final correction pass addresses the seven Important review findings and the unit-provenance issue regraded Important. Prepared eligible singletons retain their complete evidence through a one-item envelope; only ineligible/unknown-model rows retain legacy fallback. Shared-mode downstream criteria and unit notes use source positions, not display order indices. Provider-setting resolution no longer mutates process environment. Shared aggregate/status identity includes document content hashes and a captured output budget; protocol is v2. Safe operational audit omits raw document/provider text. Details and RED/GREEN evidence are in the ignored execution ledger.

Task 8 live cold/accuracy/large-run acceptance remains pending, so default is still legacy. This is a local-only experimental implementation, not a deployment or verified real-API speedup.

Fresh full suite2026-10-14:2331passed/48skipped/0failures,1180.68s.40skips require private corpus,7are existing checkpoint-resume placeholders,1requires symlink privilege. Prior fingerprint-guard failure passed after source freeze. D00/D07 shared-cold versus legacy-index-only offline comparison:121results each, no status/evidence/reason deltas;0HTTP. These do not validate live model accuracy or latency. Verification receipt:tmp/semantic-batch-final-20261014-a.xml.
