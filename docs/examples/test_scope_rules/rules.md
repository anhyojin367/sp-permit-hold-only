---
rules:
  - id: EXAMPLE_TEST_SCOPE
    title: 다른 단계와 분리한 필수시험 누락 검사 (합성 예제)
    operation: permit_required_tests
    products: [합성시험제품]
    aliases: [A.9]
    instruction: 배치 정보와 대상시험 원문 범위를 각각 확인한다. 다른 단계의 시험은 빌리지 않으며 대상 범위의 미추출 언급은 보류한다.
    params:
      permit_stage_path: [제조방법 및 시험기준, 원액]
      reviewed_scope_sha256: c3f9333382702cb86ca3f27277388d505e308bb64e0a4c1d1cc02a2e986b0776
      reviewed_document_sha256: ccc6d056c89843bf95aa7896e93720b7aae5d213b0a94247338ed251a725a4e0
      requirement_policy: all_leaf_sections_required
      sp_stage_path: [원액, 대상시험]
      coverage_start: 1 원액
      coverage_end: 2 다음단계
      batch_inventory: {type: content, context: [배치목록]}
      batch_inventory_path: [원액, 배치목록]
      batch_coverage_start: 1.1 배치목록
      batch_coverage_end: 1.2 다른단계
      test_coverage_start: 1.3 대상시험
      test_coverage_end: 1.4 다음시험
      batch_field: 제조번호
      batch_field_layout: label_value_lines
      batch_binding: single_batch_stage
---

합성 PDF 전용이며 운영 27개 규칙에는 포함되지 않는다.
두 독립 범위는 전체 범위 안에 있고 서로 겹치지 않아야 한다.
시작/끝 문구는 문서 전체에서 유일해야 하며 제목의 위치/전체 절 범위를 사람이 검토한다.
시험명의 일부나 결과줄을 경계로 삼아 실제 시험을 제외하면 안 된다.
원문에 기록이 있지만 추출/연결되지 않은 경우는 누락 FAIL이 아니라 HOLD다.
