# SP 시험결과 자동검수 시스템

## 2026-10-15: 허가서 없는 SP 단독 검수

- 기존 업로드 화면에서 **SP PDF만 등록**할 수 있습니다. 허가서는 선택 사항입니다.
- 허가서 없이 검수하면 **SP 룰 전용 · 허가서 미검증**으로 표시하고, 기존 수치·기간·단위·정합성 규칙만 실행합니다. 기본 허가서 자동 연결과 LLM/API 판정은 사용하지 않습니다.
- 규칙으로 확정할 수 없는 항목은 보류입니다. 허가서 대조 전용 규칙은 미검증(N/A)으로 기록하며, 허가사항 적합을 확인했다는 뜻이 아닙니다.
- 허가서를 함께 올리거나 **기존 SP의 허가서 추가·교체**로 연결한 뒤 다시 검수하면 SP 기본 판정의 보류 항목에만 허가서를 참조하는 기존 방식으로 처리합니다. 두 모드의 결과 캐시는 구분합니다.
- 연결해 둔 허가서가 누락되었거나 연결 정보가 잘못되면 오류로 안내합니다. 이 경우 SP 단독 검수로 자동 전환하지 않습니다.

> **2026-10-07 추가 검토 의견 반영 · 화면 v112** — 시험별 ‘허가서 근거 문장 보기’와 R07 등 MD 근거 펼침에도 동일한 표시 정리를 적용합니다. 문서확인번호·페이지 꼬리말·PDF의 짧은 줄바꿈이 다시 나타나던 두 출력 경로를 수정했습니다. 원래 근거 데이터와 PDF 쪽 링크, 판정·MD 기준은 유지하며 정리된 문장을 원문 그대로라고 표시하지 않습니다. [원인·수정·검증 범위](docs/FEEDBACK_20261007.md)를 참고하세요. 아래 v111의 통신 오류 처리도 유지합니다.

> **2026-10-07 CLOVA 통신 오류 처리 · 화면 v111** — 호출 제한·시간 초과를 문서 보류와 분리하고, 제한된 재시도 후 남은 검수를 중단합니다. 이전의 텍스트 표시·R24 레이아웃·최종 화면 진행 표시를 유지합니다. 기존 정상 결과의 판정 캐시 버전과 MD 기준은 바꾸지 않습니다.

### v111 호출·재시도 정책

