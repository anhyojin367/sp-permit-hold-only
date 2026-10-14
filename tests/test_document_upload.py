from pathlib import Path
import json
import fitz
import pytest

from sp_document_upload import save_submission, replace_permits, list_uploaded_sps


def pdf_bytes(text="sample"):
    with fitz.open() as doc:
        doc.new_page().insert_text((50, 50), text)
        return doc.tobytes()


def index_for(path):
    return json.loads((path.parent / "_gmail_index.json").read_text(encoding="utf-8"))["files"]


def test_submission_without_gmail_and_separate_permits(tmp_path):
    a, b = save_submission(tmp_path, [("a.pdf", pdf_bytes()), ("a.pdf", pdf_bytes("b"))], [("permit.pdf", pdf_bytes("permit"))])
    assert a != b and len(list_uploaded_sps(tmp_path)) == 2
    records = index_for(a)
    assert records[a.name]["manual_permit_files"] == records[b.name]["manual_permit_files"]
    assert len(records) == 3


def test_replacement_is_scoped_and_preserves_old_evidence(tmp_path):
    a, b = save_submission(tmp_path, [("a.pdf", pdf_bytes()), ("b.pdf", pdf_bytes())], [("old.pdf", pdf_bytes())])
    old = index_for(a)[a.name]["manual_permit_files"]
    replace_permits(a, [("new.pdf", pdf_bytes("new"))])
    records = index_for(a)
    assert records[a.name]["manual_permit_files"] != old
    assert records[b.name]["manual_permit_files"] == old
    assert (a.parent / old[0]).exists()
    assert records[a.name]["permit_link_history"][0]["files"] == old


@pytest.mark.parametrize("name,data", [("x.pdf", b"fake"), ("x.exe", b"%PDF-1.7"), ("x.pdf", b"%PDF-1.7 damaged")])
def test_invalid_upload_creates_no_submission(tmp_path, name, data):
    with pytest.raises(ValueError):
        save_submission(tmp_path, [("ok.pdf", pdf_bytes())], [(name, data)])
    assert not list(tmp_path.iterdir())


def test_path_traversal_names_are_contained(tmp_path):
    [a] = save_submission(tmp_path, [("../../escape.pdf", pdf_bytes())])
    assert a.is_relative_to(tmp_path) and a.name.endswith("escape.pdf")
    assert list_uploaded_sps(tmp_path) == [a]


def test_long_original_filename_keeps_pdf_suffix_and_short_storage_name(tmp_path):
    name = "긴파일이름" * 60 + ".pdf"
    [path] = save_submission(tmp_path, [(name, pdf_bytes())])
    assert path.suffix == ".pdf" and len(path.name) < 95
    assert index_for(path)[path.name]["original_name"] == name


def test_widget_upload_limit_matches_pdf_validation_limit():
    import toml
    from sp_document_upload import MAX_PDF_BYTES
    config = toml.loads((Path(__file__).resolve().parents[1] / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert config["server"]["maxUploadSize"] * 1024 * 1024 == MAX_PDF_BYTES


def test_over_limit_rejection_happens_before_any_file_is_saved(tmp_path, monkeypatch):
    import sp_document_upload as upload
    monkeypatch.setattr(upload, "MAX_PDF_BYTES", 4)
    with pytest.raises(ValueError, match="100MB"):
        save_submission(tmp_path, [("oversize.pdf", pdf_bytes())])
    assert not list(tmp_path.iterdir())


def test_excess_file_count_creates_no_partial_submission(tmp_path):
    sample = pdf_bytes()
    with pytest.raises(ValueError, match="30"):
        save_submission(tmp_path, [("sample.pdf", sample)] * 31)
    assert not list(tmp_path.iterdir())


def test_relative_store_returns_same_canonical_paths_as_inbox(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = save_submission(Path("incoming_sp_pdfs"), [("sample.pdf", pdf_bytes())])
    assert paths == list_uploaded_sps(Path("incoming_sp_pdfs"))
    assert all(path.is_absolute() for path in paths)


def test_narrow_dialog_style_is_scoped_to_upload_not_original_ui():
    from sp_document_upload import UPLOAD_DIALOG_STYLE
    assert '@media (max-width: 600px)' in UPLOAD_DIALOG_STYLE
    assert '[role="dialog"]:has(#sp-upload-dialog-anchor)' in UPLOAD_DIALOG_STYLE
    assert 'min-width: 0' in UPLOAD_DIALOG_STYLE
    assert '<span id="sp-upload-dialog-anchor" hidden>' in UPLOAD_DIALOG_STYLE
