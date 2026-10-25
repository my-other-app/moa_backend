"""Sending a photo, a video, a sound or a document from the club's inbox.

The dashboard uploads the file (multipart/form-data: `size`, then the
`file`; `caption` and `reply_to` anywhere before or after it). Nothing of it
is kept on our server, not even for a moment on disk: the request's bytes
are read as they arrive and passed on, as they arrive, to Meta's media
upload on the club's number (`POST /{phone}/media`, multipart, with an exact
Content-Length from `size`), which answers a media id good for 30 days. The
message then names that id (`image`, `video`, `audio` or `document`, with
its caption -- up to 1,024 characters, not on audio -- and a document's
file name), as a reply when `reply_to` names one of the conversation's
messages. The message is kept with the media id, so the inbox shows it
through the same authenticated proxy (and safe headers) as what arrives.

What Meta takes (its media page), checked before a byte is sent:

| kind     | types                                         | at most |
|----------|-----------------------------------------------|---------|
| image    | image/jpeg, image/png                         | 5 MB    |
| video    | video/mp4, video/3gpp                         | 16 MB   |
| audio    | audio/aac, audio/amr, audio/mpeg, audio/mp4, audio/ogg (Opus) | 16 MB |
| document | text/plain, PDF, Word, Excel, PowerPoint (old and new) | 100 MB |

A MB here is 1,000,000 bytes, below what Meta allows whichever it means.
The type is the part's own, or its file name's when the browser gave none;
the first bytes must agree for a JPEG, PNG, PDF, MP4 or Ogg (which must be
Opus). Only inside the 24-hour window (409 WINDOW_CLOSED), as any free-form
message; never to someone blocked.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

import httpx
from python_multipart.multipart import MultipartParser, parse_options_header

from app.api.club_whatsapp import graph, inbox, optout, replies
from app.api.club_whatsapp import service as accounts
from app.api.club_whatsapp.models import OUT, PENDING, VIA_INBOX, ClubWhatsAppConversations, ClubWhatsAppMessages
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

MB = 1_000_000
#: kind -> (types, the largest Meta takes).
KINDS: dict[str, tuple[frozenset, int]] = {
    "image": (frozenset({"image/jpeg", "image/png"}), 5 * MB),
    "video": (frozenset({"video/mp4", "video/3gpp"}), 16 * MB),
    "audio": (frozenset({"audio/aac", "audio/amr", "audio/mpeg", "audio/mp4", "audio/ogg"}), 16 * MB),
    "document": (frozenset({
        "text/plain", "application/pdf", "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint", "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }), 100 * MB),
}
#: A type from the file's name, when the browser gave none (or "octet-stream").
BY_EXTENSION = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "mp4": "video/mp4", "3gp": "video/3gpp",
    "aac": "audio/aac", "amr": "audio/amr", "mp3": "audio/mpeg", "m4a": "audio/mp4", "ogg": "audio/ogg", "opus": "audio/ogg",
    "txt": "text/plain", "pdf": "application/pdf", "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
LARGEST = max(limit for _types, limit in KINDS.values())
#: Room for the form's other fields and its boundaries.
FORM_ROOM = 64 * 1024
CAPTION_MAX = 1024
FILENAME_MAX = 240
#: The bytes read before the type is checked against them.
SNIFF = 64
#: A field other than the file is short.
FIELD_MAX = 8 * 1024


def _refuse(code: int, message: str, error_code: str, errors: Optional[dict] = None) -> CustomHTTPException:
    return CustomHTTPException(status_code=code, message=message, error_code=error_code, errors=errors)


def _base(mime: Optional[str]) -> str:
    return (mime or "").split(";", 1)[0].strip().lower()


def classify(mime: Optional[str], filename: Optional[str]) -> tuple[str, str, int]:
    """(kind, type, the most bytes) for a file, or 415 MEDIA_TYPE."""
    base = _base(mime)
    if base in ("", "application/octet-stream"):
        ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
        base = BY_EXTENSION.get(ext, base)
    if base == "audio/opus":
        base = "audio/ogg"
    for kind, (types, limit) in KINDS.items():
        if base in types:
            return kind, base, limit
    raise _refuse(415, "WhatsApp can't send that kind of file. Send a JPEG or PNG photo, an MP4 or 3GP video, an "
                       "AAC, AMR, MP3, M4A or Ogg (Opus) sound, or a TXT, PDF, Word, Excel or PowerPoint document.",
                  "MEDIA_TYPE", {"mime": base or None})


def sniff_ok(mime: str, head: bytes) -> bool:
    """Whether the first bytes are what the type says, where that is cheap to tell."""
    if mime == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if mime == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "application/pdf":
        return head.lstrip()[:5] == b"%PDF-"
    if mime in ("video/mp4", "audio/mp4"):
        return head[4:8] == b"ftyp"
    if mime == "audio/ogg":
        # Meta takes Ogg with Opus only: its first page names the codec.
        return head.startswith(b"OggS") and b"OpusHead" in head
    return True


def clean_filename(raw: Optional[str]) -> Optional[str]:
    name = "".join(ch for ch in (raw or "") if ch.isprintable() and ch not in '"\\/\r\n')
    name = " ".join(name.split()).strip(" .")
    return name[:FILENAME_MAX] or None


def check_caption(kind: str, caption: Optional[str]) -> Optional[str]:
    text = (caption or "").strip() or None
    if text is None:
        return None
    if kind == "audio":
        raise _refuse(422, "A sound can't have a caption on WhatsApp.", "CAPTION_NOT_ALLOWED", {"field": "caption"})
    if len(text) > CAPTION_MAX:
        raise _refuse(422, f"A caption is at most {CAPTION_MAX} characters.", "CAPTION_TOO_LONG", {"field": "caption"})
    return text


# ─── Reading the upload as it arrives ────────────────────────────────────


@dataclass
class _Form:
    """A multipart body read as it arrives (python-multipart's push parser):
    short fields kept, the file's bytes handed on as they come, never kept."""

    boundary: bytes
    fields: dict = field(default_factory=dict)
    filename: Optional[str] = None
    file_type: Optional[str] = None
    file_started: bool = False
    file_done: bool = False
    chunks: list = field(default_factory=list)
    received: int = 0
    _headers: dict = field(default_factory=dict)
    _hname: bytes = b""
    _hvalue: bytes = b""
    _part: Optional[str] = None
    _is_file: bool = False
    _buf: bytearray = field(default_factory=bytearray)

    def __post_init__(self) -> None:
        self.parser = MultipartParser(self.boundary, callbacks={
            "on_part_begin": self._begin, "on_header_field": self._hfield, "on_header_value": self._hval,
            "on_header_end": self._hend, "on_headers_finished": self._headers_done, "on_part_data": self._data,
            "on_part_end": self._end,
        })

    def _begin(self) -> None:
        self._headers, self._part, self._is_file, self._buf = {}, None, False, bytearray()

    def _hfield(self, data: bytes, start: int, end: int) -> None:
        self._hname += data[start:end]

    def _hval(self, data: bytes, start: int, end: int) -> None:
        self._hvalue += data[start:end]

    def _hend(self) -> None:
        self._headers[self._hname.lower()] = self._hvalue
        self._hname, self._hvalue = b"", b""

    def _headers_done(self) -> None:
        _disp, options = parse_options_header(self._headers.get(b"content-disposition", b""))
        self._part = options.get(b"name", b"").decode("utf-8", "replace")
        if self._part == "file":
            if self.file_started:
                raise _refuse(422, "Send one file at a time.", "MEDIA_ONE_FILE")
            self._is_file, self.file_started = True, True
            self.filename = clean_filename(options.get(b"filename", b"").decode("utf-8", "replace"))
            self.file_type = self._headers.get(b"content-type", b"").decode("latin-1").strip() or None

    def _data(self, data: bytes, start: int, end: int) -> None:
        if self._is_file:
            piece = bytes(data[start:end])
            self.received += len(piece)
            self.chunks.append(piece)
        else:
            self._buf += data[start:end]
            if len(self._buf) > FIELD_MAX:
                raise _refuse(422, "A field of the upload is too long.", "INVALID_UPLOAD")

    def _end(self) -> None:
        if self._is_file:
            self.file_done = True
        elif self._part:
            self.fields[self._part] = self._buf.decode("utf-8", "replace")

    def write(self, chunk: bytes) -> None:
        self.parser.write(chunk)

    def take(self) -> list[bytes]:
        out, self.chunks = self.chunks, []
        return out


def _boundary(content_type: Optional[str]) -> bytes:
    kind, options = parse_options_header((content_type or "").encode("latin-1", "replace"))
    boundary = options.get(b"boundary")
    if kind != b"multipart/form-data" or not boundary:
        raise _refuse(422, "Upload the file as multipart/form-data.", "INVALID_UPLOAD")
    return boundary


async def _until_file(form: _Form, stream: AsyncIterator[bytes]) -> None:
    """Read until the file's headers and its first bytes (enough to tell its type) are in."""
    async for chunk in stream:
        form.write(chunk)
        if form.file_started and (form.received >= SNIFF or form.file_done):
            return
    if not form.file_started:
        raise _refuse(422, "Attach a file.", "MEDIA_MISSING")


async def _file_bytes(form: _Form, stream: AsyncIterator[bytes], size: int) -> AsyncIterator[bytes]:
    """The file's bytes, from what is already read and then as the request
    gives them; the upload fails (and Meta keeps nothing) when it isn't
    exactly `size` long."""
    if form.received > size:
        raise _refuse(422, "The file is longer than the size the upload gave.", "MEDIA_SIZE_MISMATCH")
    for piece in form.take():
        yield piece
    while not form.file_done:
        try:
            chunk = await stream.__anext__()
        except StopAsyncIteration:
            break
        form.write(chunk)
        for piece in form.take():
            if form.received > size:
                raise _refuse(422, "The file is longer than the size the upload gave.", "MEDIA_SIZE_MISMATCH")
            yield piece
    if not form.file_done or form.received != size:
        raise _refuse(422, "The file isn't the size the upload gave.", "MEDIA_SIZE_MISMATCH")


async def upload(keys: graph.Keys, mime: str, filename: str, size: int, data: AsyncIterator[bytes]) -> str:
    """Meta's media upload on the club's number, streamed: the media id."""
    boundary = secrets.token_hex(16)
    safe_name = (filename or "file").replace('"', "").encode("utf-8")
    head = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"messaging_product\"\r\n\r\nwhatsapp\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\n{mime}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\""
    ).encode("utf-8") + safe_name + f"\"\r\nContent-Type: {mime}\r\n\r\n".encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")

    async def body() -> AsyncIterator[bytes]:
        yield head
        async for piece in data:
            yield piece
        yield tail

    headers, query = graph._auth(keys, None)
    headers = {**headers, "Content-Type": f"multipart/form-data; boundary={boundary}",
               "Content-Length": str(len(head) + size + len(tail))}
    try:
        async with graph._client(120.0) as client:
            r = await client.post(graph.url(f"{keys.phone_number_id}/media"), headers=headers, params=query or None, content=body())
    except httpx.HTTPError as exc:
        raise graph.GraphError(f"Meta could not be reached ({exc.__class__.__name__})", unreachable=True) from None
    if r.status_code >= 300:
        raise graph._error(r)
    try:
        media_id = str((r.json() or {}).get("id") or "")
    except ValueError:
        media_id = ""
    if not media_id:
        raise graph.GraphError("Meta took the file without giving it an id", unreachable=True)
    return media_id


