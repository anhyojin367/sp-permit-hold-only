---
applicability:
  schema_version: 1
  id: EXAMPLE_APPLICABILITY
  company: 합성제조사
  product: 합성시험제품
  review_note: 고정 합성 허가의 전체 안내·원액 단계·두 시험과 세 조건의 해석을 시험 목적으로 검토했다. 실제 제품 승인용이 아니다.
  input_layout: explicit_page_block_v1
  reviewed_document_id: bb75de604a47cca29298efffc1def2abdde3ca27501f29de9cbb8a14a735f078
  reviewed_node_ids:
    - b73598801208eb1fc5badd2c5ed18cc37cf7d7f46364be7c99a27e97ed644832
    - 94bb0f505eb8177083fd786a8223c491d66fc2ec1116b4dc231540b06e9276dd
    - c09f545c9c9f7e7bc01c8293405ed2512fa51febd9dd52ae9d72a7be487e167a
    - 1e2e22a9bc79a08f86b0387c40bc9f8b78a40b36f1cf61255a2cf0f5960a8602
    - c64781fdc81afeea8fee938302a9b67e3ef338cd0804d94e5905dca3f077b4ff
  reviewed_condition_ids:
    - 3a463a3959e8b437258a85257d45389a438f83f4a09c6bbcb68af560b7adef1f
    - 28225933d9fe6fecae3d7ab505ffc8eb4868992fd129f53abf25487f3fc0424e
    - 3181af3cde2b9fa4b528ce02047bb63b28c4dadf60ee245ededed920582816ac
  stages:
    - source_node_id: c09f545c9c9f7e7bc01c8293405ed2512fa51febd9dd52ae9d72a7be487e167a
      sp_stage: 원액
  fields:
    - {name: 용기규격, unit: mL}
    - {name: 시험차수, unit: ""}
    - {name: 농도, unit: mg/mL}
  requirements:
    # 무균시험: 원액의 수행 조건 + 자기 시험차수 조건을 모두 적용
    - source_node_id: 1e2e22a9bc79a08f86b0387c40bc9f8b78a40b36f1cf61255a2cf0f5960a8602
      stage_node_id: c09f545c9c9f7e7bc01c8293405ed2512fa51febd9dd52ae9d72a7be487e167a
      sp_stage: 원액
      combine: all
    # 함량시험: 농도 조건은 희석 절차의 조건이지 시험 면제가 아님
    - source_node_id: c64781fdc81afeea8fee938302a9b67e3ef338cd0804d94e5905dca3f077b4ff
      stage_node_id: c09f545c9c9f7e7bc01c8293405ed2512fa51febd9dd52ae9d72a7be487e167a
      sp_stage: 원액
      combine: all
  conditions:
    - source_span_id: 3a463a3959e8b437258a85257d45389a438f83f4a09c6bbcb68af560b7adef1f
      purpose: test_applicability
      field: 용기규격
      operator: eq
      value: "100"
      unit: mL
    - source_span_id: 28225933d9fe6fecae3d7ab505ffc8eb4868992fd129f53abf25487f3fc0424e
      purpose: test_applicability
      field: 시험차수
      operator: gte
      value: "2"
      unit: ""
    - source_span_id: 3181af3cde2b9fa4b528ce02047bb63b28c4dadf60ee245ededed920582816ac
      purpose: procedure
      field: 농도
      operator: gte
      value: "10"
      unit: mg/mL
---

# 조건 적용 MD 예제 (합성 문서 전용)

실행 조건은 위 YAML이다. 아래 설명을 바꾸는 것만으로 새 연산이 생기지는 않는다.
설명을 포함한 파일 전체가 응답 지문에 들어가므로 어느 부분을 바꾸어도 이전 응답은 재사용할 수 없다.

- 100 mL 용기일 때만 원액의 두 시험을 수행한다.
- 무균시험에는 시험차수 2 이상이라는 추가 조건이 있다.
- 농도 10 mg/mL 이상이라는 조건은 함량시험의 희석 절차에만 쓰인다.
- 값 없음은 조건 미해당이 아니다. 해당 단계·배치에서 독립 사실을 찾지 못하면 보류한다.

문서/문단/구간 ID는 `prove_permit_applicability.py`가 만드는 고정 permit.pdf에 대한 검토 지문이다.
문서가 달라졌다고 ID를 자동 갱신해 적용하면 안 된다. 전체 문단과 조건의 의미를 다시 검토해야 한다.
이 설정은 코드에서 명시적으로 지정할 때만 읽으며 운영27개 MD에 자동 포함되지 않는다.
