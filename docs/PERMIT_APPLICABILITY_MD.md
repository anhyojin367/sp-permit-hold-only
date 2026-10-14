# 허가 조건을 MD에서 수정하는 방식 (v80 선택형)

## 이번에 된 것

기존 합성 시험은 Python 함수에 조건을 적었다. 이제 `permit_applicability_md.py`가 명시적으로 선택한
MD의 YAML 설정을 읽어 동일한 조건 검증기에 연결한다. 시험 차수 기준, 용기 규격, 비교 연산,
모든 조건/하나 이상 조건 조합을 **지원 범위 안에서 Python 수정 없이** 바꿀 수 있다.

실행 가능한 예제는 `examples/applicability_rules/conditions.md`다. 운영27규칙이나 실제 제품 화면에
자동 적용하지 않는다. 현재는 실제 SP 표를 연결하기 전의 진단용 입력 양식이다.

v81에서 별도 `single_batch_scoped_records_v1`과 전체 PDF를 직접 읽는 `evaluate_numeric_pdf`를
추가했다. 정확한 정보/시험 절·독립 단일 배치·원문 필드 전체를 검증한다. 범위와 재현은
`SCOPED_CONDITION_FACTS.md`를 참고한다. 아래 페이지 블록 양식은 기존 의미를 유지한다.

## 사람이 수정하는 곳과 코드가 고정하는 곳

| MD 설정 | 뜻 | 검사 |
|---|---|---|
| `conditions[].operator`, `value`, `unit` | eq/ne/gt/gte/lt/lte 수치 조건 | 숫자는 따옴표로 감싼 십진수, 단위 선언과 일치 |
| `conditions[].purpose` | 시험 수행 조건 또는 절차 조건 | procedure는 시험 면제 조합에서 제외 |
| `requirements[].combine` | all 또는 any | 생략 불가; 불명확한 수행 조건이 있으면 보류 |
| `stages`와 `requirements` | 검토한 제조단계와 그 말단시험 전부 | 단계 중첩·잘못된 연결·전체 시험 누락/추가 거부 |
| `fields` | 해당 SP에서 읽을 값과 단위 | 조건 필드와 정확히 대응; 없는 값은 추측하지 않음 |
| `reviewed_document_id`, `reviewed_node_ids` | 허가서 전체와 문단 검토 범위 | 다른 문서·누락/중복·옛 지문 거부 |
| `reviewed_condition_ids` | 검토된 조건 목록 | 조건 행만 실수로 지우면 거부 |
| `company`, `product` | 적용할 제출 문서 범위 | 정확히 일치해야 적용 |

전역 안내/날짜를 포함한 전체 문단을 대조하고, 조건 원문은 그 시험의 상위/자기 본문에서만 가져온다.
선택된 단계의 모든 말단을 시험 의무로 다룬다는 해석은 **검토자가 확인하는 입력 전제**다. 안내 문단을
시험으로 잘못 분류한 설정까지 ID 검사만으로 고쳐 주지는 않는다.

## 중요한 안전 경계

- **MD는 자동 승인서가 아니다.** 검토 사유와 해시는 인증 서명이 아니다. 숫자/연산자를 형식에 맞게
  잘못 적으면 잘못된 조건이 실행될 수 있다. 원문과 의미가 맞는지 검토하고 정상·오류·경계값 시험을 돌려야 한다.
- 단위나 필드 이름을 조건과 선언에서 동시에 잘못 바꾸면 형식 검사를 통과할 수 있지만, 원문에서 값이
  확인되지 않거나 단위가 다르면 보류한다. 허가 기준값을 SP 관측값 대신 사용하지 않는다.
- 허가서가 바뀌면 새 ID를 자동 복사해 통과시키지 않는다. 전체 문단과 조건 해석을 다시 검토한다.
- MD 본문은 설명이고 실행 정의는 첫 YAML 구간이다. 아래에 자연어 한 줄을 덧붙였다고 새 연산이
  자동 생성되지는 않는다. 자유문장→구조화 조건의 자동 생성·검토는 아직 별도 작업이다.
- 설명·공백·주석까지 파일 전체 바이트의 SHA256이 응답 계약에 묶인다. MD가 바뀌면 이전 응답은 거부한다.
  로드된 설정은 스냅샷이며 내부 목록을 바꿔 같은 MD 해시로 다른 조건을 실행할 수 없게 했다.
