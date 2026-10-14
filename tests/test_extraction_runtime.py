import importlib
import json
import sys
from types import SimpleNamespace

import pytest

import sp_pdf_judger.extraction_runtime as extraction_runtime
from sp_pdf_judger.extraction_runtime import (
    extraction_backend, extraction_fingerprint, runtime_manifest, validate_extraction_report,
)


class _FakePdfPage:
    def __init__(self, lines):
        self.images = []
        self.curves = []
        self.width = 595
        self._words = []
        for line_number, line in enumerate(lines):
            x0 = 40.0
            top = 40.0 + line_number * 20.0
            for value in line.split():
                width = max(float(len(value) * 6), 6.0)
                self._words.append(
                    {
                        "text": value,
                        "x0": x0,
                        "x1": x0 + width,
                        "top": top,
                        "bottom": top + 10.0,
                    }
                )
                x0 += width + 8.0

    def extract_words(self, **_kwargs):
        return list(self._words)

    def find_tables(self):
        return []

    def close(self):
        return None


class _FakePdf:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _install_fake_pdf(monkeypatch, module, page_lines):
    pages = [_FakePdfPage(lines) for lines in page_lines]
    monkeypatch.setattr(module.pdfplumber, "open", lambda _path: _FakePdf(pages))


def test_cid_page_uses_ocr_without_touching_readable_pages(tmp_path, monkeypatch):
    module = importlib.import_module("json추출")
    monkeypatch.setenv("SP_TABLE_BACKEND", "pdfplumber")
    _install_fake_pdf(
        monkeypatch,
        module,
        [
            ["정상 첫 페이지"],
            [
                "시험기준 2.00 x 10(cid:2874) cells/mL 이상",
                "시험결과 3.00 x 10(cid:2874) cells/mL",
            ],
            ["정상 마지막 페이지"],
        ],
    )
    calls = []

    def selective_ocr(_pdf_path, page_numbers):
        calls.append(list(page_numbers))
        return (
            {
                2: ((
                    "시험기준 2.00 x 106 cells/mL 이상\n"
                    "시험결과 3.00 x 106 cells/mL"
                ), "windows_ocr")
            },
            [],
        )

    monkeypatch.setattr(module, "ocr_selected_sp_pages", selective_ocr, raising=False)
    pdf_path = tmp_path / "three-pages.pdf"
    pdf_path.write_bytes(b"fake")
    reader = module.PDFReader(pdf_path)

    pages = reader.read()

    assert calls == [[2]]
    assert pages[0].text == "정상 첫 페이지"
    assert pages[2].text == "정상 마지막 페이지"
    assert "2.00 x 10^6 cells/mL" in pages[1].text
    assert "3.00 x 10^6 cells/mL" in pages[1].text
    assert "(cid:" not in pages[1].text
    assert reader.page_audit[1]["native_unreadable"] is True
    assert reader.page_audit[1]["unreadable"] is False
    assert reader.page_audit[1]["ocr_attempted"] is True
    assert reader.page_audit[1]["ocr_recovered"] is True
    assert reader.page_audit[1]["ocr_source"] == "windows_ocr"


def test_unresolved_cid_after_ocr_remains_blocking(tmp_path, monkeypatch):
    module = importlib.import_module("json추출")
    monkeypatch.setenv("SP_TABLE_BACKEND", "pdfplumber")
    _install_fake_pdf(
        monkeypatch,
        module,
        [["시험결과 3.00 x 10(cid:2874) cells/mL"]],
    )
    calls = []

    def unusable_ocr(_pdf_path, page_numbers):
        calls.append(list(page_numbers))
        return ({1: ("시험결과를 판독하지 못함", "windows_ocr")}, [])

    monkeypatch.setattr(module, "ocr_selected_sp_pages", unusable_ocr, raising=False)
    pdf_path = tmp_path / "unresolved.pdf"
    pdf_path.write_bytes(b"fake")
    reader = module.PDFReader(pdf_path)

    reader.read()

    assert calls == [[1]]
    assert reader.page_audit[0]["ocr_attempted"] is True
    assert reader.page_audit[0]["ocr_recovered"] is False
    blocking = [row for row in reader.page_audit if row["unreadable"]]
    with pytest.raises(RuntimeError, match="1쪽"):
        validate_extraction_report({"blocking_issues": blocking})


