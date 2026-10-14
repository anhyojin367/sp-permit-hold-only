# 저장 결과 재생용 고정 fixture

2026-09-28 전달본에서 선별한 JSON 40개다. `corpus/`는 추출 결과,
`integrated_offline/`와 `live/`는 당시 D00~D12의 저장 판정이다.
운영 입력이나 정답을 강제로 만드는 규칙이 아니라, 단계별 카드/CSV 표시와 기존 판정의 일관성을 검사하는 입력이다.
**현재 v104의 새 판정 결과나 새로운 API 호출 증거가 아니다.**

```powershell
python scripts/prepare_test_fixtures.py
python -m pytest -q -rs
```

준비 스크립트는 과거 절대 경로를 현재 프로젝트 위치로 바꾸어 `.local_validation/`에 설치한다.
이미 있는 결과는 덮어쓰지 않는다. 과거 미리보기 이미지 파일은 포함하지 않는다.
새 판정은 별도 `--output-dir`로 실행해 저장 입력을 덮어쓰지 않는다.
최신 실제 PDF 회귀는 `더미데이터/` D00~D16과 `scripts/validate_corpus.py`를 사용한다.
