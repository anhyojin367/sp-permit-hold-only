# SkyCovione Authoritative Permit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register the supplied SkyCovione permit, recover its Korean text reliably, map SP tests to permit tests without using dotted section numbers, and let an LLM make permit-authoritative verdicts only for SK bioscience SkyCovione documents.

**Architecture:** A product permit catalog resolves an immutable PDF and a typed product policy before cache-key construction. A permit-specific OCR/extraction layer produces document-wide hierarchical chunks, and `JudgeEngine` receives the resolved policy so the existing primary verdict is preserved for non-target products while a matched target-product permit verdict can override it.

**Tech Stack:** Python 3.12, PyMuPDF, Pydantic, pytest, Windows Media OCR through Windows PowerShell 5.1, JSON configuration.

**Spec:** `docs/superpowers/specs/2026-09-16-sky-covione-authoritative-permit-design.md`

## Global Constraints

- Activate authoritative permit behavior only when both company and product resolve to SK bioscience and SkyCovione aliases.
- Ignore dotted heading numbers only for matching; preserve them as source metadata.
- Preserve the complete permit test paragraph across physical page boundaries.
- A mapped permit verdict overrides the SP-only verdict; an absent permit test preserves the SP-only verdict.
- An ambiguous mapping, invalid permit text, LLM failure, or 429 produces `검수보류` for a permit candidate instead of silently accepting an SP result.
- A permit-based failure reason must name the concrete permit condition and conflicting SP result.
- Existing behavior for every other company and product must remain unchanged.
- No test may call an external network service.

---

### Task 1: Product permit catalog and exact policy resolution

**Files:**
- Create: `sp_pdf_judger/permit_catalog.py`
- Create: `sp_pdf_judger/permits/catalog.json`
- Test: `tests/test_permit_catalog.py`

**Interfaces:**
- Produces: `PermitPolicy(policy_id: str, authoritative: bool, ignore_section_numbers: bool, require_llm: bool, ocr_mode: str)`.
- Produces: `ResolvedPermit(policy: PermitPolicy | None, paths: tuple[Path, ...], fingerprint: str, errors: tuple[str, ...])`.
- Produces: `resolve_permits(explicit_paths: Iterable[Path], company: str | None, product: str | None, package_dir: Path | None = None) -> ResolvedPermit`.

- [ ] **Step 1: Write failing policy-resolution tests**

```python
from pathlib import Path

from sp_pdf_judger.permit_catalog import resolve_permits


def test_sky_policy_requires_matching_company_and_product(tmp_path: Path) -> None:
    document = tmp_path / "sky.pdf"
    document.write_bytes(b"permit")
    package = tmp_path / "sp_pdf_judger"
    (package / "permits" / "documents").mkdir(parents=True)
    target = package / "permits" / "documents" / "sky_covione_multidose.pdf"
    target.write_bytes(document.read_bytes())
    (package / "permits" / "catalog.json").write_text(
        '{"permits":[{"policy_id":"sky","companies":["SK바이오사이언스"],'
        '"products":["스카이코비원멀티주"],"path":"permits/documents/sky_covione_multidose.pdf",'
        '"sha256":"85441a43c7cb9f242a5c20c6648026689c795ad4e8321e215bf68d48fc9a4b1c",'
        '"authoritative":true,"ignore_section_numbers":true,"require_llm":true,"ocr_mode":"auto"}]}',
        encoding="utf-8",
    )

    matched = resolve_permits([], "SK바이오사이언스", "스카이코비원멀티주", package)
    other_company = resolve_permits([], "동국바이오사이언스", "스카이코비원멀티주", package)
    other_product = resolve_permits([], "SK바이오사이언스", "다른제품", package)

    assert matched.policy is not None and matched.policy.authoritative is True
    assert matched.policy.ignore_section_numbers is True
    assert matched.paths == (target.resolve(),)
    assert other_company.policy is None and other_company.paths == ()
    assert other_product.policy is None and other_product.paths == ()
```

Use the hand-calculated SHA-256 for the fixture bytes in the final test fixture, or compute it once as fixture setup data and write the literal catalog value; do not call production hashing helpers to derive both actual and expected values.

- [ ] **Step 2: Run the new test and verify RED**

Run: `python -m pytest -q tests/test_permit_catalog.py`

Expected: import failure for `sp_pdf_judger.permit_catalog`.

- [ ] **Step 3: Implement catalog parsing and normalization**

