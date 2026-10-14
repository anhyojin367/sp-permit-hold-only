from __future__ import annotations

from pathlib import Path

import fitz


def render_first_page(pdf_path: Path, out_path: Path, zoom: float = 2.0) -> Path:
    doc = fitz.open(pdf_path)
    try:
        page = doc.load_page(0)
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        pix.save(out_path)
    finally:
        doc.close()
    return out_path
