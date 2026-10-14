---
rules:
  - id: EX-C3
    title: 공정 시작·종료일시와 처리시간 대조
    aliases: [C.3]
    operation: elapsed_time
    instruction: >-
      같은 공정 레코드의 온전한 시작일시와 종료일시 차이를 기재 처리시간과 비교한다.
      종료가 시작보다 빠르거나 차이가 허용 초를 초과하면 불충족이다.
      누락·중복·시간대·알 수 없는 형식은 보류하며 임의로 자정이나 다음날을 가정하지 않는다.
    selector:
      type: content
      context: [배양 공정]
    params:
      start_field: 시작일시
      end_field: 종료일시
      duration_field: 처리시간
      tolerance_seconds: 0
  - id: EX-C10
    title: 첨부용제 제조일·사용기간·유효일 대조
    aliases: [C.10]
    operation: expiry
    instruction: >-
      첨부용제의 제조일에 사용기간을 달력 월로 더한 날짜와 유효일이 같아야 한다.
      말일·윤년을 반영하고 첨부용제의 필드를 완제의약품 필드와 섞지 않는다.
      온전한 날짜·정수 개월 근거가 없으면 보류한다.
    selector:
      type: content
      context: [첨부용제]
    params:
      start_field: 제조년월일
      end_field: 유효일
      duration_field: 사용기간
      end_policy: exact
---

# 시간 규칙 설정 예제 — 운영 기본값 아님

`scripts/prove_time_policies.py`가 만든 별도 정상·오류·누락 PDF로 검증하는 예제다.
제공 D00~D12에는 이 예제의 시작/종료 시각이나 첨부용제의 세 필드가 없어
기본 27개 규칙에 무조건 적용하지 않는다. 대상이 없는 경우 합격도 하지 않는다.

실제 제품에 적용하려면 해당 제품의 사용기간 계산 규약·허용오차·추출 필드와
selector를 확인하고 운영 MD의 rules에 복사한다. `products`로 제품 범위를 제한한다.
용제와 완제의약품의 제조일·유효일을 같은 것으로 가정하지 않는다.
이 예제는 새 허가서의 규정을 확인한 결과가 아니다.
