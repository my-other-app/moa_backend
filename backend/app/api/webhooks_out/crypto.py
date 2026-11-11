"""Endpoints' signing secrets, and the signature itself.

A secret (`whsec_` and 43 random characters) is shown once, when it is made
or rotated. The server must sign with it afterwards, so it cannot be kept as
a hash: it is kept Fernet-encrypted under WEBHOOK_KEYS_SECRET
(app/core/keyring.py), its own key. Without that setting the feature is
unavailable -- no endpoint can be added -- and says so.

The signature (docs/WEBHOOKS.md has receivers' code):

    MOA-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>

with the secret's whole text, `whsec_` included, as the HMAC key. During a
rotation's overlap a second `v1=` carries the old secret's signature, so a
receiver that still has the old one keeps verifying.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Iterable, Optional

from app.core.keyring import Keyring, Unavailable as _Unavailable

PREFIX = "whsec_"
SIGNATURE_HEADER = "MOA-Signature"
EVENT_ID_HEADER = "MOA-Event-Id"
#: Receivers reject a timestamp older than this (and so does `verify`).
TOLERANCE_SECONDS = 300


class Unavailable(_Unavailable):
    """No usable WEBHOOK_KEYS_SECRET, or a secret it cannot decrypt."""


_ring = Keyring("WEBHOOK_KEYS_SECRET", Unavailable, what="webhook signing secrets")

UNAVAILABLE_MESSAGE = (
    "Webhooks are not available yet: the server has no APP_WEBHOOK_KEYS_SECRET to keep signing secrets with. "
    "Ask the MyOtherApp team."
)


def available() -> bool:
    return _ring.available()


def new_secret() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def hint(secret: str) -> str:
    return secret[-4:]


def seal(secret: str) -> str:
    return _ring.encrypt(secret)


def open_(sealed: Optional[str]) -> str:
    return _ring.decrypt(sealed)


def sign(secret: str, timestamp: int, body: bytes) -> str:
    message = f"{int(timestamp)}.".encode("ascii") + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def header(secrets_: Iterable[str], timestamp: int, body: bytes) -> str:
    """`t=<t>,v1=<sig>[,v1=<sig of the old secret>]`."""
    parts = [f"t={int(timestamp)}"]
    for secret in secrets_:
        parts.append(f"v1={sign(secret, timestamp, body)}")
    return ",".join(parts)


def verify(secret: str, header_value: str, body: bytes, *, now: Optional[int] = None,
           tolerance: int = TOLERANCE_SECONDS) -> bool:
    """What a receiver does (docs/WEBHOOKS.md), here for the tests and for
    anyone checking by hand: a fresh timestamp and one v1 that matches."""
    import time

    timestamp, signatures = None, []
    for item in (header_value or "").split(","):
        key, _, value = item.strip().partition("=")
        if key == "t" and value.isdigit():
            timestamp = int(value)
        elif key == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        return False
    if abs((now if now is not None else int(time.time())) - timestamp) > tolerance:
        return False
    expected = sign(secret, timestamp, body)
    return any(hmac.compare_digest(expected, s) for s in signatures)
