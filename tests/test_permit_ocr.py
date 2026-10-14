from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import fitz
import pytest
from PIL import Image, ImageDraw, ImageFont

import sp_pdf_judger.permit_ocr as permit_ocr
from sp_pdf_judger.permit_ocr import (
    extract_permit_page_texts,
    looks_corrupt_permit_text,
    select_page_text,
)


def _blank_pdf(path: Path) -> None:
    document = fitz.open()
    document.new_page()
    document.save(path)
    document.close()


def _corrupt_native_pages(_pdf_path: Path) -> list[str]:
    return ["2.1.2.1.8. ����������������� ��Ų"]


def test_replacement_character_heavy_native_text_uses_ocr() -> None:
    native = "2.1.2.1.8. ����������������� ��Ų"
    ocr = "2.1.2.1.8. 돼지유래바이러스부정시험 최소 21일간 배양"

    assert looks_corrupt_permit_text(native) is True
    assert select_page_text(native, ocr) == (ocr, "windows_ocr")


def test_cid_marker_is_corrupt_text() -> None:
    assert looks_corrupt_permit_text("시험결과 3.00 x 10(cid:2874) cells/mL") is True


def test_explicit_page_ocr_renders_only_requested_pages(
    tmp_path: Path, monkeypatch
) -> None:
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(b"fake")
    rendered: list[list[int]] = []
    monkeypatch.setattr(
        permit_ocr,
        "_render_corrupt_pages",
        lambda _path, page_numbers, _image_dir: rendered.append(list(page_numbers)),
    )
    monkeypatch.setattr(
        permit_ocr,
        "_run_windows_ocr",
        lambda _image_dir: (
            {2: "시험결과 3.00 x 106 cells/mL", 10: "시험결과 3.20 x 108 PFU/mL"},
            [],
        ),
    )

    pages, errors = permit_ocr.ocr_selected_pages(
        pdf_path,
        [10, 2, 2],
        image_dir=tmp_path / "images",
    )

    assert errors == []
    assert rendered == [[2, 10]]
    assert pages == [
        permit_ocr.PermitPageText(2, "시험결과 3.00 x 106 cells/mL", "windows_ocr"),
        permit_ocr.PermitPageText(10, "시험결과 3.20 x 108 PFU/mL", "windows_ocr"),
    ]


def test_selected_page_ocr_cleanup_error_does_not_hide_recovered_text(
    tmp_path: Path, monkeypatch
) -> None:
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(b"fake")

    class CleanupFails:
        name = str(tmp_path / "rendered")

        def cleanup(self):
            raise PermissionError("image is still in use")

    monkeypatch.setattr(permit_ocr.tempfile, "TemporaryDirectory", lambda **_kwargs: CleanupFails())
    monkeypatch.setattr(permit_ocr, "_render_corrupt_pages", lambda *_args: None)
    monkeypatch.setattr(
        permit_ocr,
        "_run_windows_ocr",
        lambda _image_dir: ({1: "시험결과 3.00 x 106 cells/mL"}, []),
    )

    pages, errors = permit_ocr.ocr_selected_pages(pdf_path, [1])

    assert pages == [
        permit_ocr.PermitPageText(1, "시험결과 3.00 x 106 cells/mL", "windows_ocr")
    ]
    assert any("cleanup" in error.lower() for error in errors)


def test_windows_ocr_timeout_is_reported_without_raising(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        permit_ocr.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("powershell.exe", 180)
        ),
    )

    pages, errors = permit_ocr._run_windows_ocr(tmp_path)

    assert pages == {}
    assert any("timed out" in error.lower() for error in errors)


def test_valid_korean_native_text_does_not_use_ocr() -> None:
    native = "2.3.1.7. 확인시험 Component B 중간체원액에 대한 시험"

    assert looks_corrupt_permit_text(native) is False
    assert select_page_text(native, "잘못된 OCR") == (native, "native")


