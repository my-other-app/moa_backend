"""Conditional GETs for the API's JSON: an ETag on each answer, and a 304
when the caller already holds it.

The app keeps what it last read and asks again with that answer's tag in
If-None-Match. When nothing changed, the reply is a 304 of a few hundred
bytes instead of the whole list again. The route still runs, so its reads
and side effects (a view counted, guests merged) happen as before. Only
the bytes sent back shrink.

What gets a tag is a GET or HEAD under /api/v1/ that the route answered
200 with JSON (application/json or a +json type). It must also have a
Content-Length of at most MAX_BODY, no ETag of its own, no Set-Cookie and
no `no-store`. Everything else passes through as it was, unbuffered: the
live room's stream (text/event-stream, no length), the file, PDF and image
downloads, redirects, errors, HTML and CSV. A route that sets its own ETag
(the certificate and ticket downloads) keeps it.

The tag is weak, W/"<32 hex>", a fast hash of the bytes sent: xxh3-128
when the xxhash package is installed, else BLAKE2b-128. A personal answer
gets a tag of that person's own body. Cache-Control is never written here:
whatever the route set (public, private, or nothing) is what goes out, on
the 200 and on the 304. So a tag never makes a private answer shared.
The 304 carries the headers RFC 9110 15.4.5 asks of it (Cache-Control,
Content-Location, Date, ETag, Expires and Vary, as the 200 would have had
them) and none of the route's others. The middlewares outside this one
(CORS, the security headers, the request id) add theirs to it as to any
response.

Plain ASGI (see ProcessingTimeMiddleware for why). The response's start
is held back until its body has arrived, which for a JSON answer is one
message. APP_ETAGS=false passes every request straight through.
"""
import hashlib
from typing import Optional

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings

try:
    import xxhash
except ImportError:
    xxhash = None

#: Only the API's own routes. Not the short links, the health checks or /docs.
PREFIX = "/api/v1/"
METHODS = frozenset({"GET", "HEAD"})
#: A larger answer goes out untagged rather than being hashed (an export,
#: an admin list). Hashing 2 MiB with BLAKE2b takes about 2 ms.
MAX_BODY = 2 * 1024 * 1024
#: What a 304 keeps of the 200's headers (RFC 9110 15.4.5; Starlette's
#: StaticFiles keeps the same).
KEPT_BY_304 = frozenset({b"cache-control", b"content-location", b"date", b"etag", b"expires", b"vary"})


if xxhash is not None:

    def digest(body: bytes) -> str:
        return xxhash.xxh3_128_hexdigest(body)

else:

    def digest(body: bytes) -> str:
        return hashlib.blake2b(body, digest_size=16).hexdigest()


def weak_etag(body: bytes) -> str:
    return f'W/"{digest(body)}"'


def _opaque(tag: str) -> str:
    return tag[2:] if tag.startswith("W/") else tag


def matches(if_none_match: Optional[str], etag: str) -> bool:
    """Whether an If-None-Match header names this ETag: `*`, or any tag in
    its list by the weak comparison, where W/"x" and "x" are the same (RFC
    9110 13.1.2). Also used by the document downloads (app.api.events.router)."""
    if not if_none_match:
        return False
    mine = _opaque(etag)
    for candidate in if_none_match.split(","):
        candidate = candidate.strip()
        if candidate == "*" or (candidate and _opaque(candidate) == mine):
            return True
    return False


def _if_none_match(scope: Scope) -> Optional[str]:
    values = [value.decode("latin-1") for name, value in scope.get("headers") or () if name.lower() == b"if-none-match"]
    return ", ".join(values) if values else None


def _taggable(start: Message) -> Optional[int]:
    """The body's declared length when this response may get a tag, else None."""
    if start["status"] != 200:
        return None
    is_json, length = False, None
    for name, value in start.get("headers") or ():
        name = name.lower()
        if name == b"content-type":
            media = value.split(b";", 1)[0].strip().lower()
            is_json = media == b"application/json" or media.endswith(b"+json")
        elif name == b"content-length":
            try:
                length = int(value)
            except ValueError:
                return None
        elif name in (b"etag", b"set-cookie", b"transfer-encoding"):
            return None
        elif name == b"cache-control" and b"no-store" in value.lower():
            # Nothing the caller may keep, so nothing to ask about again.
            return None
    if not is_json or length is None or length > MAX_BODY:
        return None
    return length


class ETagMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] not in METHODS
            or not scope["path"].startswith(PREFIX)
            or not settings.ETAGS
        ):
            await self.app(scope, receive, send)
            return

        wanted = _if_none_match(scope)
        held: Optional[Message] = None
        chunks: list[bytes] = []
        size = 0
        expected = 0
        passing = False

        async def release(final: bool) -> None:
            """Send what was held back as it was, untagged; the rest passes."""
            nonlocal passing
            passing = True
            await send(held)
            if chunks or final:
                await send({"type": "http.response.body", "body": b"".join(chunks), "more_body": not final})

        async def tagging(message: Message) -> None:
            nonlocal held, size, expected, passing
            if passing:
                await send(message)
                return
            kind = message["type"]
            if kind == "http.response.start":
                length = _taggable(message)
                if length is None:
                    passing = True
                    await send(message)
                else:
                    held, expected = message, length
                return
            if held is None or kind != "http.response.body":
                # Not the start-then-body this expects: hand over what is held.
                if held is not None:
                    await release(final=False)
                passing = True
                await send(message)
                return

            body = message.get("body", b"")
            chunks.append(body)
            size += len(body)
            more = message.get("more_body", False)
            if more:
                if size > expected:
                    await release(final=False)
                return
            if size != expected:
                # Not the body its Content-Length promised (a HEAD answered
                # without one): no tag for what was not seen.
                await release(final=True)
                return

            whole = chunks[0] if len(chunks) == 1 else b"".join(chunks)
            etag = weak_etag(whole)
            tag = (b"etag", etag.encode("latin-1"))
            if matches(wanted, etag):
                headers = [(k, v) for k, v in held.get("headers", []) if k.lower() in KEPT_BY_304]
                await send({"type": "http.response.start", "status": 304, "headers": [*headers, tag]})
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            await send({**held, "headers": [*held.get("headers", []), tag]})
            await send({"type": "http.response.body", "body": whole, "more_body": False})

        await self.app(scope, receive, tagging)
