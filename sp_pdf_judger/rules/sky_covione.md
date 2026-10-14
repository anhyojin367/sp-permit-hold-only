---
result_aliases:
  - products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    test: 무균시험
    criteria: 균이 확인되지 않아야 함
    values: [x]
    normalized_result: 균이 확인되지 않음
    reason: 제공 B.14 요구사항의 X는 불검출 표기이다. 이 제품의 무균시험과 해당 부재 기준에만 적용하고 원문 결과는 보존한다.
permit_search_aliases:
  - products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    names: [잔류 숙주세포 유래 DNA시험, 잔류 숙주세포 유래 DNA함량시험]
    reason: 제공 SP와 허가서의 잔류 DNA 측정 명칭을 검색 후보로 연결한다. 실제 제조단계·방법·기준은 원문으로 다시 판단한다.
  - products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    names: [잔류 숙주세포 유래 단백질시험, 잔류 숙주세포 유래 단백질함량시험]
    reason: 제공 문서의 잔류 숙주세포 단백질 측정 명칭 차이이며 단백질함량시험 일반 항목과 합치지 않는다.
  - products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    names: [주사제 실용량시험, 주사제의 실용량시험]
    reason: 조사 유무에 따른 제공 문서의 검색 명칭 차이이다.
  - products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    names: [HAP시험, 'HAP(Hamster Antibody Production)시험']
    reason: 허가서에 병기된 HAP 약어의 확장 명칭이다.
  - products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    names: [MAP시험, 'MAP(Mouse Antibody Production)시험']
    reason: 허가서에 병기된 MAP 약어의 확장 명칭이다.
  - products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    names: [MMV부정시험, 'MMV(Mouse Minute Virus)부정시험']
    reason: 허가서에 병기된 MMV 약어의 확장 명칭이다.
  - products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    names: [플라스미드 대장균수확인시험, 플라스미드 보유 대장균수확인시험]
    reason: 보유 표기의 유무에 따른 검색 후보 연결이며 결과 단위/방법의 동일성을 자동 인정하지 않는다.
  - products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    names: [소유래플라스마부정시험, 소유래바이러스부정시험]
    reason: SP 제목은 플라스마이지만 기준문은 소유래 바이러스 오염 부정이다. 동일 시험으로 단정하지 않고 원문 대조 후보만 추가한다.
  - products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    names: [돼지유래플라스마부정시험, 돼지유래바이러스부정시험]
    reason: SP 제목은 플라스마이지만 기준문은 돼지유래 바이러스 오염 부정이다. 동일 시험으로 단정하지 않고 원문 대조 후보만 추가한다.
