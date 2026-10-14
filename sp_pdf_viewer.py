# -*- coding: utf-8 -*-
"""Helpers for finding and rendering the manufacturing summary page of a PDF."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image
from pypdf import PdfReader


FLOWCHART_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("제조요약도", 12),
    ("제조 요약도", 12),
    ("제조요약정보", 11),
    ("제조 요약 정보", 11),
    ("제조공정도", 10),
    ("제조 공정도", 10),
    ("제조공정", 6),
    ("제조 공정", 6),
    ("제조방법", 4),
    ("제조 방법", 4),
    ("manufacturing flow", 8),
    ("manufacturing process", 6),
    ("flow chart", 6),
    ("flowchart", 6),
)


@dataclass(frozen=True)
class FlowchartPage:
    page_index: int
    page_count: int
    score: int
    reason: str

    @property
    def page_number(self) -> int:
        return self.page_index + 1


def get_pdf_page_count(pdf_path: str | Path) -> int:
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        return len(document)
    finally:
        document.close()


def find_manufacturing_summary_page(pdf_path: str | Path) -> FlowchartPage:
    """Find the most likely manufacturing summary page using PDF text."""

    path = Path(pdf_path)
    reader = PdfReader(str(path))
    page_count = len(reader.pages)
    if page_count == 0:
        return FlowchartPage(page_index=0, page_count=0, score=0, reason="empty PDF")

    best_index = 0
    best_score = 0
    best_hits: list[str] = []

    for index, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        score, hits = _score_page(text)
        if score > best_score:
            best_index = index
            best_score = score
            best_hits = hits

    if best_score <= 0:
        return FlowchartPage(
            page_index=0,
            page_count=page_count,
            score=0,
            reason="제조요약도 키워드를 찾지 못해 1페이지를 표시합니다.",
        )

    return FlowchartPage(
        page_index=best_index,
        page_count=page_count,
        score=best_score,
        reason=", ".join(best_hits[:4]),
    )


def render_pdf_page(pdf_path: str | Path, page_index: int, scale: float = 2.0) -> Image.Image:
    """Render a single PDF page to a PIL image."""

    document = pdfium.PdfDocument(str(pdf_path))
    try:
        page_count = len(document)
        if page_count == 0:
            raise ValueError("PDF has no pages.")
        safe_index = min(max(page_index, 0), page_count - 1)
        page = document[safe_index]
        try:
            bitmap = page.render(scale=scale)
            return bitmap.to_pil()
        finally:
            page.close()
    finally:
        document.close()


def render_manufacturing_flowchart(
    pdf_path: str | Path,
    scale: float = 2.3,
) -> tuple[FlowchartPage, Image.Image]:
    """Render only the flowchart area from the manufacturing summary page."""

    flowchart_page = find_manufacturing_summary_page(pdf_path)
    page_image = render_pdf_page(pdf_path, flowchart_page.page_index, scale=scale)
    return flowchart_page, crop_flowchart_area(page_image)


def crop_flowchart_area(image: Image.Image) -> Image.Image:
    """Crop page header, section title, and footer away from a rendered page."""

    rgb = image.convert("RGB")
    grayscale = rgb.convert("L")
    width, height = grayscale.size
    if width == 0 or height == 0:
        return rgb

    ink_mask = grayscale.point(lambda pixel: 255 if pixel < 235 else 0)
    ink_data = ink_mask.tobytes()

    row_counts = [
        ink_data[y * width : (y + 1) * width].count(255)
        for y in range(height)
    ]

    scan_top = int(height * 0.20)
    line_threshold = max(30, int(width * 0.10))
    first_line_rows = [
        y for y in range(scan_top, height)
        if row_counts[y] >= line_threshold
    ]

    if first_line_rows:
        top = max(0, first_line_rows[0] - int(height * 0.03))
    else:
        top = int(height * 0.34)

    strong_bottom_threshold = max(24, int(width * 0.055))
    bottom_line_rows = [
        y for y in range(top, height)
        if row_counts[y] >= strong_bottom_threshold
    ]
    if bottom_line_rows:
        bottom = min(height, bottom_line_rows[-1] + int(height * 0.012))
    else:
        weak_threshold = max(8, int(width * 0.012))
        content_rows = [
            y for y in range(top, height)
            if row_counts[y] >= weak_threshold
        ]
        bottom = min(height, content_rows[-1] + int(height * 0.035)) if content_rows else int(height * 0.95)

    content_mask = ink_mask.crop((0, top, width, bottom))
    bbox = content_mask.getbbox()
    if bbox is None:
        return rgb.crop((0, top, width, bottom))

    left, _, right, _ = bbox
    x_padding = int(width * 0.035)
    y_padding = int(height * 0.006)
    crop_box = (
        max(0, left - x_padding),
        max(0, top - y_padding),
        min(width, right + x_padding),
        min(height, bottom + y_padding),
    )
    return rgb.crop(crop_box)


def _score_page(text: str) -> tuple[int, list[str]]:
    normalized = " ".join(text.split()).casefold()
    if not normalized:
        return 0, []

    score = 0
    hits: list[str] = []
    for keyword, weight in FLOWCHART_KEYWORDS:
        count = normalized.count(keyword.casefold())
        if count:
            score += count * weight
            hits.append(keyword)
    return score, hits