Implement immutable dataclasses and normalization that casefolds, removes punctuation/whitespace, and normalizes `에스케이바이오사이언스`/`SK bioscience` only through catalog aliases. Reject a catalog PDF when its SHA-256 differs; return the error without adding the path. Merge valid explicit paths first, then the catalog path, de-duplicated by resolved path.

`catalog.json` must register:

```json
{
  "permits": [
    {
      "policy_id": "sk_bioscience_sky_covione_multidose",
      "companies": ["SK바이오사이언스", "에스케이바이오사이언스", "SK bioscience"],
      "products": ["스카이코비원멀티주", "스카이코비원", "SKYCovione"],
      "path": "permits/documents/sky_covione_multidose.pdf",
      "sha256": "c430105ce6c9bd0d9ef652912439735aa4bc01b86d94d74b97f745166fb56ea1",
      "authoritative": true,
      "ignore_section_numbers": true,
      "require_llm": true,
      "ocr_mode": "auto"
    }
  ]
}
```

- [ ] **Step 4: Verify GREEN and non-target regression**

Run: `python -m pytest -q tests/test_permit_catalog.py tests/test_sky_covione_mapping.py`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add sp_pdf_judger/permit_catalog.py sp_pdf_judger/permits/catalog.json tests/test_permit_catalog.py
git commit -m "feat: add product-scoped permit catalog"
```

---

### Task 2: Reliable Korean text recovery for permit PDFs

**Files:**
- Create: `sp_pdf_judger/permit_ocr.py`
- Create: `scripts/windows_ocr_images.ps1`
- Test: `tests/test_permit_ocr.py`

**Interfaces:**
- Produces: `PermitPageText(page_number: int, text: str, source: str)`.
- Produces: `looks_corrupt_permit_text(text: str) -> bool`.
- Produces: `extract_permit_page_texts(pdf_path: Path, ocr_mode: str = "auto") -> tuple[list[PermitPageText], list[str]]`.

- [ ] **Step 1: Write failing corruption and fallback tests**

```python
from pathlib import Path

from sp_pdf_judger.permit_ocr import looks_corrupt_permit_text, select_page_text


def test_replacement_character_heavy_native_text_uses_ocr() -> None:
    native = "2.1.2.1.8. �����������̷����������� ��Ų"
    ocr = "2.1.2.1.8. 돼지유래바이러스부정시험 최소 21일간 배양"

    assert looks_corrupt_permit_text(native) is True
    assert select_page_text(native, ocr) == (ocr, "windows_ocr")


def test_valid_korean_native_text_does_not_use_ocr() -> None:
    native = "2.3.1.7. 확인시험 Component B 중간체원액에 대한 시험"

    assert looks_corrupt_permit_text(native) is False
    assert select_page_text(native, "잘못된 OCR") == (native, "native")
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest -q tests/test_permit_ocr.py`

Expected: import failure for `sp_pdf_judger.permit_ocr`.

- [ ] **Step 3: Implement extraction and the Windows OCR helper**

`permit_ocr.py` must:

```python
@dataclass(frozen=True)
class PermitPageText:
    page_number: int
    text: str
    source: str


def select_page_text(native: str, ocr: str) -> tuple[str, str]:
    if not looks_corrupt_permit_text(native):
        return clean_text(native), "native"
    if _is_usable_korean_text(ocr):
        return clean_text(ocr), "windows_ocr"
    return "", "unreadable"
