from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .utils import clean_text


@dataclass
class ParsedMeasurement:
    value: float
    unit: str
    value_canonical: float
    unit_canonical: str
    pretty: str


_SUP_TO_NORMAL = str.maketrans(
    {
        "⁰": "0",
        "¹": "1",
        "²": "2",
        "³": "3",
        "⁴": "4",
        "⁵": "5",
        "⁶": "6",
        "⁷": "7",
        "⁸": "8",
        "⁹": "9",
        "⁻": "-",
        "⁺": "+",
    }
)

_NORMAL_TO_SUP = str.maketrans(
    {
        "0": "⁰",
        "1": "¹",
        "2": "²",
        "3": "³",
        "4": "⁴",
        "5": "⁵",
        "6": "⁶",
        "7": "⁷",
        "8": "⁸",
        "9": "⁹",
        "-": "⁻",
        "+": "⁺",
    }
)


UNIT_ALIASES = {
    "": "scalar",
    "%": "fraction",

    "mL": "mL",
    "ml": "mL",
    "ML": "mL",
    "L": "L",

    "mg": "mg",
    "g": "g",
    "kg": "kg",
    "μg": "μg",
    "µg": "μg",
    "ug": "μg",
    "ng": "ng",

    "μg/mL": "μg/mL",
    "µg/mL": "μg/mL",
    "ug/mL": "μg/mL",
    "mg/mL": "mg/mL",
    "ng/mL": "ng/mL",

    "μg/mg of protein": "μg/mg of protein",
    "µg/mg of protein": "μg/mg of protein",
    "ug/mg of protein": "μg/mg of protein",
    "ng/mg of protein": "ng/mg of protein",

    "EU/mL": "EU/mL",
    "EU/ml": "EU/mL",
    "EU/dose": "EU/dose",
    "EU/mg of protein": "EU/mg of protein",

    "IU/mL": "IU/mL",
    "IU/ml": "IU/mL",

    "CFU/mL": "CFU/mL",
    "CFU/ml": "CFU/mL",
    "CFU/10mL": "CFU/10mL",
    "CFU/10ml": "CFU/10mL",

    "cells/mL": "cells/mL",
    "cell/mL": "cells/mL",
    "cells/ml": "cells/mL",
    "cell/ml": "cells/mL",

    "mOsm/kg": "mOsm/kg",
    "mOsmol": "mOsmol",

    "nm": "nm",
    "Da": "Da",
    "kDa": "kDa",

    "개": "count",
    "개/vial": "count_per_vial",
}


KNOWN_UNITS = sorted(UNIT_ALIASES.keys(), key=len, reverse=True)


def normalize_measurement_text(text: str | None) -> str:
    # Compatibility unit glyphs (㎍, ㎖, ㎛) are formatting, not new units.
    # Keep superscripts unchanged so exponents and squared units retain meaning.
    value = "".join(unicodedata.normalize("NFKC", char) if "\u3300" <= char <= "\u33ff" else char
                    for char in clean_text(text)).replace("µ", "μ").replace("％", "%")
    return re.sub(r"(?<=[A-Za-zμ])\s*/\s*(?=[A-Za-zμ])", "/", value)


def _superscript(exp: str) -> str:
    return exp.translate(_NORMAL_TO_SUP)


def _normalize_exp(exp: str | None) -> int | None:
    if exp is None:
        return None

    exp = clean_text(exp)
    if not exp:
        return None

    exp = exp.replace("^", "")
    exp = exp.translate(_SUP_TO_NORMAL)
    exp = re.sub(r"\s+", "", exp)

    try:
        return int(exp)
    except ValueError:
        return None


def _format_base_number(value: float, original: str) -> str:
    original = clean_text(original).replace(",", "")

    if "." in original:
        decimals = len(original.split(".", 1)[1])
        decimals = min(max(decimals, 1), 4)
        return f"{value:.{decimals}f}"

    if value.is_integer():
        return str(int(value))

    return str(value)


