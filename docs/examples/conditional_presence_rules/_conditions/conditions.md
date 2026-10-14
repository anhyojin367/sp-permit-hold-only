---
applicability:
  schema_version: 1
  id: SYNTHETIC_CONDITIONAL
  company: 합성제조사
  product: 합성시험제품
  review_note: 합성 fixture 전용, 실제 허가의 승인이 아님
  input_layout: single_batch_scoped_records_v1
  source_scopes:
  - sp_stage: 원액
    batch_field: 제조번호
    batch_source:
      record_path:
      - 원액
      - 정보
      - A 정보
      record_type: content
      start: 1.1.1 A 정보
      end: 1.1.2 B 정보
    fields:
    - name: 용기규격
      label: 용기규격
      source:
        record_path:
        - 원액
        - 정보
        - A 정보
        record_type: content
        start: 1.1.1 A 정보
        end: 1.1.2 B 정보
    - name: 시험차수
      label: 시험차수
      source:
        record_path:
        - 원액
        - 정보
        - A 정보
        record_type: content
        start: 1.1.1 A 정보
        end: 1.1.2 B 정보
    - name: 농도
      label: 시험결과
      source:
        record_path:
        - 원액
        - 시험
        - A 시험
        record_type: test
        test_name: 농도확인시험
        start: 시험명 농도확인시험
        end: 시험명 다음시험
  reviewed_document_id: 73aa3468fd44d0c66e7b46bda240b3a3c913627c1634b7eca5caa4a0110a73df
  reviewed_node_ids:
  - 5878802a5ad29872586b7356f17e40db124ffcd8f58ac27280fbf334c47c5770
  - 2b7eaee44aae8a2e065d4d15d55bb541b36461c006aff8563be40d62af7d6b9d
  - f88be55597321149bd565b060eee7e8ba28375d1154e63679346e76fde447d78
  - b06759e1abcd76efcde7c6069cfe0f6f235f311edf480cd48a00ea96bcd3ff9a
  - 48ff1f2319a4a0f6d5bcdd9a0624caece1b1275fff1585956aeca85f8a18245b
  reviewed_condition_ids:
  - 102fa0ea1fb971822e9a55d91d71db838c290095e6a3d7f29ba37869a2349b97
  - 919656d2af890e3f02fcef232ffe19b23fe6f3d26827a7bdaf90fb388acee240
  - 11cd17b4613dd5b21d264ae3cef769d0baa35dbad6c453ddfc2ff9cf4d1086c7
  stages:
  - source_node_id: f88be55597321149bd565b060eee7e8ba28375d1154e63679346e76fde447d78
    sp_stage: 원액
  fields:
  - name: 용기규격
    unit: mL
  - name: 시험차수
    unit: ''
  - name: 농도
    unit: mg/mL
  requirements:
  - source_node_id: b06759e1abcd76efcde7c6069cfe0f6f235f311edf480cd48a00ea96bcd3ff9a
    stage_node_id: f88be55597321149bd565b060eee7e8ba28375d1154e63679346e76fde447d78
    sp_stage: 원액
    combine: all
  - source_node_id: 48ff1f2319a4a0f6d5bcdd9a0624caece1b1275fff1585956aeca85f8a18245b
    stage_node_id: f88be55597321149bd565b060eee7e8ba28375d1154e63679346e76fde447d78
    sp_stage: 원액
    combine: all
  conditions:
  - source_span_id: 102fa0ea1fb971822e9a55d91d71db838c290095e6a3d7f29ba37869a2349b97
    purpose: test_applicability
    field: 용기규격
    operator: eq
    value: '100'
    unit: mL
  - source_span_id: 919656d2af890e3f02fcef232ffe19b23fe6f3d26827a7bdaf90fb388acee240
    purpose: test_applicability
    field: 시험차수
    operator: gte
    value: '2'
    unit: ''
  - source_span_id: 11cd17b4613dd5b21d264ae3cef769d0baa35dbad6c453ddfc2ff9cf4d1086c7
    purpose: procedure
    field: 농도
    operator: gte
    value: '10'
    unit: mg/mL
---