```

Render only corrupt pages at approximately 180 DPI into `%TEMP%/sp_pdf_judger_permit_ocr/{pdf_sha256}/`. Run the fixed PowerShell helper with `subprocess.run([...], shell=False, check=False, capture_output=True, text=True, encoding="utf-8")`. Cache page text and the PDF hash in UTF-8 JSON. On non-Windows or Windows OCR failure, call Tesseract only when `pytesseract.get_tesseract_version()` succeeds; otherwise return a page error.

`scripts/windows_ocr_images.ps1` must enumerate `page-*.png`, preserve OCR line boundaries with `OcrResult.Lines`, set UTF-8 output, and emit one JSON object:

```json
{"pages":[{"page_number":1,"text":"..."}],"errors":[]}
```

- [ ] **Step 4: Run unit tests and a source-PDF smoke check**

Run: `python -m pytest -q tests/test_permit_ocr.py`

Then run a local smoke command that calls `extract_permit_page_texts()` for the supplied PDF and asserts that page 4 contains `돼지유래바이러스부정시험` and `21일`, and page 5 contains `유정란접종시험`, `80%`, and `혈구응집반응`.

Expected: unit tests pass; smoke command exits 0 without network access.

- [ ] **Step 5: Commit**

```powershell
git add sp_pdf_judger/permit_ocr.py scripts/windows_ocr_images.ps1 tests/test_permit_ocr.py
git commit -m "feat: recover Korean permit text with OCR fallback"
```

---

### Task 3: Hierarchical, number-independent permit chunks

**Files:**
- Modify: `sp_pdf_judger/permit_pdf_store.py`
- Test: `tests/test_permit_pdf_store.py`

**Interfaces:**
- Extends: `PermitChunk` with `page_start`, `page_end`, `section_path_titles`, `normalized_test_name`, `normalized_stage_path` while retaining `page_number` compatibility.
- Produces: `normalize_permit_heading(text: str) -> str`.
- Changes: `PermitPdfStore(..., policy: PermitPolicy | None = None, page_text_extractor=extract_permit_page_texts)`.

- [ ] **Step 1: Write failing parsing and retrieval tests**

```python
from sp_pdf_judger.permit_catalog import PermitPolicy
from sp_pdf_judger.permit_pdf_store import PermitPdfStore
from sp_pdf_judger.schemas import ExtractedRecord


SKY_POLICY = PermitPolicy(
    policy_id="sky", authoritative=True, ignore_section_numbers=True,
    require_llm=True, ocr_mode="auto",
)


def test_component_b_identity_ignores_dotted_numbers_and_keeps_full_body() -> None:
    pages = [
        (1, "2.3. Component B 중간체 원액\n2.3.1. Component B 중간체원액에 대한 시험\n2.3.1.7. 확인시험\nDot Blot 시험법에 따라 시험한다."),
        (2, "반응 후 발색된 점이 확인되어야 한다.\n2.3.1.8. 엔도톡신시험\n820 EU/mg 미만이어야 한다."),
    ]
    store = PermitPdfStore.from_page_texts(pages, policy=SKY_POLICY, source_file="permit.pdf")
    record = ExtractedRecord(
        record_type="test",
        section_number="9.8.7",
        section_title="Component B 중간체 원액",
        section_path=["원액 시험", "Component B 중간체 원액"],
        test_name="2.1.99 확인시험",
        criteria="확인되어야 함",
        result="발색된 점 확인",
    )

    chunks = store.search(record, top_k=3)

    assert chunks[0].title == "확인시험"
    assert chunks[0].section_path_titles[-2:] == ("Component B 중간체원액에 대한 시험", "확인시험")
    assert "Dot Blot" in chunks[0].text
    assert "발색된 점이 확인되어야 한다" in chunks[0].text
    assert "엔도톡신" not in chunks[0].text
```

Add a second test with CHO and Component B `확인시험` chunks; mutating the stage score to ignore the Component B path must make the test fail.

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest -q tests/test_permit_pdf_store.py`

Expected: missing constructor/helper or wrong first chunk/body.

- [ ] **Step 3: Implement document-wide section parsing**

Use dotted numbers only to infer heading depth. Strip a leading `^\s*\d+(?:\.\d+)+(?:\.)?\s*` from normalized identities. Append text before the first heading on a later page to the previous open chunk. Score normalized test-name tokens and stage-path tokens; do not award exact/prefix section-number points when `policy.ignore_section_numbers` is true. Generic titles such as `확인시험`, `성상`, and `무균시험` require a stage-path overlap before they can rank as direct matches.

- [ ] **Step 4: Run store, OCR, and existing semantic tests**

Run: `python -m pytest -q tests/test_permit_pdf_store.py tests/test_permit_ocr.py tests/test_table_and_semantic_judgement.py`

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add sp_pdf_judger/permit_pdf_store.py tests/test_permit_pdf_store.py
git commit -m "feat: map permit tests by stage and semantic title"
```

---

### Task 4: Structured authoritative permit LLM verdict

**Files:**
- Modify: `sp_pdf_judger/llm.py`
- Modify: `sp_pdf_judger/judgement.py`
- Create: `tests/test_sky_covione_authoritative_permit.py`

**Interfaces:**
- Extends: `JudgeResponse` with optional `permit_match_status`, `matched_permit_test`, `permit_basis`, and `failed_requirements`.
- Changes: `JudgeEngine(..., permit_policy: PermitPolicy | None = None)`.
- Produces source: `permit_pdf_llm_authoritative`.

- [ ] **Step 1: Write a failing override test**

```python
from sp_pdf_judger.config import FAIL_LABEL, PASS_LABEL
from sp_pdf_judger.judgement import JudgeEngine
from sp_pdf_judger.llm import JudgeResponse
from sp_pdf_judger.permit_catalog import PermitPolicy
from sp_pdf_judger.permit_pdf_store import PermitChunk
from sp_pdf_judger.rag import UcumRagStore
from sp_pdf_judger.schemas import ExtractedRecord


