---
rules:
  - id: EXAMPLE_PERMIT_REQUIRED
    title: 허가서의 필수시험을 배치별로 확인 (합성 예제 전용)
    operation: permit_required_tests
    products: [합성시험제품]
    aliases: [A.9, A.15]
    instruction: 검토된 허가 원액 단계의 모든 말단 시험은 각 제조번호에서 수행해야 한다. 상위 묶음명 또는 다른 배치의 시험으로 대신하지 않는다. 원문 범위와 배치 연결이 불명확하면 보류한다. 이것은 시험 기록 존재 검사이며 결과 적합 판정과 별개다.
    params:
      permit_stage_path: [제조방법 및 시험기준, 원액]
      reviewed_scope_sha256: c3f9333382702cb86ca3f27277388d505e308bb64e0a4c1d1cc02a2e986b0776
      reviewed_document_sha256: ccc6d056c89843bf95aa7896e93720b7aae5d213b0a94247338ed251a725a4e0
      requirement_policy: all_leaf_sections_required
      sp_stage_path: [원액, 시험]
      coverage_start: 1 원액
      coverage_end: 2 다음단계
      batch_inventory: {type: content, context: [배치목록]}
      batch_field: 제조번호
      batch_binding: explicit_test_field
      test_batch_field: 제조번호
---

이 예제는 `scripts/prove_required_test_policies.py`의 합성 제품 전용이다.
운영 27개 규칙에 자동으로 포함되지 않는다. 시험 목록은 Python이나 MD의 고정 시험명 배열이 아니라
연결된 허가서의 **정확한 전체 단계 경로**에서 열거한다.

`reviewed_scope_sha256`는 해당 하위 문단의 전체 내용·계층을 검토했다는 MD 설정이다.
`reviewed_document_sha256`도 함께 고정하여 상위 장·서문·이미지 등 문서 전체 변경을 감지한다.
PDF 파일에서는 원본 바이트 해시를 사용하므로 메타데이터만 달라져도 재검토 보류할 수 있다.
이 합성 permit.pdf는 ReportLab의 invariant 모드로 매 실행 동일 바이트를 생성한다.
승인된 디지털 서명이나 LLM의 검토 결과가 아니며, 조건부 시험을 자동 해석하지 않는다.
이 예제는 무균시험·함량시험이 무조건 필요한 합성 허가만 대상으로 한다.
조건/시험/계층이 달라지면 해시가 달라져 보류한다. 보류를 없애려고 새 해시를 자동 복사하면 안 된다.

배치 목록은 시험 결과에서 역산하지 않고 별도 제조번호 기록을 읽는다. 여러 배치에서는 각 시험의
명시적인 제조번호 연결이 필요하다. `single_batch_stage`는 별도 목록에서 정확히 한 배치만 확인되는
검토된 양식에만 사용할 수 있고, 그때 `test_batch_field`는 적지 않는다.

명칭 차이는 `name_mappings`에 허가 단계 이하 시험 경로, SP 시험 경로들, 검토 사유를 적어 연결한다.
검색 별칭은 이 연산에서 동등성으로 사용하지 않는다. 상위/하위 제목과 문장 조각을 합쳐 시험명을 만들지 않는다.
부모가 있는 하위시험 하나로 같은 이름의 독립시험도 채우지 않는다. `L1/L2` 배치 표기는 현재 보류하며,
독립 목록의 각 행에 제조번호를 구분해야 한다. 실제 slash를 포함하는 단일 제조번호 양식은 아직 지원하지 않는다.

누락 FAIL에는 유일한 시작·끝 경계, 텍스트가 있는 연속 페이지, 원문에 존재하는 레코드/배치 연결이 필요하다.
원문에 시험명이 있으나 연결되지 않은 경우, OCR 의심, 범위 누락, 중복 기록은 HOLD다.
형식이 다른 PDF/스캔본/표형 배치 연결은 별도로 검증해야 한다. 원문 텍스트 일치가 추출 무오류를 보증하지는 않는다.
