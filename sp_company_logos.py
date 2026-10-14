# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
from pathlib import Path


LOGO_DIR = Path("company_logos")
LOGO_MAP_FILE = LOGO_DIR / "logos.json"


def find_company_logo(company: str) -> Path | None:
    """Return a locally saved official company logo if one is mapped."""

    mapping = _load_logo_map()
    normalized_company = _normalize(company)
    for raw_key, raw_file in mapping.items():
        key = _normalize(raw_key)
        if not key or not raw_file:
            continue
        if key in normalized_company or normalized_company in key:
            candidate = LOGO_DIR / raw_file
            if candidate.exists():
                return candidate
    return None


def _load_logo_map() -> dict[str, str]:
    if not LOGO_MAP_FILE.exists():
        return {}
    try:
        return json.loads(LOGO_MAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _normalize(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"(주식회사|\(주\)|㈜|co\.?|ltd\.?|inc\.?)", "", value, flags=re.IGNORECASE)
    return re.sub(r"[^0-9a-z가-힣]+", "", value)
