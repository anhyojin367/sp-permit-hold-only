# SP-first / HOLD-only 독립 버전

이 폴더를 VS Code에서 별도로 여세요. 기존 소스 및 다른 실험 버전과 독립된 복사본입니다. 이 문서와 `launch_hold_only.py`가 이 버전의 실행 기준이며, 기존 README는 이전 공통 기능 설명으로 남아 있습니다.

SP 자체 규칙과 SP-only 의미 판정을 먼저 수행한 뒤, 보류(HOLD) 시험 항목만 허가서 근거로 재평가합니다. 이미 PASS/FAIL인 항목은 허가서로 다시 판정하지 않습니다. 허가서 기반 문서 전체 정합성 검사는 수행하지 않으며, 제외된 검사를 합격으로 간주하지 않습니다. 근거 부족이나 안전 제약이 있으면 보류가 유지될 수 있습니다.

## 최초 설정

실제 `.env`, `.streamlit/secrets.toml`, API 키, Gmail 계정 및 `.venv`는 복사하지 않았습니다. 필요한 API 설정을 반드시 **이 새 폴더에** 별도로 입력하세요. 상위 폴더의 `.env` 및 전역 Gmail secrets를 자동으로 가져오지 않습니다. 로컬 `.env`의 CLOVA 설정은 실행 중 다시 읽으며 같은 이름의 프로세스 환경변수보다 우선합니다. API 키를 문서나 Git에 넣지 마세요.

새 폴더의 PowerShell에서:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`.env.example`을 참고하여 이 폴더에 `.env`를 만들고 `CLOVA_API_KEY`, 필요한 경우 `CLOVA_MODEL`/`CLOVA_BASE_URL`을 설정하세요. Gmail은 초기 설정 없이 수동 업로드로 사용 가능합니다. 이 폴더의 secrets에 Gmail 정보를 명시적으로 넣으면 기존 앱 동작에 따라 자동 연결될 수 있으므로 원하지 않으면 넣지 마세요.

## 실행

```powershell
.\run_sp_app.bat
# 또는
.\.venv\Scripts\python.exe .\launch_hold_only.py --port 8507
# 서버 시작 없이 경로와 실행 명령 확인
.\.venv\Scripts\python.exe .\launch_hold_only.py --check
```

런처는 이 폴더의 `.venv`가 있으면 사용하고, 없으면 사용자의 `python`을 사용합니다. 기존 버전의 Python 경로는 참조하지 않습니다. 기본 접속 주소는 `http://127.0.0.1:8507`이며 포트가 사용 중이면 `--port`로 변경하세요. VS Code 실행 구성은 현재 선택한 Python 인터프리터를 사용합니다.

런처와 `sp_app.py` 직접 실행 모두 초기 bootstrap에서 이 폴더를 작업 디렉터리로 고정합니다. 임시 파일/OCR/판정 캐시, 허가서 인덱스, 응답 캐시, 업로드는 `.runtime` 아래로 강제 격리합니다. 상태 인덱스, 정적 PDF 및 OUTPUT 보고서도 이 새 폴더에만 생성됩니다. 기존 경로 환경변수는 덮어쓰며 `SP_PERMIT_REVIEW_SCOPE=hold_only`, `SP_PERMIT_SEMANTIC_MODE=legacy`를 고정합니다. 여기서 legacy는 공유근거 모드 설정 이름이며 전체 문서 재검토로 되돌아간다는 뜻이 아닙니다. 라이브러리/테스트용 코어 기본값과 앱 실행 모드는 다릅니다.

기존 결과/캐시를 새 폴더에 복사하지 마세요. 원래 앱과 동시에 실행하려면 서로 다른 포트를 사용하세요.

## 별도 Streamlit Community Cloud 배포

기존 앱과 연결된 저장소에 덮어쓰지 말고 이 폴더를 새 GitHub 저장소로 업로드한 뒤 새 앱을 만드세요. 로컬 Windows 경로가 아니라 새 저장소의 브랜치와 `sp_app.py`를 선택합니다. `launch_hold_only.py`와 `.bat`는 로컬 실행용이므로 웹 배포의 진입점으로 사용하지 않습니다.

- Main file path: `sp_app.py`
- Python: `3.12` (Advanced settings에서 선택)
- Python 의존성: 루트 `requirements.txt`
- Linux OCR 의존성: 루트 `packages.txt`의 Tesseract 및 한국어·영어 언어 데이터

API 키는 GitHub 파일에 넣지 말고 **새 앱의 Secrets 설정**에 아래처럼 최상위 항목으로 입력하세요. `[clova]` 하위 섹션으로 감싸지 않습니다. 아래 키 값은 예시이며 실제 키를 별도로 입력해야 합니다. 모델과 주소는 본인의 CLOVA 사용 설정에 맞추세요.

```toml
CLOVA_API_KEY = "YOUR_CLOVA_API_KEY"
CLOVA_MODEL = "HCX-007"
CLOVA_BASE_URL = "https://clovastudio.stream.ntruss.com/v1/openai"
CLOVA_MAX_COMPLETION_TOKENS = 4096
SP_OCR_MODE = "tesseract"
```

Cloud에서는 최상위 Secrets가 환경변수로 제공되며, 이 버전의 CLOVA 설정은 해당 환경변수를 읽습니다. `.env`, `.streamlit/secrets.toml`, 가상환경, 업로드 문서, 판정 결과·로그·캐시는 커밋하지 마세요. 이 배포 안내는 직접 PDF 업로드를 기준으로 합니다. Gmail 기본 설정은 프로젝트 로컬 secrets 파일을 읽는 구조이므로 Cloud Secrets에 Gmail 항목만 넣어 자동 연결된다고 가정하면 안 됩니다.

회사의 실데이터를 올리기 전에는 앱 접근 권한과 CLOVA로의 문서 내용 전송 허용 여부를 확인하세요. 현재 업로드·판정 상태는 앱 서버의 공용 폴더에 저장되며 사용자별 격리가 아닙니다. 비공개 저장소와 앱의 접근 제한은 별도로 확인해야 합니다. Community Cloud는 비공개 앱 수에 제한이 있으므로 기존 앱을 임의로 공개 전환하거나 삭제하지 마세요.

Cloud의 로컬 저장소는 영구 보관소가 아닙니다. 재배포·재시작 등에 따라 업로드와 캐시가 사라질 수 있고, 그 경우 최초 분석 시간이 다시 필요합니다. OCR 패키지 설정은 포함했지만 Windows 로컬 테스트만으로 Linux 배포 성공이나 실제 API 처리 시간을 보장하지는 않습니다.

공식 문서: [배포](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy), [의존성](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies), [Secrets](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management), [앱 공유·접근 권한](https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app).