class CapturingPermitLlm:
    enabled = True
    def __init__(self, response):
        self.response = response
        self.calls = []
    def explain(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class StaticPermitStore:
    enabled = True
    extraction_errors = []
    def search(self, record, top_k=5):
        return [PermitChunk(
            source_file="permit.pdf", page_number=5, page_start=5, page_end=5,
            section_number="2.1.2.1.10.3", title="유정란접종시험",
            section_path_titles=("CHO 마스터 세포주에 대한 시험", "외래성인자부정시험", "유정란접종시험"),
            normalized_test_name="유정란접종시험", normalized_stage_path="cho마스터세포주 외래성인자부정시험",
            text="요막강과 난황낭 시험은 각각 최소 10개를 사용하고, 각 배양 및 계대 후 생존률 80% 이상이며 혈구응집반응이 없어야 한다.",
        )]


def test_matched_permit_failure_overrides_sp_rule_pass() -> None:
    llm = CapturingPermitLlm(JudgeResponse(
        status=FAIL_LABEL,
        reason="혈구응집반응이 확인되어 검수불합격으로 판단했습니다.",
        normalized_criteria="혈구응집반응이 없어야 함",
        normalized_result="혈구응집반응 확인",
        permit_match_status="matched",
        matched_permit_test="CHO 마스터 세포주 > 유정란접종시험",
        permit_basis="혈구응집반응이 없어야 한다",
        failed_requirements=["혈구응집반응이 없어야 한다"],
    ))
    policy = PermitPolicy("sky", True, True, True, "auto")
    engine = JudgeEngine(UcumRagStore(), llm, StaticPermitStore(), permit_policy=policy)

    evaluation = engine.judge_record(ExtractedRecord(
        record_type="test", section_title="CHO 마스터 세포주",
        test_name="9.4 유정란접종시험", criteria="생존률 80% 이상",
        result="생존률 90%, 혈구응집반응 확인",
    ))

    assert evaluation.final_status == FAIL_LABEL
    assert evaluation.source == "permit_pdf_llm_authoritative"
    assert "혈구응집반응이 없어야" in evaluation.reason
    assert "혈구응집반응 확인" in evaluation.reason
    assert llm.calls[0]["authoritative_permit"] is True
```

Add focused tests for `not_found` preserving the original SP PASS, `ambiguous` producing HOLD, a `None` LLM response producing HOLD, all complex permit criteria being present in the LLM `criteria`/context, and the same record with `permit_policy=None` preserving existing behavior.

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest -q tests/test_sky_covione_authoritative_permit.py`

Expected: `JudgeResponse` rejects new fields or `JudgeEngine` lacks `permit_policy`; after schema acceptance the override assertion must still fail because current code calls permits only on HOLD.

- [ ] **Step 3: Implement the structured LLM contract**

Add optional fields with defaults so existing callers remain compatible. Extend `ClovaJudgeClient.explain` with keyword defaults `authoritative_permit: bool = False` and `record_context: str | None = None`. In authoritative mode, the prompt must require one of the three match statuses, prohibit section-number matching, require every condition to be checked, and state that the permit wins over SP criteria.

In `JudgeEngine._judge_single_value`, always compute the existing primary result first. When an authoritative policy and permit store are present, run the permit phase regardless of PASS/FAIL/HOLD. Apply:

```python
if match_status == "matched":
    return permit_verdict
if match_status == "ambiguous":
    return HOLD_LABEL, ambiguity_reason, ...
if match_status == "not_found":
    return primary_verdict
if response_missing_or_invalid:
    return HOLD_LABEL, safe_unavailable_reason, ...
```

For FAIL, construct the final Korean reason from `permit_basis` or the first `failed_requirements` entry plus the normalized/result text. Do not expose API errors. Retain the existing HOLD-only permit branch byte-for-byte in behavior when the policy is absent or non-authoritative.

- [ ] **Step 4: Verify GREEN and regression behavior**

Run: `python -m pytest -q tests/test_sky_covione_authoritative_permit.py tests/test_gemini_second_phase_judgement.py tests/test_table_and_semantic_judgement.py`

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add sp_pdf_judger/llm.py sp_pdf_judger/judgement.py tests/test_sky_covione_authoritative_permit.py
git commit -m "feat: make SkyCovione permit verdict authoritative"
```

---

### Task 5: Pipeline, bridge cache, and bundled permit integration

**Files:**
- Modify: `sp_pdf_judger/pipeline.py`
- Modify: `sp_judgement_bridge.py`
- Create: `sp_pdf_judger/permits/documents/sky_covione_multidose.pdf`
- Modify: `tests/test_sky_covione_authoritative_permit.py`

**Interfaces:**
- Consumes: `resolve_permits()` and `PermitPolicy` from Task 1.
- Passes: resolved policy to `PermitPdfStore` and `JudgeEngine`.
- Records: resolved paths, policy ID, fingerprint, and extraction errors in result metadata/cache signature.

- [ ] **Step 1: Add a failing pipeline-scope test**

Construct two `DocumentJudgePipeline` instances using an injected temporary catalog/package directory or a monkeypatched catalog resolver: one for SK bioscience/SkyCovione and one for another product. Assert the target engine has an authoritative policy and default permit path, while the other engine has neither. Assert an explicitly linked non-target permit still creates a store but does not enable authoritative mode.

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest -q tests/test_sky_covione_authoritative_permit.py::test_pipeline_scopes_authoritative_policy_to_exact_company_and_product`

Expected: target pipeline has no policy/default path.

- [ ] **Step 3: Bundle and integrate the permit**

Copy the user-supplied PDF without re-encoding to `sp_pdf_judger/permits/documents/sky_covione_multidose.pdf` and verify SHA-256 equals `c430105ce6c9bd0d9ef652912439735aa4bc01b86d94d74b97f745166fb56ea1`.

Resolve permit paths once in `ensure_judgement_artifacts` before `_cache_key`. Use the resolved fingerprint in the key, pass resolved paths/company/product to both pipeline phases, and expose resolved permit paths in artifacts. In `DocumentJudgePipeline`, pass the policy into `PermitPdfStore` and `JudgeEngine`; add `permit_policy_id` and extraction diagnostics to metadata without changing existing keys.

- [ ] **Step 4: Verify targeted integration tests**

Run: `python -m pytest -q tests/test_permit_catalog.py tests/test_permit_ocr.py tests/test_permit_pdf_store.py tests/test_sky_covione_authoritative_permit.py`

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add sp_pdf_judger/pipeline.py sp_judgement_bridge.py sp_pdf_judger/permits/documents/sky_covione_multidose.pdf tests/test_sky_covione_authoritative_permit.py
git commit -m "feat: bundle and auto-apply SkyCovione permit"
```

---

### Task 6: End-to-end regression and requirement audit

**Files:**
- Modify only if a failing regression exposes an in-scope defect.
- Test: all files under `tests/`.

**Interfaces:**
- Validates all outputs produced by Tasks 1-5.

- [ ] **Step 1: Run the supplied-PDF extraction smoke test**

Run the offline smoke test from Task 2 twice. The first run must report OCR or native/OCR mixed sources; the second run must load the SHA-keyed cache. Both runs must contain the Component B confirmation-test text and the complete embryonated-egg criteria tokens.

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest -q tests`

Expected: zero failures.

- [ ] **Step 3: Audit the user requirements against evidence**

Check each literal requirement against a passing test or smoke output:

- number prefixes ignored;
- Component B stage disambiguates confirmation test;
- permit is authoritative only for exact company/product;
- complete multi-condition criteria reaches LLM;
- shorter-than-21-day example can be rejected by a matched LLM response;
- mismatch reason includes the permit condition;
- LLM failure is HOLD;
- other products keep existing behavior.

- [ ] **Step 4: Inspect git diff and generated files**

Run: `git status --short`, `git diff --check`, and `git diff --stat 9a12389..HEAD`. Confirm `.sp_judgement_status`, incoming PDFs, outputs, OCR caches, and temporary rendered images are not staged. If this verification exposes an in-scope defect, return to the owning task, add a failing regression test, fix it, rerun that task's checks, and amend that task with a new focused commit rather than creating an untested verification-only change.
