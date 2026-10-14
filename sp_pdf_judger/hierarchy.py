from __future__ import annotations

import re

from .schemas import Evaluation, ExtractedRecord, TreeNode
from .utils import clean_text, slugify


MEASUREMENT_OR_CRITERIA_KEYWORDS = [
    "이상", "이하", "초과", "미만",
    "CFU", "cfu",
    "mL", "ml",
    "mg", "g", "kg",
    "μg", "µg", "ug", "ng",
    "IU", "EU", "LD", "log",
    "%", "cells", "cell", "mOsm", "Osm", "nm",
    "≥", "≤", ">", "<", "~",
]


UNIT_TOKEN_RE = re.compile(
    r"(?<![A-Za-z])("
    r"EU/mL|IU/mL|CFU/mL|cells/mL|"
    r"CFU|cfu|mL|ml|L|mg|g|kg|μg|µg|ug|ng|"
    r"IU|EU|LD|log|cells|cell|mOsm|Osm|nm|%"
    r")(?![A-Za-z])"
)


def _is_valid_section_number(section_number: str | None) -> bool:
    section_number = clean_text(section_number)

    if not section_number:
        return False

    if not re.fullmatch(r"\d+(?:\.\d+)*", section_number):
        return False

    parts = section_number.split(".")

    if parts[0] == "0":
        return False

    # 1.0, 7.0, 0.5 같은 시험기준 수치가 섹션 번호로 들어오는 것 방지
    if any(part == "0" for part in parts[1:]):
        return False

    return True


def _looks_like_criteria_text(text: str | None) -> bool:
    text = clean_text(text)

    if not text:
        return False

    # 비교어가 있으면 시험기준일 가능성이 높음
    if any(k in text for k in ["이상", "이하", "초과", "미만", "≥", "≤", ">", "<", "~"]):
        return True

    # 1.0 x 10^6, 3.00 x 10⁶ 같은 과학적 표기
    if re.search(r"\d+(?:\.\d+)?\s*[xX]\s*10", text):
        return True

    # 숫자와 단위가 같이 있을 때만 기준/측정값으로 판단
    # E.coLi의 L처럼 단어 안에 들어간 문자는 단위로 보지 않음
    if re.search(r"\d", text) and UNIT_TOKEN_RE.search(text):
        return True

    return False


def _is_manufacturing_summary_section(
    section_number: str | None,
    section_title: str | None,
) -> bool:
    section_number = clean_text(section_number)
    section_title = clean_text(section_title)
    compact = re.sub(r"\s+", "", f"{section_number} {section_title}")

    return section_number == "1.2" or "제조요약도" in compact


def _section_sort_key(section_number: str) -> tuple:
    parts: list[tuple[int, int | str]] = []

    for token in clean_text(section_number).split("."):
        try:
            parts.append((0, int(token)))
        except ValueError:
            parts.append((1, token))

    return tuple(parts)


def _child_sort_key(node: TreeNode) -> tuple:
    if node.node_type == "section":
        return (
            0,
            node.order_idx if node.order_idx else 999999,
            _section_sort_key(node.section_number or "9999"),
        )

    return (
        1,
        node.order_idx if node.order_idx else 999999,
        clean_text(node.title),
    )


def _make_section_title(section_number: str, section_title: str | None) -> str:
    section_number = clean_text(section_number)
    section_title = clean_text(section_title)

    if section_title:
        return f"{section_number} {section_title}"

    return section_number


def _ensure_section_node(
    section_nodes: dict[str, TreeNode],
    section_number: str | None,
    section_title: str | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
    order_idx: int = 0,
) -> TreeNode | None:
    section_number = clean_text(section_number)
    section_title = clean_text(section_title)

    if not _is_valid_section_number(section_number):
        return None

    if _is_manufacturing_summary_section(section_number, section_title):
        return None

    if _looks_like_criteria_text(f"{section_number} {section_title}"):
        return None

    if section_number not in section_nodes:
        section_nodes[section_number] = TreeNode(
            key=f"section-{section_number}",
            title=_make_section_title(section_number, section_title),
            level=len(section_number.split(".")),
            section_number=section_number,
            section_title=section_title or None,
            node_type="section",
            page_start=page_start,
            page_end=page_end,
            order_idx=order_idx,
        )
    else:
        node = section_nodes[section_number]

        if section_title and not node.section_title:
            node.section_title = section_title
            node.title = _make_section_title(section_number, section_title)

        if page_start is not None and (node.page_start is None or page_start < node.page_start):
            node.page_start = page_start

        if page_end is not None and (node.page_end is None or page_end > node.page_end):
            node.page_end = page_end

        if order_idx and (not node.order_idx or order_idx < node.order_idx):
            node.order_idx = order_idx

    return section_nodes[section_number]


def _add_section_path(
    section_nodes: dict[str, TreeNode],
    record: ExtractedRecord,
) -> None:
    """
    json추출.py가 만든 section_path를 그대로 사용해서 섹션 트리를 만든다.
    이 함수가 핵심이다.
    """
    if not record.section_path:
        return

    for item in record.section_path:
        number = clean_text(item.get("number"))
        title = clean_text(item.get("title"))

        _ensure_section_node(
            section_nodes=section_nodes,
            section_number=number,
            section_title=title,
            page_start=record.page_start,
            page_end=record.page_end,
            order_idx=record.order_idx,
        )


