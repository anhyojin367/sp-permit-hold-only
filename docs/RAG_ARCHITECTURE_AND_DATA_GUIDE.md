# SP AI 검수 RAG 구조 및 데이터 운영 가이드

## 1. 문서 목적

이 문서는 현재 시스템의 RAG 구조, 실제 판정 반영 위치, 데이터 추가 방법을 AI 개발자가 아닌 제약 도메인 담당자도 추적할 수 있도록 설명한다.

규칙 변경 이력과 PDF별 정답지 운영은
`docs/RULE_REGRESSION_AND_RAG_OPERATIONS.md`를 함께 참고한다.

현재 시스템에는 역할이 다른 세 종류의 외부 지식이 있다.

| 지식 종류 | 저장 위치 | 역할 |
|---|---|---|
| 공통 RAG | `rag_data/*.pdf`, UCUM JSON/JSONL/XLSX | 시험명·단위·약전·생기법 등 관련 근거 검색 |
| 연결 허가서 | 선택 문서의 허가서 PDF | 해당 제품의 보류 항목을 추가 판정 |
| 검수 디테일 | `sp_pdf_judger/domain_details/**/*.md` | 전체·회사·제품별 누락 방지 체크리스트 |

세 종류는 대체 관계가 아니다. 현재 SP의 명시적 기준과 결과가 우선이며, RAG와 디테일은 그 해석과 누락 점검을 돕는다.

RAG와 MD는 Python 검증기를 자동으로 만들거나 실행하는 계층이 아니다. 정확한 새 계산이나
새 구조 추출이 필요한 규칙은 추출기·검증기·테스트를 코드로 구현해야 한다.

## 2. 전체 처리 흐름

```text
SP PDF 선택
  → 문서 메타데이터에서 회사명·제품명 확인
  → 시험 레코드 추출 및 표 행 확장
  → 결정적 규칙으로 1차 비교
  → 시험명·기준·결과로 공통 RAG 검색
  → 전체 + 회사 + 제품 디테일 병합
  → 필요한 항목에 Gemini 판정 보조 요청
  → 보류 항목에 한해 연결 허가서 추가 검토
  → 제조단계별/전체 결과 집계 및 UI 표시
```

주요 코드 경로는 다음과 같다.

| 단계 | 코드 |
|---|---|
| RAG 적재·검색 | `sp_pdf_judger/rag.py` |
| 시험 판정과 RAG 후보 선택 | `sp_pdf_judger/judgement.py` |
| LLM 프롬프트 | `sp_pdf_judger/llm.py` |
| 허가서 검색 | `sp_pdf_judger/permit_pdf_store.py` |
| 디테일 선택·병합 | `sp_pdf_judger/domain_details.py` |
| 파이프라인 조립 | `sp_pdf_judger/pipeline.py` |
| 결과 캐시·화면 연결 | `sp_judgement_bridge.py`, `sp_app.py` |

## 3. 현재 RAG의 기술 구조

### 3.1 적재 소스

`UcumRagStore`는 시작할 때 다음 파일을 찾는다.

- 프로젝트 루트 `ucum_rag_docs.json`
- 프로젝트 루트 `ucum_rag_image_units_ui_micro.jsonl`
- 프로젝트 루트 `ucum_rag_image_units_enriched.jsonl`
- 프로젝트 루트 `TableOfExampleUcumCodesForElectronicMessaging.xlsx`
- 프로젝트 루트 `rag_data` 바로 아래의 모든 `*.pdf`

PDF는 하위 폴더를 재귀적으로 읽지 않는다. `rag_data/subfolder/a.pdf`가 아니라 `rag_data/a.pdf`로 둔다.

### 3.2 PDF 텍스트와 청크

- PyMuPDF가 각 페이지의 텍스트 레이어를 읽는다.
- 빈 줄 기준 문단을 묶고 최대 약 2,600자로 청크를 만든다.
- 각 청크에는 파일명, 페이지, 청크 번호, 자료 도메인이 기록된다.
- 파일명에 `허가`, `permit`, `약전`, `생물학적`, `기호`, `unit` 등이 있으면 검색 결과의 자료 도메인을 구분한다.

