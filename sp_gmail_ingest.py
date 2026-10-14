# -*- coding: utf-8 -*-
"""Download PDF attachments from Gmail for the SP Streamlit MVP.

The trigger is intentionally simple for the first real experiment:

1. Poll Gmail over IMAP.
2. Accept only messages whose subject contains a configured keyword.
3. Save PDF attachments into the app inbox.

Default subject keyword: [식약처]
"""

from __future__ import annotations

import email
import hashlib
import imaplib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


DEFAULT_STORE_DIR = Path("incoming_sp_pdfs")
INDEX_FILE_NAME = "_gmail_index.json"
DEFAULT_SUBJECT_KEYWORD = "[식약처]"


@dataclass(frozen=True)
class GmailConfig:
    address: str
    app_password: str
    mailbox: str = "INBOX"
    search: str = "ALL"
    subject_keyword: str = DEFAULT_SUBJECT_KEYWORD

    @classmethod
    def from_env(cls) -> "GmailConfig | None":
        address = os.getenv("GMAIL_ADDRESS", "").strip()
        app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
        if not address or not app_password:
            return None

        return cls(
            address=address,
            app_password=app_password.replace(" ", ""),
            mailbox=os.getenv("GMAIL_MAILBOX", "INBOX").strip() or "INBOX",
            search=os.getenv("SP_GMAIL_SEARCH", "ALL").strip() or "ALL",
            subject_keyword=os.getenv("SP_GMAIL_SUBJECT_KEYWORD", DEFAULT_SUBJECT_KEYWORD).strip()
            or DEFAULT_SUBJECT_KEYWORD,
        )