def test_unusable_ocr_returns_unreadable_page_and_public_error(
    tmp_path: Path, monkeypatch
) -> None:
    pdf_path = tmp_path / "permit.pdf"
    _blank_pdf(pdf_path)
    monkeypatch.setattr(permit_ocr, "_native_page_texts", _corrupt_native_pages)
    monkeypatch.setattr(
        permit_ocr,
        "_run_windows_ocr",
        lambda *_args: ({1: "garbled OCR output"}, []),
    )
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))

    pages, errors = extract_permit_page_texts(pdf_path)

    assert pages == [permit_ocr.PermitPageText(1, "", "unreadable")]
    assert any("page 1" in error and "unreadable" in error for error in errors)


def test_valid_pdf_sha_cache_skips_ocr_on_second_extraction(
    tmp_path: Path, monkeypatch
) -> None:
    pdf_path = tmp_path / "permit.pdf"
    _blank_pdf(pdf_path)
    calls: list[Path] = []
    monkeypatch.setattr(permit_ocr, "_native_page_texts", _corrupt_native_pages)
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))

    def windows_ocr(image_dir: Path):
        calls.append(image_dir)
        return ({1: "돼지유래바이러스부정시험 21일"}, [])

    monkeypatch.setattr(permit_ocr, "_run_windows_ocr", windows_ocr)

    first_pages, first_errors = extract_permit_page_texts(pdf_path)
    second_pages, second_errors = extract_permit_page_texts(pdf_path)

    assert first_errors == []
    assert second_errors == []
    assert first_pages == second_pages
    assert first_pages[0].source == "windows_ocr"
    assert len(calls) == 1


def test_previous_schema_cid_cache_is_rebuilt(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "permit.pdf"
    _blank_pdf(pdf_path)
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    monkeypatch.setattr(
        permit_ocr,
        "_native_page_texts",
        lambda _path: ["시험결과 3.00 x 10(cid:2874) cells/mL"],
    )
    cache_path = permit_ocr._cache_path(pdf_path)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pdf_sha256": permit_ocr._pdf_sha256(pdf_path),
                "pages": [{
                    "page_number": 1,
                    "text": "시험결과 3.00 x 10(cid:2874) cells/mL",
                    "source": "native",
                }],
            }
        ),
        encoding="utf-8",
    )
    calls: list[Path] = []

    def windows_ocr(image_dir: Path):
        calls.append(image_dir)
        return ({1: "시험결과 3.00 x 106 cells/mL"}, [])

    monkeypatch.setattr(permit_ocr, "_run_windows_ocr", windows_ocr)

    pages, errors = extract_permit_page_texts(pdf_path)

    assert errors == []
    assert pages == [
        permit_ocr.PermitPageText(1, "시험결과 3.00 x 106 cells/mL", "windows_ocr")
    ]
    assert len(calls) == 1
    rebuilt = json.loads(cache_path.read_text(encoding="utf-8"))
    assert rebuilt["schema_version"] == permit_ocr.CACHE_SCHEMA_VERSION
    assert rebuilt["schema_version"] > 1


def test_current_schema_cid_cache_is_semantically_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    pdf_path = tmp_path / "permit.pdf"
    _blank_pdf(pdf_path)
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    native = "시험결과 3.00 x 10(cid:2874) cells/mL"
    monkeypatch.setattr(permit_ocr, "_native_page_texts", lambda _path: [native])
    cache_path = permit_ocr._cache_path(pdf_path)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": permit_ocr.CACHE_SCHEMA_VERSION,
                "pdf_sha256": permit_ocr._pdf_sha256(pdf_path),
                "pages": [{"page_number": 1, "text": native, "source": "native"}],
            }
        ),
        encoding="utf-8",
    )
    calls: list[Path] = []

    def windows_ocr(image_dir: Path):
        calls.append(image_dir)
        return ({1: "시험결과 3.00 x 106 cells/mL"}, [])

    monkeypatch.setattr(permit_ocr, "_run_windows_ocr", windows_ocr)

    pages, errors = extract_permit_page_texts(pdf_path)

    assert errors == []
    assert pages[0].source == "windows_ocr"
    assert "106 cells/mL" in pages[0].text
    assert len(calls) == 1


