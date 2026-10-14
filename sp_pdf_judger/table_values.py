"""Conservative layout parsing shared by extraction and deterministic checks.

These helpers never change the source text, invent a missing value, convert a
unit, or decide whether a business criterion passes.
"""
from __future__ import annotations

import re


def field_values(text: str, label: str) -> list[str]:
    compact = lambda value: re.sub(r"\s+", "", value)
    pattern = re.compile(r"^[\s|]*" + r"\s*".join(map(re.escape, compact(label)))
                         + r"(?=$|[\s:：|\d])\s*[:：|]?\s*(.*)$")
    found = []
    lines = []
    for line in (text or "").splitlines():
        cells = line.split("|")
        continuation = (len(cells) >= 3 and not cells[0].strip() and not cells[1].strip())
        nested_label = (len(cells) >= 2 and not cells[0].strip()
                        and cells[1].strip() in {"주소", "명칭"}
                        and lines and lines[-1].lstrip().startswith("제조사"))
        if lines and (continuation or nested_label):
            lines[-1] += " " + line.strip()
        else:
            lines.append(line)
    for line in lines:
        match = pattern.match(line)
        if match:
            # Only empty boundary cells are layout. Preserve interior separators:
            # 'A | B' must never become an invented single identifier 'AB'.
            found.append(match[1].strip(" \t|"))
            continue
        cells = [cell.strip() for cell in line.split("|") if cell.strip()]
        keys = [compact(cell) for cell in cells]
        # A nested manufacturer form may be flattened horizontally by one
        # backend and into a combined field by another. Labels, not values,
        # establish the pairing. Ambiguous/missing cells are not guessed.
        if (keys[:2] == ["제조사", "명칭"] and keys.count("주소") == 1
                and len(cells) == 5 and keys[3] == "주소"):
            values = {"명칭제조사주소": f"{cells[2]} {cells[4]}",
                      "주소": cells[4], "제조사명칭": cells[2]}
            if compact(label) in values:
                found.append(values[compact(label)])
    return found


def material_rows(lines: list[str], amount_header: str = "분량") -> list[list[str]]:
    """Join only provable soft wraps; keep incomplete/unknown rows for HOLD.

    A blank first cell alone does NOT prove a continuation (merged ingredient
    names also have blank first cells). A split lot is joined only when the
    previous lot ends in a hyphen and the continuation has no new quantity.
    """
    rows: list[list[str]] = []
    header: list[str] = []
    for line in lines:
        if re.fullmatch(r"[|:\-\s]+", line):
            continue
        cells = [cell.strip() for cell in line.split("|")]
        if cells and not cells[-1]:
            # Remove a Markdown border, but preserve a blank amount column.
            if header and len(cells) == len(header) + 2 and not cells[0]:
                cells = cells[1:-1]
            elif not header and not cells[0]:
                cells = cells[1:-1]
        if cells and cells[-1] == amount_header:
            header = cells
            continue
        if header and len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        if (header == ["원료명", "성분명", "제조번호", amount_header]
                and len(cells) == 4 and not cells[0] and not cells[3]
                and rows and len(rows[-1]) == 4 and rows[-1][3]
                and rows[-1][2].endswith("-") and cells[2]
                and re.fullmatch(r"[A-Za-z0-9-]+", cells[2])):
            rows[-1][1] = " ".join(filter(None, [rows[-1][1], cells[1]]))
            rows[-1][2] += cells[2]
            continue
        rows.append(cells)
    return rows
