# 제조번호 표와 허가 필수시험 연결

## v73에서 달라진 점

v74 후속 보완: 정확한 항목명 뒤의 `=`, `-`, `/`, 괄호 같은 미지원 문장부호도
항목 자체가 없다고 간주하지 않는다. 기본 배치로 대신 연결하지 않고 보류한다(8개 추가 변형).

허가서의 시험 목록을 읽는 것과 SP에서 **어느 배치가 그 시험을 했는지** 연결하는 것은 별개다.
기존 선택형 `permit_required_tests` 연산에 인접 항목/값 형식을 추가했다. 운영 27개 규칙에는 아직 넣지 않았다.

단순한 `제조번호: L1` 외에도 검토된 양식에서 `제조번호 L1`, 또는 같은 페이지의
`제조번호` 다음 줄 `L1`을 읽을 수 있다. 모든 표나 OCR을 자동으로 복구하는 기능은 아니다.

## MD에 지정하는 정보

기존 허가 경로/검토 지문/시험 단계 설정에 다음을 추가한다.

```yaml
batch_inventory: {type: content, context: [배치목록]}
batch_inventory_path: [원액, 배치목록]
batch_coverage_start: 1.1 배치목록
batch_coverage_end: 1.3 시험
batch_field: 제조번호
batch_field_layout: label_value_lines
batch_binding: single_batch_stage
```

- `batch_inventory_path`: 상위 장까지 포함한 정확한 정보 절 경로. 비슷한 제목의 다른 단계는 제외한다.
- `batch_coverage_start/end`: 배치 정보의 시작과 다음 절 경계. 전체 검사 범위 안에서 각각 유일해야 한다.
- `batch_field`: 줄 처음에 온전하게 있는 항목명. `제조용 세포주 제조번호`를 `제조번호`로 취급하지 않는다.
- `label_value_lines`: 동일 줄 공백/콜론/세로줄 또는 같은 페이지의 인접 비어 있지 않은 줄.
- `single_batch_stage`: 독립 정보 절에서 정확히 한 배치가 확인될 때만 그 단계의 시험을 연결한다.
  여러 배치이면 `explicit_test_field`와 `test_batch_field`를 사용한다.

예제는 `docs/examples/batch_field_rules/single`과 `multi`에 각각 있다. 두 폴더 중 하나를 선택해 검사한다.
상위 폴더를 그대로 운영 규칙 폴더로 지정하면 두 예제가 동시에 적용될 수 있으므로 그렇게 사용하지 않는다.

## 잘못된 합격을 막는 확인

1. 정보 레코드의 content/remarks와 raw_text에 **같은 항목과 값**이 있어야 한다.
2. 그 레코드 전체가 지정된 원문 범위와 기재 페이지에 있어야 한다.
3. 정보 절 원문의 제조번호 **전체 목록/개수**가 추출된 독립 레코드 목록과 같아야 한다.
4. 미추출 배치, 중복, 미정, `L1/L2`처럼 묶인 값, 페이지를 넘겨 이어 붙여야 하는 값은 보류한다.
5. 시험의 명시 제조번호와 기본 배치가 충돌하면 보류한다. 지원하지 않는 제조번호 표기도
   “필드 없음”으로 취급해 기본 배치를 붙이지 않는다.
6. 시험명은 해당 시험의 raw_text에도 있어야 한다. 다른 페이지/레코드의 같은 이름으로 대체하지 않는다.

추출기가 시험 필드 뒤에 추가한 `:` 또는 `|`만 원문 대조 시 정규화한다.
시간·비율·측정값 안의 문장부호, 숫자, 단위는 제거하지 않는다. 레코드/원본 파일 자체는 수정하지 않는다.

`batch_inventory_audit`에 경로, 페이지 범위, 표기 방식, 원문 항목 개수, 연결 배치를 남긴다.
합격은 **필수 시험의 결과 기록 존재**만 뜻한다. 시험 결과값 적합 판정과는 별개다.

## 실제 자료에 대해 확인한 범위

### v75: 정보 절과 시험 절을 따로 지정하기

정보 절에서 시험 절까지의 넓은 범위에는 다른 제조단계가 끼어 있을 수 있다.
이때 아래 두 항목을 함께 추가하면, 시험 기록 존재/누락의 원문 대조를 지정된 시험 구간에서 수행한다.
기존 `coverage_start/end`는 양쪽 구간을 포함하는 전체 범위로 유지한다.

```yaml
sp_stage_path: [원액, 대상시험]
coverage_start: 1 원액
coverage_end: 2 다음단계
batch_inventory_path: [원액, 배치목록]
batch_coverage_start: 1.1 배치목록
batch_coverage_end: 1.2 다른단계
test_coverage_start: 1.3 대상시험
test_coverage_end: 1.4 다음시험
```

