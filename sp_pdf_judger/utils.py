from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any


def clean_text(text: str | None) -> str:
    if text is None:
        return ""
    text = str(text).replace("\xa0", " ").replace("\u3000", " ").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def safe_read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def slugify(value: str) -> str:
    value = clean_text(value).lower()
    value = re.sub(r"[^a-z0-9가-힣]+", "-", value)
    return value.strip("-") or "node"


def html_escape(text: str | None) -> str:
    return html.escape(clean_text(text))
