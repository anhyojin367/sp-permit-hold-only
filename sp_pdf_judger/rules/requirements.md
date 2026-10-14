---
rules: []
requirements:
  - {id: A.1, instruction: 문서 안 여러 위치의 버전 정보가 서로 같은지 확인, rule_ids: [R01], coverage: active, scope: 반복된 버전과 개정일 원문 비교}
  - {id: A.2, instruction: 페이지 번호가 1부터 마지막까지 연속인지 및 표시 전체 수와 실제 수가 맞는지 확인, rule_ids: [R02], coverage: active, scope: 번호 없는 표지를 제외한 인쇄 페이지 번호}
  - {id: A.3, instruction: 제조요약도의 제조단계 및 공정 항목이 상세제조기록에 모두 있는지 확인, rule_ids: [R03, R29, R04, R05, R12], coverage: partial, scope: 등록된 필수 시험 및 요약도 필드 비교; 임의 신규 공정은 범위 정의 필요}
  - {id: A.4, instruction: 신청제품정보에 반복 기재된 값들을 다른 구간과 비교, rule_ids: [R04, R05, R06, R12, R27], coverage: partial, scope: 제조번호·제조량·수량·제조일·사용기간; 임의 기타 필드는 미지원}
  - {id: A.5, instruction: 약어 및 기호 목록과 문서 내 사용 의미 확인, components: [permit_search_aliases, domain_details], coverage: partial, scope: 등록 약어의 검색 연결; 임의 약어 의미의 전수 검증을 뜻하지 않음}
  - {id: A.6, instruction: 시험기준과 결과의 단위를 확인하고 허가사항 또는 시험기준과 연결, rule_ids: [R13], components: [JudgeEngine], coverage: active, scope: 같은 측정대상의 단위 전체 및 연결 허가 기준}
  - {id: A.7, instruction: 대한민국약전 및 생기법 일반시험법과 시험명·방법 연결, rule_ids: [R20], components: [general_test_methods, PermitPdfStore], coverage: partial, scope: 제공 허가서와 등록 시험법 범위; 외부 약전 전체를 자동 취득하지 않음}
  - {id: A.8, instruction: 유럽약전 시험법과 시험명·방법 연결, rule_ids: [R20], components: [general_test_methods, PermitPdfStore], coverage: partial, scope: 제공 허가서와 등록 시험법 범위}
  - {id: A.9, instruction: 최신 허가사항 반영 SP 버전과 회사 제출 SP 버전의 일치 확인, rule_ids: [R01, R19, R20], components: [_version_gate], coverage: partial, scope: 현재 기준 버전 및 허가서의 지정 기준·방법 비교; 최신 여부는 기준자료 갱신 필요}
  - {id: A.10, instruction: 학습된 SP 포맷과 제출 SP의 전체 포맷 일치도 확인, components: [extraction_runtime, extract_records], coverage: requires_reference, scope: 삭제하지 않고 유지; 추출 품질 검증과 전체 시각 포맷 동일성은 다름. 비교 템플릿·허용차가 명시되기 전 자동 충족 선언 금지}
  - {id: A.11, instruction: 문서 버전 아래 날짜를 인식해 일치 여부 확인, rule_ids: [R01], coverage: active, scope: 문서에 반복된 개정일}
  - {id: A.12, instruction: 페이지 번호·목차 번호·각 시험에 맞는 표 연결 확인, rule_ids: [R02], components: [tree_builder, table_expander], coverage: partial, scope: 페이지 및 절 경로로 연결; 임의 목차와 표의 전체 대응은 추가 원문 검증 필요}
  - {id: A.13, instruction: SP 문서 중간 페이지 누락을 페이지 번호 기준으로 판별, rule_ids: [R02], coverage: active, scope: 인쇄된 번호의 연속성; 번호까지 다시 매긴 삭제는 필수 항목 검사로 보완}
  - {id: A.14, instruction: 제조공정·최종원액 공정·최종원액 무균시험 누락 확인, rule_ids: [R03, R29], coverage: active, scope: 스카이코비원 MD에 지정한 필수 단계와 시험}
  - {id: A.15, instruction: 'IU/mL, CoV, RBD PAb, 생기일반, 생기각조를 단위·약어·용어로 인식', rule_ids: [R13], components: [unit_normalizer, permit_search_aliases], coverage: partial, scope: 단위와 등록 검색 명칭; 모든 문맥에서 의미 동일성을 단정하지 않음}
  - {id: A.16, instruction: 제조요약도 정보를 추출해 해당 항목과 비교, rule_ids: [R04, R05, R06, R12], coverage: active, scope: 단계별 제조번호·량·수량·일자}
  - {id: A.17, instruction: 완제의약품 제조년월일을 다른 항목으로 잘못 추출하지 않는지 확인, rule_ids: [R12, R24], components: [table_values], coverage: partial, scope: 필드 경계와 단계 연결 검사; 임의 신규 양식의 완전 추출 보장은 아님}
  - {id: A.18, instruction: 시험방법은 시험법 내용으로 시험결과는 수치 또는 결과값으로 추출되는지 확인, rule_ids: [R26], components: [table_expander, extraction_runtime], coverage: partial, scope: 알려진 필드 분리 및 중복 방지; 모호한 원문은 보류}
  - {id: A.19, instruction: 판정의 기준·결과를 클릭 가능하게 하고 근거 위치와 판정 사유 확인, components: [ui_html, policy_audit], coverage: active, scope: 실제 원문 페이지 링크와 별도 근거 영역; 없는 페이지는 만들지 않음}
  - {id: A.20, instruction: 필수 결재자 또는 확인자의 서명 여부 및 서명 누락 확인, rule_ids: [R22], coverage: partial, scope: 텍스트형 서명란의 존재; 손서명 진위나 이미지 서명 판별은 미지원}
  - {id: A.21, instruction: 허가사항과 SP의 제조소명 및 제조소 정보 일치 확인, rule_ids: [R21], coverage: partial, scope: 현재 등록된 제조소 주소 비교; 다중 제조소의 역할별 매칭은 별도 범위 필요}
  - {id: A.22, instruction: 허가사항과 SP 제품명의 오기·누락·상이 표기 확인, rule_ids: [R23], coverage: active, scope: 원문 제품명 유지; 오타로 검수 진입을 막거나 임의 교정하지 않음}
  - {id: A.23, instruction: 단위가 필요한 시험기준 또는 결과에서 필수 단위 미표기 확인, rule_ids: [R13], components: [numeric_safety], coverage: active, scope: 지원되는 측정대상과 단위; 없는 단위를 추정하지 않음}
  - {id: B.1, instruction: 허가사항 기준이 있으면 우선 사용하고 없으면 자사 시험기준 사용, rule_ids: [R07, R08, R19], components: [policy_criteria, JudgeEngine], coverage: active, scope: 기준 없음과 허가서 매칭 실패를 구별; 실패를 자사기준으로 덮지 않음}
  - {id: B.2, instruction: 특정 기준값을 결과가 만족하는지 확인, rule_ids: [R07], components: [numeric_safety, judgement], coverage: active, scope: 수치·대상·단위가 연결된 값}
  - {id: B.3, instruction: 시험결과 단위와 기준 단위 일치 확인, rule_ids: [R13], coverage: active, scope: 단백질 분모까지 포함한 전체 단위}
  - {id: B.4, instruction: 기준 수치와 결과 수치의 소수점 자릿수 일치 확인, rule_ids: [R14], coverage: active, scope: 같은 측정대상에 명시된 소수점; 일반적인 유효숫자 규칙과 구분}
  - {id: B.5, instruction: 이상·초과·이하·미만 조건 적용, rule_ids: [R07, R08], components: [numeric_safety], coverage: active, scope: 경계값의 포함과 제외 구별}
  - {id: B.6, instruction: 최솟값과 최댓값 범위 내 결과 확인, rule_ids: [R19], components: [criteria_parser, numeric_safety], coverage: active, scope: 한 대상의 양쪽 경계}
  - {id: B.7, instruction: 여러 측정값의 합산 결과와 문서 총합 재계산, rule_ids: [R17, R30], coverage: partial, scope: 최종원액 직접 투입 성분; 하위 완충액 조성 중복 합산 금지}
  - {id: B.8, instruction: 모든 조건을 만족해야 하는 경우 전부 확인, rule_ids: [R28], components: [numeric_safety], coverage: active, scope: 연결된 복합 조건의 AND 검사}
  - {id: B.9, instruction: 여러 조건 중 최소 요구 개수 이상 충족 확인, rule_ids: [R08], coverage: active, scope: 박테리오파지 유전자 최소 개수 등 명시된 조건}
  - {id: B.10, instruction: 복수 대상 또는 목표값이 결과에서 누락되지 않았는지 확인, rule_ids: [R08, R28], coverage: active, scope: 대상별 결과를 연결하고 일부 누락을 전체 충족으로 표시하지 않음}
  - {id: B.11, instruction: 대상별 표 형식 결과를 각 조건에 대응해 판정, rule_ids: [R08, R28], components: [table_expander], coverage: active, scope: 순서가 달라도 라벨로 연결; 불명확한 대응은 보류}
  - {id: B.12, instruction: '1.3 IU/mL처럼 단위와 연산자를 함께 비교', rule_ids: [R07, R28], components: [unit_normalizer], coverage: active, scope: 수치만 비교하지 않음}
  - {id: B.13, instruction: '7.0~8.0 같은 범위 기준 확인', rule_ids: [R19], components: [criteria_parser], coverage: active, scope: B.6과 같은 연산자를 공유하되 요구항목은 보존}
  - {id: B.14, instruction: '검출되지 않아야 함에 대해 불검출·검출되지 않음·X 등을 같은 의미로 인식', components: [result_aliases, judgement], coverage: active, scope: X는 제품·시험·부재 기준에 한정한 MD 사전; 모든 X를 충족으로 보지 않음}
  - {id: B.15, instruction: '부재 조건과 퍼센트 이하 등의 복합 조건을 나누어 판정', rule_ids: [R08], components: [numeric_safety, JudgeEngine], coverage: partial, scope: 추출 가능한 대상별 조건; 정성 의미가 불명확하면 CLOVA 또는 보류}
  - {id: B.16, instruction: '8.0 미만에 7.66처럼 수치는 통과해도 자릿수까지 확인', rule_ids: [R14], coverage: active, scope: B.4와 공동 검사; 기준의 소수점 정책 적용}
  - {id: B.17, instruction: 신청제품정보와 본문 정보의 일치 확인, rule_ids: [R04, R27], coverage: partial, scope: 제조번호·제조일·사용기간 등의 등록 필드}
  - {id: B.18, instruction: 제조년월일과 사용기간으로 최종 유효년월일 계산 후 신청제품정보 비교, rule_ids: [R11], coverage: active, scope: MD end_policy로 정확히 일치 또는 상한 준수 구분}
  - {id: B.19, instruction: 제조수량보다 신청수량이 적을 수 있는 일부 감소 가능성 확인, rule_ids: [R06], coverage: partial, scope: 신청수량 감소를 제조수량 불일치로 오인하지 않음; 초과 검사는 별도 연산 필요}
  - {id: B.20, instruction: 제조년월일과 저장기간 시작일 및 저장기간 계산 확인, rule_ids: [R25], coverage: active, scope: 온전한 날짜와 정수 개월의 달력 연산}
  - {id: B.21, instruction: 제조량·사용량·분량·투입량 구분, rule_ids: [R05, R17, R30], coverage: active, scope: 단계별 필드와 직접 투입 목록 경계}
  - {id: B.22, instruction: SP 기준과 허가사항 기준의 단위 및 수치 일치 확인, rule_ids: [R19, R13], components: [JudgeEngine], coverage: partial, scope: 지정 생존율 기준 및 단위 비교; 모든 시험기준 변경을 전수 검증한 것은 아님}
  - {id: B.23, instruction: 용기 규격별 기준이 다르면 해당 규격 기준 선택, components: [PermitPdfStore, JudgeEngine], coverage: requires_reference, scope: 제공 문서의 규격과 기준이 유일하게 연결되어야 함; 새 다중규격 양식은 정답 검증 필요}
  - {id: B.24, instruction: IU와 EU를 동등 단위로 인식해 단위 불일치로 판정하지 않음, rule_ids: [R13], coverage: active, scope: 스카이코비원 엔도톡신의 동일 분모일 때만 허용; 다른 시험과 분모 환산에는 적용하지 않음}
  - {id: C.1, instruction: 제조요약도의 단계와 연결 관계를 기준으로 상세제조기록 구성 및 순서 비교, rule_ids: [R03, R29, R18], coverage: partial, scope: 지정 필수 단계와 요약도 연결 간 날짜; 임의 본문 순서의 전수 그래프 검사는 별도 검증 필요}
  - {id: C.2, instruction: 제조공정 순서에 따라 제조일자와 공정일자의 선후 확인, rule_ids: [R18], coverage: active, scope: 요약도의 실제 간선; 병렬 공정은 임의 순서를 부여하지 않음}
  - {id: C.3, instruction: 시험일이 해당 시험대상 제조일보다 앞서지 않는지 확인, rule_ids: [R24], coverage: active, scope: 같은 제조단계에 연결된 시험}
  - {id: C.4, instruction: 시험대상별 제조일자와 시험 시작일 비교, rule_ids: [R24], coverage: active, scope: C.3과 연산 공유; 시작·종료 근거 누락은 보류}
  - {id: C.5, instruction: 원액 및 완제의약품의 저장·보관기간 초과 확인, rule_ids: [R16, R25], coverage: active, scope: 등록 원액과 최종원액의 문서상 저장한도}
  - {id: C.6, instruction: 저장기간 시작일과 종료일 관계 확인, rule_ids: [R16, R25], coverage: active, scope: 시작 이후 종료 및 허가 한도 초과 여부}
  - {id: C.7, instruction: 완제의약품 사용기간 기준으로 최종 유효년월일 계산, rule_ids: [R11], coverage: active, scope: B.18과 연산 공유; 달력의 월말 처리 포함}
  - {id: C.8, instruction: 동일 제조번호의 날짜 및 사용기간 문서 내 일치 확인, rule_ids: [R27, R12], coverage: active, scope: 원료 제조번호와 현재 생산물 제조일을 섞지 않음}
  - {id: C.9, instruction: 확인·서명일 또는 최종 승인일이 제조공정이나 최종시험 완료 전이면 불충족, rule_ids: [R15], coverage: partial, scope: 현재 최종 품질책임자 서명과 완제품 시험; 다른 승인 역할은 추가 정의 필요}
  - {id: C.10, instruction: 중간체 원액 시험보다 나노파티클원액 시험일이 더 빠르면 안 됨, rule_ids: [R18, R24], coverage: requires_reference, scope: 제조일 순서와 시험일 간 순서는 다름; 시험의 시작·종료와 비교할 대상쌍을 명시한 규칙 추가 필요}
  - {id: C.11, instruction: 완제품 모든 시험 이후 품질부서책임자가 서명했는지 확인, rule_ids: [R15], coverage: active, scope: 추출된 모든 완제품 시험의 완료일과 최종 서명일}
  - {id: C.12, instruction: 공정의 최소 소요 기간 충족 확인, rule_ids: [R10], coverage: partial, scope: 현재 CHO 마스터 성숙마우스 28일; 다른 공정의 최소기간은 근거와 함께 MD 추가}
  - {id: G01, instruction: 최신 허가사항이 반영된 문서 버전 적용 여부, rule_ids: [R01, R19, R20], coverage: partial, scope: A.9와 동일; 내부 버전 일치와 최신성은 구분}
  - {id: G02, instruction: 페이지 누락 여부, rule_ids: [R02], coverage: active, scope: A.2 및 A.13}
  - {id: G03, instruction: 공정 항목 누락 여부, rule_ids: [R03, R29], coverage: partial, scope: A.3 및 A.14}
  - {id: G04, instruction: 제조번호 일치 여부, rule_ids: [R04, R27], coverage: active, scope: 같은 단계 및 같은 생산물}
  - {id: G05, instruction: 제조량 일치 여부, rule_ids: [R05], coverage: active, scope: 요약도와 본문}
  - {id: G06, instruction: 제조수량 일치 여부, rule_ids: [R06], coverage: active, scope: 신청수량과 구분}
  - {id: G07, instruction: 시험결과값 허가 기준 이탈 여부, rule_ids: [R07, R08, R28], components: [JudgeEngine], coverage: active, scope: 연결이 입증된 허가 기준}
  - {id: G08, instruction: 이상·이하·초과·미만 비교 연산자 판정 오류, rule_ids: [R07, R08], coverage: active, scope: B.5 경계조건}
  - {id: G09, instruction: 정성적 기준에 대한 결과 용어 불일치, components: [result_aliases, JudgeEngine], coverage: active, scope: 실제 관측값과 요구문 반복을 구별}
  - {id: G10, instruction: 필수 시험 소요 기간 미달 여부, rule_ids: [R10], coverage: partial, scope: C.12의 등록 기간}
  - {id: G11, instruction: 유효기간 허가사항 일치 여부, rule_ids: [R11], coverage: partial, scope: 현재 신청정보 사용기간으로 계산; 허가서 사용기간 변경 자체의 전수 검증은 별도}
  - {id: G12, instruction: 제조년월일 일치 여부, rule_ids: [R12, R27], coverage: active, scope: 같은 단계와 배치}
  - {id: G13, instruction: 약전 및 허가사항에 따른 시험 단위 정합성, rule_ids: [R13], coverage: active, scope: B.3 및 B.24}
  - {id: G14, instruction: 시험기준의 유효자릿수 준수 여부, rule_ids: [R14], coverage: active, scope: 제공 정답표의 소수점 자릿수 정책}
  - {id: G15, instruction: 최종 서명일자 선후 관계, rule_ids: [R15], coverage: active, scope: 최종시험일과 비교}
  - {id: G16, instruction: 최종원액 저장기간 준수 여부, rule_ids: [R16, R25], coverage: active, scope: 실제 문서상 저장기한}
  - {id: G17, instruction: 최종원액 제조량의 주성분 및 첨가제 합산량 초과 여부, rule_ids: [R17], coverage: active, scope: 초과 검사와 R30 합계 일치 검사는 목적이 다름}
  - {id: G18, instruction: 완제의약품 제조년월일 공정 순서 일치 여부, rule_ids: [R18], coverage: active, scope: 공정 연결 간 제조일 비교}
  - {id: G19, instruction: 신청수량의 총 제조수량 초과 여부, rule_ids: [R06], coverage: partial, scope: 감소 허용은 유지; 신청수량 상한의 독립 비교는 별도 연산 필요}
  - {id: G20, instruction: 원획분 및 최종원액 저장기간 만료일과 후행 공정 제조년월일 일치 여부, rule_ids: [R16, R25], coverage: partial, scope: 만료일 이전 사용과 날짜의 정확한 동일성은 다름; 현재 초과 여부만 검사하며 같은 날을 강제하지 않음}
  - {id: G21, instruction: 제조소 정보 불일치, rule_ids: [R21], coverage: partial, scope: A.21 제조소 주소}
  - {id: G22, instruction: 서명 누락, rule_ids: [R22], coverage: partial, scope: A.20 텍스트 서명란}
  - {id: G23, instruction: 제품명 불일치, rule_ids: [R23], coverage: active, scope: 원본 공통표의 마지막 번호도 21로 중복되어 고유 관리번호 G23으로 연결}
---

# 요구사항 전체 목록과 실행 규칙의 구분

2026-10-01 제공 캡처의 A 23개, B 24개, C 12개, 공통 23개를 모두 유지한다.
A.10도 삭제하지 않는다. 문구가 겹쳐도 요구사항 번호는 보존하고 실행 연산만 공유한다.
기존 더미 파일의 R01~R30은 변경하지 않는다. 공통표의 번호와 기존 R 번호가 서로
다르므로 공통표는 G01~G23으로 관리한다. 구버전 더미의 B.22 등은 현재 B.21과
의미가 대응할 수 있으며 파일명 자체는 판정 근거가 아니다.

`coverage`는 구현 범위의 표시이지 제출 문서의 충족/불충족 판정이 아니다.
`active`도 scope에 적힌 범위에만 해당한다. `partial`과 `requires_reference`를
완료 또는 충족으로 집계하지 않는다. 실행 결과에는 이 목록과 적용 범위가 함께 저장된다.
현재 문서별 검수 집계는 실제 실행한 시험/MD 점검 수이며 독립된 오류 원인 수가 아니다.
추가 구현 시 해당 범위의 정상·경계·누락·반대 사례 테스트 후 coverage를 갱신한다.