def test_precomputed_sha_avoids_rehash_and_can_disable_legacy_page_cache(
    tmp_path: Path, monkeypatch
) -> None:
    pdf_path = tmp_path / "permit.pdf"
    _blank_pdf(pdf_path)
    digest = permit_ocr._pdf_sha256(pdf_path)
    monkeypatch.setattr(permit_ocr, "_native_page_texts", lambda _path: ["정상 허가서 본문"])
    monkeypatch.setattr(
        permit_ocr,
        "_pdf_sha256",
        lambda _path: (_ for _ in ()).throw(AssertionError("precomputed hash was ignored")),
    )
    monkeypatch.setattr(
        permit_ocr,
        "_read_cache",
        lambda *_args: (_ for _ in ()).throw(AssertionError("legacy cache was read")),
    )
    monkeypatch.setattr(
        permit_ocr,
        "_write_cache",
        lambda *_args: (_ for _ in ()).throw(AssertionError("legacy cache was written")),
    )

    pages, errors = extract_permit_page_texts(
        pdf_path,
        pdf_sha256=digest,
        use_page_cache=False,
    )

    assert errors == []
    assert pages == [permit_ocr.PermitPageText(1, "정상 허가서 본문", "native")]


def test_tesseract_uses_korean_and_english_language(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "page-1.png"
    image.write_bytes(b"not decoded by the fake")
    calls: list[tuple[str, str, int]] = []
    fake = SimpleNamespace(
        image_to_string=lambda path, lang, timeout: calls.append((path, lang, timeout)) or "한글 English",
    )
    monkeypatch.setattr(permit_ocr, "pytesseract", fake)
    monkeypatch.setattr(permit_ocr, "_tesseract_is_available", lambda: True)

    pages, errors = permit_ocr._run_tesseract_ocr(tmp_path)

    assert errors == [] and pages == {1: "한글 English"}
    assert calls == [
        (str(image), "kor+eng", permit_ocr.TESSERACT_OCR_TIMEOUT_SECONDS)
    ]


def test_tesseract_version_probe_uses_bounded_subprocess(monkeypatch) -> None:
    calls: list[tuple[list[str], int]] = []

    def bounded_run(command, **kwargs):
        calls.append((command, kwargs["timeout"]))
        return SimpleNamespace(returncode=0, stdout="tesseract 5.5.0\n leptonica", stderr="")

    monkeypatch.setattr(permit_ocr.subprocess, "run", bounded_run)

    version = permit_ocr._probe_tesseract_version("tesseract.exe")

    assert version == "tesseract 5.5.0"
    assert calls == [
        (["tesseract.exe", "--version"], permit_ocr.TESSERACT_VERSION_TIMEOUT_SECONDS)
    ]


def test_malformed_cache_is_ignored_and_rebuilt(
    tmp_path: Path, monkeypatch
) -> None:
    pdf_path = tmp_path / "permit.pdf"
    _blank_pdf(pdf_path)
    monkeypatch.setattr(permit_ocr, "_native_page_texts", _corrupt_native_pages)
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    cache_path = permit_ocr._cache_path(pdf_path)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("not json", encoding="utf-8")
    calls: list[Path] = []

    def windows_ocr(image_dir: Path):
        calls.append(image_dir)
        return ({1: "유정란접종시험 80% 혈구응집반응"}, [])

    monkeypatch.setattr(permit_ocr, "_run_windows_ocr", windows_ocr)

    pages, errors = extract_permit_page_texts(pdf_path)

    assert errors == []
    assert pages[0].source == "windows_ocr"
    assert len(calls) == 1
    rebuilt = json.loads(cache_path.read_text(encoding="utf-8"))
    assert rebuilt["pdf_sha256"]
    assert rebuilt["pages"] == [
        {"page_number": 1, "text": "유정란접종시험 80% 혈구응집반응", "source": "windows_ocr"}
    ]


def test_stale_cache_hash_is_ignored_and_rebuilt(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "permit.pdf"
    _blank_pdf(pdf_path)
    monkeypatch.setattr(permit_ocr, "_native_page_texts", _corrupt_native_pages)
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    cache_path = permit_ocr._cache_path(pdf_path)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pdf_sha256": "stale",
                "pages": [{"page_number": 1, "text": "old text", "source": "native"}],
            }
        ),
        encoding="utf-8",
    )
    calls: list[Path] = []

    def windows_ocr(image_dir: Path):
        calls.append(image_dir)
        return ({1: "돼지유래바이러스부정시험 21일"}, [])

    monkeypatch.setattr(permit_ocr, "_run_windows_ocr", windows_ocr)

    pages, errors = extract_permit_page_texts(pdf_path)

    assert errors == []
    assert pages == [
        permit_ocr.PermitPageText(1, "돼지유래바이러스부정시험 21일", "windows_ocr")
    ]
    assert len(calls) == 1
    assert json.loads(cache_path.read_text(encoding="utf-8"))["pdf_sha256"] != "stale"


