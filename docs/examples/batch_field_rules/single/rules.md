---
rules:
  - id: EXAMPLE_BATCH_LINES
    title: 정보 절의 제조번호와 필수시험 연결 (합성 예제)
    operation: permit_required_tests
    products: [합성시험제품]
    aliases: [A.9]
    instruction: 검토한 정보 절의 제조번호 전체와 추출 목록을 대조한다. 정확히 한 배치가 확인된 경우에만 그 단계의 시험을 연결한다. 다른 단계와 머리말의 제조번호는 사용하지 않는다.
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
      batch_binding: single_batch_stage
---

합성 PDF 전용이며 운영 규칙에는 포함되지 않는다. 전체 허가 검토와 적용 범위의 책임을 해시가 대신하지 않는다.
`label_value_lines`는 같은 줄의 항목명/값 또는 같은 페이지의 인접한 두 줄만 지원한다.
일반 표 구조 추론, 두 열을 한꺼번에 읽는 추출 결과, OCR 복원은 지원한다고 가정하지 않는다.
