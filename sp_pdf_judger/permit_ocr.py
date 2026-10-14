from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Sequence

import fitz

try:
    import pytesseract
except Exception:  # pragma: no cover - availability differs by host
    pytesseract = None

from .utils import clean_text


CACHE_SCHEMA_VERSION = 2
OCR_LANGUAGE = "kor+eng"
OCR_RENDER_DPI = 180
WINDOWS_OCR_TIMEOUT_SECONDS = 180
TESSERACT_OCR_TIMEOUT_SECONDS = 180
TESSERACT_VERSION_TIMEOUT_SECONDS = 10
_HANGUL_RE = re.compile(r"[가-힣]")
_MOJIBAKE_RE = re.compile(r"(?:[\uFFFD]{2,}|[ÃÂâ][^\s]{1,3})")
_CID_MARKER_RE = re.compile(r"\(cid:\d+\)", re.IGNORECASE)


@dataclass(frozen=True)
class PermitPageText:
    page_number: int
    text: str
    source: str


def looks_corrupt_permit_text(text: str) -> bool:
    """Return whether PDF-native text is implausible for a Korean permit."""
    value = clean_text(text)
    if not value:
        return True
    if _CID_MARKER_RE.search(value):
        return True

    replacement_ratio = value.count("\uFFFD") / len(value)
    if replacement_ratio >= 0.03 or _MOJIBAKE_RE.search(value):
        return True

    non_ascii_letters = [char for char in value if char.isalpha() and ord(char) > 127]
    if len(non_ascii_letters) >= 8:
        hangul_count = len(_HANGUL_RE.findall(value))
        if hangul_count / len(non_ascii_letters) < 0.12:
            return True
    return False


def _is_usable_korean_text(text: str) -> bool:
    value = clean_text(text)
    return bool(_HANGUL_RE.search(value)) and not looks_corrupt_permit_text(value)


def _is_usable_ocr_text(text: str) -> bool:
    value = clean_text(text)
    return bool(re.search(r"[0-9A-Za-z가-힣]", value)) and not looks_corrupt_permit_text(value)


def select_page_text(native: str, ocr: str) -> tuple[str, str]:
    if not looks_corrupt_permit_text(native):
        return clean_text(native), "native"
    if _is_usable_korean_text(ocr):
        return clean_text(ocr), "windows_ocr"
    return "", "unreadable"


def _pdf_sha256(pdf_path: Path) -> str:
    digest = hashlib.sha256()
    with pdf_path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _windows_ocr_helper_path() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts" / "windows_ocr_images.ps1"


def _resolve_tesseract_executable() -> str | None:
    if pytesseract is None:
        return None
    command = str(
        getattr(getattr(pytesseract, "pytesseract", None), "tesseract_cmd", "")
        or "tesseract"
    )
    command_path = Path(command)
    if command_path.is_file():
        return str(command_path.resolve())
    return shutil.which(command)


def _probe_tesseract_version(executable: str) -> str | None:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TESSERACT_VERSION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    output = result.stdout or result.stderr or ""
    return next((line.strip() for line in output.splitlines() if line.strip()), None)


@lru_cache(maxsize=1)
def ocr_runtime_identity() -> dict[str, object]:
    """Return stable OCR implementation details used by extraction cache identity."""
    helper = _windows_ocr_helper_path()
    helper_sha256 = _pdf_sha256(helper) if helper.is_file() else None

    tesseract_executable = _resolve_tesseract_executable()
    tesseract_version = (
        _probe_tesseract_version(tesseract_executable)
        if tesseract_executable
        else None
    )

    return {
        "language": OCR_LANGUAGE,
        "render_dpi": OCR_RENDER_DPI,
        "windows_timeout_seconds": WINDOWS_OCR_TIMEOUT_SECONDS,
        "tesseract_timeout_seconds": TESSERACT_OCR_TIMEOUT_SECONDS,
        "tesseract_version_timeout_seconds": TESSERACT_VERSION_TIMEOUT_SECONDS,
        "windows_helper_sha256": helper_sha256,
        "windows_platform_version": platform.version() if os.name == "nt" else None,
        "tesseract_executable": tesseract_executable,
        "tesseract_version": tesseract_version,
    }


def _cache_root() -> Path:
    temp_dir = Path(os.environ.get("TEMP") or tempfile.gettempdir())
    return temp_dir / "sp_pdf_judger_permit_ocr"


def _cache_path(pdf_path: Path, pdf_sha256: str | None = None) -> Path:
    return _cache_root() / (pdf_sha256 or _pdf_sha256(pdf_path)) / "page_texts.json"


def _native_page_texts(pdf_path: Path) -> list[str]:
    with fitz.open(pdf_path) as document:
        return [clean_text(page.get_text("text") or "") for page in document]