- 시험 경계는 둘 다 필요하며 정확한 배치 경로/경계 설정도 있어야 한다.
- 각 경계는 문서 전체에서 유일하고 순서가 맞아야 한다. 두 구간은 전체 범위 안에 있고 서로 겹치지 않아야 한다. 정보가 뒤에 오는 검토된 양식도 허용한다.
- `sp_stage_path`가 선택한 시험은 일부만 걸러 버리지 않는다. 구간 밖·잘못된 페이지·OCR 의심·원문 전체 불일치 기록이 있으면 연결 불확실로 보류한다.
- 다른 단계의 동일 시험은 빌리지 않는다. 대상 구간에 시험명만 남고 연결된 결과 기록이 없으면 누락으로 단정하지 않고 보류한다.
- 완전히 빈 구간/누락·공백 페이지도 추출 완전성을 주장하지 않고 보류한다. 구간의 본문과 연결 근거가 확인되었을 때만 누락을 불충족으로 판정한다.
- `test_coverage_audit`에 경로, 실제 페이지, 시작/끝 문구를 남기고 기존 원문 링크 경로에 연결한다.

경계 문구와 전체 절의 대응은 MD 작성 시 원문을 검토해야 한다. 자동 의미 추론/서명 인증이 아니다.
시험명·결과줄로 범위를 임의 축소하거나 공통조건/하위 시험을 제외해서는 안 된다.
두 시험 경계가 없는 기존 규칙은 넓은 범위 전체에서 보수적으로 검사한다.
완전한 설정 예제는 `docs/examples/test_scope_rules/rules.md`에 있다.

### 제공 D00 연결 진단

D00 물리13쪽 Component A 중간체 정보에는 `SKY-CA220705`가 명시되어 있다.
제조용 세포주 번호, 같은 페이지의 Component B 번호, 머리말의 완제품 번호는 사용하지 않았다.
이 배치에 물리15~16쪽 중간체 시험 10개를 연결하고 허가서11~13쪽의 해당10개 말단 목록과 대조하는
**진단용 통합시험**을 추가했다(`tests/test_provided_batch_link.py`). 두 잔류물 시험의 명칭 차이는
해당 단계/방법/측정 대상에 한정한 명시 대응을 테스트 안에 기록했다. 해시는 고정된 합성 원본의 값이다.

이 시험은 전체 허가 공통조건 검토/운영 규칙 활성화를 대신하지 않는다. 해시를 자동 갱신하지 않는다.
본배양 배치 번호가 확인되지 않는 문제 역시 이 중간체 번호를 빌려 해결하지 않는다.
v75에서는 `3.2.2 Component A 중간체원액에 대한 시험`부터 `3.2.3 Component B 중간체원액에 대한 시험`까지를
독립 시험 구간으로 지정해 같은10개 진단과 기존카드/근거링크를 다시 검사한다.

## 재현

```powershell
.\.venv\Scripts\python.exe scripts/validate_rules.py --rules-dir docs/examples/batch_field_rules/single
.\.venv\Scripts\python.exe scripts/validate_rules.py --rules-dir docs/examples/batch_field_rules/multi
.\.venv\Scripts\python.exe scripts/prove_batch_field_policies.py
.\.venv\Scripts\python.exe -m pytest tests/test_required_batch_fields.py tests/test_batch_field_policy_pdf.py tests/test_provided_batch_link.py -q
.\.venv\Scripts\python.exe scripts/validate_rules.py --rules-dir docs/examples/test_scope_rules
.\.venv\Scripts\python.exe scripts/prove_test_scope_policies.py
.\.venv\Scripts\python.exe -m pytest tests/test_required_test_scope.py tests/test_test_scope_policy_pdf.py -q
```

proof는 별도 합성9개 PDF(허가1 + SP8)를 `.local_validation/batch_field_proof`에 만들고 원본을 바꾸지 않는다.
정상 PASS, 시험 누락 FAIL, 경계 누락 HOLD, 관련 원료번호만 존재 HOLD, 시험 배치 충돌 HOLD,
복수 배치 완전 PASS, 둘째 배치 시험 누락 FAIL, 복수 배치 연결 없음 HOLD를 검사한다. API 호출은 0회다.
생성된9개 PDF의 전체 페이지를 Poppler로 렌더해 글자/줄 잘림이 없음을 확인했다.

독립 시험 구간 proof는 `.local_validation/test_scope_proof`에 허가1+SP7개 PDF를 만든다.
대상 구간 정상 PASS/누락 FAIL, 대상 구간 미추출 언급 HOLD, 경계 누락 HOLD, 중복 HOLD,
결과 누락 HOLD, 다른 단계에만 시험이 있는 명시적 기록 없음 구간 FAIL을 검사한다.
8개 PDF의 전 페이지를 렌더·육안 확인했으며 원본 변경/API 호출은 없다.

남은 범위: 조건부 의무의 전체 근거 기반 CLOVA 계약, 복수 허가 개정 관계, 스캔/복잡한 다열 표,
실제 제품별 업무 검토 후 운영 적용과 새 판정 카드 UI 검증. 기존 `delimited_line` 기본 계약은
독립 배치 범위를 지정하지 않으면 새 원문 전체 목록 대조를 수행하지 않으므로 단순 검토 양식 전용이다.