def download_gmail_sp_pdfs(
    store_dir: str | Path = DEFAULT_STORE_DIR,
    *,
    config: GmailConfig | None = None,
    max_messages: int = 20,
) -> dict[str, Any]:
    """
    Fetch matching Gmail PDF attachments and save new files locally.

    변경 핵심:
    - PDF sha256만으로 중복 제거하지 않는다.
    - 같은 제조번호/같은 PDF 내용이어도 새 메일이면 제출함에 새로 저장한다.
    - 같은 메일을 polling으로 다시 읽는 경우만 UID + 첨부 순번 기준으로 중복 저장을 막는다.
    - 디버그 파일 _gmail_debug_last.json을 남겨 왜 안 떴는지 확인할 수 있게 한다.
    """

    resolved_config = config or GmailConfig.from_env()
    if resolved_config is None:
        return {
            "ok": False,
            "downloaded": [],
            "matched_messages": 0,
            "skipped": [],
            "error": "Gmail 계정과 앱 비밀번호가 설정되지 않았습니다.",
        }

    store_path = Path(store_dir)
    store_path.mkdir(parents=True, exist_ok=True)
    index_path = store_path / INDEX_FILE_NAME
    debug_path = store_path / "_gmail_debug_last.json"
    index = _load_index(index_path)

    downloaded: list[dict[str, Any]] = []
    skipped: list[str] = []
    debug_rows: list[dict[str, Any]] = []
    matched_messages = 0

    try:
        with imaplib.IMAP4_SSL("imap.gmail.com") as mailbox:
            mailbox.login(resolved_config.address, resolved_config.app_password)
            mailbox.select(resolved_config.mailbox)

            status, data = mailbox.uid("search", None, resolved_config.search)
            if status != "OK":
                raise RuntimeError(f"Gmail UID search failed: {status}")

            message_uids = data[0].split()[-max_messages:]

            for uid in reversed(message_uids):
                uid_text = _decode_message_id(uid)

                if _uid_already_checked(index, uid_text, resolved_config):
                    debug_rows.append(
                        {
                            "uid": uid_text,
                            "status": "uid_already_checked",
                        }
                    )
                    continue

                status, header_data = mailbox.uid(
                    "fetch",
                    uid,
                    "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE MESSAGE-ID)])",
                )
                if status == "OK" and header_data:
                    raw_header = _first_bytes_payload(header_data) or b""
                    header_message = email.message_from_bytes(raw_header, policy=policy.default)
                    subject = _decode_header_value(header_message.get("Subject", ""))
                    message_date = _message_datetime(header_message)
                    message_id_header = header_message.get("Message-ID") or ""
                    message_key = message_id_header or f"uid:{uid_text}"
                    debug_base = {
                        "uid": uid_text,
                        "message_id": message_id_header,
                        "message_key": message_key,
                        "subject": subject,
                        "received_at": message_date.isoformat(),
                        "mailbox": resolved_config.mailbox,
                        "search": resolved_config.search,
                        "subject_keyword": resolved_config.subject_keyword,
                    }
                    if not _subject_matches(subject, resolved_config.subject_keyword):
                        skipped.append(f"subject skipped: {subject}")
                        debug_rows.append(
                            {
                                **debug_base,
                                "status": "subject_skipped",
                                "reason": "subject keyword not matched",
                            }
                        )
                        _remember_checked_uid(index, uid_text, "subject_skipped", resolved_config)
                        continue

                status, message_data = mailbox.uid("fetch", uid, "(BODY.PEEK[])")
                if status != "OK" or not message_data:
                    skipped.append(f"uid fetch failed: {uid_text}")
                    debug_rows.append(
                        {
                            "uid": uid_text,
                            "status": "uid_fetch_failed",
                        }
                    )
                    continue

                raw_message = _first_bytes_payload(message_data)
                if not raw_message:
                    skipped.append(f"empty raw message: {uid_text}")
                    debug_rows.append(
                        {
                            "uid": uid_text,
                            "status": "empty_raw_message",
                        }
                    )
                    continue

                raw_message_digest = hashlib.sha256(raw_message).hexdigest()
                message = email.message_from_bytes(raw_message, policy=policy.default)
                subject = _decode_header_value(message.get("Subject", ""))
                message_date = _message_datetime(message)
                message_id_header = message.get("Message-ID") or ""
                message_key = message_id_header or f"uid:{uid_text}"

                debug_base = {
                    "uid": uid_text,
                    "message_id": message_id_header,
                    "message_key": message_key,
                    "subject": subject,
                    "received_at": message_date.isoformat(),
                    "mailbox": resolved_config.mailbox,
                    "search": resolved_config.search,
                    "subject_keyword": resolved_config.subject_keyword,
                }

                if not _subject_matches(subject, resolved_config.subject_keyword):
                    skipped.append(f"subject skipped: {subject}")
                    debug_rows.append(
                        {
                            **debug_base,
                            "status": "subject_skipped",
                            "reason": "subject keyword not matched",
                        }
                    )
                    _remember_checked_uid(index, uid_text, "subject_skipped", resolved_config)
                    continue

                matched_messages += 1
                attachment_count = 0
                pdf_attachment_count = 0

                for part in message.walk():
                    attachment_name = _decode_header_value(part.get_filename() or "")
                    if not attachment_name:
                        continue

                    attachment_count += 1

                    if not attachment_name.lower().endswith(".pdf"):
                        debug_rows.append(
                            {
                                **debug_base,
                                "status": "non_pdf_attachment_skipped",
                                "attachment_name": attachment_name,
                                "attachment_index": attachment_count,
                            }
                        )
                        continue

                    pdf_attachment_count += 1
                    payload = part.get_payload(decode=True)
                    if not payload:
                        skipped.append(f"empty attachment: {attachment_name}")
                        debug_rows.append(
                            {
                                **debug_base,
                                "status": "empty_attachment",
                                "attachment_name": attachment_name,
                                "attachment_index": attachment_count,
                            }
                        )
                        continue

                    digest = hashlib.sha256(payload).hexdigest()

                    # 같은 메일의 같은 첨부만 중복으로 본다.
                    # 같은 파일이더라도 새 메일 UID면 새 제출문서로 저장한다.
                    if _already_saved_same_uid_attachment(
                        index=index,
                        gmail_uid=uid_text,
                        attachment_index=attachment_count,
                        attachment_name=attachment_name,
                        digest=digest,
                    ):
                        skipped.append(f"same uid attachment already saved: {attachment_name}")
                        debug_rows.append(
                            {
                                **debug_base,
                                "status": "same_uid_attachment_skipped",
                                "attachment_name": attachment_name,
                                "attachment_index": attachment_count,
                                "sha256": digest,
                            }
                        )
                        continue

                    saved_name = _make_saved_name(message_date, attachment_name, store_path)
                    saved_path = store_path / saved_name
                    saved_path.write_bytes(payload)
                    attachment_type = _classify_pdf_attachment(attachment_name)

                    record = {
                        "file": saved_name,
                        "original_name": attachment_name,
                        "attachment_type": attachment_type,
                        "size_bytes": len(payload),
                        "sha256": digest,
                        "subject": subject,
                        "message_id": message_key,
                        "message_id_header": message_id_header,
                        "gmail_uid": uid_text,
                        "raw_message_digest": raw_message_digest,
                        "attachment_index": attachment_count,
                        "received_at": message_date.isoformat(),
                        "subject_keyword": resolved_config.subject_keyword,
                        "account_address": resolved_config.address,
                    }

                    # sha256 index는 조회 편의용으로만 유지한다.
                    # 중복 차단 기준으로 사용하지 않는다.
                    index.setdefault("sha256", {})[digest] = record
                    index.setdefault("files", {})[saved_name] = record
                    _remember_saved_uid_attachment(
                        index=index,
                        gmail_uid=uid_text,
                        attachment_index=attachment_count,
                        attachment_name=attachment_name,
                        digest=digest,
                        record=record,
                    )
                    downloaded.append(record)
                    debug_rows.append(
                        {
                            **debug_base,
                            "status": "saved",
                            "file": saved_name,
                            "attachment_name": attachment_name,
                            "attachment_index": attachment_count,
                            "attachment_type": attachment_type,
                            "sha256": digest,
                            "size_bytes": len(payload),
                        }
                    )

                if pdf_attachment_count == 0:
                    skipped.append(f"no pdf attachment: {subject}")
                    debug_rows.append(
                        {
                            **debug_base,
                            "status": "no_pdf_attachment",
                            "attachment_count": attachment_count,
                        }
                    )

                _remember_checked_uid(index, uid_text, "checked", resolved_config)

        _rebuild_related_files(index, store_path)
        _save_index(index_path, index)
        _save_gmail_debug(debug_path, debug_rows)

        return {
            "ok": True,
            "downloaded": downloaded,
            "matched_messages": matched_messages,
            "skipped": skipped,
            "debug_path": str(debug_path),
            "error": None,
        }
    except Exception as exc:
        error = _friendly_gmail_error(str(exc))
        _save_gmail_debug(
            debug_path,
            debug_rows
            + [
                {
                    "status": "error",
                    "error": error,
                }
            ],
        )
        return {
            "ok": False,
            "downloaded": downloaded,
            "matched_messages": matched_messages,
            "skipped": skipped,
            "debug_path": str(debug_path),
            "error": error,
        }


