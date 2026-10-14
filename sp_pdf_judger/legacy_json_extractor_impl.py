
import re
import json
import argparse
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Set

import pdfplumber


@dataclass
class Config:
    save_intermediate: bool = True

    label_aliases: Dict[str, str] = field(default_factory=dict)
    field_map: Dict[str, str] = field(default_factory=dict)

    test_name_labels: List[str] = field(default_factory=list)
    criteria_labels: List[str] = field(default_factory=list)
    result_labels: List[str] = field(default_factory=list)
    method_labels: List[str] = field(default_factory=list)
    remarks_labels: List[str] = field(default_factory=list)

    heading_patterns: List[re.Pattern] = field(default_factory=list)
    heading_forbidden_patterns: List[re.Pattern] = field(default_factory=list)

    noise_patterns: List[re.Pattern] = field(default_factory=list)

    test_name_positive_patterns: List[re.Pattern] = field(default_factory=list)
    test_name_negative_patterns: List[re.Pattern] = field(default_factory=list)

    special_test_names: Set[str] = field(default_factory=set)
    subtest_names: Set[str] = field(default_factory=set)

    raw_skip_patterns: List[re.Pattern] = field(default_factory=list)
    table_noise_patterns: List[re.Pattern] = field(default_factory=list)
    parent_group_names: Set[str] = field(default_factory=set)