def _add_fallback_section_path(
    section_nodes: dict[str, TreeNode],
    record: ExtractedRecord,
) -> None:
    """
    section_path가 없는 예전 JSON을 위한 fallback.
    """
    section_number = clean_text(record.section_number)
    section_title = clean_text(record.section_title)

    if not _is_valid_section_number(section_number):
        return

    if _is_manufacturing_summary_section(section_number, section_title):
        return

    parts = section_number.split(".")

    for i in range(1, len(parts) + 1):
        prefix = ".".join(parts[:i])

        title = section_title if i == len(parts) else None

        _ensure_section_node(
            section_nodes=section_nodes,
            section_number=prefix,
            section_title=title,
            page_start=record.page_start,
            page_end=record.page_end,
            order_idx=record.order_idx,
        )


def _link_sections(section_nodes: dict[str, TreeNode]) -> list[TreeNode]:
    for node in section_nodes.values():
        node.children = []

    roots: list[TreeNode] = []

    for section_number in sorted(section_nodes.keys(), key=_section_sort_key):
        node = section_nodes[section_number]

        parts = section_number.split(".")
        parent_number = ".".join(parts[:-1]) if len(parts) > 1 else ""
        parent = section_nodes.get(parent_number)

        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)

    return roots


def _content_title_for_record(record: ExtractedRecord) -> str:
    content_label = clean_text(record.content_label)
    section_title = clean_text(record.section_title)

    if content_label and content_label != section_title:
        return content_label

    return "세부정보"


def _content_text_for_record(record: ExtractedRecord) -> str:
    return clean_text(record.content or record.raw_text or "")


def _prune_empty_sections(nodes: list[TreeNode]) -> list[TreeNode]:
    out: list[TreeNode] = []

    for node in nodes:
        if node.node_type == "section":
            node.children = _prune_empty_sections(node.children)

            if node.children:
                out.append(node)
        else:
            out.append(node)

    return out


def build_document_tree(
    records: list[ExtractedRecord],
    evaluations: list[Evaluation],
) -> list[TreeNode]:
    evaluation_by_position = {
        ev.record_position: ev
        for ev in evaluations
        if isinstance(ev.record_position, int)
    }
    evaluation_by_order = {
        ev.order_idx: ev
        for ev in evaluations
    }

    section_nodes: dict[str, TreeNode] = {}

    # 1. section_path 기반으로 정확한 섹션 노드 생성
    for record in records:
        if record.section_path:
            _add_section_path(section_nodes, record)
        else:
            _add_fallback_section_path(section_nodes, record)

    roots = _link_sections(section_nodes)

    # 2. heading은 섹션 생성에만 사용하고 카드로 렌더링하지 않음
    # 3. content/test는 반드시 자기 section_number와 정확히 같은 섹션 아래에만 붙임
    for record_position, record in enumerate(records):
        record_type = clean_text(record.record_type)

        if record_type == "heading":
            continue

        if _is_manufacturing_summary_section(record.section_number, record.section_title):
            continue

        section_number = clean_text(record.section_number)

        if not _is_valid_section_number(section_number):
            continue

        parent = section_nodes.get(section_number)

        if parent is None:
            continue

        if record_type == "content":
            content_text = _content_text_for_record(record)

            if not content_text:
                continue

            # section title 자체를 중복 카드로 만들지 않음
            if content_text.strip() == f"{section_number} {clean_text(record.section_title)}".strip():
                continue

            parent.children.append(
                TreeNode(
                    key=f"content-{record.order_idx}-{section_number}",
                    title=_content_title_for_record(record),
                    level=parent.level + 1,
                    section_number=record.section_number,
                    section_title=record.section_title,
                    node_type="content",
                    page_start=record.page_start,
                    page_end=record.page_end,
                    info_lines=[content_text],
                    source_record=record,
                    order_idx=record.order_idx,
                )
            )

        elif record_type == "test":
            ev = evaluation_by_position.get(record_position)
            if ev is None:
                ev = evaluation_by_order.get(record.order_idx)

            tree_identity = (
                ev.record_position
                if ev is not None and ev.record_position is not None
                else record.order_idx
            )

            title = (
                clean_text(record.test_name)
                or clean_text(record.content_label)
                or clean_text(record.section_title)
                or "시험 항목"
            )

            parent.children.append(
                TreeNode(
                    key=f"test-{tree_identity}-{section_number}-{slugify(title)}",
                    title=title,
                    level=parent.level + 1,
                    section_number=record.section_number,
                    section_title=record.section_title,
                    node_type="test",
                    status=ev.final_status if ev and ev.comparison_completed else None,
                    page_start=record.page_start,
                    page_end=record.page_end,
                    evaluation=ev,
                    source_record=record,
                    order_idx=record.order_idx,
                )
            )

    def finalize(node: TreeNode) -> None:
        for child in node.children:
            finalize(child)

        node.children.sort(key=_child_sort_key)

        # 정보 섹션에 빨간/초록 점이 뜨는 문제 방지
        if node.node_type == "section":
            node.status = None

    for root in roots:
        finalize(root)

    roots.sort(key=_child_sort_key)

    return _prune_empty_sections(roots)