def _subject_matches(subject: str, keyword: str) -> bool:
    keyword = keyword.strip()
    if not keyword:
        return True
    return keyword.casefold() in subject.casefold()


def _classify_pdf_attachment(filename: str) -> str:
    normalized = re.sub(r"[\s_\-./()\[\]{}]+", "", filename.casefold())
    permit_tokens = (
        "허가서",
        "품목허가",
        "허가사항",
        "허가증",
        "permit",
        "authorization",
        "approval",
        "license",
    )
    if any(token in normalized for token in permit_tokens):
        return "permit"
    return "sp"


def _rebuild_related_files(index: dict[str, Any], store_path: Path | None = None) -> None:
    files = index.setdefault("files", {})
    by_message: dict[str, list[dict[str, Any]]] = {}

    for record in files.values():
        if not isinstance(record, dict):
            continue

        record.setdefault("attachment_type", _classify_pdf_attachment(str(record.get("file", ""))))

        if store_path is not None and not record.get("size_bytes"):
            try:
                record["size_bytes"] = (store_path / str(record.get("file", ""))).stat().st_size
            except OSError:
                record["size_bytes"] = 0

        message_id = str(record.get("message_id", "") or record.get("subject", ""))
        by_message.setdefault(message_id, []).append(record)

    for records in by_message.values():
        if len(records) > 1 and all(record.get("attachment_type") == "permit" for record in records):
            largest = max(records, key=lambda record: int(record.get("size_bytes") or 0))
            largest["attachment_type"] = "sp"

        sp_files = [
            str(record.get("file", ""))
            for record in records
            if record.get("attachment_type") != "permit"
        ]
        permit_files = [
            str(record.get("file", ""))
            for record in records
            if record.get("attachment_type") == "permit"
        ]

        for record in records:
            record["related_sp_files"] = [
                name for name in sp_files if name and name != record.get("file")
            ]
            record["related_permit_files"] = [
                name for name in permit_files if name and name != record.get("file")
            ]

            sha = record.get("sha256")
            if sha and sha in index.get("sha256", {}):
                index["sha256"][sha] = record


def _uid_attachment_key(
    *,
    gmail_uid: str,
    attachment_index: int,
    attachment_name: str,
    digest: str,
) -> str:
    raw = f"{gmail_uid or ''}::{attachment_index or 0}::{attachment_name or ''}::{digest or ''}"
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()