- 잘못된 YAML/중복 키/별칭·병합/임의 Python 태그/미지원 필드/부정확한 자료형은 거부한다.
  설정 검사는 CLOVA 클라이언트를 만들기 전에 한다. 임의 코드나 외부 파일을 실행하지 않는다.
- 기존 `permit_required_tests`는 무조건 필수 목록 전용이다. 이 MD 파일로 기존 연산의 의미를 바꾸지 않는다.

## 검토된 수치 조건의 실행 (v80)

검토된 `100 mL와 같은가`, `시험차수 2 이상인가` 같은 비교를 LLM에게 재계산하도록 요구하지 않는다.
`evaluate_numeric_applicability`와 명시적 `reviewed_numeric_v1` 실행 방식은 다음 순서로 동작한다.

1. MD를 새로 읽고 회사/제품·허가 전체 지문·시험/조건 전체 목록을 검증한다.
2. PDF 추출기가 제공한 전체 물리 페이지에서 단계·배치·필드 사실을 새로 만든다. 이전에 만든
   Target이나 모델이 제안한 사실/수행 여부를 입력으로 받지 않는다. 물리1쪽 시작·연속 페이지 및
   검토된 단계와의 정확한 대응을 요구한다. 지원하지 않는 단계를 조용히 제외하지 않는다.
3. 기존 십진수 비교기를 공유해 조건별 참/거짓/불명을 계산한다. 단위 불일치, 값 없음, 중복/충돌은
   unknown이다. all/any에서도 수행 조건 중 불명이 있으면 보수적으로 보류한다.
4. procedure 조건은 별도로 기록하되 시험 면제에 사용하지 않는다. 반환값의 `complete`는
   **수행 여부의 계산 완료**이며 절차 적합·시험 결과 합격·출하 승인을 뜻하지 않는다.
5. MD/허가/배치·원문 위치/사실/조건/결과와 실행 방식에 연결된 `evaluation_id`를 남긴다.
   실행 중 MD가 바뀌면 결과 반환을 거부한다. 해시는 이력 식별용이며 인증 서명은 아니다.

이 경로는 API0이고 모델 응답 자체가 없다. `accepted`는 입력 계약 검증을 뜻하며, 필요한 사실이
부족하면 `accepted=true`여도 `evaluation_status=held` 및 `applicability=unknown`이다.
잘못된 MD/허가/페이지·단계 범위는 예외로 중단한다. 호출자는 이를 합격이나 빈 목록으로 바꾸면 안 된다.

LLM 응답 실패를 이 경로로 자동 재시도하는 fallback은 없다. 실행 방식을 시작 전에 선택해야 한다.
이전 `llm_evidence_v1` 프로토콜과 실패 응답 검증은 그대로 보존한다. 모델이 올바른 수치 응답을 내는
문제를 해결한 것이 아니라, 이미 검토된 숫자 계산을 모델의 역할에서 분리한 것이다. LLM의 자연어
조건 해석·조건 초안/근거 연결과 그 검토 절차, 실제 SP 표 어댑터는 여전히 후속 작업이다.

기존 페이지 블록 함수는 PDF 추출기 쪽의 신뢰된 전체 페이지를 입력으로 받는다. 페이지 텍스트를 임의로 삭제/위조한
호출자까지 인증하는 API가 아니며, 끝부분 페이지 누락을 텍스트 목록만으로 알아낼 수는 없다.
현재 proof는 선택한 PDF 전페이지 추출을 수행하지만 실제 운영 연결에서도 이 전제가 보장되어야 한다.
v81 표 전용 진입점은 원본 PDF 전체 바이트를 직접 읽어 이 페이지 목록 전제를 보강한다.

## 기존 PDF/CLOVA 근거 프로토콜 (비교 검증용)

1. 명시적으로 선택한 MD를 읽고 자료형·전체 선언을 검사한다.
2. 해당 회사/제품의 허가 원문 지문·전체 문단·선택 단계 말단 전체·조건 구간을 대조한다.
3. SP에서 단계/배치별 사실을 독립적으로 추출한다. 현재 지원 양식은 한 물리 페이지당
   `제조단계: ...`, `제조번호: ...`가 각각 하나이고 조건 필드가 `필드: 값`으로 있는 형태다.
