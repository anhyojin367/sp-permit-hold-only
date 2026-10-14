---
rules:
  - id: DEMO-SEMANTIC
    title: MD 자연어 편집 검증용 (실제 기준 아님)
    operation: semantic_review
    selector: {type: test, context: [CHO 마스터 세포주], test: 세포성장 및 증식확인시험}
    instruction: >-
      이것은 엔진 검증용 임시 기준이다. 실제 의약품 적합성 결정이 아니다.
      제공된 시험결과의 세포농도만 비교한다. 4.00 x 10^6 cells/mL 이상이면 PASS,
      미달이면 FAIL, 값을 확인할 수 없으면 HOLD이다. 다른 조건은 이 규칙의 대상이 아니다.
---