스캔 이미지만 있고 텍스트 레이어가 없는 PDF는 이 경로에서 검색되지 않는다. 이런 파일은 OCR된 PDF로 바꾸거나, 추출 텍스트를 JSON/JSONL로 넣어야 한다.

### 3.3 정규화

검색 전에 `µ/μ`, `㎎`, `㎖`, `㎛`, 위첨자, 온도 기호 등 단위 표기를 ASCII 또는 대표 표기로 확장한다. 예를 들어 `㎍`, `μg`, `ug`의 표기 차이 때문에 근거가 검색에서 누락되는 문제를 줄인다.

### 3.4 검색 방식

현재 구조는 외부 임베딩 API나 벡터 데이터베이스를 사용하지 않는 로컬 TF-IDF 검색이다.

- 단어 1~2그램 TF-IDF: 65%
- 문자 2~5그램 TF-IDF: 35%
- 두 코사인 유사도 점수를 합산하여 상위 문서를 선택

문자 TF-IDF를 함께 쓰는 이유는 OCR 오탈자, 한글 조사, 단위 기호 차이에 조금 더 견고하게 검색하기 위해서다.

`JudgeEngine._search_rag_contexts()`는 단순 상위 결과만 쓰지 않고, 허가서·기준서·기호 사전 등 서로 다른 자료 도메인의 첫 결과가 함께 들어가도록 최대 9개 근거를 구성한다.

## 4. 판정에 실제로 반영되는 방식

### 4.1 결정적 규칙이 우선

수치, 범위, 단위, 정성 표현 등 코드로 확정할 수 있는 비교는 결정적 규칙이 우선한다. LLM과 RAG는 확정 결과를 임의로 뒤집는 용도가 아니다.

### 4.2 LLM 입력

LLM이 호출될 때 다음 입력이 함께 전달된다.

- 시험명
- 시험기준
- 시험결과
- 결정적 판정 근거 또는 단계 지시
- 공통 RAG 검색 결과
- 선택된 전체 디테일
- 선택된 회사 디테일
- 선택된 제품 디테일

예를 들어 회사가 `GC녹십자`, 제품이 `녹십자-알부민주20%`로 인식되면 다음 세 파일이 함께 병합된다.

```text
domain_details/overall.md
domain_details/companies/gc_biopharma.md
domain_details/products/albumin.md
```

### 4.3 허가서 반영

연결 허가서는 공통 `rag_data`와 별도의 `PermitPdfStore`로 관리한다. 1차 및 보조 판정 뒤에도 보류인 시험에 현재 시험과 직접 연결되는 허가서 근거가 있을 때만 추가 판정한다.

### 4.4 추적 메타데이터

최종 `ProcessingResult.metadata`에는 다음 값이 남는다.

- `rag_doc_count`, `rag_sources`
- `domain_detail_sources`
- `domain_detail_matched_company`, `domain_detail_matched_product`
- `domain_detail_fingerprint`
- `document_company`, `document_product`
- `llm_model`, `llm_call_count`, `llm_success_count`, `llm_last_error`

이 값으로 어떤 데이터와 디테일이 실제 요청에 사용되었는지 확인할 수 있다.

## 5. 공통 RAG 데이터 추가 방법

### 5.1 PDF 추가

1. 승인된 참고 PDF를 `rag_data`에 넣는다.
2. 파일명에 자료 성격을 알 수 있는 단어를 넣는다.

```text
rag_data/대한민국약전_일반시험법_2026.pdf
rag_data/생물학적제제_기준및시험방법_2026.pdf
```

3. PDF에서 텍스트 선택과 복사가 되는지 확인한다.
4. Streamlit 프로세스를 재시작한다.
5. 로그 또는 결과 메타데이터의 `rag_sources`에 파일명이 포함되는지 확인한다.

RAG는 파이프라인 생성 시 메모리에 적재되므로 실행 중 파일만 복사해서는 기존 파이프라인에 반영되지 않는다.

### 5.2 JSON 추가

