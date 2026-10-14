"""Process-local bootstrap for this standalone HOLD-only application."""
from pathlib import Path
import os
import tempfile

REVIEW_SCOPE = 'hold_only'


def bootstrap_runtime() -> Path:
    """Pin all mutable runtime roots before Streamlit/project imports."""
    root = Path(__file__).resolve().parent
    runtime = root / '.runtime'
    locations = {
        'TEMP': runtime / 'tmp',
        'TMP': runtime / 'tmp',
        'TMPDIR': runtime / 'tmp',
        'SP_PDF_DIR': runtime / 'incoming_sp_pdfs',
        'SP_PERMIT_INDEX_DIR': runtime / 'permit_index',
        'CLOVA_RESPONSE_CACHE_DIR': runtime / 'clova_responses',
    }
    for key, path in locations.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    # tempfile caches its first lookup; reset even when an embedding host used it.
    tempfile.tempdir = str(locations['TEMP'])
    os.environ['SP_PERMIT_REVIEW_SCOPE'] = REVIEW_SCOPE
    os.environ['SP_PERMIT_SEMANTIC_MODE'] = 'legacy'
    os.chdir(root)
    return root
