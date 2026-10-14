"""Validated local submissions using the inbox's existing attachment contract."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Iterable

import fitz

MAX_PDF_BYTES = 100 * 1024 * 1024
_LOCK = threading.RLock()
Upload = tuple[str, bytes]

# Only the new upload dialog is affected; the original desktop layout is intact.
UPLOAD_DIALOG_STYLE = """
<style>
@media (max-width: 600px) {
  [role="dialog"]:has(#sp-upload-dialog-anchor) {
    min-width: 0 !important;
    width: calc(100vw - 32px) !important;
    max-width: calc(100vw - 32px) !important;
    margin-left: 12px !important;
    margin-right: 12px !important;
  }
}
</style>
<span id="sp-upload-dialog-anchor" hidden></span>
"""


def validate_pdf(name: str, data: bytes) -> str:
    name = PureWindowsPath(name.replace("/", "\\")).name
    if not name.lower().endswith(".pdf"):
        raise ValueError("PDF 파일만 등록할 수 있습니다.")
    if not data or len(data) > MAX_PDF_BYTES:
        raise ValueError("PDF 파일은 0바이트보다 크고 100MB 이하여야 합니다.")
    if not data.lstrip().startswith(b"%PDF-"):
        raise ValueError("유효한 PDF 파일이 아닙니다.")
    try:
        with fitz.open(stream=data, filetype="pdf") as document:
            if document.needs_pass:
                raise ValueError("암호를 해제한 PDF를 등록해 주세요.")
            if not len(document):
                raise ValueError("페이지가 없는 PDF입니다.")
    except (RuntimeError, fitz.FileDataError) as exc:
        raise ValueError("손상된 PDF를 읽을 수 없습니다.") from exc
    return re.sub(r'[^\w가-힣.()\[\] -]', "_", name)


def _prepare(uploads: Iterable[Upload]) -> list[Upload]:
    return [(validate_pdf(name, data), data) for name, data in uploads]


def _write_index(folder: Path, index: dict) -> None:
    temporary = folder / f".index-{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, folder / "_gmail_index.json")
    finally:
        temporary.unlink(missing_ok=True)


def _save_file(folder: Path, name: str, data: bytes, kind: str, submission: str) -> dict:
    digest = hashlib.sha256(data).hexdigest()
    saved = f"{kind}_{uuid.uuid4().hex[:12]}_{Path(name).stem[:70]}.pdf"
    (folder / saved).write_bytes(data)
    return {"file": saved, "original_name": name, "attachment_type": kind,
            "source": "upload", "sha256": digest, "size_bytes": len(data),
            "message_id": submission, "subject": "직접 업로드",
            "received_at": datetime.now(timezone.utc).isoformat()}


def save_submission(store: Path, sp_files: Iterable[Upload], permit_files: Iterable[Upload] = ()) -> list[Path]:
    sps, permits = _prepare(sp_files), _prepare(permit_files)
    if not sps:
        raise ValueError("SP PDF를 하나 이상 선택해 주세요.")
    if len(sps) + len(permits) > 30:
        raise ValueError("한 번에 등록 가능한 파일은 30개입니다.")
    submission = uuid.uuid4().hex
    # Inbox entries are absolute; callbacks must use the same path identity.
    folder = Path(store).resolve() / "manual" / submission
    with _LOCK:
        folder.mkdir(parents=True, exist_ok=False)
        records = [_save_file(folder, name, data, kind, submission)
                   for kind, files in (("sp", sps), ("permit", permits)) for name, data in files]
        permit_names = [r["file"] for r in records if r["attachment_type"] == "permit"]
        for record in records:
            if record["attachment_type"] == "sp":
                record["manual_permit_files"] = permit_names
                record["related_permit_files"] = permit_names
        _write_index(folder, {"files": {r["file"]: r for r in records}, "schema_version": 1})
    return [folder / r["file"] for r in records if r["attachment_type"] == "sp"]


def replace_permits(sp_path: Path, permit_files: Iterable[Upload]) -> None:
    """Keep old permit bytes for audit; replace only this SP's explicit link."""
    permits = _prepare(permit_files)
    if not permits:
        raise ValueError("연결할 허가서 PDF를 선택해 주세요.")
    if len(permits) > 30:
        raise ValueError("한 번에 등록 가능한 파일은 30개입니다.")
    with _LOCK:
        folder = sp_path.parent
        index = json.loads((folder / "_gmail_index.json").read_text(encoding="utf-8"))
        record = index.get("files", {}).get(sp_path.name)
        if not sp_path.is_file() or not record or record.get("attachment_type") != "sp":
            raise ValueError("등록된 SP 문서만 허가서를 변경할 수 있습니다.")
        history = record.setdefault("permit_link_history", [])
        history.append({"files": record.get("manual_permit_files", record.get("related_permit_files", [])),
                        "replaced_at": datetime.now(timezone.utc).isoformat()})
        new_records = [_save_file(folder, name, data, "permit", uuid.uuid4().hex) for name, data in permits]
        index["files"].update({r["file"]: r for r in new_records})
        record["manual_permit_files"] = [r["file"] for r in new_records]
        record["related_permit_files"] = record["manual_permit_files"]
        _write_index(folder, index)


