"""The games' signed tokens and PINs (CONTRACT §1). Nothing is stored for any
of them but the PIN version and the screen version.

All are HMAC-SHA256 under a key derived from SECRET_KEY and a fixed purpose
string, base64url, dot-separated, short:

- badge   b.<event_id>.<registration id, 22 chars>.<pin_version>.<mac>
- point   p.<event_id>.<point id, 22 chars>.<mac>
- screen  s.<event_id>.<version>.<mac>
- session g.<event_id>.<registration id>.<session_version>.<expires unix>.<mac>

A token of one kind is never accepted as another (the kind is in the mac), nor
for another event. The PIN is not stored: 4 digits worked out from the
registration and its pin_version.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from dataclasses import dataclass
from typing import Optional

from app.config import settings

PURPOSE = b"moa.games.v1"
MAC_BYTES = 12


def _key() -> bytes:
    return hmac.new(settings.SECRET_KEY.encode(), PURPOSE, hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def short(value: uuid.UUID) -> str:
    return _b64(value.bytes)


def unshort(text: str) -> Optional[uuid.UUID]:
    try:
        raw = _unb64(text)
        return uuid.UUID(bytes=raw) if len(raw) == 16 else None
    except Exception:  # noqa: BLE001 - not ours
        return None


def _mac(kind: str, body: str) -> str:
    return _b64(hmac.new(_key(), f"{kind}|{body}".encode(), hashlib.sha256).digest()[:MAC_BYTES])


def _sign(kind: str, body: str) -> str:
    return f"{kind}.{body}.{_mac(kind, body)}"


def _open(token: str, kind: str, parts: int) -> Optional[list[str]]:
    """The body's parts when `token` is a genuine one of `kind` with this many, else None."""
    if not isinstance(token, str) or len(token) > 300:
        return None
    # A scanned QR may carry the whole address (https://.../g/<token>): the token is its last part.
    pieces = token.strip().split("?")[0].rstrip("/").rsplit("/", 1)[-1].split(".")
    if len(pieces) != parts + 2 or pieces[0] != kind:
        return None
    body, mac = ".".join(pieces[1:-1]), pieces[-1]
    if not hmac.compare_digest(_mac(kind, body), mac):
        return None
    return pieces[1:-1]


@dataclass(frozen=True)
class Badge:
    event_id: int
    registration_id: uuid.UUID
    pin_version: int


@dataclass(frozen=True)
class Point:
    event_id: int
    point_id: uuid.UUID


@dataclass(frozen=True)
class Session:
    event_id: int
    registration_id: uuid.UUID
    version: int
    expires: int


def badge(event_id: int, registration_id: uuid.UUID, pin_version: int) -> str:
    return _sign("b", f"{int(event_id)}.{short(registration_id)}.{int(pin_version)}")


def read_badge(token: str) -> Optional[Badge]:
    p = _open(token, "b", 3)
    if p is None:
        return None
    reg = unshort(p[1])
    try:
        return Badge(int(p[0]), reg, int(p[2])) if reg is not None else None
    except ValueError:
        return None


def point(event_id: int, point_id: uuid.UUID) -> str:
    return _sign("p", f"{int(event_id)}.{short(point_id)}")


def read_point(token: str) -> Optional[Point]:
    p = _open(token, "p", 2)
    if p is None:
        return None
    pid = unshort(p[1])
    try:
        return Point(int(p[0]), pid) if pid is not None else None
    except ValueError:
        return None


def screen(event_id: int, version: int) -> str:
    return _sign("s", f"{int(event_id)}.{int(version)}")


def read_screen(token: str) -> Optional[tuple[int, int]]:
    p = _open(token, "s", 2)
    if p is None:
        return None
    try:
        return int(p[0]), int(p[1])
    except ValueError:
        return None


def session(event_id: int, registration_id: uuid.UUID, version: int, expires: int) -> str:
    return _sign("g", f"{int(event_id)}.{short(registration_id)}.{int(version)}.{int(expires)}")


def read_session(token: str) -> Optional[Session]:
    p = _open(token, "g", 4)
    if p is None:
        return None
    reg = unshort(p[1])
    try:
        return Session(int(p[0]), reg, int(p[2]), int(p[3])) if reg is not None else None
    except ValueError:
        return None


def pin(event_id: int, registration_id: uuid.UUID, pin_version: int) -> str:
    """The attendee's 4 digits: derived, never stored."""
    digest = hmac.new(_key(), f"pin|{int(event_id)}|{registration_id}|{int(pin_version)}".encode(), hashlib.sha256).digest()
    return f"{int.from_bytes(digest[:8], 'big') % 10000:04d}"


def pin_matches(event_id: int, registration_id: uuid.UUID, pin_version: int, given: str) -> bool:
    return hmac.compare_digest(pin(event_id, registration_id, pin_version), str(given or "").strip())


def badge_url(token: str) -> str:
    from app.core import public_urls

    return public_urls.site_url(f"/g/{token}")


def code_of(event_id: int, registration_id: uuid.UUID, pin_version: int) -> dict:
    """What a badge and a ticket show: the QR's address, its token and the PIN."""
    token = badge(event_id, registration_id, pin_version)
    return {"url": badge_url(token), "token": token, "pin": pin(event_id, registration_id, pin_version)}