def test_contradictory_ocr_exponents_remain_blocking_and_do_not_mutate_page(
    tmp_path, monkeypatch
):
    module = importlib.import_module("json추출")
    monkeypatch.setenv("SP_TABLE_BACKEND", "pdfplumber")
    native = "시험결과 3.00 x 10(cid:2874) cells/mL"
    _install_fake_pdf(monkeypatch, module, [[native]])
    monkeypatch.setattr(
        module,
        "ocr_selected_sp_pages",
        lambda *_args: ({1: (
            "시험결과 3.00 x 106 cells/mL\n"
            "재확인 3.00 x 108 cells/mL",
            "windows_ocr",
        )}, []),
    )
    pdf_path = tmp_path / "contradictory.pdf"
    pdf_path.write_bytes(b"fake")
    reader = module.PDFReader(pdf_path)

    pages = reader.read()

    assert pages[0].text == native
    assert pages[0].ordered_elements[0].text == native
    assert reader.page_audit[0]["unreadable"] is True
    assert reader.page_audit[0]["ocr_recovered"] is False


def test_table_cid_keeps_all_structured_content_unmodified_and_blocks(monkeypatch):
    module = importlib.import_module("json추출")
    native = "시험결과 3.00 x 10(cid:2874) cells/mL"
    table_value = "표 결과 3.20 x 10(cid:2876) PFU/mL"
    element = module.OrderedElement("text", native, 10.0)
    page = module.RawPage(1, native, [[[table_value]]], [element], native)
    reader = module.PDFReader("not-opened.pdf")
    reader.page_audit = [{
        "page": 1,
        "native_unreadable": True,
        "unreadable": True,
        "ocr_attempted": False,
        "ocr_recovered": False,
    }]
    monkeypatch.setattr(
        module,
        "ocr_selected_sp_pages",
        lambda *_args: ({1: (
            "시험결과 3.00 x 106 cells/mL",
            "windows_ocr",
        )}, []),
    )

    recovered = reader._recover_unreadable_text_pages([page])

    assert recovered[0].text == native
    assert recovered[0].ordered_elements[0].text == native
    assert recovered[0].tables == [[[table_value]]]
    assert recovered[0].native_text == native
    assert reader.page_audit[0]["unreadable"] is True
    assert reader.page_audit[0]["ocr_recovered"] is False


def test_readable_pages_do_not_invoke_sp_ocr(tmp_path, monkeypatch):
    module = importlib.import_module("json추출")
    monkeypatch.setenv("SP_TABLE_BACKEND", "pdfplumber")
    _install_fake_pdf(monkeypatch, module, [["정상 텍스트"], ["another native page"]])

    def unexpected_ocr(*_args):
        raise AssertionError("readable pages must stay on the native fast path")

    monkeypatch.setattr(module, "ocr_selected_sp_pages", unexpected_ocr, raising=False)
    pdf_path = tmp_path / "native.pdf"
    pdf_path.write_bytes(b"fake")
    reader = module.PDFReader(pdf_path)

    pages = reader.read()

    assert [page.text for page in pages] == ["정상 텍스트", "another native page"]
    assert [row["ocr_attempted"] for row in reader.page_audit] == [False, False]


def test_backend_is_explicit_not_inferred_from_installed_packages(monkeypatch):
    monkeypatch.delenv("SP_TABLE_BACKEND", raising=False)
    assert extraction_backend() == "hybrid"
    baseline = extraction_fingerprint()
    monkeypatch.setenv("SP_TABLE_BACKEND", "pdfplumber")
    assert extraction_fingerprint() != baseline
    assert runtime_manifest()["packages"]["pdfplumber"]
    monkeypatch.setenv("SP_TABLE_BACKEND", "typo")
    with pytest.raises(ValueError):
        extraction_backend()


def test_sp_ocr_mode_is_explicit_and_changes_extraction_fingerprint(monkeypatch):
    monkeypatch.delenv("SP_OCR_MODE", raising=False)
    baseline = extraction_fingerprint()
    assert runtime_manifest()["sp_ocr_mode"] == "auto"

    monkeypatch.setenv("SP_OCR_MODE", "off")
    assert runtime_manifest()["sp_ocr_mode"] == "off"
    assert extraction_fingerprint() != baseline

    monkeypatch.setenv("SP_OCR_MODE", "typo")
    with pytest.raises(ValueError, match="SP_OCR_MODE"):
        runtime_manifest()


def test_ocr_implementation_identity_changes_extraction_fingerprint(monkeypatch):
    monkeypatch.setattr(
        extraction_runtime,
        "ocr_runtime_identity",
        lambda: {"windows_helper_sha256": "first"},
        raising=False,
    )
    first = extraction_runtime.extraction_fingerprint()
    assert extraction_runtime.runtime_manifest()["ocr_runtime"]["windows_helper_sha256"] == "first"

    monkeypatch.setattr(
        extraction_runtime,
        "ocr_runtime_identity",
        lambda: {"windows_helper_sha256": "second"},
    )
    assert extraction_runtime.extraction_fingerprint() != first


