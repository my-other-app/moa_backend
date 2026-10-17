"""Encrypting a club's Razorpay secrets at rest.

Fernet with settings.PAYMENT_KEYS_SECRET. A comma-separated list is a
rotation: the first key encrypts, any of them decrypts. Without a usable key
the feature is unavailable: nothing can be saved, and a saved gateway cannot
be used (new orders go to the platform; an existing order on it answers 503).
"""
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import settings

logger = logging.getLogger(__name__)


class Unavailable(Exception):
    """No usable PAYMENT_KEYS_SECRET, or a value it cannot decrypt."""


_cache: tuple[str, MultiFernet | None] | None = None


def _fernet() -> MultiFernet | None:
    global _cache
    raw = (settings.PAYMENT_KEYS_SECRET or "").strip()
    if _cache is not None and _cache[0] == raw:
        return _cache[1]
    fernet = None
    if raw:
        try:
            fernet = MultiFernet([Fernet(part.strip().encode()) for part in raw.split(",") if part.strip()])
        except (ValueError, TypeError):
            # Never the value itself: it is the key to every club's secret.
            logger.error("APP_PAYMENT_KEYS_SECRET is not a Fernet key; club payment gateways are unavailable")
            fernet = None
    _cache = (raw, fernet)
    return fernet


def available() -> bool:
    return _fernet() is not None


def encrypt(plain: str) -> str:
    fernet = _fernet()
    if fernet is None:
        raise Unavailable("PAYMENT_KEYS_SECRET is not set")
    return fernet.encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str:
    fernet = _fernet()
    if fernet is None:
        raise Unavailable("PAYMENT_KEYS_SECRET is not set")
    if not token:
        raise Unavailable("nothing saved")
    try:
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise Unavailable("the saved value cannot be decrypted with PAYMENT_KEYS_SECRET") from exc
