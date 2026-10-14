# SkyCovione Batched Permit Judgement Performance Design

## Status

- Date: 2026-10-08
- Branch: `feat/permit-index-grouped-judgement`
- Scope: the exact Dongkook unknown-product `SKY-M` mapping path and the existing SkyCovione deterministic-first path
- Safety rule: other companies, products, mappings, and legacy permit policies must not change

## Intent

Reduce first-run review time without weakening permit authority or hiding missing evidence. The system must keep the permit as the controlling source, compare structured numeric, duration, unit, and qualitative facts locally where possible, and ask CLOVA only for semantically ambiguous groups. Independent ambiguous groups may share one provider request, but every group must retain its own identity, evidence, facts, response validation, verdict, and audit trail.

## Measured Baseline

The same SP and permit content produced these observed runs:

- API disabled: 39.892 seconds, zero provider calls, cold permit index 171 ms.
- API enabled: 625.399 seconds, 105 logical CLOVA calls, warm permit index 14 ms.
- LLM-related increase: 585.507 seconds, 93.62% of total runtime.
- Latest permit: 23 native-readable pages, zero OCR pages, 147 chunks, 251 parsed conditions.
- Latest legacy after-phase: 104 logical calls (1 SP semantic, 100 permit decisions, 3 grounding repairs).
- Provider response writes: median 5.560 seconds apart, mean 6.412 seconds, p95 14.377 seconds.

The current long runtime is therefore not caused by permit extraction, OCR, or index loading. It is caused by row-by-row provider traffic on the `submission-linked/legacy` route.

## Root Cause

The UI maps only this source identity to SkyCovione:

1. extracted company is `동국바이오사이언스`;
2. extracted product is `제품명 미확인`;
3. manufacturing/product number starts with `SKY-M`.

The mapped product becomes `스카이코비원멀티주`, but the provenance of that mapping is lost. Permit policy resolution later sees a Dongkook company plus an explicitly uploaded permit, produces `policy_id=submission-linked`, and keeps `evaluation_mode=legacy`. The deterministic-first group pipeline is therefore bypassed, and the authoritative permit is sent to CLOVA once per expanded row.

For the measured document, deterministic-first dry classification produced:

- 92 test records and 92 independent groups;
- 11 deterministic groups;
- 53 semantically ambiguous groups (44 unparsed-condition, 9 qualitative);
- 27 fail-closed groups (unit/candidate/batch/condition/fact incompatibility or absence);
- 1 genuine permit-miss primary fallback.

Calling the 53 ambiguous groups individually would still take several minutes. They must be batched without merging their verdict identities.

## Selected Architecture

### 1. Preserve mapping provenance

`InboxDocument` gains a backward-compatible trailing field:

```python
product_mapping_id: str = ""
```

The exact existing mapping stores:

```text
dongkook_unknown_sky_m_to_sky_covione
```

The value is passed through the app, judgement bridge, job identity, and permit resolver. It is not inferred later from the mapped product alone. This prevents the optimization from crossing into another company, an explicitly named product, or a non-`SKY-M` document.

For an explicitly linked permit with this exact mapping ID, the effective policy remains `policy_id=submission-linked` but uses the SkyCovione safety semantics:

- `authoritative=True`
- `ignore_section_numbers=True`
- `require_llm=True`
- `ocr_mode=auto`
- `evaluation_mode=deterministic_first`

All other submission-linked and catalog policies retain their current behavior. The policy ID is deliberately unchanged so the already valid permit index remains reusable.

### 2. Separate group preparation from provider execution

The grouped judge is split into two explicit phases:

1. Prepare a group using the current candidate, condition, fact, execution, and gate logic.
2. Resolve the prepared result:
   - deterministic verdict immediately;
   - fail-closed HOLD immediately;
   - genuine permit miss through the existing primary fallback;
   - semantic ambiguity through the batch coordinator.

The existing `validate_group_response` remains the authority for every provider result. Provider output cannot override deterministic failures, cite facts from another group, invent evidence IDs, omit mandatory conditions, or change a trusted permit quotation.

### 3. Batch only semantically ambiguous groups

A new batch protocol carries up to six independent group payloads per provider request. A stable greedy partition also limits combined input to 60,000 characters. A group larger than the character budget is sent alone and never truncated.

The wire response contains one item per requested `group_id`. Each item contains the same grounded fields as the existing one-group response:

- selected evidence ID;
- exactly one decision for each mandatory condition;
- condition-bound fact IDs;
- literal source quotation;
- group status and reason.

Batch validation requires:

- exact requested group-ID coverage;
- no duplicate or extra group IDs;
- no condition, fact, or evidence ID from another group;
- existing per-group grounding and deterministic recomputation.

Missing, duplicate, malformed, or cross-linked items become HOLD only for the affected batch/group. A provider service error remains an operational failure and stops publication, preserving the existing retry contract. Batch prompts and raw provider bodies are never written to user-visible metadata.

### 4. Cache boundaries

The submission permit fingerprint includes the effective policy projection and mapping ID:

- policy ID;
- authoritative flag;
- ignore-section-numbers flag;
- require-LLM flag;
- OCR mode;
- evaluation mode;
- product mapping ID.