def _read_cache(cache_path: Path, pdf_sha256: str, page_count: int) -> list[PermitPageText] | None:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != CACHE_SCHEMA_VERSION
            or payload.get("pdf_sha256") != pdf_sha256
            or not isinstance(payload.get("pages"), list)
            or len(payload["pages"]) != page_count
        ):
            return None
        valid_sources = {"native", "windows_ocr", "tesseract", "unreadable"}
        pages: list[PermitPageText] = []
        for expected_page_number, item in enumerate(payload["pages"], start=1):
            if not isinstance(item, dict):
                return None
            page_number = item.get("page_number")
            text = item.get("text")
            source = item.get("source")
            if (
                not isinstance(page_number, int)
                or isinstance(page_number, bool)
                or page_number != expected_page_number
                or not isinstance(text, str)
                or source not in valid_sources
            ):
                return None
            if (source == "unreadable" and text) or (
                source != "unreadable"
                and (not clean_text(text) or looks_corrupt_permit_text(text))
            ):
                return None
            pages.append(PermitPageText(page_number=page_number, text=text, source=source))
        return pages
    except (OSError, TypeError, ValueError, KeyError):
        return None


def _write_cache(cache_path: Path, pdf_sha256: str, pages: list[PermitPageText]) -> None:
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "pdf_sha256": pdf_sha256,
        "pages": [
            {"page_number": page.page_number, "text": page.text, "source": page.source}
            for page in pages
        ],
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(cache_path)


def _render_corrupt_pages(pdf_path: Path, page_numbers: list[int], image_dir: Path) -> None:
    image_dir.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf_path) as document:
        for page_number in page_numbers:
            page = document.load_page(page_number - 1)
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(OCR_RENDER_DPI / 72, OCR_RENDER_DPI / 72),
                alpha=False,
            )
            pixmap.save(image_dir / f"page-{page_number}.png")


def _run_windows_ocr(image_dir: Path) -> tuple[dict[int, str], list[str]]:
    if os.name != "nt":
        return {}, ["Windows Media OCR is unavailable on this platform"]
    script = _windows_ocr_helper_path()
    if not script.exists():
        return {}, ["Windows OCR helper is missing"]
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                str(image_dir),
            ],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=WINDOWS_OCR_TIMEOUT_SECONDS,
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or not isinstance(payload.get("pages"), list):
            raise ValueError("OCR helper did not emit page JSON")
        pages = {
            int(item["page_number"]): str(item["text"])
            for item in payload["pages"]
            if isinstance(item, dict) and "page_number" in item and "text" in item
        }
        errors = [str(error) for error in payload.get("errors", [])]
        if result.returncode:
            errors.append(f"Windows OCR helper exited with code {result.returncode}")
        return pages, errors
    except subprocess.TimeoutExpired as exc:
        return {}, [f"Windows OCR timed out after {exc.timeout} seconds"]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, [f"Windows OCR failed: {exc}"]


def _tesseract_is_available() -> bool:
    executable = _resolve_tesseract_executable()
    return bool(executable and _probe_tesseract_version(executable))


