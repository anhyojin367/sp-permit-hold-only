"""Keep the reviewed synthetic permit byte-identical across Python/zlib versions.

The production document hash guard remains strict. Only the proof scripts use
this fixed, previously reviewed fixture; submitted PDFs are never substituted.
"""
import hashlib
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEWED_PERMIT = ROOT / "tests/fixtures/reviewed_required_test_permit.pdf"
REVIEWED_SHA256 = "ccc6d056c89843bf95aa7896e93720b7aae5d213b0a94247338ed251a725a4e0"
APPLICABILITY_PERMIT = ROOT / "tests/fixtures/reviewed_applicability_permit.pdf"
APPLICABILITY_SHA256 = "e96a0b016d4d18978fc7b4df418d10492ce3fe1d791115a78c18eda0300ed0ae"


def copy_reviewed_permit(destination: Path) -> None:
    if hashlib.sha256(REVIEWED_PERMIT.read_bytes()).hexdigest() != REVIEWED_SHA256:
        raise ValueError("The reviewed synthetic permit fixture has changed")
    shutil.copyfile(REVIEWED_PERMIT, destination)


def copy_applicability_permit(destination: Path) -> None:
    if hashlib.sha256(APPLICABILITY_PERMIT.read_bytes()).hexdigest() != APPLICABILITY_SHA256:
        raise ValueError("The reviewed synthetic applicability fixture has changed")
    shutil.copyfile(APPLICABILITY_PERMIT, destination)