This invalidates an aggregate legacy result when the mapped document enters deterministic-first mode, while leaving the content-addressed permit index warm.

The final judgement artifact identity also includes an LLM execution fingerprint containing only non-secret semantics:

- enabled versus disabled;
- model name;
- endpoint identity hash;
- batch protocol version.

It never includes or logs an API key. A result completed without a key cannot be reused after CLOVA becomes available.

Provider response caching continues to use the complete validated request signature. Identical permit/SP evidence batches can therefore be reused, while a changed result or policy produces a different key.

### 5. Metadata and UI progress

Add safe aggregate diagnostics:

- `permit_group_batch_count`
- `permit_group_batch_sizes`
- `permit_group_provider_calls`
- `permit_group_estimated_saved_calls`
- deterministic, ambiguous, fail-closed, and fallback group counts

Progress text reports `허가서 의미 판정 묶음 n/N` rather than one line per test. No prompt text, full local path, API key, or raw provider error is persisted.

## Failure Semantics

- Deterministic FAIL can never be promoted by CLOVA.
- Missing batch identity, incompatible units/methods, unusable permit evidence, or absent required facts remain HOLD and do not consume a provider request.
- A malformed batch response cannot partially authorize an unvalidated item.
- A CLOVA 401/403/429/timeout/connection failure remains an operational error; it is not converted into document PASS/FAIL/HOLD.
- A genuine permit test miss preserves the existing SP-primary fallback.
- Other products and policies remain on their current legacy or catalog-selected route.

## Expected Behavior Change

This change intentionally does not promise parity with the earlier `128 PASS / 1 FAIL / 2 HOLD` legacy run. That run asked the model to decide nearly every row even when batch identity, unit compatibility, or source facts were incomplete. The new route keeps 27 structurally unsupported groups fail-closed unless their local extraction/linking is separately improved. This is a safety property, not a performance shortcut.

For the measured document, the acceptance target is:

- 11 groups decided locally;
- 53 ambiguous groups in at most 9 provider requests;
- 27 structurally unsupported groups remain HOLD without provider calls;
- 1 primary fallback;
- no more than 10 after-phase provider calls, allowing one bounded compatibility/repair call;
- no policy or verdict change outside the exact mapped target and existing deterministic-first target.

Based on observed provider latency, this should reduce the current first-run wall time from about 10.4 minutes to roughly 1–2 minutes. Provider latency is external, so the executable acceptance criterion is the bounded call count and complete grounding validation, not a hard wall-clock threshold.

## Testing Strategy

### Mapping and policy isolation

- The exact Dongkook + unknown product + `SKY-M` mapping records the mapping ID.
- Other companies, known products, and non-`SKY-M` numbers do not record it.
- Only the exact mapping ID changes an explicit submission permit to deterministic-first.
- Explicit catalog `legacy` behavior for unrelated products remains unchanged.

### Batch protocol

- Stable partitioning honors six groups and 60,000 characters without truncation.
- Exact group-ID coverage is required.
- Duplicate, missing, extra, and cross-group evidence/fact/condition IDs fail closed.
- Per-group validation produces the same verdict as the existing one-group validator for valid fixtures.
- One malformed batch cannot authorize any malformed or missing item.
- Provider service failures propagate as operational failures.

### Pipeline integration

- The measured fixture shape (92 groups, 53 ambiguous) requires at most 9 provider calls with a fake client.
- Deterministic and fail-closed groups cause zero provider calls.
- Original record-position fan-out and table row separation remain intact.
- Existing non-target legacy pipeline tests remain byte-for-byte behavior compatible.

### Cache correctness

- Changing effective evaluation mode or mapping ID changes the submission fingerprint and aggregate artifact key.
- Enabled/disabled LLM mode, model, endpoint identity, or batch protocol version changes the artifact key.
- API keys are absent from keys, metadata, logs, and test snapshots.
- Policy routing changes do not change the permit-index key for identical PDF content/parser semantics.

### Verification

- Focused mapping, catalog, batch protocol, grouped pipeline, bridge cache, and service-failure suites.
- Offline benchmark with provider calls blocked.
- Full test suite.
- One optional live smoke run with the user-provided configuration, never used as the sole correctness proof.

## Rejected Alternatives

### Parallel per-row calls

Rejected. The transport intentionally serializes traffic per API identity, and removing the lock would reintroduce 429 failures and unsafe retries.

### Deterministic-first routing without batching

Rejected as incomplete. The current document still has 53 semantic groups, so one call per group remains several minutes.

### Sending fail-closed groups to the model

Rejected. A model cannot safely repair missing batch identity, incompatible units, or absent facts without inventing evidence.

### Global cache-version bump

Rejected. It would unnecessarily invalidate unrelated and potentially 300-page documents. Fingerprints must change only for the affected semantics.

### Reusing an offline completed artifact after enabling CLOVA

Rejected. LLM execution mode is part of the final result identity.

## Non-goals

- Increasing provider rate limits or changing the user's CLOVA plan.
- Weakening fail-closed rules to reproduce a high legacy PASS count.
- Replacing the persistent permit index or OCR subsystem.
- Changing unrelated company/product mapping or version judgement.
- Uploading or pushing the branch to GitHub during this work.