def _already_saved_same_uid_attachment(
    *,
    index: dict[str, Any],
    gmail_uid: str,
    attachment_index: int,
    attachment_name: str,
    digest: str,
) -> bool:
    key = _uid_attachment_key(
        gmail_uid=gmail_uid,
        attachment_index=attachment_index,
        attachment_name=attachment_name,
        digest=digest,
    )

    attachments = index.get("uid_attachments", {})
    if isinstance(attachments, dict) and key in attachments:
        return True

    files = index.get("files", {})
    if not isinstance(files, dict):
        return False

    for record in files.values():
        if not isinstance(record, dict):
            continue

        if str(record.get("gmail_uid", "")) != str(gmail_uid or ""):
            continue

        if int(record.get("attachment_index", 0) or 0) != int(attachment_index or 0):
            continue

        if str(record.get("original_name", "")) != str(attachment_name or ""):
            continue

        if str(record.get("sha256", "")) != str(digest or ""):
            continue

        return True

    return False


def _remember_saved_uid_attachment(
    *,
    index: dict[str, Any],
    gmail_uid: str,
    attachment_index: int,
    attachment_name: str,
    digest: str,
    record: dict[str, Any],
) -> None:
    key = _uid_attachment_key(
        gmail_uid=gmail_uid,
        attachment_index=attachment_index,
        attachment_name=attachment_name,
        digest=digest,
    )
    index.setdefault("uid_attachments", {})[key] = {
        "file": record.get("file", ""),
        "gmail_uid": gmail_uid,
        "attachment_index": attachment_index,
        "original_name": attachment_name,
        "sha256": digest,
    }


def _checked_uid_key(gmail_uid: str, config: GmailConfig) -> str:
    raw = "::".join(
        [
            str(gmail_uid or ""),
            str(config.mailbox or ""),
            str(config.search or ""),
            str(config.subject_keyword or ""),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()


def _uid_already_checked(index: dict[str, Any], gmail_uid: str, config: GmailConfig) -> bool:
    checked = index.get("checked_uids", {})
    return isinstance(checked, dict) and _checked_uid_key(gmail_uid, config) in checked


def _remember_checked_uid(
    index: dict[str, Any],
    gmail_uid: str,
    status: str,
    config: GmailConfig,
) -> None:
    gmail_uid = str(gmail_uid or "").strip()
    if not gmail_uid:
        return
    index.setdefault("checked_uids", {})[_checked_uid_key(gmail_uid, config)] = {
        "gmail_uid": gmail_uid,
        "status": status,
        "mailbox": config.mailbox,
        "search": config.search,
        "subject_keyword": config.subject_keyword,
        "checked_at": datetime.now().isoformat(),
    }


def _save_gmail_debug(path: Path, rows: list[dict[str, Any]]) -> None:
    try:
        path.write_text(
            json.dumps(
                {
                    "created_at": datetime.now().isoformat(),
                    "rows": rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _friendly_gmail_error(error: str) -> str:
    lowered = error.casefold()
    if "application-specific password required" in lowered:
        return (
            "Gmail이 일반 비밀번호 로그인을 거부했습니다. "
            "Google 계정에서 2단계 인증을 켠 뒤 16자리 앱 비밀번호를 생성해서 입력하세요."
        )
    if "invalid credentials" in lowered or "authentication failed" in lowered:
        return "Gmail 로그인에 실패했습니다. Gmail 주소와 16자리 앱 비밀번호를 다시 확인하세요."
    return error


def _load_index(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"sha256": {}, "files": {}, "uid_attachments": {}, "checked_uids": {}}
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"sha256": {}, "files": {}, "uid_attachments": {}, "checked_uids": {}}

    index.setdefault("sha256", {})
    index.setdefault("files", {})
    index.setdefault("uid_attachments", {})
    index.setdefault("checked_uids", {})
    return index


def _save_index(path: Path, index: dict[str, Any]) -> None:
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def _first_bytes_payload(message_data: list[Any]) -> bytes | None:
    for item in message_data:
        if isinstance(item, tuple) and isinstance(item[1], bytes):
            return item[1]
    return None


def _decode_message_id(message_id: bytes) -> str:
    return message_id.decode("utf-8", errors="replace")


def _decode_header_value(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _message_datetime(message: email.message.EmailMessage) -> datetime:
    raw_date = message.get("Date")
    if raw_date:
        try:
            parsed = parsedate_to_datetime(raw_date)
            if parsed:
                return parsed
        except (TypeError, ValueError):
            pass
    return datetime.now().astimezone()


def _make_saved_name(received_at: datetime, filename: str, store_path: Path) -> str:
    stem = _sanitize_filename(Path(filename).stem) or "sp_document"
    suffix = Path(filename).suffix.lower() or ".pdf"
    timestamp = received_at.strftime("%Y%m%d_%H%M%S")
    candidate = f"{timestamp}_{stem}{suffix}"
    counter = 2

    while (store_path / candidate).exists():
        candidate = f"{timestamp}_{stem}_{counter}{suffix}"
        counter += 1

    return candidate


def _sanitize_filename(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:120]
