# PDF별 규칙 회귀 로그

검수를 실행하면 이 폴더에 PDF별 Excel 파일이 자동 생성됩니다.

- 파일명: `<PDF명>__<PDF SHA 앞 12자리>__rule_regression.xlsx`
- `회귀요약` 5행: 정답지
- `회귀요약` 6행 이후: 코드/RAG/MD/모델 조합별 실행 이력
- 값 형식: `충족/불충족/보류`
- `판정상세`: 규칙별 수치와 설명
- `규칙카탈로그`: `overall.md`의 각 규칙과 실행 코드 연결 상태
- `RAG_MD사용내역`: 실제 사용한 RAG 자료, MD 파일, LLM 컨텍스트

첫 실행값은 검토 전 초안입니다. 사람이 PDF와 판정 결과를 확인한 뒤 다음 명령으로
최신 실행값을 정답지로 확정합니다.

```powershell
python scripts/rule_regression_log.py set-gold "OUTPUT\<파일명>.xlsx"
```

이 폴더의 `.xlsx`와 `rule_regression_index.json`은 실행 산출물이므로 Git에 올리지
않습니다. 정답지를 팀 기준 데이터로 공유할 때는 별도 승인된 저장소에 보관하세요.