# ─── Sending it ──────────────────────────────────────────────────────────


async def send(session, request, club_id: int, conv: ClubWhatsAppConversations, actor: inbox.Actor,
               actor_user=None) -> ClubWhatsAppMessages:
    """Read the upload, hand it to Meta and send it to them: the message as kept."""
    account, keys = await inbox._sending_account(session, club_id, actor_user)
    await optout.check_send(session, conv, kind="media")
    if not inbox.window_open(conv):
        expires = inbox.window_expires_at(conv)
        raise _refuse(409, "It's been more than 24 hours since they last wrote. WhatsApp only lets you send an approved "
                           "template now.", "WINDOW_CLOSED", {"window_expires_at": expires.isoformat() if expires else None})
    if conv.business_phone_id != account.phone_number_id:
        raise _refuse(409, "This conversation was on another of your numbers.", "OTHER_NUMBER")
    length = (request.headers.get("content-length") or "").strip()
    if length.isdigit() and int(length) > LARGEST + FORM_ROOM:
        raise _refuse(413, "That file is larger than WhatsApp sends (100 MB at most, for a document).", "MEDIA_TOO_LARGE",
                      {"max_bytes": LARGEST})
    form = _Form(_boundary(request.headers.get("content-type")))
    # Nothing of the session is held while the bytes come and go.
    await session.commit()
    stream = request.stream().__aiter__()
    await _until_file(form, stream)
    kind, mime, limit = classify(form.file_type, form.filename)
    size_text = (form.fields.get("size") or "").strip()
    if not size_text.isdigit() or int(size_text) <= 0:
        raise _refuse(422, "Give the file's size (in bytes) before the file.", "MEDIA_SIZE_MISSING", {"field": "size"})
    size = int(size_text)
    if size > limit:
        raise _refuse(413, f"WhatsApp sends a {kind} of {limit // MB} MB at most.", "MEDIA_TOO_LARGE",
                      {"kind": kind, "max_bytes": limit})
    head = b"".join(form.chunks)[:SNIFF]
    if not sniff_ok(mime, head):
        raise _refuse(415, "That file isn't what its type says"
                      + (" (WhatsApp takes Ogg sound only in Opus)." if mime == "audio/ogg" else "."), "MEDIA_TYPE", {"mime": mime})
    # What came before the file is checked before a byte goes to Meta.
    if "caption" in form.fields:
        check_caption(kind, form.fields.get("caption"))
    filename = form.filename or f"file.{mime.rsplit('/', 1)[-1][:8]}"
    try:
        media_id = await upload(keys, mime, filename, size, _file_bytes(form, stream, size))
    except graph.GraphError as exc:
        if exc.unreachable:
            raise _refuse(502, "Couldn't reach WhatsApp with the file. Try again.", "META_UNREACHABLE") from None
        if graph.is_token_problem(exc):
            _status, code, words = graph.explain(exc)
            await accounts.mark_failed(session, account, words, exc.code, source=accounts.READ)
            await session.commit()
            raise _refuse(409, f"Meta didn't accept your number's keys. {words}", code) from None
        raise _refuse(422, f"WhatsApp didn't take the file: {exc.message}"[:300], "MEDIA_REFUSED", {"meta_code": exc.code}) from None
    # Whatever followed the file (a caption, the message it answers).
    async for chunk in stream:
        form.write(chunk)
    caption = check_caption(kind, form.fields.get("caption"))
    reply_to = (form.fields.get("reply_to") or "").strip()
    context = await replies.context_for(session, conv, int(reply_to)) if reply_to.isdigit() else None
    part: dict = {"id": media_id}
    if caption:
        part["caption"] = caption
    if kind == "document":
        part["filename"] = filename
    body = {"messaging_product": "whatsapp", "recipient_type": "individual", **inbox.recipient(conv), "type": kind, kind: part}
    if context:
        body["context"] = {"message_id": context}
    row = ClubWhatsAppMessages(
        conversation_id=conv.id, club_id=club_id, direction=OUT, type=kind, body=caption, media_id=media_id[:128],
        media_mime=mime, media_filename=filename if kind == "document" else form.filename, context_wamid=context,
        status=PENDING, via=VIA_INBOX, sent_by_user_id=actor.user_id, sent_by_member_id=actor.member_id, sent_by_name=actor.name,
    )
    return await inbox._send(session, account, keys, conv, body, row)