def test_unusable_windows_text_falls_back_to_usable_tesseract(
    tmp_path: Path, monkeypatch
) -> None:
    pdf_path = tmp_path / "permit.pdf"
    _blank_pdf(pdf_path)
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    monkeypatch.setattr(permit_ocr, "_native_page_texts", _corrupt_native_pages)
    monkeypatch.setattr(
        permit_ocr, "_run_windows_ocr", lambda *_args: ({1: "garbled OCR output"}, [])
    )
    tesseract_calls: list[Path] = []

    def tesseract_ocr(image_dir: Path):
        tesseract_calls.append(image_dir)
        return ({1: "\ub3fc\uc9c0\uc720\ub798\ubc14\uc774\ub7ec\uc2a4\ubd80\uc815\uc2dc\ud5d8 \ucd5c\uc18c 21\uc77c"}, [])

    monkeypatch.setattr(permit_ocr, "_run_tesseract_ocr", tesseract_ocr)

    pages, errors = extract_permit_page_texts(pdf_path)

    assert errors == []
    assert pages == [
        permit_ocr.PermitPageText(
            1, "돼지유래바이러스부정시험 최소 21일", "tesseract"
        )
    ]
    assert len(tesseract_calls) == 1


def test_tesseract_fallback_requests_korean_and_english(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "page-2.png").write_bytes(b"image")
    calls: list[tuple[str, str, int]] = []
    fake_tesseract = SimpleNamespace(
        get_tesseract_version=lambda: "test-version",
        image_to_string=lambda image, lang, timeout: calls.append((image, lang, timeout)) or "혼합 text",
    )
    monkeypatch.setattr(permit_ocr, "pytesseract", fake_tesseract)
    monkeypatch.setattr(permit_ocr, "_tesseract_is_available", lambda: True)

    pages, errors = permit_ocr._run_tesseract_ocr(tmp_path)

    assert errors == []
    assert pages == {2: "혼합 text"}
    assert calls == [(
        str(tmp_path / "page-2.png"),
        "kor+eng",
        permit_ocr.TESSERACT_OCR_TIMEOUT_SECONDS,
    )]


@pytest.mark.parametrize(
    "cached_page",
    [
        {"page_number": True, "text": "old text", "source": "native"},
        {"page_number": 1, "text": "old text", "source": "unexpected"},
        {"page_number": 1, "text": "", "source": "native"},
    ],
)
def test_semantically_invalid_cache_page_is_rebuilt(
    tmp_path: Path, monkeypatch, cached_page: dict[str, object]
) -> None:
    pdf_path = tmp_path / "permit.pdf"
    _blank_pdf(pdf_path)
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    monkeypatch.setattr(permit_ocr, "_native_page_texts", _corrupt_native_pages)
    cache_path = permit_ocr._cache_path(pdf_path)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pdf_sha256": permit_ocr._pdf_sha256(pdf_path),
                "pages": [cached_page],
            }
        ),
        encoding="utf-8",
    )
    calls: list[Path] = []

    def windows_ocr(image_dir: Path):
        calls.append(image_dir)
        return ({1: "\uc720\uc815\ub780\uc811\uc885\uc2dc\ud5d8 80% \ud608\uad6c\uc751\uc9d1\ubc18\uc751"}, [])

    monkeypatch.setattr(permit_ocr, "_run_windows_ocr", windows_ocr)

    pages, errors = extract_permit_page_texts(pdf_path)

    assert errors == []
    assert pages == [
        permit_ocr.PermitPageText(1, "\uc720\uc815\ub780\uc811\uc885\uc2dc\ud5d8 80% \ud608\uad6c\uc751\uc9d1\ubc18\uc751", "windows_ocr")
    ]
    assert len(calls) == 1


