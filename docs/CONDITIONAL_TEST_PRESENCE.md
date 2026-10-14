# 조건 MD에서 필수시험 누락 검사까지 (선택 적용 v1)

**검토된 수행 조건 → 해당 배치의 수행 대상 → 시험 결과 기록의 존재**를 연결한다.
결과 자체의 적합은 기존 시험별 수치/정성 판정이 별도로 검사한다. 운영27규칙에는 자동으로 켜지지 않는다.
제공 허가서에 합성 조건을 붙이지 않았으며, 해시/형식 검증이 MD 의미의 승인은 아니다.

## 수정할 파일

예제는 `docs/examples/conditional_presence_rules/`에 있다.

- `rules.md`: `permit_conditional_tests`, 제품, 허가 전체 경로/지문, SP 정보·시험 절 경계,
  독립 배치 목록과 명칭 대응을 지정한다.
- `_conditions/conditions.md`: 회사/제품, 허가 전체 원문 ID와 조건 소유 ID, 모든 말단 시험,
  용기규격·차수·농도 원문 위치, 연산자/값/단위를 지정한다.
- `applicability_md: _conditions/conditions.md`가 연결한다. 명시한 RuleBook 안의 파일만 허용한다.
  외부 절대 경로/상위 이동/외부로 연결되는 심볼릭 링크는 거부한다.

조건 MD도 규칙 지문/캐시 키에 포함된다. 이미 읽은 RuleBook에서 파일만 바꾸면 보류하며 새로 읽어야 한다.
실행 중 SP·허가 PDF·조건 MD가 바뀌어도 보류한다. 수치/연산자/조건 종류/범위의 의미는 검토해야 한다.

## 상태의 의미

| 수행 여부 | 기록 | 시험별 존재 검사 |
|---|---|---|
| required | 같은 단계·배치에 원문과 연결된 결과 1건 | PASS: 기록 존재, 시험 적합 판정 아님 |
| required | 완전한 검사 범위에서 없음 | FAIL: 필수시험 누락 |
| required | 원문에는 언급/결과 없음/중복/연결 불명 | HOLD: 추출·근거 확인 필요 |
| not_required | 유무와 무관 | N/A: 수행 미해당, 실제 시험 합격 아님 |
| unknown | 유무와 무관 | HOLD: 수행 여부 불명, 면제 불가 |

기존 구조 검증 카드의 ‘충족’은 **수행 대상/기록 확인 규칙의 충족**이다. 전부 미해당이면
‘수행 대상 0건, 수행 미해당 2건, 시험 결과의 합격 판정이 아닙니다’와 개별 시험명을 표시한다.
시험별 감사 행은 N/A이며, 실제 시험 레코드나 시험 합격 결과를 만들지 않는다.
확인된 누락과 다른 보류가 함께 있으면 불충족을 유지하고 추가 보류 이유도 표시한다.
희석 절차 생략(`procedure`)을 시험 자체의 면제(`test_applicability`)로 취급하지 않는다.

## 근거 연결

허가 소유 ID(`source_node_id`)와 존재 검사 ID(`source_id`)는 다르다. 동일 PDF 바이트 지문,
정확한 전체 절 경로/번호, 선택 단계의 전체 말단 목록으로 일대일 연결한다. 시험명만으로 연결하지 않는다.
두 검사의 독립 배치 타입/전체 경로/시작·끝/필드도 같아야 한다. 전체 SP PDF를 다시 읽으므로
호출자의 부분 페이지로 원문 언급을 숨길 수 없다. 결과 필드와 추출 결과도 대조하여 기준 값을 빌리면 보류한다.
페이지/인용/문자 위치/지문/조건 결과/두 ID 대응을 저장하고 기존 원문 링크를 재사용한다.
회사·제품은 제출 건의 명시적 컨텍스트와 대조한다. 이 검사만으로 SP 제품명 오기를 승인하지 않는다.

## 재현

```powershell
.\.venv\Scripts\python.exe scripts/validate_rules.py --rules-dir docs/examples/conditional_presence_rules
.\.venv\Scripts\python.exe scripts/prove_conditional_test_presence.py --rules-dir docs/examples/conditional_presence_rules
.\.venv\Scripts\python.exe -m pytest -q tests/test_conditional_test_presence.py
```

proof는 `.local_validation/conditional_presence_proof/`에 정상/필수 누락/차수1/미해당/자기 값 누락/
단위 불일치/절차 조건 불충족의 합성7사례를 만든다. 지정 외부 MD는 덮어쓰지 않는다.
실제 PDF 추출→두 MD→허가/조건 검증→존재 검사→기존 카드 어댑터를 실행한다. CLOVA 호출은0이며
기존 D00~D12 CLOVA 회귀와 별도다. 최초 fixture 마지막 줄이 종이 밖으로 잘린 문제는 페이지를 나누어
수정했다. 판정 경계를 완화하지 않았다.

## 한계

단일 허가서·검토된 한 제조단계·독립 단일 배치·정해진 텍스트 표만 지원한다. 자유로운 자연어 MD를
모두 자동 실행하는 기능은 아니다. 복수 배치/OCR/임의 다열 표/중첩 논리/여러 개정판은 미완료다.
기존 `permit_required_tests`의 `all_leaf_sections_required` 의미는 변경하지 않았다.
합성 브라우저 검증은 기존 카드 렌더러와 실제 PDF 뷰어 연결 검증이다. 실제 제품 운영 활성화나
업로드→전체 시뮬레이션 완료를 뜻하지 않는다. 새 제품/양식/MD 수정마다 회귀와 원문 검토가 필요하다.