def _clean_unit_tail(tail: str) -> str:
    tail = clean_text(tail)
    if not tail:
        return ""

    tail = tail.replace("％", "%")
    tail = tail.replace("µ", "μ")

    tail = re.split(
        r"\s*(이상|이하|초과|미만|이어야|하여야|함|일치|확인|무|음성|양성|불검출|검출)\b",
        tail,
        maxsplit=1,
    )[0]

    tail = tail.strip(" :：,;()[]{}")
    return tail


def _extract_unit(tail: str) -> str:
    tail = _clean_unit_tail(tail)
    if not tail:
        return ""

    compact_tail = tail.replace(" ", "")

    for unit in KNOWN_UNITS:
        if not unit:
            continue

        unit_compact = unit.replace(" ", "")

        if compact_tail.startswith(unit_compact):
            return unit

    first = tail.split()[0] if tail.split() else tail
    first = first.strip(" :：,;()[]{}")
    return first


def _canonicalize_unit(unit: str) -> str:
    unit = clean_text(unit).replace("µ", "μ")
    return UNIT_ALIASES.get(unit, unit or "scalar")


def _canonicalize_value(value: float, unit_canonical: str) -> float:
    if unit_canonical == "fraction":
        return value / 100.0

    return value


def _format_pretty(
    base_value: float,
    base_original: str,
    exponent: int | None,
    unit: str,
) -> str:
    number_part = _format_base_number(base_value, base_original)

    if exponent is not None:
        number_part = f"{number_part} x 10{_superscript(str(exponent))}"

    if unit == "%":
        return f"{number_part}%"

    if unit:
        return f"{number_part} {unit}"

    return number_part


def parse_number_and_unit(text: str | None) -> ParsedMeasurement | None:
    """
    지원 예시:
    - 3.00 x 10^6 cells/mL
    - 3.00 x 10⁶ cells/mL
    - 10.00 x 10^6 cells/mL 이상
    - 91%
    - ≤100.0EU/mL
    - 25EU/mL
    """
    text = normalize_measurement_text(text)

    if not text:
        return None

    text = text.replace("×", "x")
    text = text.replace("％", "%")
    text = text.replace("µ", "μ")

    pattern = re.compile(
        r"(?P<base>[+-]?\d[\d,]*(?:\.\d+)?)"
        r"(?:\s*[xX]\s*10\s*(?:\^\s*)?(?P<exp>[+\-]?\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+))?"
    )

    match = pattern.search(text)
    if not match:
        return None

    base_original = match.group("base")
    base_value = float(base_original.replace(",", ""))

    exponent = _normalize_exp(match.group("exp"))
    value = base_value * (10 ** exponent) if exponent is not None else base_value

    tail = text[match.end():]
    unit = _extract_unit(tail)
    unit = unit.replace("µ", "μ")

    unit_canonical = _canonicalize_unit(unit)
    value_canonical = _canonicalize_value(value, unit_canonical)

    pretty = _format_pretty(
        base_value=base_value,
        base_original=base_original,
        exponent=exponent,
        unit=unit,
    )

    return ParsedMeasurement(
        value=value,
        unit=unit,
        value_canonical=value_canonical,
        unit_canonical=unit_canonical,
        pretty=pretty,
    )


def normalize_exponent_for_display(text: str | None) -> str:
    text = clean_text(text)
    if not text:
        return ""

    text = text.replace("×", "x")
    text = text.replace("µ", "μ")

    def repl(match: re.Match) -> str:
        base = match.group("base")
        exp = match.group("exp")
        exp = exp.translate(_SUP_TO_NORMAL)
        return f"{base} x 10{_superscript(exp)}"

    text = re.sub(
        r"(?P<base>\d+(?:\.\d+)?)\s*[xX]\s*10\s*(?:\^\s*)?(?P<exp>[+\-]?\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)",
        repl,
        text,
    )

    return text