- 요청 한 건은 **최초 1회 + 최대 2회 재시도**입니다. 개별 요청 timeout은 최대 60초, 호출 간 대기와 재시도 시작 여부는 150초 예산으로 제한합니다. 이는 HTTP 전송 각 단계의 timeout이며 문서 전체가 150초 안에 끝난다는 보장은 아닙니다.
- 429·연결/응답 시간 초과·5xx는 제한적으로 재시도하고, 401/403·일반 요청 오류는 즉시 중단합니다. 형식 대체 재요청은 JSON 검증 실패 또는 `response_format`/`json_schema` 미지원이 명시된 오류에만 허용합니다. 형식 대체로 회복한 요청은 논리 호출 1건 성공으로 기록합니다.
- `Retry-After` 및 CLOVA의 요청/토큰 reset 헤더를 따릅니다. 429 후에는 같은 서버 프로세스에서 호출 간격을 늘리고, 성공 한 번으로 즉시 원래 속도로 돌아가지 않습니다. 이 제어는 **서버 프로세스 단위**이며 다른 PC/서버의 같은 계정 호출까지 통제하지 않습니다. [네이버 공식 이용량 제어 안내](https://guide.ncloud-docs.com/docs/en/clovastudio-ratelimiting).
- 재시도 소진 시 나머지 시험·MD 의미 판정을 계속 호출하지 않습니다. 화면에 오류 종류와 HTTP/제공자 코드(있는 경우)를 표시하며, 실패를 충족/불충족/보류나 완료 집계로 발행하지 않습니다. API 키·원문·서버 응답 본문은 오류 로그에 넣지 않습니다.
- `.sp_judgement_status/clova_responses/`에 스키마 검증을 통과한 응답만 저장합니다. 동일 요청·모델·출력 스키마·API 주소·MD 지문일 때 재사용하여 명시적 재검수의 중복 호출을 줄입니다. 중간 추출 단계 재개 기능은 아니며, 캐시가 삭제되거나 서버 저장소가 초기화되면 재사용할 수 없습니다. 외부 호출 실패는 캐시하지 않습니다. 실제 제공자 호출 수와 저장 응답을 포함하는 논리 호출 수는 다릅니다.
- 운영 실패는 작업 폴더의 `service_failure.json`과 문서함의 실패 상태로 보존합니다. 새로고침/결과 열람은 자동 재호출하지 않고, 사용자가 **검수 진행**을 눌러야 실패 자료를 별도 보관한 뒤 재시도합니다. 이 런타임 자료는 Git에 올리지 않습니다.
- 검증: 전체 회귀 **1,827 passed / 7 skipped / 실패 0**(Python 3.14, 705.38초). 실행 중 추가한 실제 SDK 모의 HTTP·근거 보정 실패 시험을 포함한 별도 집중 39시험도 통과했습니다(전체와 중복되므로 합산하지 않음). 저장 D00~D16/허가서 카드 1,510개 재표시에서 원래 판정·집계 불변(API 0), 로컬 v111 홈 표시 확인. 이 시험들은 공개 서버의 새 CLOVA 검수 성공을 뜻하지 않으며 서비스 한도나 외부 장애 자체를 제거한 것은 아닙니다.

> **2026-10-06 최종 화면 대기 표시 · 화면 v110** — 최종 판정 페이지도 진행 단계·경과 시간을 갱신하고, 기다리는 동안 문서 목록으로 돌아갈 수 있게 했습니다. 같은 입력은 기존 작업에 연결하며, API 실패를 정상 판정으로 대체하지 않습니다. 아래 v109의 텍스트·레이아웃 수정도 모두 포함합니다.

> **2026-10-06 전체 검토 의견 반영 · 화면 v109** — 허가서 줄바꿈·페이지 꼬리말·위첨자 표시와 D07/D08의 R24 긴 설명 레이아웃을 수정했습니다. 최신 로직 검토 Word의 D00~D16은 모두 판정 이상없음으로 확인되어 MD·판정 조건·집계는 유지했습니다. [텍스트 표시 수정](docs/FEEDBACK_20261006.md) 및 [로직 검토·배포 범위](docs/LOGIC_REVIEW_20261006.md)를 참고하세요. 검수 영수증의 판정 릴리스와 화면 버전은 별개이며, 예전 결과를 새 검수로 바꾸지 않습니다.

> **2026-10-02 재검토 의견 반영 · v108** — 단위 대응 근거, 관련 MD 번호와 구현 범위, 집계 기준, 원문 펼치기, 검수 파일·코드 지문 및 다크 설정에서의 가독성을 보강했습니다. 기존 판정 기준과 결과 건수 계산은 유지합니다. [수정 내용·검증 기록](docs/FEEDBACK_20261002.md)을 참고하세요.

> **2026-10-01 검토 의견 반영 · v107** — 새 D00~D16(교체된 D06·D13 포함)에 맞춰 시험의 최종 상태와 이유, IU/EU 동일 분모 비교, 누락 공정의 연쇄 보류, 제품명 오류 문서의 검수 진입을 보강했습니다. A/B/C·공통 요구사항 82개를 삭제 없이 MD에 보존하고 실행 범위와 미지원 범위를 구분했습니다. [변경 내용과 검증 기록](docs/FEEDBACK_20261001.md)을 먼저 읽어 주세요. 82개 모두가 임의 문서에서 완전 자동 구현되었다는 뜻은 아닙니다.

> **2026-09-30 배포 호환성 수정 · v106** — 실제 배포 로그의 Python 3.14.7에 맞춰 실행 패키지를 고정했습니다. Camelot의 Python별 pypdf 요구 조건을 구분하고 Streamlit을 1.61.0으로 갱신했습니다. 판정 엔진·MD·제출 PDF는 변경하지 않았습니다. 새 3.14 환경의 D00~D16 오프라인 결과는 기존 3.10 기준과 17/17 일치합니다. [배포 호환성 확인](docs/PORTABLE_EXTRACTION.md#python-314-배포-호환성--v106)을 참고하세요.

> **2026-09-30 업데이트 · v105** — 표의 빈 셀·줄바꿈 처리, 추출 환경 고정·기록, 긴 PDF 분할 처리, 통신 실패와 문서 판정의 구분을 보강했습니다. 최신 결과와 제한은 [추출 이식성·검증 안내](docs/PORTABLE_EXTRACTION.md)를 참고하세요. 아래 v104는 이전 공개 기준입니다.

> **2026-09-29 업데이트 · v104** — 기존 UI와 메일 수신을 유지하면서 SP/허가서 직접 업로드, 실행 가능한 MD 규칙, 허가 기준 대조, 판정·시뮬레이션 작업 분리 및 D00~D16 회귀를 추가했습니다. 아래 기존 설명에 최신 동작을 반영했습니다. 모든 제품·양식의 무오류 판정이나 의약품 출하 승인을 보장하는 시스템은 아닙니다.

## 처음 보는 분을 위한 안내

| 알고 싶은 것 | 문서 |
|---|---|
| 근거 펼침의 문서확인번호·짧은 줄바꿈 수정 (화면 v112) | [추가 검토 의견 반영](docs/FEEDBACK_20261007.md) |
| 전체 검토: 허가서 텍스트·R24 레이아웃, 판정 유지 (화면 v109) | [텍스트 표시](docs/FEEDBACK_20261006.md) · [로직 검토](docs/LOGIC_REVIEW_20261006.md) |
| 두 번째 검토: 단위 설명·가독성·집계·검수 버전 (v108) | [재검토 의견 반영](docs/FEEDBACK_20261002.md) |
| 새 Word 검토 의견·더미·82개 요구사항 반영 (v107) | [최신 검토 의견 반영](docs/FEEDBACK_20261001.md) |
| 이번에 무엇을 바꿨고 어디까지 검증했는가 | [업데이트·인수인계](docs/RELEASE_20260929.md) |
| 전체 흐름과 파일별 역할 | [시스템 구조](docs/SYSTEM_OVERVIEW.md) |
| Python을 고치지 않고 MD 규칙을 수정하는 법 | [MD 작성·검증 안내](docs/MD_RULES_GUIDE.md) |
| D02 개별 시험 표시, D13~D16, 정상 문서 보류 이유 | [사용자 제보 수정](docs/FEEDBACK_20260929.md) |
| 로컬·배포 표 추출 차이와 긴 문서 처리 (v105) | [추출 이식성·검증 안내](docs/PORTABLE_EXTRACTION.md) |
| 아직 안 된 것 / A·B·C 요구사항 범위 | [검증 범위와 한계](docs/VALIDATION_SCOPE.md) |

### 기존 코드와 크게 달라진 점

- **입력**: Gmail 외에 화면의 파일 업로드로 SP PDF와 그 SP에 연결할 허가서를 함께 등록합니다. 문서함에는 원래 파일명도 표시합니다.
- **MD**: `sp_pdf_judger/rules/`의 YAML front matter를 읽어 규칙을 실행합니다. `domain_details/`의 설명용 MD와는 다릅니다. 수치·기간·누락 등은 검증된 Python 연산으로, `semantic_review`는 CLOVA 의미 판단으로 처리합니다.
- **허가서**: 연결된 허가서의 단계·시험·근거를 확인해 적용 기준을 대조합니다. 단순 검색 유사도만으로 통과시키지 않습니다. 불명확한 적용 범위나 환산 근거는 보류합니다.
- **판정 일관성**: D02의 생존율이 맞더라도 관찰기간이 부족하면 해당 시험 카드도 불충족입니다. 문서 규칙 결과와 개별 시험의 상태·근거를 연결합니다.
- **응답성·복구**: 검수/시뮬레이션 준비를 백그라운드 작업으로 분리하고, 캐시 식별·중복 실행·공유 상태 저장을 보호합니다. 실패한 판정의 단순 열람은 자동 유료 재시도를 하지 않습니다. 중간 단계부터 이어 하는 기능은 아직 미구현입니다.
- **재현 자료**: `더미데이터/`에 승인된 합성 D00~D16, 더미 허가서, 판정기준 Word를 포함합니다. 실제 수신함·캐시·키는 포함하지 않습니다.

### 빠른 실행 (Windows PowerShell)

```powershell
git clone https://github.com/anhyojin367/sp_semi.git
cd sp_semi
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
# .env의 CLOVA_API_KEY에 새로 발급한 본인 키를 입력합니다.
.\.venv\Scripts\python.exe -m streamlit run sp_app.py --server.port 8501 --server.address 127.0.0.1
```

브라우저에서 `http://127.0.0.1:8501`을 엽니다. 8501이 사용 중이면 기존 서버를 확인하거나 포트를 8503 등으로 바꾸세요.
새 복제본은 빈 문서함으로 시작합니다. **파일 업로드 → SP와 허가서 선택 → 등록 → 문서 선택 → 검수 진행** 순서로 사용합니다.
직접 업로드에는 Gmail 로그인이 필요 없습니다. CLOVA 판정은 외부 전송·API 비용이 발생합니다.
PDF 텍스트 추출과 달리 스캔 OCR에는 별도 OCR 환경이 필요하며, Windows OCR 관련 시험은 Windows 환경에서 검증했습니다.

### 빠른 검증 (API 호출 없음)

```powershell
.\.venv\Scripts\python.exe scripts/validate_rules.py
.\.venv\Scripts\python.exe scripts/prepare_test_fixtures.py
.\.venv\Scripts\python.exe -m pytest -q -rs
.\.venv\Scripts\python.exe scripts/validate_corpus.py --output-dir .local_validation/fresh_offline
```

저장 fixture 재생과 새 PDF 판정은 구별합니다. CLOVA 통합 검증은 `--live`를 명시해야 하며 별도 비용이 발생합니다.
v104의 기존 자동시험 결과는 **1,622개 통과**(미구현 단계 재개 초안 7개 제외), 실제 D00~D16 오프라인 지정 오류 누락은 0건입니다.
v104는 초안 7개를 기본 pytest에서 **사유가 보이는 skip**으로 남겼습니다. 당시 게시 파일만 펼친 새 사본에서도 **1,622 통과 / 7 skip / 실패 0**을 재확인했습니다. v105 로컬 개선의 전체시험은 **1,655 통과 / 7 skip / 실패 0**, 독립 추출 환경 D00~D16 비교는 **17/17 일치**입니다. 이는 클라우드에서 전 문서를 재검수했다는 뜻은 아닙니다. 상세 검증 결과와 보류 사유는 [v105 안내](docs/PORTABLE_EXTRACTION.md)와 [이전 업데이트 기록](docs/RELEASE_20260929.md)을 참고하세요.

> **보안 주의:** 과거 커밋의 예제 설정에 실키가 포함되어 있었습니다. 현재 예제는 빈 값이지만 Git 이력은 재작성하지 않았습니다. 해당 키는 폐기·재발급해야 합니다. `.env`와 `.streamlit/secrets.toml`은 Git에 올리지 마세요.

> **Rule · RAG · LLM을 역할별로 분리한 의약품 SP 문서 자동검수 시스템**

의약품 SP(Summary Protocol) PDF에서 제조 및 시험 정보를 구조화하고,  
**Deterministic Rule · RAG · LLM**을 역할별로 결합하여 시험 결과를 자동 검수하는 AI 시스템입니다.

단순히 PDF 전체를 LLM에 입력해 판정을 맡기는 방식이 아니라,

- **코드로 확정 가능한 조건은 Deterministic Rule로 검증**
- **추가 근거가 필요한 항목은 RAG로 관련 문서 검색**
- **정성적 의미 해석이 필요한 항목에만 LLM을 제한적으로 활용**
- **OCR 오류·정보 누락·근거 부족 시 추측하지 않고 Hold 처리**
- **규칙·RAG·도메인 Context 변경에 대한 Regression Test 및 Traceability 관리**

를 핵심 설계 원칙으로 합니다.

---

## System Overview

```text
SP PDF
  │
  ▼
PDF / OCR Parsing
  │
  ▼
Structured Test & Manufacturing Records
  │
  ▼
Deterministic Rule Validation
  │
  ├───────────────┐
  │               │
  ▼               ▼
확정 가능        추가 근거 필요
  │               │
  │               ▼
  │        RAG Evidence Retrieval
  │               │
  │               ▼
  │       Domain-specific Context
  │        Overall / Company / Product
  │               │
  │               ▼
  │          LLM Assist
  │               │
  └───────┬───────┘
          ▼
    Pass / Hold / Fail
          │
          ▼
Regression Log & Traceability
```

### Core Principle

> **모든 판단을 LLM에 맡기지 않는다.**

수치 비교, 날짜 관계, 단위, 제조번호, 공정 순서와 같이  
코드로 명확하게 검증할 수 있는 항목은 Deterministic Rule이 우선합니다.

LLM은 규칙만으로 확정하기 어려운 의미 비교를 보조하며,  
충분한 근거가 없는 경우에는 임의의 결론 대신 **Hold** 상태를 반환합니다.

---

# 1. Problem

SP 문서는 단순 텍스트 문서가 아닙니다.

실제 검수 과정에서는 다음과 같은 문제가 동시에 발생합니다.

- 회사·제품마다 문서 구조와 시험 항목이 다름
- PDF 표 구조 및 OCR 결과가 일정하지 않음
- 동일한 단위가 여러 표기 방식으로 등장
- 수치 기준과 정성적 기준이 혼재
- 제품별 허가사항 및 예외 조건 존재
- 제조번호·제조일자·공정 순서 등 문서 간 관계 검증 필요
- AI가 근거 없이 추측할 경우 잘못된 판정으로 이어질 수 있음

따라서 본 프로젝트에서는 **추출 → 규칙 검증 → 근거 검색 → 의미 해석 → 보류 및 검증**의 역할을 분리했습니다.

---

# 2. Core Features

## 2.1 PDF / OCR 기반 정보 구조화

SP PDF에서 시험 및 제조 정보를 추출하여 이후 검증에 사용할 수 있는 구조화 데이터로 변환합니다.

주요 추출 대상은 다음과 같습니다.

- 회사명
- 제품명
- 문서 제목
- 제조번호
- 제조일자
- 제조량
- 시험명
- 시험방법
- 시험기준
- 시험결과
- 제조단계
- 공정 순서 및 날짜 정보

PDF 텍스트 레이어와 OCR 결과를 함께 활용하며,  
추출된 값은 판정 단계에서 다시 정규화됩니다.

---

## 2.2 Deterministic Rule Validation

명확한 조건으로 검증할 수 있는 항목은 LLM을 사용하지 않고 Python 코드에서 직접 판정합니다.

예:

- 이상 / 이하 / 초과 / 미만
- 수치 범위 비교
- 단위 정규화
- 복수 시험기준 AND 조건
- 제조번호 일치 여부
- 제조일자 비교
- 시험일 허용 범위
- 제조 공정 간 날짜 선후관계
- 제조량 비교
- 공정 순서 검증

```text
Structured Record
      │
      ▼
Condition Parsing
      │
      ▼
Unit / Date Normalization
      │
      ▼
Deterministic Validator
      │
      ▼
Pass / Fail / Hold
```

결정적 규칙으로 확정된 판정은 RAG나 LLM이 임의로 뒤집지 않도록 역할을 분리했습니다.

---

## 2.3 RAG Evidence Retrieval

규칙만으로 충분한 근거를 확보하기 어려운 경우 관련 문서를 검색하여 LLM Context에 제공합니다.

현재 검색 계층은 외부 Vector DB가 아닌 **로컬 TF-IDF 기반 Retrieval**로 구성되어 있습니다.

### Retrieval

```text
시험명 + 시험기준 + 시험결과
            │
            ▼
      Query Normalization
            │
      ┌─────┴─────┐
      ▼           ▼
 Word TF-IDF   Character TF-IDF
   1~2 gram       2~5 gram
     65%            35%
      └─────┬─────┘
            ▼
     Cosine Similarity
            │
            ▼
     Evidence Ranking
```

문자 단위 TF-IDF를 함께 사용하여 다음과 같은 표기 차이에 대한 검색 누락을 완화합니다.

- OCR 오탈자
- 한글 조사 차이
- 단위 기호 차이
- `µ`, `μ`, `u` 등 문자 차이
- `㎎`, `mg` 등 표기 방식 차이

### Retrieval Sources

검색에 사용되는 데이터 예:

- 대한민국약전
- 생물학적제제 기준 및 시험방법
- 단위·기호 자료
- UCUM 관련 데이터
- 승인된 참고 문서

검색 결과에는 자료명, 페이지, 청크 내용 등의 Source 정보가 함께 유지됩니다.

---

## 2.4 Company / Product-specific Domain Context

SP 검수 기준은 회사와 제품에 따라 달라질 수 있습니다.

이를 하나의 거대한 Prompt에 모두 넣는 대신,  
문서에서 인식한 회사와 제품에 맞춰 필요한 Context만 선택적으로 병합합니다.

```text
overall.md
    +
company-specific context
    +
product-specific context
```

예:

```text
GC녹십자
+
녹십자-알부민주20%

        ↓

overall.md
+
companies/gc_biopharma.md
+
products/albumin.md
```

각 Context에는 다음과 같은 정보가 포함될 수 있습니다.

- 회사별 용어
- 제품별 시험 항목
- 자주 누락되는 검수 포인트
- 필드 간 비교 관계
- 사람 확인이 필요한 예외 조건
- Hold가 필요한 상황

Domain Context는 새로운 Python Rule을 자동 생성하는 용도가 아니라,  
**이미 추출된 정보를 LLM이 올바르게 해석하도록 보조하는 지식 계층**으로 사용합니다.

---

## 2.5 LLM-assisted Semantic Judgment

LLM은 전체 시험을 직접 판정하지 않습니다.

문자열은 다르지만 의미상 같은 표현인지 판단해야 하는 경우와 같이  
정성적 해석이 필요한 항목에서 제한적으로 사용합니다.

예:

```text
시험기준
"검출되지 않아야 한다"

시험결과
"미검출"
```

LLM 호출 시에는 단순히 두 문장만 전달하지 않고 다음 Context를 함께 제공합니다.

```text
시험명
+
시험방법
+
시험기준
+
시험결과
+
Deterministic Rule 결과
+
RAG Evidence
+
Overall Context
+
Company Context
+
Product Context
```

이를 통해 LLM 판단이 가능한 한 제공된 근거 안에서 이루어지도록 설계했습니다.

---

## 2.6 Hold-first Design

규제 문서 검수에서는 잘못된 자동 Pass보다 **판단을 보류하는 것**이 더 중요할 수 있습니다.

따라서 충분한 근거를 확보하지 못한 경우 임의의 판정을 생성하지 않고 Hold 상태로 처리합니다.

Hold 예:

- OCR 결과가 불완전한 경우
- 시험기준이 누락된 경우
- 시험결과가 누락된 경우
- 제품별 예외가 불명확한 경우
- 관련 근거 문서를 찾지 못한 경우
- 추가 허가서 확인이 필요한 경우

```text
Evidence Sufficient
      │
      ├──→ Pass
      │
      └──→ Fail


Evidence Insufficient
      │
      └──→ Hold
               │
               ▼
        Human / Permit Review
```

---

## 2.7 Permit Document Review

공통 RAG 문서와 제품별 허가서는 분리하여 관리합니다.

제품별 허가서는 모든 제품에 대한 전역 검색 대상으로 사용하지 않고,
현재 SP 문서와 연결된 허가서만 검토에 사용합니다. 최신 경로에서는 보류 항목에만 한정하지 않고,
SP 기준상 합격인 시험도 연결된 권위 있는 허가 기준과 대조합니다.

```text
SP 기준 1차 판정 (Pass / Fail / Hold)
      ↓
연결 허가서의 적용 단계·시험·원문 근거 확인
      ↓
허가 기준 대조 + 확정 가능한 수치 재계산
      ↓
MD 규칙 / 개별 시험 상태 연결 → Pass / Fail / Hold
```

이를 통해 다른 제품의 허가 정보가 잘못 검색되어 판정에 영향을 주는 위험을 줄입니다.

---

## 2.8 Persistent Permit Index and Grouped Judgment

허가서 PDF는 SP 한 건마다 다시 OCR·파싱하지 않습니다. 처음 사용하는 허가서는 PDF 내용과
OCR/파서 스키마를 기준으로 한 구조화 인덱스로 원자적으로 저장하고, 이후 같은 허가서는 그
인덱스를 읽습니다. 인덱스에는 페이지별 원문, OCR 출처, 시험·단계 chunk, source span,
구조화 조건이 들어가며 SP 내용이나 API 응답은 저장하지 않습니다.

```text
허가서 첫 사용: PDF text extraction / 필요한 페이지 OCR
                         ↓
              Persistent structured permit index
                         ↓
SP 시험행: 동일 단계 + 동일 시험 + 동일 제조번호(batch) + 동일 방법/허가 후보로 그룹화
                         ↓
          Numeric / time / unit 조건을 코드로 결정적 비교
                         ↓
        ┌────────────────┴────────────────┐
        │                                 │
명확한 조건: Pass / Fail          의미가 모호한 조건만 제한 크기 묶음 요청
                                          │
                                  근거 부족: Hold / 서비스 오류: 중단
```

즉, 같은 시험·같은 배치의 여러 SP 행은 한 번에 판정하고 결과를 원래 행에 다시 연결합니다.
수치, 기간, 시각, 개수, 단위처럼 코드로 비교 가능한 조건은 CLOVA에 보내지 않습니다.
CLOVA는 구조화 조건만으로 확정할 수 없는 의미적 모호성이 남은 그룹에만 보조적으로 사용하며,
허가서 대응 시험이나 필요한 관측값이 부족하면 추측하지 않고 Hold로 닫습니다.
429/연결 오류 같은 서비스 실패를 문서의 합격·불합격·보류로 바꾸지는 않습니다.

### 직접 업로드와 현장 시연 (v113)

일반 직접 업로드는 기존의 **허가서 우선 개별 의미 판정 경로**를 사용합니다.
v110~v112에서 모든 연결 허가서를 고속 그룹 경로로 보낸 변경은 D07 회귀를 일으켜 복구했습니다.
중간 배양 14일과 전체 기간 28일의 혼동, ‘적합’과 ‘이물이 없음’의 잘못된 직접 비교가 확인됐습니다.
허가서 인덱스·OCR·요청별 응답 캐시는 유지하며, v112 완료 결과는 새 검수에서 재사용하지 않습니다.
이전 완료 결과 파일을 삭제하거나 합격으로 덮어쓰지는 않습니다.

기존 catalog와 정확한 제품 보정 ID에 명시된 그룹 정책, 회사·제품 매핑은 변경하지 않았습니다.
그룹 경로의 복합 조건 해석은 아직 일반 운영에 적합하다고 검증되지 않았으므로 다른 제품에
확대 적용하지 마세요. 아래 묶음 처리 설명은 해당 그룹 정책을 선택한 경우에만 적용됩니다.
SP 자체 기준의 비교용 시험행은 로컬에서 판정하고 최종 허가서 의미 판정만 묶음 요청합니다.
문서 정책 검사는 계속 실행되므로 별도의 의미 규칙 요청이 있을 수 있습니다.

- 허가서 후보의 원문·단계·시험·조건 그래프·코드 판정이 모두 동일할 때만 중복 전송을 줄입니다.
  기준값이나 시험방법이 다른 후보는 합치지 않으며 전체 원문은 로컬 근거에 유지됩니다.
- 요청은 최대 6개 그룹·60,000글자로 제한합니다. 중복 축소 후에도 단일 그룹이 제한을 넘으면
  내용을 잘라 합격 처리하지 않고 `llm_request_too_large`로 대조 미완료를 표시합니다.
- 429·일시 연결 오류는 서버 대기 지시와 총 150초 예산 안에서 최대 3회 시도합니다.
  인증/권한 오류는 반복하지 않습니다. 외부 서비스의 장애나 계정 한도를 없애는 기능은 아닙니다.
- 재시도 시 동일 요청의 저장 응답을 재사용합니다. 이것은 요청별 캐시이며, 실패한 전체 작업의
  SP 추출 단계까지 이어받는 체크포인트 기능은 아닙니다.
- 서명 뒤에 이미 확인된 동일 제목·번호의 구간이 반복되면 해당 제조단계로 다시 구분합니다.
  모르는 역순 숫자 문장을 임의의 제조단계로 추정하지 않습니다.
- 진행 화면은 `허가서 의미 판정 묶음 n/N`과 CLOVA 대기 사유를 함께 표시합니다.
  시험행 수·페이지 수·묶음 수·실제 HTTP 시도 수는 서로 다른 값입니다.
- 묶음 LLM의 송신 JSON 스키마는 필수 필드와 판정값을 엄격하게 지정합니다.
  조건별 판정은 `pass/fail/hold`, 묶음 판정은 `검수합격/검수불합격/검수보류`이며 이유도 필수입니다.
  관대한 수신 디코더는 송신 스키마와 분리하고, 항목 형식과 그룹 ID의 일대일 대응까지 확인한 뒤
  응답 성공으로 집계하고 캐시합니다. 잘못된 응답은 문서 보류 대신 서비스 응답 오류로 중단합니다.
  이전 v110 완료 결과와 응답은 새 프로토콜에서 재사용하지 않으며 원본 캐시는 삭제하지 않습니다.
- 의미 판정이 필요한 코드의 미판정(`hold`)과 확정된 구조화 조건의 제약을 입력에서 구분합니다.
  PDF 줄바꿈은 전송용 인용문에서 정리하되 원문은 보존합니다. 근거 비교는 원문에 줄바꿈이 있을 때만
  공백 배치 차이를 허용하고, 숫자·소수점·영문 단위·단어 토큰의 변경은 계속 거부합니다.

현장 데이터는 페이지 수만으로 시간을 보장할 수 없습니다. 이미지/OCR 비율, 추출된 제조단계와
배치의 품질, 허가서 매칭 범위, 복합조건 수 및 계정의 처리량을 함께 점검해야 합니다.
첫 방문 자료는 업로드 후 대조 미완료 항목과 추출 보고서를 반드시 확인하세요.
실데이터를 외부 CLOVA로 전송해도 되는지는 해당 회사의 데이터 처리 승인이 선행되어야 합니다.

---

# 3. Reliability & Traceability

AI의 최종 출력만 저장하는 것이 아니라  
**어떤 데이터와 근거가 해당 판정에 사용되었는지** 추적할 수 있도록 Metadata를 관리합니다.

주요 Metadata:

- `rag_doc_count`
- `rag_sources`
- `domain_detail_sources`
- `domain_detail_matched_company`
- `domain_detail_matched_product`
- `domain_detail_fingerprint`
- `document_company`
- `document_product`
- `llm_model`
- `llm_call_count`
- `llm_success_count`
- `llm_last_error`
- `permit_index_cache_hit`
- `permit_index_elapsed_ms`
- `permit_ocr_page_count`
- `permit_group_path_counts`
- `permit_group_provider_calls`
- `permit_group_estimated_saved_calls`
- `permit_group_planned_batch_count` / `permit_group_planned_prompt_chars`
- `permit_group_compacted_candidate_count` / `permit_group_oversized_count`
- `permit_group_transport` (`http_attempts`, `retries`, `cache_hits`; 앱 요청 문맥에서 측정)

`provider_calls`는 캐시 조회를 포함하는 논리 호출 수입니다. 실제 HTTP 시도는 `transport`로
확인합니다. `estimated_saved_calls`는 행별 호출 대비 계획상의 차이이며 보류·미매칭도 포함하므로,
이를 검증 완료율이나 실측 성능 향상률로 해석하면 안 됩니다.

이를 통해 특정 결과가 어떤 RAG 자료, Domain Context, LLM 설정을 사용하여 생성됐는지 확인할 수 있습니다.

---

# 4. Regression Testing

도메인 규칙이나 RAG 자료를 추가할 때 기존 판정 결과가 의도하지 않게 바뀔 수 있습니다.

이를 확인하기 위해 PDF별 Regression Log를 관리합니다.

```text
Input PDF
   +
Permit Document
   +
Rule Version
   +
RAG Dataset
   +
Domain Context
   +
LLM Configuration
          │
          ▼
      Evaluation
          │
          ▼
  Regression History
```

### Regression Log

PDF별 Excel 파일에는 다음 정보가 기록됩니다.

- 정답지
- 실행별 판정 결과
- 시험·제조 정보별 세부 판정
- 규칙별 구현 상태
- 사용한 RAG 자료
- 사용한 Domain Context
- LLM 실행 정보
- 실행 환경 Fingerprint

예:

```text
OUTPUT/
└── <PDF명>__<PDF_HASH>__rule_regression.xlsx
```

동일한 PDF와 동일한 코드·RAG·Context·모델 조합은 중복 기록하지 않으며,  
구성 요소 중 하나라도 변경되면 새로운 실행으로 기록합니다.

---

# 5. Rule / RAG / LLM Responsibility

본 시스템에서는 각 계층의 역할을 명확히 분리합니다.

| Component | Responsibility |
|---|---|
| Extractor | PDF/OCR에서 필요한 정보를 구조화 |
| Deterministic Rule | 명확하게 계산·비교 가능한 기준 판정 |
| RAG | 관련 기준 및 참고 근거 검색 |
| Domain Context | 회사·제품별 검수 관점 및 예외 제공 |
| LLM | 정성적 의미 해석 및 보조 판정 |
| Permit Review | 연결 허가서의 적용 범위·원문 근거 확인 및 허가 기준 우선 대조 |
| Regression Logger | 판정 변화 및 사용 근거 추적 |

### Important

```text
Rule ≠ RAG
RAG ≠ Validator
Domain Context ≠ Python Rule
LLM ≠ Final Authority
```

각 계층이 담당해야 할 역할을 분리하여  
시스템의 재현성과 유지보수성을 높이는 것을 목표로 합니다.

---

# 6. Architecture

```text
┌──────────────────────────────────────────────┐
│                    SP PDF                    │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│              PDF / OCR Parsing               │
│  Test · Manufacturing · Metadata Extraction  │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│              Structured Records              │
└──────────────────────┬───────────────────────┘
                       │
              ┌────────┴────────┐
              │                 │
              ▼                 ▼
┌─────────────────────┐   ┌─────────────────────┐
│ Deterministic Rules │   │    RAG Retrieval    │
│                     │   │                     │
│ Numeric             │   │ Pharmacopeia       │
│ Unit                │   │ Standards           │
│ Date                │   │ Unit / Symbol Data  │
│ Manufacturing       │   │ Reference Docs      │
└──────────┬──────────┘   └──────────┬──────────┘
           │                         │
           └────────────┬────────────┘
                        ▼
┌──────────────────────────────────────────────┐
│               Domain Context                 │
│        Overall + Company + Product            │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│                  LLM Assist                  │
│          Semantic / Qualitative Judge        │
└──────────────────────┬───────────────────────┘
                       │
             ┌─────────┴─────────┐
             ▼                   ▼
      ┌────────────┐       ┌────────────┐
      │ Pass / Fail│       │    Hold    │
      └──────┬─────┘       └──────┬─────┘
             │                    │
             │                    ▼
             │           ┌──────────────────┐
             │           │ Permit / Human   │
             │           │      Review      │
             │           └────────┬─────────┘
             │                    │
             └──────────┬─────────┘
                        ▼
┌──────────────────────────────────────────────┐
│       Regression Log & Traceability          │
└──────────────────────────────────────────────┘
```

---

# 7. Project Structure

```text
sp_semi/
│
├── sp_app.py
│   └── Streamlit UI 및 사용자 인터랙션
│
├── sp_gmail_ingest.py
│   └── Gmail PDF 첨부파일 수집
│
├── sp_document_metadata.py
│   └── 문서 Metadata 추출
│
├── sp_document_upload.py / sp_document_models.py
│   └── SP·허가서 직접 업로드 및 제출 건 모델
│
├── sp_judgement_bridge.py / sp_review_jobs.py / sp_process_lock.py
│   └── 검수 작업·캐시·프로세스 잠금 및 상태 저장
│
├── sp_simulation_jobs.py / sp_flowchart_data.py
│   └── 시뮬레이션 비동기 준비·공정 및 판정 숫자 연결
│
├── sp_pdf_viewer.py
│   └── PDF 및 제조요약도 렌더링
│
├── sp_pdf_judger/
│   │
│   ├── extractor.py
│   │   └── 시험 정보 추출
│   │
│   ├── criteria_parser.py
│   │   └── 시험기준 Parsing
│   │
│   ├── rules/ (document.md, sky_covione.md 등)
│   │   └── 실행 가능한 MD 규칙 및 검토 설정
│   │
│   ├── policy_engine.py / policy_schema.py / policy_summary.py
│   │   └── MD 검증·실행·개별 시험 상태 연결
│   │
│   ├── date_normalizer.py
│   │   └── 날짜 정보 정규화
│   │
│   ├── manufacturing_info_validator.py
│   │   └── 제조정보 결정적 검증
│   │
│   ├── judgement.py
│   │   └── 시험 결과 판정 및 RAG 연계
│   │
│   ├── rag.py
│   │   └── TF-IDF 기반 Evidence Retrieval
│   │
│   ├── llm.py
│   │   └── LLM 보조 판정
│   │
│   ├── permit_pdf_store.py
│   │   └── 연결 허가서 관리
│   │
│   ├── domain_details.py
│   │   └── 회사 / 제품 Context 선택 및 병합
│   │
│   ├── domain_details/
│   │   ├── overall.md
│   │   ├── companies/
│   │   └── products/
│   │
│   └── pipeline.py
│       └── 전체 검수 Pipeline 조립
│
├── rag_data/
│   └── 공통 RAG 참고 자료
│
├── docs/
│   ├── RAG_ARCHITECTURE_AND_DATA_GUIDE.md
│   ├── DOMAIN_DETAIL_AUTHORING_GUIDE.md
│   └── RULE_REGRESSION_AND_RAG_OPERATIONS.md
│
├── tests/
│   └── 추출·MD·허가·UI·동시성 회귀 및 과거 저장 fixture
│
├── 더미데이터/
│   └── 공개 승인된 D00~D16, 더미 허가서, 판정기준 Word
│
├── OUTPUT/
│   └── 판정 결과 및 Regression Log
│
└── requirements.txt
```

---

# 8. Tech Stack

## Language / Framework

- Python
- Streamlit
- Pydantic
- PyYAML

## Document Processing

- PyMuPDF
- pdfplumber
- pypdf
- pypdfium2
- Tesseract OCR

## Retrieval / AI

- RAG
- TF-IDF
- Cosine Similarity
- scikit-learn
- LLM API

## Engineering / Validation

- Pytest
- Git / GitHub
- Regression Testing
- Rule-based Validation
- Judgment Logging
- Metadata Tracking

---

# 9. Quick Start

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
streamlit run sp_app.py
```

Windows에서는 다음 파일을 사용할 수도 있습니다.

```text
run_sp_app.bat
```

기본 접속 주소:

```text
http://localhost:8501
```

---

# 10. Gmail Ingestion

선택적으로 Gmail에서 SP PDF 첨부파일을 자동 수집할 수 있습니다.

필요 정보:

- Gmail 주소
- Gmail App Password
- 검색할 메일 제목 조건

예:

```text
메일 제목에 [식약처] 포함
        │
        ▼
PDF Attachment Download
        │
        ▼
incoming_sp_pdfs/
```

Gmail App Password는 일반 로그인 비밀번호가 아니며,  
Google 계정의 2단계 인증 후 별도로 생성해야 합니다.

---

# 11. Domain Context 확인

문서에서 인식한 회사·제품에 따라 어떤 Domain Context가 선택되는지 CLI에서 확인할 수 있습니다.

```powershell
python -m sp_pdf_judger.domain_details `
  --company "GC녹십자" `
  --product "녹십자-알부민주20%" `
  --show-context
```

예:

```text
overall.md
+
companies/gc_biopharma.md
+
products/albumin.md
```

---

# 12. RAG Source 확인

현재 적재된 RAG 데이터 수와 Source를 확인합니다.

```powershell
python -c "from sp_pdf_judger.rag import UcumRagStore; s=UcumRagStore(); print(len(s.docs)); print(*s.loaded_sources, sep='\n')"
```

---

# 13. Test

개발 의존성을 설치하고 저장 fixture를 준비한 뒤 전체 테스트를 실행합니다.
7개의 미구현 단계 재개 계약시험은 skip으로 표시하며, 환경 의존 시험의 skip도 `-rs`로 확인합니다.

```bash
python -m pip install -r requirements-dev.txt
python scripts/prepare_test_fixtures.py
python -m pytest -q -rs
```

Domain Context 관련 테스트:

```bash
python -m pytest tests/test_domain_details.py
```

## Offline permit index benchmark

다음 명령은 SP를 한 번 추출한 뒤, 격리된 빈 캐시에서 cold 실행과 같은 캐시를 재사용하는
warm 실행을 연속 수행합니다.

```powershell
python scripts/benchmark_permit_index.py `
  --sp "더미데이터/D00_정상기준본.pdf" `
  --permit "sp_pdf_judger/permits/documents/sky_covione_multidose.pdf" `
  --company "SK bioscience" `
  --product "스카이코비원멀티주" `
  --cache-dir ".local_validation/permit-index-benchmark" `
  --output ".local_validation/measurement.json"
```

`--company`, `--product`, `--cache-dir`, `--output`은 선택 사항입니다. `--cache-dir`를 지정하면 그
디렉터리 아래에 새 benchmark 전용 캐시를 남기며, 기존 캐시를 삭제하거나 cold 측정에
재사용하지 않습니다. 생략하면 현재 작업 디렉터리 아래의 임시 캐시를 사용하고 종료 시
정리합니다.

이 도구는 명시적 그룹 정책을 가진 회사·제품 조합만 측정합니다. 일반 업로드 경로는
그룹 정책으로 강제 변경하지 않고 입력 오류로 종료합니다. 위 예시는 그룹 경로의
오프라인 측정이며, 일반 업로드의 LLM 판정 시간이나 정확도를 보여 주지는 않습니다.

출력 JSON은 cold/warm 전체 경과 시간과 인덱스 경과 시간, `cache_hit`, OCR 페이지 및
출처, permit extractor/chunk parser/condition parser 호출 수, 그룹 경로별 개수,
provider 호출 수, 그룹화로 절약한 추정 호출 수를 함께 보여 줍니다. 이 benchmark는
CLOVA client와 provider 요청 경계를 실행 전에 비활성화하고 네트워크 연결도 차단합니다.
모호한 그룹은 provider를 호출하지 않고 Hold가 되며, 호출 시도가 감지되면 결과를 성공으로
출력하지 않고 오류로 종료합니다.

측정은 실제 업로드의 정책 선택을 그대로 사용하며 강제로 그룹 모드를 켜지 않습니다.
planned batch count/크기/글자 수와 과대 요청 수를 API 없이 확인할 수 있습니다.
이 시간에는 실제 CLOVA 응답·429 대기, 앱의 SP 비교용 선행 판정 및 UI/보고서 생성이
포함되지 않으므로 전체 시연 완료 시간으로 표시하지 않습니다.

---

# 14. Adding New Rules

**최신 실행 MD 경로는 `sp_pdf_judger/rules/`입니다.** 기존 연산으로 표현 가능한 조건은
`document.md` / `sky_covione.md`의 구조화 규칙을 수정하고 검증하면 됩니다.
자연어 의미 규칙은 `operation: semantic_review`로 명시합니다. 단순히 설명 문장을 아무 MD에 쓰면 자동 실행되는 것은 아닙니다.
새로운 추출 필드나 지원하지 않는 연산이 필요하면 Python 구현과 회귀시험도 필요합니다.
[실제 작성 예제와 명령](docs/MD_RULES_GUIDE.md)을 먼저 참고하세요. 아래는 기존 원칙입니다.

새로운 검수 기준은 단순히 Prompt에 추가하지 않고, 먼저 규칙의 성격을 구분합니다.

### 기존 추출 정보를 의미적으로 해석하면 되는 경우

```text
Domain Context / LLM Assist
```

### 새로운 구조 추출 또는 정확한 계산이 필요한 경우

```text
Extractor
    ↓
Validator
    ↓
Pipeline
    ↓
Test
```

권장 절차:

```text
1. 규칙 및 적용 범위 정의
        ↓
2. 필요한 Field 존재 여부 확인
        ↓
3. 필요한 경우 Extractor 추가
        ↓
4. Deterministic Validator 구현
        ↓
5. Pipeline 연결
        ↓
6. 정상 / 오류 Test 추가
        ↓
7. Domain Context 업데이트
        ↓
8. Regression Test
```

---

# 15. Design Principles

## Rule First

명확하게 코드로 검증할 수 있는 항목은 LLM에 맡기지 않습니다.

---

## Evidence Before Judgment

LLM 판단 전에 관련 기준과 근거를 먼저 검색하여 제공합니다.

---

## Domain-aware Context

모든 제품에 동일한 Prompt를 사용하는 대신  
회사·제품에 필요한 Context만 선택적으로 제공합니다.

---

## Hold Instead of Guess

정보가 부족한 경우 임의의 결론을 만들지 않고 Hold 처리합니다.

---

## Traceability

어떤 문서·Context·모델이 판정에 사용됐는지 기록합니다.

---

## Regression Safety

Rule, RAG, Domain Context 변경이 기존 판정에 미치는 영향을 확인합니다.

---

# 16. Security & Data Sharing

이번 버전에는 사용자 공개 승인을 받은 합성 더미자료와 허가서/참고자료가 포함됩니다.
실제 메일 수신함(`incoming_sp_pdfs/`), 판정 상태(`.sp_judgement_status/`),
렌더 PDF(`static/pdf_view/`), 가상환경·로컬 검증 캐시는 추적하지 않습니다.
기존 로컬 파일은 삭제하지 않고 Git 추적에서만 제외했습니다.
과거 커밋의 노출 키는 예제에서 삭제해도 무효화되지 않습니다. 반드시 발급처에서 폐기·재발급하세요.

공개 Repository에는 실제 운영 과정에서 사용될 수 있는 다음 정보가 포함되지 않도록 관리합니다.

- Gmail Password
- Gmail App Password
- API Key
- `.env`
- 인증 Token
- 개인정보
- 실제 기관 수신 문서
- 외부 공개가 제한된 내부 자료

실제 운영 환경에서는 민감 데이터와 인증정보를 별도의 보안 환경에서 관리해야 합니다.

---

# 17. Documentation

보다 상세한 시스템 구조와 운영 방법은 `docs` 폴더를 참고합니다.

### RAG Architecture

```text
docs/RAG_ARCHITECTURE_AND_DATA_GUIDE.md
```

- RAG 데이터 구조
- 검색 방식
- Domain Context
- LLM 입력
- Permit Document 연계
- 데이터 추가 절차

### Regression / Rule Operations

```text
docs/RULE_REGRESSION_AND_RAG_OPERATIONS.md
```

- 규칙별 구현 상태
- PDF별 Regression Log
- 정답지 관리
- RAG / Domain Context 변경 추적
- 실행 Fingerprint

---

# 18. Engineering Perspective

본 프로젝트는 단순한 LLM 기반 문서 요약 또는 질의응답 데모가 아니라,

```text
Document Parsing
      ↓
Structured Data
      ↓
Deterministic Validation
      ↓
Evidence Retrieval
      ↓
Domain-aware Context
      ↓
LLM-assisted Reasoning
      ↓
Pass / Hold / Fail
      ↓
Regression & Traceability
```

까지 연결하는 **End-to-End AI 검수 시스템**을 목표로 합니다.

프로젝트를 진행하며 가장 중요하게 둔 질문은 다음과 같습니다.

> **“AI가 무엇을 판단할 수 있는가?”보다  
> “어떤 판단은 반드시 코드와 근거가 책임져야 하는가?”**

이를 기준으로 Rule, Retrieval, LLM, Validation의 책임을 분리하여  
실제 업무 환경에서 신뢰하고 사용할 수 있는 AI 시스템을 설계하고 있습니다.

## 실험용 허가서 근거 공유 모드 (로컬 전용)

기본값은 기존 `legacy`입니다. `shared_evidence`는 제조단계·시험명을 정확히 찾고,
원문/자식항목/공통 조건/참조항목의 출처가 검증된 동일 근거만 최대 4개 독립 시험에 공유합니다.
배치·결과·기간은 행마다 따로 전달하고 LLM이 각 행을 판정합니다. 불명확한 배치, 표 미전개,
불완전한 근거, 예산을 확인하지 못한 모델은 기존 단일 판정 경로를 사용합니다.
`deterministic_first`로 전환하지 않습니다. API 오류를 보류 판정으로 바꾸지 않습니다.

사용 중인 서버를 직접 종료한 뒤 **이 작업트리 터미널**에서 실행합니다. 사용 중인 포트는 다른 번호를 선택하세요.

```powershell
$env:SP_PERMIT_SEMANTIC_MODE='shared_evidence'
.\.venv\Scripts\python.exe -m streamlit run sp_app.py --server.port 8503
```

되돌리려면 서버를 종료하고 `$env:SP_PERMIT_SEMANTIC_MODE='legacy'`로 바꾼 뒤 재실행합니다.
실제 `.env`나 `secrets.toml`을 수정할 필요는 없습니다. 새 모드는 기존 완료 결과와 캐시 키가 분리됩니다.
새 SP 내용/허가 근거/모델/규칙/프로토콜이 달라진 요청은 동일 응답으로 취급하지 않습니다.
개선 모드에서는 파일 크기와 수정시간이 같아도 내용 해시가 바뀌면 완료 캐시를 재사용하지 않습니다.
`CLOVA_MAX_COMPLETION_TOKENS`도 작업 시작 시 고정하며, 변경된 예산으로 기존 완료 결과를 재사용하지 않습니다.
단독 시험도 준비된 전체 허가 근거를 전달합니다(기존 상위 12개 후보 검색으로 되돌아가지 않음).
같은 표시 번호가 있어도 후속 수치 검사와 단위 표기 설명은 원래 행 위치별로 연결합니다.

`semantic_batch_diagnostics`는 묶인 시험 수, 단일 경로 수, 입력 크기 추정치, 준비/호출 시간과
HTTP 시도·재시도·보정·캐시 적중을 기록합니다. 게이트 대기와 서버 응답 시간은 별도이며,
실제 토큰은 제공자가 보고한 경우에만 집계합니다. 미보고 토큰은 `null`이지 0이 아닙니다.
캐시 적중은 과거 토큰을 이번 호출량에 더하지 않습니다. 바이트 추정치는 실제 tokenizer 측정이 아닙니다.
실제 cold API 속도·모델 정확도 검증 전까지 실험 모드이며, 기본 활성화나 배포를 하지 않습니다.

격리 벤치마크(기본값은 네트워크 차단, LLM 정확도 검증 아님):

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_authoritative_batch.py --sp "SP.pdf" --permit "허가서.pdf" --out "tmp/새실험" --company "실제회사명" --product "실제제품명" --mode shared_evidence
```

`--out`은 새 폴더여야 합니다. `--cache-state index-only --seed-run "tmp/이전실험"`은
일치하는 실험의 허가 인덱스만 복사합니다. `responses`는 응답 재사용 실험임을 표시하며 최초 속도로 취급하지 않습니다.
원본 PDF를 복사하거나 사용자 캐시를 지우지 않습니다. 출력에는 원문을 포함하는 추출/인덱스 캐시가 있으므로 외부 공유하지 마세요.
`metrics.json`의 진단/레코드 비교에는 원문 대신 상태·근거/이유 해시를 사용합니다.
`--allow-live`는 별도 유료 호출 승인을 받은 경우에만 사용하며 실행당 HTTP 최대 250회, 실패 후 자동 전체 재실행은 없습니다.
기존 제품 정책이 deterministic-first인 경우 이 도구는 정책을 바꾸지 않고 비교를 거부합니다.

2026-10-13 오프라인 관측: D00/D07 각 92개 시험에는 반복 공유 대상이 없어 묶음 호출 절감은 0입니다.
205쪽 SP·318쪽 허가서 더미는 782개 시험 중 690개를 230개 요청으로 계획했지만,
4개 시험의 정확 일치 후보가 입력 예산을 초과했습니다. 해당 요청은 기준 일부를 버리지 않고 중단하므로
**대용량 전체 검수 완료·실제 API 호출 절감·실데이터 정확성을 입증한 결과가 아닙니다.**
HTTP/응답 캐시 적중은 모두 0이었고, 준비 수치는 실제 LLM 실행시간과 구분해야 합니다.

2026-10-14 보완 후 검증: 전체 테스트 **2,331 통과 / 실패 0 / 48 건너뜀**(약 19분 41초).
건너뜀은 비공개 코퍼스 조건 40개, 기존 단계별 재개 기능 미구현 항목 7개, Windows 심볼릭 링크 권한 1개입니다.
D00/D07을 각각 새 캐시로 오프라인 실행하고 기존 모드와 비교했을 때 121개 결과의 상태·근거·이유 해시가 모두 같았습니다.
이 두 문서의 묶음 호출 절감은 0이며, 실제 API 속도나 LLM 정확도를 검증한 결과는 아닙니다.
