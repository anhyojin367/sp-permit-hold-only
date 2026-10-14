from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .utils import clean_text


@dataclass(frozen=True)
class NormalizedDate:
    year: int
    month: int | None = None
    day: int | None = None

    @property
    def precision(self) -> str:
        if self.day is not None:
            return "day"
        if self.month is not None:
            return "month"
        return "year"

    @property
    def canonical(self) -> str:
        if self.day is not None and self.month is not None:
            return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"

        if self.month is not None:
            return f"{self.year:04d}-{self.month:02d}"

        return f"{self.year:04d}"

    def to_date_for_order(self) -> date:
        month = self.month or 1
        day = self.day or 1
        return date(self.year, month, day)


def normalize_date_text(value: str | None) -> NormalizedDate | None:
    """
    날짜 표현 정규화.

    지원 예:
    - 2025.01.01
    - 2025.1.1
    - 2025-01-01
    - 2025/01/01
    - 2025년 1월 1일
    - 2025년 01월
    - 2025.01
    """
    text = clean_text(value)

    if not text:
        return None

    text = text.replace("．", ".")
    text = text.replace("。", ".")
    text = text.replace("－", "-")
    text = text.replace("／", "/")
    text = re.sub(r"\s+", " ", text).strip()

    patterns = [
        r"(20\d{2})\s*년\s*(\d{1,2})?\s*월?\s*(?:(\d{1,2})\s*일?)?",
        r"(20\d{2})\s*[.\-/]\s*(\d{1,2})(?:\s*[.\-/]\s*(\d{1,2}))?",
        r"(20\d{2})\s+(\d{1,2})(?:\s+(\d{1,2}))?",
    ]

    for pattern in patterns:
        m = re.search(pattern, text)

        if not m:
            continue

        year = int(m.group(1))
        month = int(m.group(2)) if m.group(2) else None
        day = int(m.group(3)) if m.group(3) else None

        if month is not None and not (1 <= month <= 12):
            continue

        if day is not None and not (1 <= day <= 31):
            continue

        try:
            if month is not None and day is not None:
                date(year, month, day)
            elif month is not None:
                date(year, month, 1)
            else:
                date(year, 1, 1)
        except ValueError:
            continue

        return NormalizedDate(
            year=year,
            month=month,
            day=day,
        )

    return None


def dates_equivalent(left: str | None, right: str | None) -> bool:
    """
    날짜 동등성 비교.

    원칙:
    - 둘 다 일자까지 있으면 YYYY-MM-DD까지 비교
    - 한쪽이 월까지만 있으면 YYYY-MM까지만 비교
    - 한쪽이 연도까지만 있으면 YYYY까지만 비교
    """
    left_date = normalize_date_text(left)
    right_date = normalize_date_text(right)

    if left_date is None or right_date is None:
        return False

    if left_date.year != right_date.year:
        return False

    if left_date.month is not None and right_date.month is not None:
        if left_date.month != right_date.month:
            return False

    if left_date.day is not None and right_date.day is not None:
        if left_date.day != right_date.day:
            return False

    return True


def date_to_order_value(value: str | None) -> date | None:
    normalized = normalize_date_text(value)

    if normalized is None:
        return None

    return normalized.to_date_for_order()


def canonical_date(value: str | None) -> str:
    normalized = normalize_date_text(value)

    if normalized is None:
        return clean_text(value)

    return normalized.canonical