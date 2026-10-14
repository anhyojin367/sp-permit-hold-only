from __future__ import annotations

import os
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 compatibility
    tomllib = None

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent

# Streamlit을 다른 working directory에서 실행해도 프로젝트 루트의 .env를 확실히 읽는다.
load_dotenv(BASE_DIR / ".env")
# This standalone version never discovers dotenv files in parent folders.

APP_TITLE = "SP 시험결과 자동판별 시스템"

def _load_streamlit_secret_section(section: str) -> dict[str, object]:
    secrets_path = BASE_DIR / ".streamlit" / "secrets.toml"
    if tomllib is None or not secrets_path.exists():
        return {}
    try:
        with secrets_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return {}
    value = data.get(section, {})
    return value if isinstance(value, dict) else {}


_CLOVA_SECRETS = _load_streamlit_secret_section("clova")


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


DEFAULT_CLOVA_MODEL = os.getenv("CLOVA_MODEL", str(_CLOVA_SECRETS.get("model", "HCX-007")))
CLOVA_API_KEY = os.getenv("CLOVA_API_KEY", str(_CLOVA_SECRETS.get("api_key", "")))
CLOVA_BASE_URL = os.getenv(
    "CLOVA_BASE_URL",
    str(_CLOVA_SECRETS.get("base_url", "https://clovastudio.stream.ntruss.com/v1/openai")),
)
CLOVA_MAX_COMPLETION_TOKENS = _positive_int(
    os.getenv("CLOVA_MAX_COMPLETION_TOKENS", str(_CLOVA_SECRETS.get("max_completion_tokens", 4096))),
    4096,
)

OCR_LANG = "kor+eng"
OCR_TEXT_THRESHOLD = 80

UCUM_JSON_CANDIDATES = [
    BASE_DIR / "ucum_rag_docs.json",
    Path("/mnt/data/ucum_rag_docs.json"),
]

UCUM_JSONL_CANDIDATES = [
    BASE_DIR / "ucum_rag_image_units_ui_micro.jsonl",
    Path("/mnt/data/ucum_rag_image_units_ui_micro.jsonl"),
    BASE_DIR / "ucum_rag_image_units_enriched.jsonl",
    Path("/mnt/data/ucum_rag_image_units_enriched.jsonl"),
]

UCUM_XLSX_CANDIDATES = [
    BASE_DIR / "TableOfExampleUcumCodesForElectronicMessaging.xlsx",
    Path("/mnt/data/TableOfExampleUcumCodesForElectronicMessaging.xlsx"),
]

RAG_DATA_DIR = BASE_DIR / "rag_data"

LEGACY_EXTRACTOR_OUTDIR_NAME = "legacy_extract"

PASS_LABEL = "검수합격"
FAIL_LABEL = "검수불합격"
HOLD_LABEL = "검수보류"

STATUS_COLORS = {
    PASS_LABEL: "#22c55e",
    FAIL_LABEL: "#ef4444",
    HOLD_LABEL: "#f59e0b",
}

SECTION_NUMBER_RE = r"^(\d+(?:\.\d+)*)\.?\s+(.+)$"