def build_config() -> Config:
    label_aliases = {
        "시험 기준": "시험기준",
        "시험 결과": "시험결과",
        "시험 방법": "시험방법",
        "시험 항목": "시험항목",
        "시험 명": "시험명",
        "항 목": "항목",
        "항목 명": "항목명",
        "기 준": "기준",
        "결 과": "결과",
        "규 격": "규격",
        "비 고": "비고",
        "참 고": "참고",
        "특이 사항": "특이사항",
        "측정 결과": "측정결과",
        "실험 결과": "실험결과",
        "분석 방법": "분석방법",
        "시험 법": "시험법",
        "분석 법": "분석법",
        "품질 기준": "품질기준",
        "허용 기준": "허용기준",
    }

    field_map = {
        "시험명": "test_name",
        "시험항목": "test_name",
        "항목명": "test_name",
        "항목": "test_name",
        "시험": "test_name",

        "시험기준": "criteria",
        "기준": "criteria",
        "규격": "criteria",
        "품질기준": "criteria",
        "허용기준": "criteria",

        "시험결과": "result",
        "결과": "result",
        "측정결과": "result",
        "실험결과": "result",

        "시험방법": "method",
        "시험법": "method",
        "분석방법": "method",
        "분석법": "method",
        "방법": "method",

        "비고": "remarks",
        "특이사항": "remarks",
        "참고": "remarks",
    }

    test_name_labels = ["시험명", "시험항목", "항목명", "항목", "시험"]
    criteria_labels = ["시험기준", "기준", "규격", "품질기준", "허용기준"]
    result_labels = ["시험결과", "결과", "측정결과", "실험결과"]
    method_labels = ["시험방법", "시험법", "분석방법", "분석법", "방법"]
    remarks_labels = ["비고", "특이사항", "참고"]

    heading_patterns = [
        re.compile(r"^(\d+(?:\.\d+)*)\s+(.+)$"),
        re.compile(r"^(\d+(?:\.\d+)*)\.\s+(.+)$"),
    ]

    heading_forbidden_patterns = [
        re.compile(r"^[\d.\-~/\s%]+$"),
        re.compile(r"^\d+(?:\.\d+)?\s*(mL|ml|mg|g|kg|L|IU|CFU|PFU|%|ppm|ppb|μL|uL|℃|°C)\b"),
        re.compile(r"^\d+(?:\.\d+)?\s*(바이알|상자|병|개|정|캡슐|앰플)\b"),
        re.compile(r"^\d+(?:\.\d+)?\s*~\s*\d+(?:\.\d+)?"),
        re.compile(r"^\d+(?:\.\d+)?\s*pH", re.I),
        re.compile(r"^.*(바이알|상자|접종량|pH범위).*$"),
        re.compile(r"^.*\b(mL|ml|mg|kg|g|IU|CFU|PFU|%)\b.*$"),
    ]

    noise_patterns = [
        re.compile(r"^\s*한미약품\s*주식회사\s*$"),
        re.compile(r"^\s*Summary Protocol.*$", re.I),
        re.compile(r"^\s*Japanese encephalitis Vaccine.*$", re.I),
        re.compile(r"^\s*제조번호\s*[:：].*$"),
        re.compile(r"^\s*-\s*\d+\s*-\s*$"),
        re.compile(r"^\s*-\s*\d+\s*$"),
        re.compile(r"^\s*-\s*$"),
        re.compile(r"^\s*\d+\s*/\s*\d+\s*페이지\s*$"),
        re.compile(r"^\s*페이지\s*\d+\s*/\s*\d+\s*$"),
    ]

    test_name_positive_patterns = [
        re.compile(r".+시험$"),
        re.compile(r".+부정시험$"),
        re.compile(r".+확인시험$"),
        re.compile(r".+역가시험$"),
        re.compile(r".+측정시험$"),
        re.compile(r".+분석시험$"),
        re.compile(r".+함량시험$"),
        re.compile(r".+불활화시험$"),
        re.compile(r".+분포시험$"),
        re.compile(r".+균일성시험$"),
        re.compile(r".+이물시험$"),
        re.compile(r".+미립자시험$"),
        re.compile(r".+정량시험$"),
        re.compile(r".+크로마토그래피시험$"),
        re.compile(r".+HPLC분석시험$"),
        re.compile(r".+안정성시험$"),
        re.compile(r".+오염시험$"),
        re.compile(r".+검출시험$"),
        re.compile(r".+검사$"),
        re.compile(r".+분석$"),
        re.compile(r".+측정$"),
        re.compile(r"^성상$"),
    ]

    test_name_negative_patterns = [
        re.compile(r"^시험$"),
        re.compile(r"^시험기준"),
        re.compile(r"^시험결과"),
        re.compile(r"^시험방법"),
        re.compile(r"^시험기간"),
        re.compile(r"^시험일"),
        re.compile(r"^시험일자"),
        re.compile(r"^시험자"),
        re.compile(r"^시험책임자"),
        re.compile(r"^시험동물"),
        re.compile(r"^기준$"),
        re.compile(r"^결과$"),
        re.compile(r"^방법$"),
        re.compile(r"^비고$"),
        re.compile(r"^참고$"),
        re.compile(r"^정보$"),
        re.compile(r"^제품명"),
        re.compile(r"^품목명"),
        re.compile(r"^생기수재명"),
        re.compile(r"^제조번호"),
        re.compile(r"^제조일"),
        re.compile(r"^유효기간"),
        re.compile(r"^확인/서명일"),
        re.compile(r"^\d+(?:\.\d+)*\s*시험$"),
        re.compile(r".*페이지.*"),
        re.compile(r".*한미약품.*"),
        re.compile(r".*Summary Protocol.*", re.I),
        re.compile(r".*Japanese encephalitis Vaccine.*", re.I),
        re.compile(r".*(바이알|상자|mL|ml|mg|kg| pH범위|접종량).*"),
    ]

    raw_skip_patterns = [
        re.compile(r"^\s*-\s*\d+\s*-\s*$"),
        re.compile(r"^\s*-\s*\d+\s*$"),
        re.compile(r"^\s*-\s*$"),
        re.compile(r"^\s*\d+\s*/\s*\d+\s*페이지\s*$"),
        re.compile(r"^\s*페이지\s*\d+\s*/\s*\d+\s*$"),
        re.compile(r"^\s*구\s*분\s+접종량"),
        re.compile(r"^\s*접종량\(?.*체중"),
        re.compile(r"^\s*검체군\s*$"),
        re.compile(r"^\s*대조군\s+"),
        re.compile(r"^\s*\d+(?:\.\d+)?\s*mL\s+\d"),
        re.compile(r"^\s*0\.\d+\s*mL\s+\d"),
        re.compile(r"^\s*5\.0\s*mL\s+\d"),
    ]

    table_noise_patterns = [
        re.compile(r"^\s*구\s*분\s*\|"),
        re.compile(r".*접종량\(mL\).*투여 전 체중.*종료 시 체중.*"),
        re.compile(r"^\s*검체군\s*$"),
        re.compile(r"^\s*대조군\s+\d"),
        re.compile(r"^\s*\d+(?:\.\d+)?\s*mL\s+\d"),
        re.compile(r"^\s*0\.\d+\s*mL\s+\d"),
        re.compile(r"^\s*5\.0\s*mL\s+\d"),
    ]

    special_test_names = {"성상"}
    subtest_names = {"기니픽", "기니피그", "마우스", "토끼", "랫드", "랫트", "rat", "mouse", "guinea pig"}
    parent_group_names = {"이상독성부정시험"}

    return Config(
        save_intermediate=True,
        label_aliases=label_aliases,
        field_map=field_map,
        test_name_labels=test_name_labels,
        criteria_labels=criteria_labels,
        result_labels=result_labels,
        method_labels=method_labels,
        remarks_labels=remarks_labels,
        heading_patterns=heading_patterns,
        heading_forbidden_patterns=heading_forbidden_patterns,
        noise_patterns=noise_patterns,
        test_name_positive_patterns=test_name_positive_patterns,
        test_name_negative_patterns=test_name_negative_patterns,
        special_test_names=special_test_names,
        subtest_names=subtest_names,
        raw_skip_patterns=raw_skip_patterns,
        table_noise_patterns=table_noise_patterns,
        parent_group_names=parent_group_names,
    )


CFG = build_config()


@dataclass
class RawPage:
    page_num: int
    text: str
    tables: List[List[List[str]]]


@dataclass
class Item:
    idx: int
    type: str
    page: int
    text: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Section:
    number: str
    title: str
    depth: int
    page: int
    start_idx: int
    end_idx: int


@dataclass
class Block:
    section_number: Optional[str]
    section_title: Optional[str]
    page_start: int
    page_end: int
    items: List[Item]


@dataclass
class Record:
    section_number: Optional[str]
    section_title: Optional[str]
    test_name: Optional[str]
    criteria: Optional[str]
    result: Optional[str]
    method: Optional[str]
    remarks: Optional[str]
    page_start: int
    page_end: int
    source_types: List[str]
    raw_text: str


def clean_text(text: str) -> str:
    if text is None:
        return ""
    text = str(text).replace("\xa0", " ").replace("\u3000", " ").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def normalize_number_spacing(text: str) -> str:
    return re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", text)