`ucum_rag_docs.json`은 객체 배열이며 다음 필드를 검색 텍스트로 사용한다.

```json
[
  {
    "kind": "unit",
    "code": "mg/mL",
    "name": "밀리그램 퍼 밀리리터",
    "symbol": "mg/mL",
    "property": "mass concentration",
    "text": "농도 단위 설명"
  }
]
```

새 필드는 보존되지만 위 필드에 중요한 검색어가 있어야 검색 품질이 안정적이다.

### 5.3 JSONL 추가

한 줄에 JSON 객체 하나를 둔다. 대표 필드는 다음과 같다.

```json
{"major_category_ko":"질량농도","display_name_ko":"밀리그램 퍼 밀리리터","canonical_ucum":"mg/mL","aliases":["mg per mL"],"text":"농도 단위"}
```

파일명은 현재 `config.py`의 `UCUM_JSONL_CANDIDATES`에 등록해야 한다. 단순히 임의 이름의 JSONL을 놓는 것만으로는 자동 적재되지 않는다.

### 5.4 XLSX 추가

현재 XLSX 적재기는 UCUM 예제 파일의 첫 시트와 고정 열 구조를 전제로 한다. 다른 표 구조를 추가하려면 `rag.py`에 전용 로더를 구현해야 한다. 제약 기준표를 임의로 같은 형식에 끼워 넣는 방식은 권장하지 않는다.

## 6. 데이터 품질 및 승인 절차

### 현재 데이터 배치에서 확인된 주의점

현재 `rag_data`에는 파일명에 `스카이코비원멀티주_허가서`가 포함된 제품별 허가서 PDF도 들어 있다. `rag_data`의 PDF는 회사·제품과 무관하게 전역 검색 대상이므로, 검색어가 비슷하면 다른 회사나 제품의 참고 후보로 노출될 수 있다.

운영 전에는 제품별 허가서를 전역 `rag_data`에서 분리하고, 해당 SP 문서에 연결된 `PermitPdfStore` 경로로만 제공하는 것이 안전하다. 전역 `rag_data`에는 약전, 생물학적제제 기준 및 시험방법, 단위 사전처럼 여러 제품에 공통으로 적용되는 자료를 두는 것을 권장한다.

추가 전 최소 확인 항목:

- 자료명, 발행기관, 버전, 시행일
- 회사 내부 문서라면 사용 승인 여부
- 스캔 PDF의 OCR 품질
- 표의 열과 행이 텍스트 추출 시 유지되는지
- 이전 버전과 충돌하는 조항 존재 여부
- 개인정보 및 영업비밀 포함 여부

권장 운영 절차:

```text
제약 담당자 초안 등록
→ 출처와 버전 확인
→ AI 담당자 검색 테스트
→ 제약 담당자 내용 승인
→ main 브랜치 반영
→ 대표 문서 회귀 테스트
```

## 7. 검증 명령

회사·제품 디테일이 어떻게 선택되는지 확인:

```powershell
python -m sp_pdf_judger.domain_details `
  --company "GC녹십자" `
  --product "녹십자-알부민주20%" `
  --show-context
```

RAG 적재 수와 소스 확인:

```powershell
python -c "from sp_pdf_judger.rag import UcumRagStore; s=UcumRagStore(); print(len(s.docs)); print(*s.loaded_sources, sep='\n')"
```

테스트:

```powershell
python -m pytest tests/test_domain_details.py
```

## 8. 변경 시 주의사항

- 허가 수치나 시험기준을 출처 없이 디테일 MD에 직접 확정값으로 쓰지 않는다.
- 같은 회사에 별칭이 겹치는 MD를 여러 개 만들지 않는다.
- 제품명 매칭이 지나치게 넓은 별칭(예: `주사`, `백신`)은 사용하지 않는다.
- 회사 또는 제품 MD가 수정되면 내용 지문이 바뀌어 해당 조합의 판정 캐시는 자동 무효화된다.
- `sp_pdf_judger/md_file`의 기존 YAML은 현재 판정 컨텍스트로 사용하지 않는다. 새 디테일은 `domain_details`에 작성한다.