def test_missing_hybrid_dependency_cannot_silently_change_the_extraction(monkeypatch):
    module = importlib.import_module("json추출")
    monkeypatch.setenv("SP_TABLE_BACKEND", "hybrid")
    monkeypatch.setitem(sys.modules, "camelot", None)
    with pytest.raises(RuntimeError, match="requirements"):
        module.PDFReader("not-opened.pdf").read()


def test_hybrid_batches_all_pages_including_last_short_batch(monkeypatch):
    module = importlib.import_module("json추출")
    monkeypatch.setenv("SP_TABLE_BACKEND", "hybrid")
    pages = [SimpleNamespace(rect=SimpleNamespace(height=800)) for _ in range(35)]
    calls = []
    class FakeDoc(list):
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(module.BasePDFReader, "read", lambda _: pages)
    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(open=lambda _: FakeDoc(pages)))
    monkeypatch.setitem(sys.modules, "camelot", SimpleNamespace(read_pdf=lambda _, **kw: calls.append(kw) or []))
    assert len(module.PDFReader("not-opened.pdf").read()) == 35
    assert [c["pages"] for c in calls] == ["1-16", "17-32", "33-35"]


def test_bad_page_is_blocking_not_a_successful_partial_review():
    with pytest.raises(RuntimeError, match="300쪽"):
        validate_extraction_report({"blocking_issues": [{"page": 300, "image_only": True}]})


def test_320_page_native_text_pdf_retains_last_page_and_permit_evidence(tmp_path, monkeypatch):
    from reportlab.pdfgen.canvas import Canvas
    from sp_pdf_judger.permit_pdf_store import PermitPdfStore
    module = importlib.import_module("json추출")
    path = tmp_path / "long_native.pdf"
    canvas = Canvas(str(path))
    for page in range(1, 321):
        canvas.drawString(50, 780, f"{page}. Manufacturing evidence")
        canvas.drawString(50, 750, f"LOT-{page:04d} native source sentinel {page}")
        if page in {160, 320}:
            # Real ruled cells in the middle and final page, not just a text counter.
            for x in (50, 210, 370, 530):
                canvas.line(x, 610, x, 700)
            for y in (610, 640, 670, 700):
                canvas.line(50, y, 530, y)
            for row, values in enumerate((("Material", "Lot", "Amount"),
                                          ("Buffer", f"BUF-{page}", "15 L"),
                                          ("Protein", f"MAT-{page}", "270 L"))):
                for column, value in enumerate(values):
                    canvas.drawString(58 + 160 * column, 680 - 30 * row, value)
        if page == 319:
            canvas.rect(50, 650, 150, 45)
            canvas.rect(300, 650, 150, 45)
            canvas.drawString(60, 670, "Mixing evidence")
            canvas.drawString(310, 670, "Filling evidence")
            canvas.line(200, 673, 300, 673)
            canvas.line(290, 678, 300, 673)
            canvas.line(290, 668, 300, 673)
        canvas.showPage()
    canvas.save()
    monkeypatch.setenv("SP_TABLE_BACKEND", "pdfplumber")
    output = tmp_path / "extracted"
    summary = module.Pipeline().run(path, output)
    report = json.loads((output / "extraction_report.json").read_text(encoding="utf-8"))
    assert summary["total_pages"] == 320
    assert report["processed_pages"] == list(range(1, 321))
    assert not report["blocking_issues"]
    assert all(p["word_count"] > 0 for p in report["pages"])
    source = json.loads((output / "01_raw_pages.json").read_text(encoding="utf-8"))
    assert "LOT-0320" in source[-1]["text"]
    for page in (160, 320):
        table_text = json.dumps(source[page - 1]["tables"])
        assert f"BUF-{page}" in table_text and f"MAT-{page}" in table_text
        assert "15 L" in table_text and "270 L" in table_text
    assert "Mixing evidence" in source[318]["native_text"]
    assert "Filling evidence" in source[318]["native_text"]
    records = json.loads((output / "05_records.json").read_text(encoding="utf-8"))
    assert any("LOT-0320" in (r.get("raw_text") or "") for r in records)
    assert any("BUF-320" in (r.get("raw_text") or "") for r in records)
    store = PermitPdfStore([path])
    assert any("LOT-0320" in c.text for c in store.chunks)
    assert max(c.page_end or c.page_number for c in store.chunks) == 320
