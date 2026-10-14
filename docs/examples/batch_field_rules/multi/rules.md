---
rules:
  - id: EXAMPLE_BATCH_LINES_MULTI
    title: 정보 절의 복수 제조번호와 시험별 배치 연결 (합성 예제)
    operation: permit_required_tests
    products: [합성시험제품]
    aliases: [A.9]
    instruction: 정보 절의 모든 제조번호를 확인하고 시험마다 명시된 제조번호로만 연결한다. 한 배치의 시험이 다른 배치의 누락을 채우지 않는다.
    params:
      permit_stage_path: [제조방법 및 시험기준, 원액]
      reviewed_scope_sha256: c3f9333382702cb86ca3f27277388d505e308bb64e0a4c1d1cc02a2e986b0776
      reviewed_document_sha256: ccc6d056c89843bf95aa7896e93720b7aae5d213b0a94247338ed251a725a4e0
      requirement_policy: all_leaf_sections_required
      sp_stage_path: [원액, 시험]
      coverage_start: 1 원액
      coverage_end: 2 다음단계
      batch_inventory: {type: content, context: [배치목록]}
      batch_inventory_path: [원액, 배치목록]
      batch_coverage_start: 1.1 배치목록
      batch_coverage_end: 1.3 시험
      batch_field: 제조번호
      batch_field_layout: label_value_lines
      batch_binding: explicit_test_field
      test_batch_field: 제조번호
---

합성 PDF 전용이다. 시험별 제조번호가 없거나 추출 원문과 다르면 보류한다.
두 배치를 한 개 레코드의 `L1/L2`로 합치지 않는다. 독립된 정보 레코드로 구분되어야 한다.
