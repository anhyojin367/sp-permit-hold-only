# PDF 표 추출 이식성·긴 문서 처리 (v105/v106)

## Python 3.14 배포 호환성 — v106

9월 30일 사용자가 제공한 실제 Streamlit 로그에서 **Python 3.14.7**을 확인했다.
3.10에서 검증한 패키지를 그대로 고정한 v105에는 다음 호환 문제가 있었다.

- Streamlit 1.47은 3.14 대응 및 deferred-annotation 수정 전이다. 1.61.0으로 고정한다.
- Pillow/scikit-learn/pandas/Pydantic/PyYAML을 3.14 wheel이 있는 버전으로 고정한다.
- Camelot 1.0.9는 Python 3.12 미만에서는 pypdf 3.x, 3.12 이상에서는 4~5.x를 요구한다.
  따라서 requirements에서 Python 조건에 따라 3.17.4/5.9.0을 선택한다.
- 업무 판정 코드·MD·더미 PDF는 바꾸지 않는다. 이전 결과 재사용 방지를 위해
  앱/판정 캐시는 v106이며 HTML의 `data-app-version`은
  `sp-ui-cache-v106-python314-runtime`이다.

격리된 Windows/Python 3.14.7 설치와 의존성 검사, 홈/업로드 창 기동 확인,
Linux/Python 3.14.7 wheel 전용 설치계획 검사, Python 3.10 설치계획 검사를 수행했다.
설치계획 검사는 Linux에서 앱 전체를 실행했다는 뜻이 아니다.
3.14에서 새로 추출한 D00~D16은 기존 3.10 hybrid 결과와 **17/17 일치**했다
(시험값·MD 상태·개별 판정·집계 및 기대 오류 누락 0; CLOVA 호출 0).
근거는 `.local_validation/deploy_314/comparison.json`이다.
최종 전체 회귀시험은 **1,659 passed / 7 skipped / 실패 0 (287.74초)**이다.
7개 skip은 기존 미구현 단계 재개 계약이며 이번 호환 수정으로 완료한 것이 아니다.
근거는 `.local_validation/deploy_314/full_suite_final.xml`이다.
새 3.14 앱의 홈, 업로드 창, HTML v106 마커도 확인했다.
이 로컬 결과와 클라우드 실제 실행 확인은 구별한다.

Windows 회귀시험은 프로젝트 내 짧고 고유한 임시 경로를 지정한다.
깊은 시험 폴더에 긴 업로드 파일명/OCR 캐시 해시가 겹치면 Windows 경로
길이 제한으로 실패할 수 있다. 이번 호환 수정은 임의 길이의 Windows 경로
지원을 추가한 것이 아니며, OS의 보안/긴 경로 설정은 변경하지 않았다.

### 합성 회귀시험 허가서의 재현성

검토 완료된 MD 예제는 허가서의 **PDF 파일 SHA-256**에 바인딩된다.
같은 문장을 ReportLab으로 다시 그려도 Python/zlib가 달라지면 PDF 바이트가
달라질 수 있으며, 이 경우 기존 MD가 그 문서를 거부하는 것은 정상이다.
시험을 통과시키려고 문서 해시 검사를 끄거나 MD 해시를 새 값으로 바꾸지 않았다.

- `tests/fixtures/reviewed_required_test_permit.pdf`: 필수 시험·배치·시험 범위 예제가 검토한 합성 허가서.
- `tests/fixtures/reviewed_applicability_permit.pdf`: 조건부 적용 예제가 검토한 합성 허가서.
- `scripts/reviewed_test_fixture.py`: 두 fixture의 기존 해시를 확인한 뒤 시험용 출력 폴더에 복사한다.
- `scripts/prove_*` 중 해당 예제 4개만 이 파일들을 쓴다. 제출 SP, 수정 허가서 반례,
  실제 업로드 문서는 여전히 각 문서 자체를 읽으며 운영 파이프라인은 이 helper를 호출하지 않는다.

따라서 새 환경의 회귀시험도 검토 당시와 정확히 같은 허가서 바이트를 사용한다.
실제 허가서가 바뀌면 다시 검토해야 한다는 운영 원칙은 유지된다.