def test_rendering_receives_only_corrupt_page_numbers(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "permit.pdf"
    document = fitz.open()
    document.new_page()
    document.new_page()
    document.save(pdf_path)
    document.close()
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    monkeypatch.setattr(
        permit_ocr,
        "_native_page_texts",
        lambda _path: ["English heading", "2.1.2.1.8. ����������������� ��Ų"],
    )
    rendered: list[list[int]] = []
    monkeypatch.setattr(
        permit_ocr,
        "_render_corrupt_pages",
        lambda _path, page_numbers, _image_dir: rendered.append(page_numbers),
    )
    monkeypatch.setattr(
        permit_ocr,
        "_run_windows_ocr",
        lambda _image_dir: ({2: "돼지유래바이러스부정시험 21일"}, []),
    )

    pages, errors = extract_permit_page_texts(pdf_path)

    assert errors == []
    assert [page.source for page in pages] == ["native", "windows_ocr"]
    assert rendered == [[2]]


SOURCE_PERMIT_PDF = Path(
    __file__
).resolve().parents[1] / "sp_pdf_judger/permits/documents/sky_covione_multidose.pdf"


@pytest.mark.skipif(not SOURCE_PERMIT_PDF.exists(), reason="supplied permit PDF is absent")
def test_supplied_permit_pdf_recovers_required_pages_and_uses_cache(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEMP", str(tmp_path / "isolated-temp"))

    first_pages, first_errors = extract_permit_page_texts(SOURCE_PERMIT_PDF)
    first_by_number = {page.page_number: page for page in first_pages}
    assert first_errors == []
    assert "돼지유래바이러스부정시험" in first_by_number[4].text
    assert "21일" in first_by_number[4].text
    assert all(
        token in first_by_number[5].text
        for token in ("유정란접종시험", "80%", "혈구응집반응")
    )

    def cache_miss_only(*_args, **_kwargs):
        raise AssertionError("second extraction must return before OCR or cache writing")

    monkeypatch.setattr(permit_ocr, "_run_windows_ocr", cache_miss_only)
    monkeypatch.setattr(permit_ocr, "_write_cache", cache_miss_only)
    second_pages, second_errors = extract_permit_page_texts(SOURCE_PERMIT_PDF)

    assert second_errors == []
    assert second_pages == first_pages


def test_windows_ocr_helper_orders_nonempty_korean_images_numerically(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("requires Windows Media OCR")
    powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    font_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "malgun.ttf"
    if not powershell.exists():
        pytest.skip("requires Windows PowerShell 5.1")
    if not font_path.exists():
        pytest.skip("requires the Malgun Gothic Korean font")

    font = ImageFont.truetype(font_path, 44)
    for page_number in (10, 2):
        image = Image.new("RGB", (900, 260), "white")
        ImageDraw.Draw(image).text(
            (40, 90), f"\ud398\uc774\uc9c0 {page_number} \ud55c\uad6d\uc5b4 \uc2dc\ud5d8", font=font, fill="black"
        )
        image.save(tmp_path / f"page-{page_number}.png")

    helper = Path(__file__).parents[1] / "scripts" / "windows_ocr_images.ps1"
    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(helper),
            str(tmp_path),
        ],
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(result.stdout)
    if payload.get("errors") and any(
        "Windows Media OCR" in str(error) or "ko-KR" in str(error)
        for error in payload["errors"]
    ):
        pytest.skip("Windows Media OCR with ko-KR is unavailable")

    assert result.returncode == 0, result.stderr
    assert payload["errors"] == []
    assert [page["page_number"] for page in payload["pages"]] == [2, 10]
    for page in payload["pages"]:
        normalized = re.sub(r"\s+", "", page["text"])
        assert "페이지" in normalized
        assert "한국어" in normalized
