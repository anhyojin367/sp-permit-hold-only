from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz
from PIL import Image

try:
    import pytesseract
except Exception:
    pytesseract = None

from .config import OCR_LANG, OCR_TEXT_THRESHOLD
from .utils import clean_text


@dataclass
class PageText:
    page_num: int
    text: str
    source: str


def _page_to_pil(page: fitz.Page, zoom: float = 2.0) -> Image.Image:
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def extract_page_texts(pdf_path: Path) -> list[PageText]:
    doc = fitz.open(pdf_path)
    pages: list[PageText] = []
    try:
        for idx in range(len(doc)):
            page = doc.load_page(idx)
            native = clean_text(page.get_text("text"))
            if len(native) >= OCR_TEXT_THRESHOLD:
                pages.append(PageText(page_num=idx + 1, text=native, source="native"))
                continue

            if pytesseract is None:
                pages.append(PageText(page_num=idx + 1, text=native, source="native"))
                continue

            try:
                pil_img = _page_to_pil(page)
                ocr_text = clean_text(pytesseract.image_to_string(pil_img, lang=OCR_LANG))
                pages.append(PageText(page_num=idx + 1, text=ocr_text, source="ocr"))
            except Exception:
                pages.append(PageText(page_num=idx + 1, text=native, source="native"))
    finally:
        doc.close()
    return pages