def list_uploaded_sps(store: Path) -> list[Path]:
    paths = []
    for index_path in (Path(store) / "manual").glob("*/_gmail_index.json"):
        try:
            files = json.loads(index_path.read_text(encoding="utf-8")).get("files", {})
            for name, record in files.items():
                candidate = (index_path.parent / name).resolve()
                if candidate.parent != index_path.parent.resolve():
                    continue
                if record.get("attachment_type") == "sp" and candidate.is_file():
                    paths.append(candidate)
        except (OSError, ValueError, AttributeError):
            continue
    return sorted(paths, key=lambda path: path.stat().st_mtime_ns, reverse=True)


def render_upload_dialog(store: Path, documents: list, on_saved) -> None:
    import streamlit as st

    @st.dialog("SP 및 허가서 업로드", width="large")
    def dialog():
        st.markdown(UPLOAD_DIALOG_STYLE, unsafe_allow_html=True)
        st.caption("PDF 한 파일당 100MB 이하 · 한 번에 최대 30개 · 기존 원본을 덮어쓰지 않습니다.")
        mode = st.radio("등록 방식", ["새 SP 업로드", "기존 SP의 허가서 추가·교체"], horizontal=True)
        target = None
        sp_files = []
        if mode == "새 SP 업로드":
            sp_files = st.file_uploader("SP PDF", type=["pdf"], accept_multiple_files=True, key="manual_sp_files")
        else:
            if not documents:
                st.info("먼저 SP 문서를 등록해 주세요.")
                return
            target = st.selectbox("연결할 SP", documents, format_func=lambda doc: f"{doc.product} · {doc.path.name}")
        permit_files = st.file_uploader("허가서 PDF (새 SP 등록 시 선택 사항)", type=["pdf"], accept_multiple_files=True, key="manual_permit_files")
        if permit_files:
            names = ", ".join(f.name for f in permit_files)
            st.caption(f"연결할 허가서: {names}")
        confirmed = st.checkbox("선택한 SP와 허가서의 제품 및 연결 관계를 확인했습니다.")
        if st.button("등록", type="primary", disabled=not confirmed):
            try:
                uploaded_permits = [(f.name, f.getvalue()) for f in permit_files]
                if target is None:
                    paths = save_submission(store, [(f.name, f.getvalue()) for f in sp_files], uploaded_permits)
                    selected = paths[0]
                else:
                    replace_permits(target.path, uploaded_permits)
                    selected = target.path
                on_saved(selected)
                st.rerun()
            except (ValueError, OSError) as error:
                st.error(str(error))
    dialog()
