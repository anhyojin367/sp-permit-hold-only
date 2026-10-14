---
rules:
  - id: R27
    title: 동일 제조번호의 제조일·사용기간 일치
    aliases: [C.8]
    operation: lot_consistency
    report_group: manufacturing_info
    instruction: 같은 제조번호에 직접 연결된 제조일과 사용기간은 문서 전체에서 일치해야 한다. 사용한 원료의 제조번호에 현재 생산물의 제조일을 연결하지 않는다.
    params: {fields: [제조년월일, '사용(유효)기간']}
  - id: R24
    title: 시험대상의 제조일과 시험일 순서
    aliases: [C.3, C.4]
    operation: test_after_manufacture
    report_group: manufacturing_info
    instruction: 시험일은 해당 시험대상의 제조일보다 빠를 수 없다. 제품 전체 제조일이 아닌 같은 제조단계의 제조일과 비교한다.
    params:
      stages:
        - {source: [CHO 마스터 세포주], target: [CHO 마스터 세포주]}
        - {source: [CHO 제조용 세포주], target: [CHO 제조용 세포주]}
        - {source: [E.coli마스터 세포주], target: [E.coli마스터 세포주]}
        - {source: [E.coli제조용 세포주], target: [E.coli제조용 세포주]}
        - {source: [Component A 중간체 원액], target: [Component A 중간체 원액]}
        - {source: [Component B 중간체 원액], target: [Component B 중간체 원액]}
        - {source: [나노파티클 원액], target: [나노파티클 원액]}
        - {source: [최종원액], target: [최종원액]}
        - {source: [항원바이알], target: [항원바이알]}
  - id: R25
    title: 원액 저장기간의 시작·종료 및 한도
    aliases: [B.20, C.5, C.6]
    operation: storage_period
    instruction: 원액 저장기간은 제조일에 시작하고 종료일이 시작일보다 빠를 수 없다. 허가 저장기간 한도가 온전하게 있어야 하며 이를 넘을 수 없다. 범위·소수·미정 등 지원되지 않거나 불완전한 기간은 추측하지 않고 보류한다.
    selector: {type: content, context: [원액]}
    params: {period_field: 저장기간, start_field: 제조년월일, duration_field: 허가된 저장기간}
  - id: R26
    title: 동일 시험의 중복 추출 확인
    aliases: [A.18]
    operation: duplicate_tests
    instruction: 같은 제조단계·시험명·기간·기준·결과가 완전히 같은 시험이 두 번 추출되면 중복으로 표시한다. 다른 배치·단계는 별개로 취급한다.
    selector: {type: test}
  - id: R01
    title: 문서 버전 및 개정일 일치
    aliases: [A.1, A.11]
    operation: version_consistency
    instruction: 문서에 반복 기재된 버전과 그 아래 개정일이 같아야 한다.
  - id: R02
    title: 페이지 번호와 전체 페이지 수
    aliases: [A.2, A.13]
    operation: page_continuity
    instruction: 번호 없는 표지는 제외하고 표시된 페이지 번호가 1부터 전체 페이지 수까지 연속되어야 한다.
  - id: R04
    title: 제조요약도와 본문 제조번호 비교
    aliases: [A.4, A.16, B.17]
    depends_on: {R29: 최종원액}
    operation: diagram_consistency
    report_group: manufacturing_info
    instruction: 같은 제조단계의 제조번호는 제조요약도와 본문에서 일치해야 한다.
    params:
      field: 제조번호
      stage_aliases: {완제의약품: [완제의약품, 신청제품정보]}
  - id: R05
    title: 제조요약도와 본문 제조량 비교
    aliases: [A.4, A.16, B.21]
    depends_on: {R29: 최종원액}
    operation: diagram_consistency
    report_group: manufacturing_info
    instruction: 같은 제조단계의 제조량은 제조요약도와 본문에서 일치해야 한다.
    params: {field: 제조량}
  - id: R06
    title: 제조요약도와 본문 제조수량 비교
    aliases: [A.4, A.16, B.19]
    operation: diagram_consistency
    report_group: manufacturing_info
    instruction: 완제의약품의 제조수량을 비교한다. 신청수량 감소를 제조수량 불일치로 혼동하지 않는다.
    params:
      field: 제조수량
      stage_aliases: {완제의약품: [완제의약품]}
  - id: R12
    title: 제조요약도와 본문 제조년월일 비교
    aliases: [A.16, A.17, C.8]
    depends_on: {R29: 최종원액}
    operation: diagram_consistency
    report_group: manufacturing_info
    instruction: 같은 제조단계의 제조년월일은 제조요약도와 본문에서 일치해야 한다.
    params: {field: 제조년월일}
  - id: R14
    title: 시험기준과 결과의 소수점 자릿수
    aliases: [B.4, B.16]
    operation: precision
    instruction: 같은 측정대상의 기준과 결과에 명시된 소수점 자릿수를 비교한다. 과학표기법의 지수와 서로 다른 측정대상은 비교하지 않는다.
    selector: {type: test}
  - id: R11
    title: 제조일 및 사용기간에 따른 유효일
    aliases: [B.18, C.7]
    operation: expiry
    instruction: 신청제품정보의 최종 유효년월일은 제조일보다 빠를 수 없고, 제조년월일에 사용기간을 더한 날짜를 초과할 수 없다. 월은 달력 기준이며 같은 일이 없으면 해당 월 말일을 사용한다. 사용기간은 온전한 정수 개월 수가 필요하고 불명확하면 보류한다.
    selector: {type: content, context: [신청제품정보]}
    params: {start_field: 제조년월일, end_field: 최종 유효년월일, duration_field: '사용(유효)기간', end_policy: not_after}
  - id: R15
    title: 품질책임자 서명일과 시험완료일
    aliases: [C.9, C.11]
    operation: signature_order
    instruction: 최종 품질책임자 서명일은 완제의약품의 모든 시험 완료일보다 빠를 수 없다.
    selector: {type: content, context: [확인/서명]}
    params: {tests: {type: test, context: [완제의약품]}}
  - id: R16
    title: 최종원액 저장기한과 완제품 제조·시험일
    aliases: [C.5, C.6]
    depends_on: {R29: 최종원액}
    operation: storage_window
    instruction: 최종원액의 허가된 저장기한 안에 완제품 제조가 완료되어야 한다. 저장기한을 넘겨 기재된 제조·시험일도 확인한다.
    selector: {type: content, context: [최종원액]}
    params: {target: {context: [완제의약품]}}
  - id: R17
    title: 최종원액 제조량과 직접 투입량
    aliases: [B.7, B.21]
    depends_on: {R29: 최종원액}
    operation: mass_balance
    instruction: 최종원액 제조량은 조제에 직접 투입된 같은 단위의 성분 합계를 초과할 수 없다. 완충액 하위 조성은 중복 합산하지 않는다.
    selector: {type: content, context: [최종원액]}
    params: {output_field: 제조량, start_marker: 최종원액 조제에 사용된 주성분 및 첨가제, end_marker: 완충액 조제에 사용된 첨가제}
  - id: R18
    title: 제조공정 선후관계
    aliases: [C.1, C.2]
    operation: process_order
    report_group: manufacturing_dates
    instruction: 제조요약도의 연결 관계상 선행 공정 제조일이 후행 공정 제조일보다 늦으면 안 된다. 독립된 병렬 분기는 서로 비교하지 않는다.
    params: {date_field: 제조년월일, allow_same_day: true}
  - id: R22
    title: 필수 품질책임자 서명 존재
    aliases: [A.20]
    operation: signature
    instruction: 확인/서명 영역에서 품질책임자의 서명란이 비어 있으면 서명 누락이다. 성명 기재만으로 서명을 대체하지 않는다.
    selector: {type: content, context: [확인/서명]}
---

# 문서 공통 규칙

각 규칙은 독립적으로 실행되며 결과와 근거 페이지를 기록한다.
정상/오류 파일명은 판정 입력으로 사용하지 않는다.