def normalize_label(text: str) -> str:
    t = clean_text(text)
    t = normalize_number_spacing(t)
    t = re.sub(r"\s*[:：]\s*$", "", t)
    return CFG.label_aliases.get(t, t)


def canonical_field(label: str) -> Optional[str]:
    return CFG.field_map.get(normalize_label(label))


def infer_depth(number: str) -> int:
    return len(number.split(".")) if "." in number else 1


def is_noise(text: str) -> bool:
    t = clean_text(text)
    if not t:
        return True
    return any(p.match(t) for p in CFG.noise_patterns)


def is_heading_forbidden_title(title: str) -> bool:
    t = clean_text(title)
    if not t or is_noise(t):
        return True
    return any(p.match(t) for p in CFG.heading_forbidden_patterns)


def is_page_artifact(text: str) -> bool:
    t = clean_text(text)
    if not t:
        return True
    patterns = [
        r"^\s*-\s*\d+\s*-\s*$",
        r"^\s*-\s*\d+\s*$",
        r"^\s*-\s*$",
        r"^\s*\d+\s*/\s*\d+\s*페이지\s*$",
        r"^\s*페이지\s*\d+\s*/\s*\d+\s*$",
        r"^\s*제조번호\s*[:：].*$",
        r"^\s*한미약품\s*주식회사\s*$",
        r"^\s*Summary Protocol.*$",
        r"^\s*Japanese encephalitis Vaccine.*$",
    ]
    return any(re.match(p, t, flags=re.I) for p in patterns)


def is_value_like_line(text: str) -> bool:
    t = clean_text(text)
    if not t:
        return False
    labels = set(CFG.test_name_labels + CFG.criteria_labels + CFG.result_labels + CFG.method_labels + CFG.remarks_labels)
    if normalize_label(t) in labels:
        return False
    if detect_heading(t):
        return False
    value_patterns = [
        r".*\d.*",
        r".*적합.*",
        r".*부적합.*",
        r".*없음.*",
        r".*기준에 적합.*",
        r".*pH.*",
        r".*범위.*",
        r".*이하.*",
        r".*이상.*",
        r".*CFU.*",
        r".*PFU.*",
        r".*IU.*",
        r".*μg.*",
        r".*mg.*",
        r".*mL.*",
        r".*%.*",
    ]
    return any(re.match(p, t, flags=re.I) for p in value_patterns) or len(t) <= 120


def detect_heading(line: str) -> Optional[Tuple[str, str, int]]:
    t = normalize_number_spacing(clean_text(line))
    if not t or is_noise(t):
        return None

    for pat in CFG.heading_patterns:
        m = pat.match(t)
        if not m:
            continue
        number = clean_text(m.group(1))
        title = clean_text(m.group(2))
        if is_heading_forbidden_title(title):
            continue
        return number, title, infer_depth(number)
    return None


