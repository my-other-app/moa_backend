"""Files on the desk: the help form's screenshots (uploaded first, named by
an opaque key when the request is sent), a mail's attachments, a reply's
files. What a file is, is read from its first bytes, never from its name or
the type the browser claimed. Served only through the API, with headers that
let nothing run on our origin (the club mailbox's)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Response, UploadFile
from sqlalchemy import select

from app.api.support import store
from app.api.support.models import SupportAttachments
from app.response import CustomHTTPException

FORM_MAX_BYTES = 5 * 1024 * 1024
FORM_MAX_FILES = 3
TEAM_MAX_BYTES = 10 * 1024 * 1024
#: An upload not sent with a request within this long is deleted (the sweep).
LOOSE_FOR = timedelta(days=1)
FORM_TYPES = ("image/png", "image/jpeg", "image/webp", "application/pdf")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sniff(data: bytes) -> Optional[str]:
    """The type its first bytes say, among the kinds the desk keeps as themselves."""
    head = data[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def safe_name(name: Optional[str], fallback: str = "file") -> str:
    name = "".join(ch for ch in str(name or "") if ch.isprintable() and ch not in "\\/\r\n\"").strip()
    return (name or fallback)[:255]


async def read_upload(upload: UploadFile, limit: int) -> bytes:
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise CustomHTTPException(413, message=f"Each file can be {limit // (1024 * 1024)} MB at most.", error_code="FILE_TOO_LARGE",
                                  errors={"limit_bytes": limit})
    if not data:
        raise CustomHTTPException(422, message="That file is empty.", error_code="EMPTY_FILE")
    return data


async def keep_form_upload(session, upload: UploadFile, user_id: Optional[int]) -> SupportAttachments:
    """One of the form's files: png, jpg, webp or pdf, at most 5 MB."""
    data = await read_upload(upload, FORM_MAX_BYTES)
    kind = sniff(data)
    if kind not in FORM_TYPES:
        raise CustomHTTPException(415, message="Send a screenshot (PNG, JPG or WebP) or a PDF.", error_code="UNSUPPORTED_FILE")
    return await keep(session, data, kind, safe_name(upload.filename, "screenshot"), user_id=user_id, upload=True)


async def keep(session, data: bytes, content_type: str, filename: str, *, user_id: Optional[int] = None,
               upload: bool = False, case_id: Optional[int] = None) -> SupportAttachments:
    key = store.key_for("support", case_id or "uploads", filename)
    await asyncio.to_thread(store.put, key, data, content_type)
    row = SupportAttachments(
        upload_key=store.upload_key() if upload else None, storage_key=key, filename=filename, content_type=content_type,
        size_bytes=len(data), uploaded_by=user_id, case_id=case_id,
    )
    session.add(row)
    await session.flush()
    return row


async def claim(session, keys: list[str], *, user_id: Optional[int], limit: int = FORM_MAX_FILES) -> list[SupportAttachments]:
    """The uploads these keys name, not yet on a case, uploaded within a day
    (by this account, when one uploaded them). 422 UNKNOWN_ATTACHMENT for any other."""
    keys = list(dict.fromkeys(k for k in keys or [] if k))
    if not keys:
        return []
    if len(keys) > limit:
        raise CustomHTTPException(422, message=f"At most {limit} files.", error_code="TOO_MANY_FILES")
    rows = (await session.scalars(
        select(SupportAttachments).where(
            SupportAttachments.upload_key.in_(keys), SupportAttachments.case_id.is_(None),
            SupportAttachments.created_at >= _now() - LOOSE_FOR,
        )
    )).all()
    ok = [r for r in rows if r.uploaded_by is None or r.uploaded_by == user_id]
    if len(ok) != len(keys):
        raise CustomHTTPException(422, message="A file couldn't be found; upload it again.", error_code="UNKNOWN_ATTACHMENT",
                                  errors={"field": "attachments"})
    return ok


async def serve(row: SupportAttachments) -> Response:
    from app.api.club_mailbox.router import media_headers

    data = await asyncio.to_thread(store.get, row.storage_key)
    if data is None:
        raise CustomHTTPException(410, message="That file can't be read any more.", error_code="FILE_GONE")
    media_type, headers = media_headers(row.content_type, row.filename)
    return Response(content=data, media_type=media_type, headers=headers)
