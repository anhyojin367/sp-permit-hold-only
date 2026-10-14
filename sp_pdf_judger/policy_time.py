"""Complete, timezone-free process timestamps and exact duration arithmetic.

Unknown annotations/timezones are evidence gaps, not text to silently discard.
Calendar months belong to the expiry operator, never to elapsed-time arithmetic.
"""
from datetime import datetime
from decimal import Decimal, localcontext
import re


_STAMP_PATTERNS = (
    r"(?P<y>[0-9]{4})(?P<sep>[./-])(?P<m>[0-9]{1,2})(?P=sep)(?P<d>[0-9]{1,2})"
    r"(?:\s+|T)(?P<h>[0-9]{1,2}):(?P<minute>[0-9]{2})(?::(?P<s>[0-9]{2}))?",
    r"(?P<y>[0-9]{4})\s*년\s*(?P<m>[0-9]{1,2})\s*월\s*(?P<d>[0-9]{1,2})\s*일\s*"
    r"(?P<h>[0-9]{1,2})\s*시\s*(?P<minute>[0-9]{1,2})\s*분(?:\s*(?P<s>[0-9]{1,2})\s*초)?",
)
_DURATION_TOKEN = re.compile(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(일|시간|분|초|d|h|min|s)\s*", re.I)
_SECONDS = {"일": 86400, "d": 86400, "시간": 3600, "h": 3600,
            "분": 60, "min": 60, "초": 1, "s": 1}


def complete_timestamp(text):
    for pattern in _STAMP_PATTERNS:
        match = re.fullmatch(pattern, (text or "").strip())
        if match:
            try:
                return datetime(*(int(match[k] or 0) for k in ("y", "m", "d", "h", "minute", "s")))
            except ValueError:
                return None
    return None


def complete_duration_seconds(text):
    text = (text or "").strip()
    # Bound decimal size/precision so arithmetic can be exact without unbounded input.
    if not text or len(text) > 120:
        return None
    parts, offset = [], 0
    while offset < len(text):
        match = _DURATION_TOKEN.match(text, offset)
        if match is None or len(match[1]) > 24:
            return None
        parts.append((Decimal(match[1]), _SECONDS[match[2].lower()]))
        offset = match.end()
    # Compound units must descend, not duplicate, and use ordinary clock components.
    for index, (value, scale) in enumerate(parts):
        if index and (scale >= parts[index - 1][1] or value * scale >= parts[index - 1][1]):
            return None
        if index < len(parts) - 1 and value != value.to_integral_value():
            return None
    with localcontext() as ctx:
        ctx.prec = 40
        return sum((value * scale for value, scale in parts), Decimal(0))


def seconds_text(value):
    return format(value, "f").rstrip("0").rstrip(".") if value % 1 else str(int(value))