rules:
  - id: R29
    title: 최종원액 제조단계와 필수 시험 존재
    aliases: [A.3, A.14, C.1]
    operation: required_tests
    products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    instruction: 최종원액 제조단계에는 성상 및 무균시험 기록이 모두 있어야 한다. 제조요약도에 이름만 있거나 다른 제조단계에 동명 시험이 있어도 해당 단계의 기록을 대신하지 못한다.
    selector: {type: test, context: [최종원액]}
    params: {tests: [성상, 무균시험]}
  - id: R30
    title: 최종원액 제조량과 원액·완충액 합산 일치
    aliases: [B.7, B.21]
    depends_on: {R29: 최종원액}
    operation: mass_balance
    products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    instruction: 제공 판정기준의 최종원액 조제 제조량은 직접 투입한 나노파티클원액과 완충액 분량의 합계와 일치해야 한다. 나노파티클원액 사용량만을 제조량으로 기재하면 불충족이다. 완충액 하위 조성을 중복 합산하지 않으며 같은 단위의 전체 분량이 필요하다. 이 합계 일치 조건을 손실이 허용되는 다른 제조공정에 일반화하지 않는다.
    selector: {type: content, context: [최종원액]}
    params: {output_field: 제조량, start_marker: 최종원액 조제에 사용된 주성분 및 첨가제, end_marker: 완충액 조제에 사용된 첨가제, comparison: equal}
  - id: R28
    title: 불용성미립자시험의 측정대상별 전체 조건
    aliases: [B.8, B.10, B.11, B.12]
    operation: labelled_numeric_compare
    criterion_source: permit_first
    products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    instruction: 기준에 열거된 입자 크기별 결과를 같은 이름끼리 연결해 각각 비교한다. 결과의 표시 순서가 달라도 동일 대상을 연결하고, 한 대상만 있으면 전체 합격으로 처리하지 않는다. 허가서 수치 한도가 있으면 우선하며, 허가서가 수치 없이 시험법만 참조하면 SP 기재 수치를 보완 검사한다. 서로 다른 입자 크기의 결과로 대신하지 않는다.
    selector: {type: test, test: 주사제의 불용성미립자시험}
    params: {expected_labels: ['용기당 10 μm 이상', '용기당 25 μm 이상'], sp_supplement_if_no_numeric_limit: true}
  - id: R07
    title: 잔류 DNA시험의 엄격한 미만 기준
    aliases: [B.1, B.2, B.5, B.12]
    operation: numeric_compare
    criterion_source: permit_first
    instruction: 원문 연결이 확인된 허가서 결과 기준을 우선하여 숫자·단위·연산자를 비교한다. 허가서 결과 기준이 없으면 SP 기준을 사용한다. 미만은 경계값과 같을 때 부적합이다. 허가 기준 미확정은 보류이며 SP 기준으로 임의 대체하지 않는다.
    selector: {type: test, test: 잔류 숙주세포 유래 DNA시험}
  - id: R08
    title: 복수 유전자 중 최소 충족 개수
    aliases: [B.9, B.10, B.11, B.15]
    operation: minimum_count
    criterion_source: permit_first
    instruction: 원문 연결이 확인된 허가서 기준의 최소 개수만큼 결과가 정량한계 미만임이 확인되어야 한다. 허가서 결과 기준이 없으면 SP 기준을 사용하되 매칭 불명확은 보류한다. 이하와 미만을 구별한다.
    selector: {type: test, test: 박테리오파지부정시험}
  - id: R13
    title: 측정대상별 단위 일치
    aliases: [A.6, A.15, A.23, B.3, B.24]
    operation: unit_consistency
    products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    instruction: 같은 측정대상의 시험기준과 결과 단위 전체를 비교한다. 이 제품의 엔도톡신시험에서만 동일 분모의 IU/mL와 EU/mL, IU/mg of protein과 EU/mg of protein을 각각 연결한다. mL와 mg of protein은 환산 근거 없이 바꾸지 않는다. CFU와 PFU 및 단백질 분모가 누락된 단위는 다른 단위다.
    selector: {type: test}
    params:
      unit_aliases:
        - {test: 엔도톡신시험, from: IU/mL, to: EU/mL}
        - {test: 엔도톡신시험, from: IU/mg of protein, to: EU/mg of protein}
      expected_units: {생균수시험: CFU/mL, 단백질함량시험: μg/mL}
  - id: R03
    title: Component A 본배양액 필수 시험 누락
    aliases: [A.3, A.14]
    operation: required_tests
    products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    instruction: Component A 본배양액에는 외래성인자부정시험(in vitro)이 있어야 한다. 허가서 해당 항목과 비교한다.
    selector: {context: [Component A 본배양액]}
    params: {tests: ['외래성인자부정시험(in vitro)']}
  - id: R10
    title: 성숙마우스접종시험 최소 기간
    aliases: [C.12]
    operation: duration
    products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    instruction: CHO 마스터 세포주의 성숙마우스접종시험 관찰 기간은 최소 28일이다. 종료일에서 시작일을 뺀 일수를 사용한다.
    selector: {type: test, context: [CHO 마스터 세포주], test: 성숙마우스접종시험}
    params: {minimum_days: 28, inclusive: false}
  - id: R19
    title: 허가서와 세포 생존율 기준 비교
    aliases: [A.9, B.1, B.6, B.13, B.22]
    operation: permit_test
    products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    instruction: CHO 마스터 세포주의 세포생존율 기준 범위가 연결된 허가서의 범위와 같아야 한다. 실제 시험결과의 적합 여부와 별도로 확인한다.
    selector: {type: test, context: [CHO 마스터 세포주], test: 세포성장 및 증식확인시험}
    params:
      permit_stage: [CHO 마스터 세포주]
      permit_pattern: '생존율[^0-9]*(\d+\.\d+)\s*~\s*(\d+\.\d+)'
      sp_pattern: '생존율[^0-9]*(\d+\.\d+)\s*~\s*(\d+\.\d+)'
      sp_attribute: criteria
  - id: R20
    title: 허가서와 결핵균 시험방법 비교
    aliases: [A.7, A.8, A.9]
    operation: permit_test
    products: [스카이코비원멀티주, 스카이코비원, SKYCovione]
    instruction: Component A 본배양액 결핵균부정시험의 적용 약전이 연결된 허가서와 같아야 한다.
    selector: {type: test, context: [Component A 본배양액], test: 결핵균부정시험}
    params:
      permit_stage: [Component A 본배양액]
      permit_pattern: '(유럽약전|대한민국약전)'
      sp_pattern: '(유럽약전|대한민국약전)'
      sp_attribute: method
  - id: R21
    title: 허가서와 신청제품 제조소 주소 비교
    aliases: [A.21]
    operation: permit_field
    instruction: 신청제품 제조사 주소는 연결된 허가서 제조소 주소와 같아야 한다. 줄바꿈과 공백은 표기 차이로 정규화한다.
    selector: {type: content, context: [신청제품정보]}
    params:
      sp_field: 명칭 제조사 주소
      sp_pattern: '(?:㈜|\(주\))\s*(.+)$'
      permit_pattern: '제조소\s*자사제조,[^\n]*?대한민국,\s*(.*?)\s*허가조건'
  - id: R23
    title: 허가서와 신청제품 제품명 비교
    aliases: [A.22]
    operation: permit_field
    instruction: 신청제품정보 제품명을 연결된 허가서 제품명과 비교한다. 오타를 임의로 동일 제품명으로 간주하지 않는다.
    selector: {type: content, context: [신청제품정보]}
    params:
      sp_field: 제품명
      permit_pattern: '제품명\s*(?!별첨)([^\n(]+)(?:\(|\n)'
---

# 제품별 근거

R03의 과거 정답표에는 in vivo로 적혀 있었으나 2026-09-29 제공된 판정기준은 in vitro로 정정되어 실제 PDF와 일치한다.
R29/R30은 추가 제공된 D13/D14 판정기준에 대응한다. 파일명이 아니라 제조단계·시험·수량 원문으로 판정한다.
정성 결과의 불검출/음성 표기는 오류 자체가 아니다. 실제 관측과 요구 문장의 반복을 구별해야 한다.
R19/R20/R21/R23의 기준값은 이 파일에 숫자나 주소로 복사하지 않고 매번 연결된 허가서에서 찾는다.
