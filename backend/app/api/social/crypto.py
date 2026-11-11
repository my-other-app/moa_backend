"""Connected accounts' tokens, encrypted at rest under SOCIAL_KEYS_SECRET
(app/core/keyring.py): never a token in a log, an error or a response."""
from __future__ import annotations

from typing import Optional

from app.core.keyring import Keyring, Unavailable as _Unavailable


class Unavailable(_Unavailable):
    """No usable SOCIAL_KEYS_SECRET, or a token it cannot decrypt."""


_ring = Keyring("SOCIAL_KEYS_SECRET", Unavailable, what="social accounts' tokens")


def available() -> bool:
    return _ring.available()


def seal(plain: Optional[str]) -> Optional[str]:
    return None if not plain else _ring.encrypt(plain)


def open_(sealed: Optional[str]) -> str:
    return _ring.decrypt(sealed)


def open_or_none(sealed: Optional[str]) -> Optional[str]:
    if not sealed:
        return None
    try:
        return _ring.decrypt(sealed)
    except Unavailable:
        return None