def _run_tesseract_ocr(image_dir: Path) -> tuple[dict[int, str], list[str]]:
    if not _tesseract_is_available():
        return {}, ["Tesseract OCR is unavailable"]
    pages: dict[int, str] = {}
    errors: list[str] = []
    for image_path in sorted(image_dir.glob("page-*.png"), key=_page_number_from_image):
        page_number = _page_number_from_image(image_path)
        try:
            pages[page_number] = pytesseract.image_to_string(
                str(image_path),
                lang=OCR_LANGUAGE,
                timeout=TESSERACT_OCR_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            errors.append(f"Tesseract OCR failed for page {page_number}: {exc}")
    return pages, errors


def _page_number_from_image(image_path: Path) -> int:
    match = re.fullmatch(r"page-(\d+)\.png", image_path.name)
    return int(match.group(1)) if match else 0


def _unreadable_errors(pages: list[PermitPageText]) -> list[str]:
    return [f"Permit OCR page {page.page_number} is unreadable" for page in pages if page.source == "unreadable"]


def ocr_selected_pages(
    pdf_path: Path,
    page_numbers: Sequence[int],
    ocr_mode: str = "auto",
    *,
    image_dir: Path | None = None,
) -> tuple[list[PermitPageText], list[str]]:
    """OCR exactly the requested one-based pages without inspecting native text."""
    requested = sorted(
        {
            int(page_number)
            for page_number in page_numbers
            if isinstance(page_number, int)
            and not isinstance(page_number, bool)
            and page_number > 0
        }
    )
    if not requested:
        return [], []

    normalized_mode = (ocr_mode or "auto").strip().lower()
    allowed_modes = {"auto", "windows", "tesseract", "none", "native", "off"}
    if normalized_mode not in allowed_modes:
        raise ValueError(f"지원하지 않는 OCR 모드입니다: {ocr_mode}")
    if normalized_mode in {"none", "native", "off"}:
        pages = [PermitPageText(page_number, "", "unreadable") for page_number in requested]
        return pages, _unreadable_errors(pages)

    temporary_ocr_dir = None
    if image_dir is None:
        temporary_ocr_dir = tempfile.TemporaryDirectory(
            prefix="sp-selected-page-ocr-",
            ignore_cleanup_errors=True,
        )
        rendered_dir = Path(temporary_ocr_dir.name)
    else:
        rendered_dir = Path(image_dir)

    ocr_by_page: dict[int, str] = {}
    source_by_page: dict[int, str] = {}
    errors: list[str] = []
    try:
        try:
            _render_corrupt_pages(Path(pdf_path), requested, rendered_dir)
        except Exception as exc:
            errors.append(f"Could not render selected OCR pages: {exc}")
        else:
            if normalized_mode != "tesseract":
                windows_pages, windows_errors = _run_windows_ocr(rendered_dir)
                errors.extend(windows_errors)
                for page_number in requested:
                    if page_number in windows_pages:
                        ocr_by_page[page_number] = windows_pages[page_number]
                        source_by_page[page_number] = "windows_ocr"

            fallback_numbers = [
                page_number
                for page_number in requested
                if not _is_usable_ocr_text(ocr_by_page.get(page_number, ""))
            ]
            if fallback_numbers and normalized_mode in {"auto", "tesseract"}:
                tesseract_pages, tesseract_errors = _run_tesseract_ocr(rendered_dir)
                errors.extend(tesseract_errors)
                for page_number in fallback_numbers:
                    if page_number in tesseract_pages:
                        ocr_by_page[page_number] = tesseract_pages[page_number]
                        source_by_page[page_number] = "tesseract"
    finally:
        if temporary_ocr_dir is not None:
            try:
                temporary_ocr_dir.cleanup()
            except OSError as exc:
                errors.append(f"Could not cleanup selected OCR images: {exc}")

    pages: list[PermitPageText] = []
    for page_number in requested:
        text = clean_text(ocr_by_page.get(page_number, ""))
        if _is_usable_ocr_text(text):
            pages.append(
                PermitPageText(
                    page_number,
                    text,
                    source_by_page.get(page_number, "windows_ocr"),
                )
            )
        else:
            pages.append(PermitPageText(page_number, "", "unreadable"))
    errors.extend(_unreadable_errors(pages))
    return pages, errors


def extract_permit_page_texts(
    pdf_path: Path,
    ocr_mode: str = "auto",
    *,
    pdf_sha256: str | None = None,
    use_page_cache: bool = True,
) -> tuple[list[PermitPageText], list[str]]:
    """Recover text page-by-page without interpreting the permit's contents."""
    pdf_path = Path(pdf_path)
    try:
        native_pages = _native_page_texts(pdf_path)
    except Exception as exc:
        return [], [f"Could not read permit PDF: {exc}"]

    content_hash = pdf_sha256 or _pdf_sha256(pdf_path)
    cache_path = _cache_path(pdf_path, content_hash)
    if use_page_cache:
        cached_pages = _read_cache(cache_path, content_hash, len(native_pages))
        if cached_pages is not None:
            return cached_pages, _unreadable_errors(cached_pages)

    corrupt_numbers = [
        page_number
        for page_number, native in enumerate(native_pages, start=1)
        if looks_corrupt_permit_text(native)
    ]
    ocr_by_page: dict[int, str] = {}
    source_by_page: dict[int, str] = {}
    errors: list[str] = []
    if corrupt_numbers and ocr_mode not in {"none", "native", "off"}:
        temporary_ocr_dir = None
        if use_page_cache:
            image_dir = cache_path.parent
        else:
            temporary_ocr_dir = tempfile.TemporaryDirectory(
                prefix="sp-permit-ocr-",
                ignore_cleanup_errors=True,
            )
            image_dir = Path(temporary_ocr_dir.name)
        try:
            _render_corrupt_pages(pdf_path, corrupt_numbers, image_dir)
        except Exception as exc:
            errors.append(f"Could not render permit OCR pages: {exc}")
        else:
            windows_errors: list[str] = []
            if ocr_mode != "tesseract":
                ocr_by_page, windows_errors = _run_windows_ocr(image_dir)
                errors.extend(windows_errors)
                source_by_page.update({page_number: "windows_ocr" for page_number in ocr_by_page})
            fallback_numbers = [
                number
                for number in corrupt_numbers
                if not _is_usable_korean_text(ocr_by_page.get(number, ""))
            ]
            if fallback_numbers and ocr_mode in {"auto", "tesseract"}:
                tesseract_pages, tesseract_errors = _run_tesseract_ocr(image_dir)
                errors.extend(tesseract_errors)
                for page_number in fallback_numbers:
                    if page_number in tesseract_pages:
                        ocr_by_page[page_number] = tesseract_pages[page_number]
                        source_by_page[page_number] = "tesseract"
        finally:
            if temporary_ocr_dir is not None:
                try:
                    temporary_ocr_dir.cleanup()
                except OSError as exc:
                    errors.append(f"Could not cleanup permit OCR images: {exc}")

    pages: list[PermitPageText] = []
    for page_number, native in enumerate(native_pages, start=1):
        selected, source = select_page_text(native, ocr_by_page.get(page_number, ""))
        if source == "windows_ocr":
            source = source_by_page.get(page_number, source)
        pages.append(PermitPageText(page_number, selected, source))

    errors.extend(_unreadable_errors(pages))
    if use_page_cache:
        try:
            _write_cache(cache_path, content_hash, pages)
        except OSError as exc:
            errors.append(f"Could not write permit OCR cache: {exc}")
    return pages, errors