def normalize_test_name(text: str) -> str:
    t = clean_text(text)
    if not t:
        return ""
    t = re.sub(r"^시험\s+(?=.+(시험|검사|분석|측정)(\([^)]*\))?$)", "", t)
    t = re.sub(r"^시험(?=.+(시험|검사|분석|측정)(\([^)]*\))?$)", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def is_subtest_name(text: str) -> bool:
    return clean_text(text) in CFG.subtest_names


def is_probable_test_name(text: str) -> bool:
    t = normalize_test_name(text)
    if not t:
        return False
    if t in CFG.special_test_names:
        return True
    if " - " in t:
        left, right = [clean_text(x) for x in t.split(" - ", 1)]
        if left in CFG.parent_group_names and is_subtest_name(right):
            return True
    if len(t) > 120:
        return False
    if ":" in t or "：" in t or "|" in t:
        return False
    if is_noise(t):
        return False
    if any(p.match(t) for p in CFG.test_name_negative_patterns):
        return False
    if any(p.match(t) for p in CFG.heading_forbidden_patterns):
        return False
    if re.fullmatch(r"[\d.\-~/\s%a-zA-Zμ℃°]+", t):
        return False
    if any(p.match(t) for p in CFG.test_name_positive_patterns):
        return True
    if re.match(r".+시험\([^)]*\)$", t):
        return True
    if re.match(r".+검사\([^)]*\)$", t):
        return True
    if re.match(r".+분석\([^)]*\)$", t):
        return True
    if re.match(r".+측정\([^)]*\)$", t):
        return True
    return False


def repair_split_test_name(curr: str, nxt: Optional[str]) -> Tuple[str, bool]:
    curr = clean_text(curr)
    nxt = clean_text(nxt or "")

    m = re.search(r"\(([^)]*?)\s*\)\s*$", curr)
    if m and re.fullmatch(r"\d+\)?", nxt):
        inside = m.group(1)
        if inside.endswith("LD") or inside.endswith("ED") or inside.endswith("TCID"):
            num = re.sub(r"[^\d]", "", nxt)
            repaired = re.sub(r"\(([^)]*?)\s*\)\s*$", f"({inside}{num})", curr)
            return repaired, True

    if re.search(r"\b(LD|ED|TCID)\s*$", curr) and re.fullmatch(r"\d+", nxt):
        return curr + nxt, True
    return curr, False


def join_nonempty(parts: List[str], sep: str = " | ") -> str:
    return sep.join([clean_text(p) for p in parts if clean_text(p)])


def uniq_keep_order(values: List[str]) -> List[str]:
    seen = set()
    out = []
    for v in values:
        v = clean_text(v)
        if not v:
            continue
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def normalize_multiline_field(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    vals = uniq_keep_order([clean_text(x) for x in str(text).split("\n") if clean_text(x)])
    return "\n".join(vals) if vals else None


def should_skip_raw_line(text: str) -> bool:
    t = clean_text(text)
    if not t:
        return True
    if is_noise(t) or is_page_artifact(t):
        return True
    if detect_heading(t):
        return True
    return any(p.match(t) for p in CFG.raw_skip_patterns)


def should_skip_table_line(text: str) -> bool:
    t = clean_text(text)
    if not t:
        return True
    return any(p.match(t) for p in CFG.table_noise_patterns)


def fields_payload_count(rec: "Record") -> int:
    cnt = 0
    for v in [rec.test_name, rec.criteria, rec.result, rec.method, rec.remarks]:
        if clean_text(v):
            cnt += 1
    return cnt


class PDFReader:
    def __init__(self, pdf_path: str):
        self.pdf_path = pdf_path

    def read(self) -> List[RawPage]:
        pages: List[RawPage] = []
        with pdfplumber.open(self.pdf_path) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                text = clean_text(page.extract_text() or "")
                raw_tables = page.extract_tables() or []
                tables: List[List[List[str]]] = []
                for table in raw_tables:
                    norm_table = []
                    for row in table or []:
                        if row is None:
                            continue
                        norm_row = [clean_text(str(c)) if c is not None else "" for c in row]
                        if any(norm_row):
                            norm_table.append(norm_row)
                    if norm_table:
                        tables.append(norm_table)
                pages.append(RawPage(page_num=i, text=text, tables=tables))
        return pages


class Normalizer:
    def __init__(self):
        self._idx = 0

    def run(self, pages: List[RawPage]) -> List[Item]:
        items: List[Item] = []
        for page in pages:
            items.extend(self._normalize_text(page.text, page.page_num))
            items.extend(self._normalize_tables(page.tables, page.page_num))
        return items

    def _next_idx(self) -> int:
        x = self._idx
        self._idx += 1
        return x

    def _make_item(self, item_type: str, page: int, text: Optional[str] = None, meta: Optional[Dict[str, Any]] = None) -> Item:
        return Item(idx=self._next_idx(), type=item_type, page=page, text=text, meta=meta or {})

    def _normalize_text(self, text: str, page_num: int) -> List[Item]:
        out: List[Item] = []
        if not text:
            return out
        lines = [normalize_number_spacing(clean_text(x)) for x in text.split("\n")]
        lines = [x for x in lines if x and not is_noise(x)]

        repaired_lines: List[str] = []
        skip = False
        for i, line in enumerate(lines):
            if skip:
                skip = False
                continue
            nxt = lines[i + 1] if i + 1 < len(lines) else None
            merged, used = repair_split_test_name(line, nxt)
            repaired_lines.append(merged)
            if used:
                skip = True

        i = 0
        while i < len(repaired_lines):
            line = clean_text(repaired_lines[i])
            if not line:
                i += 1
                continue

            heading = detect_heading(line)
            if heading:
                number, title, depth = heading
                out.append(self._make_item("heading", page_num, text=line, meta={"number": number, "title": title, "depth": depth}))
                i += 1
                continue

            kv, consumed_idx = self._split_kv_with_consumption(repaired_lines, i)
            if kv:
                key, value = kv
                out.append(self._make_item("kv", page_num, meta={"key": key, "value": value, "source": "text", "consumed_next_line": consumed_idx is not None}))
                if consumed_idx is not None:
                    i = consumed_idx + 1
                else:
                    i += 1
                continue

            if line not in {":", "："}:
                out.append(self._make_item("line", page_num, text=line, meta={"source": "text"}))
            i += 1
        return out

    def _split_kv_with_consumption(self, lines: List[str], idx: int) -> Tuple[Optional[Tuple[str, str]], Optional[int]]:
        line = clean_text(lines[idx])

        m = re.match(r"^\s*([^:：]{1,40})\s*[:：]\s*(.+?)\s*$", line)
        if m:
            key = normalize_label(m.group(1))
            value = clean_text(m.group(2))
            if key and value:
                return (key, value), None

        labels = CFG.test_name_labels + CFG.criteria_labels + CFG.result_labels + CFG.method_labels + CFG.remarks_labels
        for label in sorted(set(labels), key=len, reverse=True):
            if line.startswith(label + " "):
                value = clean_text(line[len(label):])
                if value:
                    return (normalize_label(label), value), None
            if line in {label, f"{label}:", f"{label}："}:
                nxt_idx, nxt_val = self._find_next_meaningful_value(lines, idx + 1)
                if nxt_val:
                    return (normalize_label(label), nxt_val), nxt_idx

        return None, None

    def _find_next_meaningful_value(self, lines: List[str], start_idx: int) -> Tuple[Optional[int], Optional[str]]:
        collected: List[str] = []
        first_idx: Optional[int] = None
        labels = set(CFG.test_name_labels + CFG.criteria_labels + CFG.result_labels + CFG.method_labels + CFG.remarks_labels)

        for j in range(start_idx, min(start_idx + 25, len(lines))):
            cand = clean_text(lines[j])
            if not cand:
                continue
            if cand in {":", "："}:
                continue
            if is_noise(cand) or is_page_artifact(cand):
                continue
            if detect_heading(cand):
                break

            norm = normalize_label(cand)
            if norm in labels:
                break

            if first_idx is None:
                first_idx = j
            collected.append(cand)

            nxt = clean_text(lines[j + 1]) if j + 1 < len(lines) else ""
            nxt_norm = normalize_label(nxt) if nxt else ""
            if not nxt or is_page_artifact(nxt) or detect_heading(nxt) or nxt_norm in labels:
                break
            if is_value_like_line(cand) and is_value_like_line(nxt) and len(collected) < 3:
                continue
            if is_value_like_line(cand):
                break

        if not collected:
            return None, None

        value = " ".join(collected)
        value = re.sub(r"\s+", " ", value).strip()
        return first_idx, value

    def _normalize_tables(self, tables: List[List[List[str]]], page_num: int) -> List[Item]:
        out: List[Item] = []
        for table_idx, table in enumerate(tables):
            header_row_idx, header_map = self._detect_table_header(table)
            if header_row_idx is not None:
                for row_idx, row in enumerate(table[header_row_idx + 1:], start=header_row_idx + 1):
                    cells = [clean_text(c) for c in row]
                    cells = [c for c in cells if c and not is_noise(c)]
                    if not cells:
                        continue
                    fields: Dict[str, str] = {}
                    for col_idx, field_name in header_map.items():
                        if col_idx < len(row):
                            value = clean_text(row[col_idx])
                            if value and not is_noise(value):
                                fields[field_name] = value
                    if fields:
                        out.append(self._make_item("table_row", page_num, meta={"fields": fields, "raw_row": cells, "table_idx": table_idx, "row_idx": row_idx}))
                continue

            for row_idx, row in enumerate(table):
                cells = [clean_text(c) for c in row]
                cells = [c for c in cells if c and not is_noise(c)]
                if not cells:
                    continue

                repaired_cells: List[str] = []
                skip = False
                for i, cell in enumerate(cells):
                    if skip:
                        skip = False
                        continue
                    nxt = cells[i + 1] if i + 1 < len(cells) else None
                    merged, used = repair_split_test_name(cell, nxt)
                    repaired_cells.append(merged)
                    if used:
                        skip = True

                for cell in repaired_cells:
                    heading = detect_heading(cell)
                    if heading:
                        number, title, depth = heading
                        out.append(self._make_item("heading", page_num, text=cell, meta={"number": number, "title": title, "depth": depth}))
                        continue
                    kv = self._split_kv_cell(cell)
                    if kv:
                        key, value = kv
                        out.append(self._make_item("kv", page_num, meta={"key": key, "value": value, "source": "table"}))

                row_text = join_nonempty(repaired_cells)
                if row_text and row_text not in {":", "："}:
                    out.append(self._make_item("table_line", page_num, text=row_text, meta={"source": "table", "table_idx": table_idx, "row_idx": row_idx}))
        return out

    def _split_kv_cell(self, cell: str) -> Optional[Tuple[str, str]]:
        cell = clean_text(cell)
        m = re.match(r"^\s*([^:：]{1,40})\s*[:：]\s*(.+?)\s*$", cell)
        if m:
            key = normalize_label(m.group(1))
            value = clean_text(m.group(2))
            if key and value:
                return key, value
        labels = CFG.test_name_labels + CFG.criteria_labels + CFG.result_labels + CFG.method_labels + CFG.remarks_labels
        for label in sorted(set(labels), key=len, reverse=True):
            if cell.startswith(label + " "):
                value = clean_text(cell[len(label):])
                if value:
                    return normalize_label(label), value
        return None

    def _detect_table_header(self, table: List[List[str]]) -> Tuple[Optional[int], Dict[int, str]]:
        for i in range(min(3, len(table))):
            row = [normalize_label(c) for c in table[i]]
            header_map: Dict[int, str] = {}
            for col_idx, cell in enumerate(row):
                field_name = canonical_field(cell)
                if field_name:
                    header_map[col_idx] = field_name
            if len(header_map) >= 2:
                return i, header_map
        return None, {}


class SectionBuilder:
    def run(self, items: List[Item]) -> List[Section]:
        heading_positions = []
        for i, item in enumerate(items):
            if item.type != "heading":
                continue
            title = clean_text(item.meta.get("title", ""))
            if is_heading_forbidden_title(title):
                continue
            heading_positions.append(i)
        if not heading_positions:
            return []

        sections: List[Section] = []
        for idx, pos in enumerate(heading_positions):
            item = items[pos]
            end_idx = heading_positions[idx + 1] - 1 if idx < len(heading_positions) - 1 else len(items) - 1
            sections.append(Section(number=item.meta["number"], title=item.meta["title"], depth=item.meta["depth"], page=item.page, start_idx=pos, end_idx=end_idx))
        return sections


class BlockBuilder:
    def run(self, items: List[Item], sections: List[Section]) -> List[Block]:
        if not items:
            return []
        if not sections:
            return [Block(None, None, items[0].page, items[-1].page, items)]
        blocks: List[Block] = []
        if sections[0].start_idx > 0:
            prefix_items = items[:sections[0].start_idx]
            if prefix_items:
                blocks.append(Block(None, None, prefix_items[0].page, prefix_items[-1].page, prefix_items))
        for sec in sections:
            block_items = items[sec.start_idx: sec.end_idx + 1]
            blocks.append(Block(sec.number, sec.title, block_items[0].page, block_items[-1].page, block_items))
        return blocks


class Accumulator:
    def __init__(self, section_number: Optional[str], section_title: Optional[str], page: int):
        self.section_number = section_number
        self.section_title = section_title
        self.page_start = page
        self.page_end = page

        self.test_name: Optional[str] = None
        self.criteria: List[str] = []
        self.result: List[str] = []
        self.method: List[str] = []
        self.remarks: List[str] = []

        self.raw: List[str] = []
        self.source_types: set = set()

    def set_page(self, page: int):
        self.page_end = page

    def add_raw(self, text: str):
        t = clean_text(text)
        if not t or should_skip_raw_line(t):
            return
        if t not in self.raw:
            self.raw.append(t)

    def add_field(self, field_name: str, value: str):
        v = clean_text(value)
        if not v or is_noise(v) or v in {":", "："}:
            return
        if field_name == "test_name":
            name = normalize_test_name(v)
            if name:
                self.test_name = name
        elif field_name == "criteria":
            if v not in self.criteria:
                self.criteria.append(v)
        elif field_name == "result":
            if v not in self.result:
                self.result.append(v)
        elif field_name == "method":
            if v not in self.method:
                self.method.append(v)
        elif field_name == "remarks":
            if v not in self.remarks:
                self.remarks.append(v)

    def has_payload(self) -> bool:
        return bool(self.test_name or self.criteria or self.result or self.method or self.remarks)

    def has_real_payload(self) -> bool:
        return bool(self.criteria or self.result or self.method or self.remarks)

    def has_only_test_name(self) -> bool:
        return bool(self.test_name and not self.criteria and not self.result and not self.method and not self.remarks)

    def to_record(self) -> Record:
        return Record(
            section_number=self.section_number,
            section_title=self.section_title,
            test_name=self.test_name,
            criteria="\n".join(self.criteria) if self.criteria else None,
            result="\n".join(self.result) if self.result else None,
            method="\n".join(self.method) if self.method else None,
            remarks="\n".join(self.remarks) if self.remarks else None,
            page_start=self.page_start,
            page_end=self.page_end,
            source_types=sorted(self.source_types),
            raw_text="\n".join(self.raw).strip(),
        )


class RecordExtractor:
    def run(self, blocks: List[Block]) -> List[Record]:
        records: List[Record] = []
        for block in blocks:
            records.extend(self._extract_block(block))
        return self._postprocess(records)

    def _extract_block(self, block: Block) -> List[Record]:
        out: List[Record] = []
        current = Accumulator(block.section_number, block.section_title, block.page_start)
        last_kv_field: Optional[str] = None
        last_kv_value: Optional[str] = None
        parent_group_test: Optional[str] = None
        pending_parent_page: Optional[int] = None

        if is_probable_test_name(block.section_title or ""):
            current.test_name = normalize_test_name(block.section_title or "")

        def flush_current():
            nonlocal current
            if current.has_payload():
                if not (current.has_only_test_name() and current.test_name in CFG.parent_group_names):
                    out.append(current.to_record())
            current = Accumulator(block.section_number, block.section_title, current.page_end)

        for item in block.items:
            if item.type == "table_row":
                rec = self._record_from_table_row(item, block)
                if rec:
                    if current.has_payload():
                        flush_current()
                    out.append(rec)
                    parent_group_test = None
                    pending_parent_page = None
                    last_kv_field = None
                    last_kv_value = None
                continue

            if item.type == "heading":
                title = clean_text(item.meta.get("title", ""))
                if is_probable_test_name(title):
                    if current.has_payload():
                        flush_current()
                    current = Accumulator(block.section_number, block.section_title, item.page)
                    current.add_field("test_name", title)
                    current.source_types.add("heading_test_name")
                    current.add_raw(title)
                    parent_group_test = None
                    pending_parent_page = None
                    last_kv_field = "test_name"
                    last_kv_value = current.test_name
                continue

            if item.type == "kv":
                key = item.meta.get("key", "")
                value = clean_text(item.meta.get("value", ""))
                field_name = canonical_field(key)
                if not field_name or not value:
                    continue

                if parent_group_test and current.has_payload() and current.test_name == parent_group_test and not current.has_real_payload():
                    # still waiting for a subtest; keep parent suspended
                    pass

                if field_name == "test_name" and is_probable_test_name(value):
                    if current.has_payload():
                        flush_current()
                    current = Accumulator(block.section_number, block.section_title, item.page)
                    current.add_field("test_name", value)
                    current.source_types.add("kv_test_name")
                    current.add_raw(f"{key}: {value}")
                    parent_group_test = None
                    pending_parent_page = None
                    last_kv_field = "test_name"
                    last_kv_value = current.test_name
                    continue

                # if parent is pending and a field appears before subtest, attach to parent only if no subtest model exists
                if parent_group_test and current.test_name == parent_group_test and is_subtest_name(last_kv_value or ""):
                    pass

                current.set_page(item.page)
                current.source_types.add("kv")
                current.add_raw(f"{key}: {value}")
                current.add_field(field_name, value)
                last_kv_field = field_name
                last_kv_value = value
                continue

            if item.type in {"line", "table_line"}:
                line = clean_text(item.text or "")
                if not line:
                    continue
                current.set_page(item.page)

                # ignore noisy table lines and footers before touching raw_text
                if item.type == "table_line" and should_skip_table_line(line):
                    continue
                if should_skip_raw_line(line):
                    continue

                # parent group start: hold state, do not finalize as standalone record
                if line in CFG.parent_group_names:
                    if current.has_payload():
                        flush_current()
                    current = Accumulator(block.section_number, block.section_title, item.page)
                    current.add_field("test_name", line)
                    current.source_types.add("parent_group")
                    parent_group_test = line
                    pending_parent_page = item.page
                    last_kv_field = "test_name"
                    last_kv_value = line
                    continue

                # subtest under parent group
                if parent_group_test and is_subtest_name(line):
                    if current.has_payload() and not (current.has_only_test_name() and current.test_name == parent_group_test):
                        flush_current()
                    current = Accumulator(block.section_number, block.section_title, item.page)
                    current.add_field("test_name", f"{parent_group_test} - {line}")
                    current.source_types.add("subtest_name")
                    current.add_raw(line)
                    last_kv_field = "test_name"
                    last_kv_value = line
                    continue

                # if another real test starts, clear parent state
                if is_probable_test_name(line) and parent_group_test and not is_subtest_name(line):
                    parent_group_test = None
                    pending_parent_page = None
                    if current.has_payload():
                        flush_current()
                    current = Accumulator(block.section_number, block.section_title, item.page)
                    current.add_field("test_name", line)
                    current.source_types.add("line_test_name")
                    current.add_raw(line)
                    last_kv_field = "test_name"
                    last_kv_value = line
                    continue

                inline = self._parse_inline_field(line)
                if inline:
                    field_name, value = inline
                    if field_name == "test_name" and is_probable_test_name(value):
                        if current.has_payload():
                            flush_current()
                        current = Accumulator(block.section_number, block.section_title, item.page)
                        current.add_field("test_name", value)
                        current.source_types.add("line_test_name")
                        current.add_raw(line)
                        parent_group_test = None
                        pending_parent_page = None
                        last_kv_field = "test_name"
                        last_kv_value = current.test_name
                        continue

                    current.source_types.add(item.type)
                    current.add_raw(line)
                    current.add_field(field_name, value)
                    last_kv_field = field_name
                    last_kv_value = value
                    continue

                if last_kv_field in {"method", "criteria", "result", "remarks"} and clean_text(last_kv_value or "") == line:
                    continue

                if self._looks_like_continuation_value(line, current, last_kv_field, last_kv_value):
                    continue

                if is_probable_test_name(line):
                    if current.has_payload():
                        flush_current()
                    current = Accumulator(block.section_number, block.section_title, item.page)
                    current.add_field("test_name", line)
                    current.source_types.add("line_test_name")
                    current.add_raw(line)
                    parent_group_test = None
                    pending_parent_page = None
                    last_kv_field = "test_name"
                    last_kv_value = line
                    continue

                # keep harmless auxiliary lines in raw only if currently inside a real test
                if current.has_payload():
                    current.source_types.add(item.type)
                    current.add_raw(line)
                last_kv_field = None
                last_kv_value = None

        if current.has_payload():
            if not (current.has_only_test_name() and current.test_name in CFG.parent_group_names):
                out.append(current.to_record())

        return out

    def _looks_like_continuation_value(self, line: str, current: Accumulator, last_kv_field: Optional[str], last_kv_value: Optional[str]) -> bool:
        line = clean_text(line)
        if not line:
            return False
        if last_kv_field in {"method", "criteria", "result", "remarks"} and clean_text(last_kv_value or "") == line:
            return True
        if line in {clean_text(x) for x in current.method + current.criteria + current.result + current.remarks}:
            return True
        if re.match(r"^시험(일자|기간)\s+", line):
            return True
        if "|" in line and any(tok in line for tok in ["시험방법", "시험기준", "시험결과", "비고", "참고"]):
            return True
        return False

    def _record_from_table_row(self, item: Item, block: Block) -> Optional[Record]:
        fields = item.meta.get("fields", {})
        raw_row = item.meta.get("raw_row", [])
        test_name = normalize_test_name(clean_text(fields.get("test_name", "")))
        if not test_name or not is_probable_test_name(test_name):
            return None
        return Record(
            section_number=block.section_number,
            section_title=block.section_title,
            test_name=test_name,
            criteria=fields.get("criteria"),
            result=fields.get("result"),
            method=fields.get("method"),
            remarks=fields.get("remarks"),
            page_start=item.page,
            page_end=item.page,
            source_types=["table_row"],
            raw_text=join_nonempty(raw_row),
        )

    def _parse_inline_field(self, line: str) -> Optional[Tuple[str, str]]:
        m = re.match(r"^\s*([^:：]{1,40})\s*[:：]\s*(.+?)\s*$", line)
        if m:
            field_name = canonical_field(m.group(1))
            if field_name:
                return field_name, clean_text(m.group(2))
        labels = CFG.test_name_labels + CFG.criteria_labels + CFG.result_labels + CFG.method_labels + CFG.remarks_labels
        for label in sorted(set(labels), key=len, reverse=True):
            if line.startswith(label + " "):
                value = clean_text(line[len(label):])
                field_name = canonical_field(label)
                if field_name and value:
                    return field_name, value
        return None

    def _postprocess(self, records: List[Record]) -> List[Record]:
        cleaned: List[Record] = []
        for rec in records:
            rec.test_name = normalize_test_name(rec.test_name or "")
            if not is_probable_test_name(rec.test_name):
                continue
            rec.criteria = normalize_multiline_field(rec.criteria)
            rec.result = normalize_multiline_field(rec.result)
            rec.method = normalize_multiline_field(rec.method)
            rec.remarks = normalize_multiline_field(rec.remarks)
            rec.raw_text = clean_text(rec.raw_text)
            cleaned.append(rec)

        uniq_map: Dict[Tuple, Record] = {}
        ordered_keys: List[Tuple] = []
        for rec in cleaned:
            key = (rec.section_number, rec.section_title, rec.test_name, rec.criteria, rec.result, rec.method, rec.remarks, rec.page_start, rec.page_end)
            if key not in uniq_map:
                uniq_map[key] = rec
                ordered_keys.append(key)
            else:
                prev = uniq_map[key]
                prev.source_types = sorted(set(prev.source_types) | set(rec.source_types))
                prev.raw_text = self._merge_text(prev.raw_text, rec.raw_text)

        cleaned = [uniq_map[k] for k in ordered_keys]

        grouped: Dict[Tuple, List[Record]] = {}
        for rec in cleaned:
            key = (rec.section_number, rec.section_title, rec.test_name, rec.page_start, rec.page_end)
            grouped.setdefault(key, []).append(rec)

        merged_records: List[Record] = []
        for _, recs in grouped.items():
            recs = sorted(recs, key=lambda x: fields_payload_count(x), reverse=True)
            base = recs[0]
            for extra in recs[1:]:
                base.criteria = self._merge_field(base.criteria, extra.criteria)
                base.result = self._merge_field(base.result, extra.result)
                base.method = self._merge_field(base.method, extra.method)
                base.remarks = self._merge_field(base.remarks, extra.remarks)
                base.raw_text = self._merge_text(base.raw_text, extra.raw_text)
                base.source_types = sorted(set(base.source_types) | set(extra.source_types))
            merged_records.append(base)

        final_records: List[Record] = []
        child_prefixes = {rec.test_name.split(" - ")[0] for rec in merged_records if " - " in (rec.test_name or "")}
        for rec in merged_records:
            if rec.test_name in child_prefixes and fields_payload_count(rec) == 1:
                continue
            final_records.append(rec)

        return final_records

    def _merge_field(self, a: Optional[str], b: Optional[str]) -> Optional[str]:
        vals = []
        if a:
            vals.extend([clean_text(x) for x in str(a).split("\n") if clean_text(x)])
        if b:
            vals.extend([clean_text(x) for x in str(b).split("\n") if clean_text(x)])
        vals = uniq_keep_order(vals)
        return "\n".join(vals) if vals else None

    def _merge_text(self, a: Optional[str], b: Optional[str]) -> str:
        vals = []
        if a:
            vals.extend([clean_text(x) for x in str(a).split("\n") if clean_text(x)])
        if b:
            vals.extend([clean_text(x) for x in str(b).split("\n") if clean_text(x)])
        vals = uniq_keep_order(vals)
        return "\n".join(vals)


class Pipeline:
    def run(self, pdf_path: str, output_dir: str) -> Dict[str, Any]:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        pages = PDFReader(pdf_path).read()
        items = Normalizer().run(pages)
        sections = SectionBuilder().run(items)
        blocks = BlockBuilder().run(items, sections)
        records = RecordExtractor().run(blocks)

        if CFG.save_intermediate:
            self._save_json([asdict(x) for x in pages], out_dir / "01_raw_pages.json")
            self._save_json([asdict(x) for x in items], out_dir / "02_normalized_items.json")
            self._save_json([asdict(x) for x in sections], out_dir / "03_sections.json")
            self._save_json([self._block_to_dict(x) for x in blocks], out_dir / "04_blocks.json")

        self._save_json([asdict(x) for x in records], out_dir / "05_records.json")
        summary = {
            "pdf_path": str(pdf_path),
            "total_pages": len(pages),
            "total_items": len(items),
            "total_sections": len(sections),
            "total_blocks": len(blocks),
            "total_records": len(records),
            "output_dir": str(out_dir),
        }
        self._save_json(summary, out_dir / "summary.json")
        return summary

    def _save_json(self, obj: Any, path: Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    def _block_to_dict(self, block: Block) -> Dict[str, Any]:
        return {
            "section_number": block.section_number,
            "section_title": block.section_title,
            "page_start": block.page_start,
            "page_end": block.page_end,
            "items": [asdict(x) for x in block.items],
        }


def main():
    parser = argparse.ArgumentParser(description="PDF test extractor")
    parser.add_argument("--pdf", required=True, help="Input PDF path")
    parser.add_argument("--out", required=True, help="Output directory")
    args = parser.parse_args()

    summary = Pipeline().run(args.pdf, args.out)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