참고: [Streamlit 3.14 호환 수정](https://docs.streamlit.io/develop/quick-reference/release-notes/2026),
[Camelot 1.0.9 배포 메타데이터](https://pypi.org/project/camelot-py/1.0.9/).

## 배포본 사용 안내 — 2026-09-30

이 버전은 기존 `main` 및 기존 Streamlit 앱을 업데이트하기 위한 게시본이다.
Python 3.10에서 검증했으며 `requirements.txt`의 버전과 기본 `hybrid` 경로를
사용한다. 아래 시험 결과는 로컬에서 수행한 증거이며 클라우드 전수검증과 구별한다.

업데이트 후 이전 환경의 캐시 결과는 재사용하지 않는다. 기존 문서를 선택해
**검수 진행**을 누르거나 SP PDF와 해당 허가서를 함께 업로드한 뒤 검수한다.
API 키는 Streamlit Secrets에만 두며 GitHub에는 올리지 않는다.
v105 당시 페이지 상단 HTML의 `data-app-version`은 `sp-ui-cache-v105-portable-extraction`이었다.
현재 v106 표시는 위 항목을 따른다. 버전 속성은 화면에 표시되지 않는다.

## 이번 변경의 목적

동일한 SP/허가서라도 설치된 추출기에 따라 표의 빈 칸, 병합 셀,
줄바꿈이 달라져 판정이 바뀌는 문제를 다룬다. 문서의 실제 기준이나 결과를
고치지 않으며, 파일명/더미 정답에 따라 판정을 강제하지 않는다.
기존 화면의 배치와 MD 업무 기준은 변경하지 않는다.

## 확인한 원인

기존 `PDFReader`는 Camelot을 import할 수 있으면 표를 교체하고, 없거나
실패하면 조용히 pdfplumber 결과만 반환했다. 로컬에는 Camelot 1.0.9가
있었지만 설치 목록에는 없었다. pdfplumber 전용 경로에서 배포본과 같은
빈 셀/행 분리 형태를 재현했다. 실제 배포 서버의 설치 버전 전체를
확인한 것은 아니므로, 이 문서를 클라우드 환경 감사 완료로 읽으면 안 된다.

## 어떤 코드가 바뀌었나

| 파일 | 역할/변경 |
| --- | --- |
| `sp_pdf_judger/table_values.py` | 필드 경계의 빈 셀/세로형 제조사 정보/입증되는 원료행 이어짐 처리. 내부의 서로 다른 값은 합치지 않는다. |
| `sp_pdf_judger/policy_engine.py` | 위 공통 파서를 날짜·제조번호·허가서 필드·제조량 검사에 사용한다. 중복 필드는 첫 값으로 통과시키지 않는다. |
| `json추출.py` | 추출 경로를 명시하고, Camelot 처리를 16쪽 단위로 분할한다. pdfplumber 페이지 캐시와 이미지 객체를 해제한다. 독립 native_text 및 페이지별 추출 감사를 남긴다. |
| `sp_pdf_judger/extraction_runtime.py` | 추출 방식·Python/OS·주요 패키지 버전 기록, 환경 지문, 원본 스트리밍 해시, 읽지 못한 페이지 차단. |
| `sp_pdf_judger/extractor.py` | 자식 추출기의 UTF-8 출력을 OS 기본 인코딩과 무관하게 읽는다. |
| `sp_pdf_judger/pipeline.py` | 추출 보고서를 판정 결과에 보존하고, 확인된 추출 실패/빈 결과를 정상 검수로 끝내지 않는다. |
| `sp_app.py`, `sp_judgement_bridge.py` | v105 캐시와 추출 환경 지문으로 다른 환경의 이전 결과를 새 결과처럼 재사용하지 않는다. 저장된 이전 결과 파일은 삭제하지 않는다. |
| `requirements.txt` | 누락된 Camelot을 추가하고 핵심 PDF 라이브러리를 로컬에서 사용한 버전으로 명시한다. |
| `scripts/compare_extraction_runs.py` | 두 독립 실행의 D00~D16 MD 상태, 시험 개수·기준·결과·기간, 개별 판정·집계, 기대 오류 누락을 대조한다. 한 문서라도 없거나 실행 중 엔진이 바뀌면 통과시키지 않는다. |

## 실행 방식

기본은 `hybrid`(pdfplumber + Camelot)이다. 이 모드의 의존성이 없거나
표 추출이 실패하면 다른 방식으로 몰래 바꾸지 않고 오류를 표시한다.
`SP_TABLE_BACKEND=pdfplumber`는 대조시험을 위한 명시적 경로다.
값을 잘못 입력하면 시작 시 오류가 난다. 두 모드의 결과 캐시는 분리된다.

```powershell
# 기본 운영 경로
$env:SP_TABLE_BACKEND = 'hybrid'
.\.venv\Scripts\python.exe scripts/validate_corpus.py --output-dir .local_validation/portable_extraction/hybrid_check

# 별도 추출 경로 대조. 기존 파일은 수정하지 않는다.
$env:SP_TABLE_BACKEND = 'pdfplumber'
.\.venv\Scripts\python.exe scripts/validate_corpus.py --output-dir .local_validation/portable_extraction/pdfplumber_check

.\.venv\Scripts\python.exe scripts/compare_extraction_runs.py .local_validation/portable_extraction/hybrid_check .local_validation/portable_extraction/pdfplumber_check --output .local_validation/portable_extraction/comparison.json
# 대조시험 뒤 일반 실행은 기본 방식으로 복귀
$env:SP_TABLE_BACKEND = 'hybrid'
```

`--live` 없는 실행은 CLOVA를 호출하지 않는 오프라인 검사다. 실제 CLOVA
판정의 성공이나 비용 발생을 의미하지 않는다. 검사기의 종료 코드 0만
보지 말고, 대조 보고서에서 추가 보류/차이와 `all_equal`도 확인해야 한다.

## 긴 문서의 근거 보존

- 페이지 상한을 두고 뒷부분을 버리지 않는다.
- Camelot의 페이지 이미지 작업을 16쪽으로 나눈다. 최종 페이지의 짧은 묶음도 처리한다.
- `01_raw_pages.json`: 페이지별 텍스트·표·좌표 및 표 재구성과 독립적인 `native_text`.
- `05_records.json`: 기존 UI/판정에서 쓰는 구조화 레코드.
- `06_extraction_result.json`: 원본 SHA-256, 페이지, 레코드, 추출 보고서.
- `extraction_report.json`: 전체/처리 페이지, 페이지별 단어·표·이미지 수,
  읽지 못한 페이지, 추출 방식과 실행 환경.
- 텍스트가 없는 이미지 전용 페이지나 깨진 문자 근거는 조용히 버리지 않고
  확인 필요 오류를 낸다. 이번 목표는 스캔본 OCR 개선이 아니다.

## 검증 범위와 남는 제한

320쪽 텍스트 합성 PDF에서 SP 구조화 결과 및 허가서 근거에 마지막 페이지가
남는 것을 검사한다. 160/320쪽의 선 있는 표와 319쪽의 도형 내 텍스트도
검사한다. 도형의 화살표 연결 의미를 이해했다는 시험은 아니다.
이는 실제 회사의 수백 쪽 혼합 양식 정확도나 처리 시간
보장이 아니다. 원본에 정보가 빠졌거나 서로 다른 값이 있으면 여전히
보류/불충족이어야 한다. 특히 빈 셀을 무조건 앞 값으로 채우거나 단위를
임의 환산하는 방법은 사용하지 않는다.

검토되지 않은 회사 양식의 표·제조요약도 연결 관계까지 100% 보장하지 않는다.
기존 제품별 추출 후처리와 요약도 추론은 남아 있으며, 새 양식에서는
원문 페이지/표와 함께 확인해야 한다. 이번 작업의 수락 근거는 별도의
로컬 시험 결과와 D00~D16 대조 보고서이다. 9월 29일 로컬 개선 단계에서는
실제 회사 문서 외부 전송, Git 푸시, 배포를 수행하지 않았다.
9월 30일 사용자의 별도 배포 요청으로 이 소스를 게시한다.

## 2026-09-29 재현 결과

### 독립 환경과 추출 방식 대조

기존 `.venv`에는 시스템 패키지 공유가 켜져 있었다. 비교용으로
`.local_validation/portable_extraction/clean_env`를 새로 만들고 공유 없이
현재 `requirements.txt`를 설치했다. 원래 가상환경은 바꾸지 않았다.
새 환경 `pip check`는 의존성 충돌 없음으로 끝났다.

- 기존 환경: Windows/Python 3.10.12, NumPy 1.26.4, 명시적 pdfplumber 경로.
- 새 환경: Windows/Python 3.10.12, NumPy 2.2.6, Camelot 1.0.9 포함 hybrid 경로.
- D00~D16 각각 PDF에서 새로 추출했으며, 두 실행 모두 CLOVA 호출 0회.
- **17/17 문서의 시험 기준·결과·기간, MD 상태, 개별 판정, 요약 건수 일치.**
- 지정 기대 오류/정상 대조군 검사 누락 0. D00/D15/D16 불충족 오탐 0.
- 보고서: `.local_validation/portable_extraction/comparison_final.json`, `all_equal=true`.
- 입력 보고서: `clean_hybrid_v2/`, `pdfplumber_v2/`. 이름이 `*_final`인 이전
  탐색 실행에는 수정 전 R21 차이가 있으므로 최신 통과 자료로 사용하지 않는다.
- 공통 엔진 지문: `a7b93bdba6036806272fc773d77a37fedf8ace76a0b2fecc018ea1b7a4c74fc1`.

위 비교는 Windows 안에서의 새 설치/추출 방식 비교다. Linux 배포 서버에서
재실행한 결과는 아니며, 모든 간접 의존성을 잠근 완전한 lock 파일도 아니다.

### CLOVA 연결 판정

동일한 최종 소스로 `--live --cases D00 D01`을 실행했다.

| 문서 | 충족 | 불충족 | 보류 | 전체 |
| --- | ---: | ---: | ---: | ---: |
| D00 | 129 | 0 | 2 | 131 |
| D01 | 128 | 1 | 2 | 131 |

각각 논리 LLM 호출 104/성공 104이며 기존 응답 캐시 재사용이 포함될 수 있다.
원격으로 새 요청 104회를 보냈다는 뜻은 아니다. 근거는 `live_final/D00.json`,
`live_final/D01.json`이다. 원본 근거 미해결 보류가 있어 엄격한
`live_acceptance_complete=false` 및 종료 코드 1을 유지했다. 이를 완전 자동
판정 완료로 바꾸지 않았다. LLM 없는 오프라인 실행과 실제 모드의 전체 건수는
130 대 131로 다르므로 서로 같은 검증으로 합쳐 보고하지 않는다.

공통 보류 2건은 Component B 엔도톡신의 SP `EU/mL` 대 허가서
`EU/mg of protein`, 나노파티클 무균시험 결과란의 요구 문장 반복이다.
새 추출기가 이 값을 임의로 바꾸거나 원본/MD를 수정해 정상 처리하지 않았다.
D13은 필수 단계 누락에 따른 추가 보류도 있어 전 문서 보류가 2건인 것은 아니다.

### 통신 실패와 문서의 보류는 다르다

실제 UI 검사 중 API 연결 실패가 판정에 보류를 늘리고, 저장된 집계가 있다는
이유로 문서함이 '검수 완료'를 표시하는 문제도 발견했다. `status=failed`는
이제 **검수 오류 · 재시도 필요**로 표시하며, 실패한 집계를 완료 숫자로
내보내지 않는다. 시뮬레이션/최종 판정 페이지도 실패 메타데이터를 확인해
중단한다. 연결·API 설정을 바로잡은 뒤 기존 '검수 진행' 버튼으로 명시적으로
재시도한다. 과거 실패 기록은 보관되며, 조회만으로 유료 재시도를 하지 않는다.

이 보호 동작은 원문 근거가 불명확한 정상적인 HOLD를 없애지 않는다.
실제 API 오류를 정성시험 불충족이나 문서 자체의 보류로 오인하지 않도록
실행 상태와 판정 결과를 구분하는 것이다.

2026-09-29 실제 로컬 UI에서 같은 D01을 명시적으로 다시 검수했다.
SP 단계 LLM 1/1 성공, 허가서 단계 104/104 성공, 양쪽 마지막 오류 없음,
최종 **128 충족/1 불충족/2 보류/131 전체**로 완료됐다. 상태 인덱스는
`completed`이며 artifact key는 `cdf94d563d3004ec542f60fd`다.
이는 runner의 JSON을 화면에 복사한 결과가 아니라 UI 버튼에서 새로 실행한
결과다. 중간 429 응답에는10/20/40초 backoff를 적용해 10분 이상 걸렸다.
화면 애니메이션 시간과 실제 PDF·CLOVA 검수 시간을 혼동하면 안 된다.

### 회귀시험과 현장 확인의 차이

최종 전체 자동시험은 **1,655개 통과/실패 0/기존 미구현 단계 재개 계약 7개
skip, 414.70초**다(`full_suite_ui_final.xml`). 비교 검사기용 5개 시험,
통신 실패 표시용 4개 시험, 표/도형 텍스트를 추가한 320쪽 시험을 포함한다.
앞선 `full_suite_final.xml`(1,646개), `full_suite_delivery.xml`(1,651개)는
후속 UI 보호 코드 추가 전의 성공 기록이며 최종 수치에 더해서 집계하지 않는다.

이 결과는 현재 더미의 **명시된 기대 항목**과 추출 이식성에 대한 증거다.
모든 실제 회사 양식·모든 A/B/C 아이디어·제조요약도의 임의 연결 구조까지
완료했다는 의미는 아니다. 현장에서는 회사 문서의 원문과 추출 근거를 먼저
대조하고, 새로운 양식은 정답 기준을 만들어 같은 회귀시험에 추가해야 한다.
