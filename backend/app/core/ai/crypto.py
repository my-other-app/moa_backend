"""Encrypting a club's own AI key at rest.

The same mechanism as a club's Razorpay secrets (payment_gateways.crypto),
with a secret of its own, settings.AI_KEYS_SECRET: Fernet, and a
comma-separated list is a rotation -- the first key encrypts, any of them
decrypts. That helper is bound to PAYMENT_KEYS_SECRET, and one leaked
secret should not open both. Without a usable key, a club cannot connect
its own AI key, and one already saved cannot be used (its actions answer 503
rather than falling back to the platform's key).
"""
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import settings

logger = logging.getLogger(__name__)


class Unavailable(Exception):
    """No usable AI_KEYS_SECRET, or a value it cannot decrypt."""


_cache: tuple[str, MultiFernet | None] | None = None


def _fernet() -> MultiFernet | None:
    global _cache
    raw = (settings.AI_KEYS_SECRET or "").strip()
    if _cache is not None and _cache[0] == raw:
        return _cache[1]
    fernet = None
    if raw:
        try:
            fernet = MultiFernet([Fernet(part.strip().encode()) for part in raw.split(",") if part.strip()])
        except (ValueError, TypeError):
            # Never the value itself: it is the key to every club's AI key.
            logger.error("APP_AI_KEYS_SECRET is not a Fernet key; clubs cannot use their own AI keys")
            fernet = None
    _cache = (raw, fernet)
    return fernet


def available() -> bool:
    return _fernet() is not None


def encrypt(plain: str) -> str:
    fernet = _fernet()
    if fernet is None:
        raise Unavailable("AI_KEYS_SECRET is not set")
    return fernet.encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str:
    fernet = _fernet()
    if fernet is None:
        raise Unavailable("AI_KEYS_SECRET is not set")
    if not token:
        raise Unavailable("nothing saved")
    try:
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise Unavailable("the saved value cannot be decrypted with AI_KEYS_SECRET") from exc
