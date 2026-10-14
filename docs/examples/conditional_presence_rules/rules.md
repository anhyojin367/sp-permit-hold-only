---
rules:
- id: SYNTH-REQ
  title: 조건부 필수시험 기록 확인 (시험 결과 적합 판정 아님)
  instruction: 검토된 수치 수행 조건과 같은 배치의 필수시험 기록을 확인한다.
  operation: permit_conditional_tests
  products:
  - 합성시험제품
  aliases:
  - A.9
  params:
    permit_stage_path:
    - 제조방법
    - 원액
    reviewed_scope_sha256: 0a58971ee6ab4bf33dd89440393c389aa7ffd0ed8ab723a43179ca2d83325e18
    reviewed_document_sha256: 808904401e9fc824a2223353abf9ede8077db7ae5cd10e3f6e7277bf93931fe4
    requirement_policy: reviewed_numeric_applicability
    applicability_md: _conditions/conditions.md
    sp_stage_path:
    - 원액
    - 시험
    - A 시험
    coverage_start: 1. 원액
    coverage_end: 2. 다음 단계
    batch_inventory:
      type: content
      context:
      - A 정보
    batch_field: 제조번호
    batch_field_layout: label_value_lines
    batch_inventory_path:
    - 원액
    - 정보
    - A 정보
    batch_coverage_start: 1.1.1 A 정보
    batch_coverage_end: 1.1.2 B 정보
    test_coverage_start: 1.2.1 A 시험
    test_coverage_end: 2. 다음 단계
    batch_binding: single_batch_stage
---