4. 각 배치의 근거만 CLOVA에 제공한다. 모델은 조건별 인용과 truth를 반환한다.
5. 코드가 전체 ID/인용/수치 비교를 다시 검사하고 최종 required/not_required/unknown을 계산한다.
6. 배치 응답을 합친 뒤 전체 의무·배치 누락을 다시 검사한다. 실험용 runner는 잘못된 응답을 한 번만
   재검토하고 모든 시도와 실패를 기록한다. 재실패하면 보류한다.

required는 시험 수행이 필요하다는 뜻이며 시험결과 합격과 다르다. 필수시험 존재·누락 판정과 기존 카드
연결은 아직 후속 단계다. 실제 제품의 복잡한 표, 복수 개정 허가서, 모든 자연어 조건은 현재 지원 범위가 아니다.

현재 조건식은 원문 구간ID 하나당 수치 비교 하나와 평면 all/any 조합만 지원한다. 한 문단에 여러 조건이
섞였거나 AND/OR가 중첩된 문장을 임의로 하나의 비교로 축약하면 안 된다. 문장/조건별 근거 구간과
복합 논리식 지원은 별도 확장이 필요하다.

## 재현

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_permit_applicability_md.py tests/test_permit_applicability_md_pdf.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_reviewed_numeric_execution.py
.\.venv\Scripts\python.exe scripts/prove_permit_applicability.py --execution-mode reviewed_numeric_v1 --rules-md docs/examples/applicability_rules/conditions.md --output .local_validation/applicability_numeric_proof
.\.venv\Scripts\python.exe scripts/prove_permit_applicability.py --rules-md docs/examples/applicability_rules/conditions.md --isolate-targets --output .local_validation/applicability_md_proof
.\.venv\Scripts\python.exe scripts/prove_permit_applicability.py --live --rules-md docs/examples/applicability_rules/conditions.md --isolate-targets --output .local_validation/applicability_md_proof
```

live는 승인된 CLOVA 비용이 발생한다. 응답캐시가 포함될 수 있으며 논리 요청 수와 신규 과금 요청은 다르다.
수치 경로는 `numeric-report.json`에 별도로 기록하며 `--live` 또는 `--isolate-targets`와 혼합하면 거부한다.
오래된 live 실패 보고서를 덮어쓰지 않는다. 모델 프로토콜의 offline 모드는 모의 근거 응답을 검사하는
기존 시험이며, 새 수치 경로는 그 모의 응답 생성/검증도 거치지 않는다.
정상/미해당/사실누락/절차 조건/복수배치5사례를 별도 합성 PDF로 검증한다. 원본 사용자 문서는 수정하지 않는다.
MD에 잘못 쓴 `ne`를 `eq`로 고쳐 같은 PDF의 결과와 계약 지문이 달라지는 시험도 있다. 이것은 MD 수정
반영의 증거이지 서로 모순된 두 설정을 모두 올바른 허가 해석으로 승인하는 시험이 아니다.

최신 전체 회귀/실호출 결과는 `WORK_STATUS.md`를 따른다. 실패 이력을 삭제하거나 결과를 억지로 합격시키지 않는다.

### v79 실호출에서 확인한 한계

최초 MD 연결 실호출은5사례 중4개 기대일치였다. required 사례에서 모델이 독립 사실로 확인된
100mL=100mL 조건을 재검토 후에도 unknown으로 답했다. 코드의 독립 비교는required였지만 모델 근거
응답은 거부되어 해당 묶음은 보류됐다. 파일은 `live-first-md-failure.json`으로 보존했다.
따라서 MD 로더의 작동, 숫자 비교의 정확성, 모델이 항상 올바른 조건 응답을 내는 것은 서로 다른 검증이다.
기존 v78 합성5사례 성공을 이번 새 MD 계약의 전부 성공으로 재사용하지 않는다. 모델 수치 응답을
필수 관문으로 두는 구조는 여전히 불필요한 보류를 만들 수 있으며, 운영 연결 전에 역할 분리가 더 필요하다.

v80에서는 위 별도 수치 경로로 역할을 분리했다. 동일 MD·동일6PDF(7쪽)에서5사례 기대일치/API0을
확인했으며 missing_fact는 의도한 보류로 남았다. 예전 CLOVA8응답도 새 비교기로 재검증해 기존의
수락4사례/거부1사례를 그대로 재현했다. 이를 CLOVA5/5 성공이라고 보고하지 않는다.
