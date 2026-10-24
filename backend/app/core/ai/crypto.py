"""Encrypting a club's own AI key at rest.

The same mechanism as a club's Razorpay secrets (app/core/keyring.py), with a
secret of its own, settings.AI_KEYS_SECRET: Fernet, and a comma-separated
list is a rotation -- the first key encrypts, any of them decrypts. One
leaked secret should not open both. Without a usable key, a club cannot
connect its own AI key, and one already saved cannot be used (its actions
answer 503 rather than falling back to the platform's key).
"""
from app.core.keyring import Keyring, Unavailable as _Unavailable


class Unavailable(_Unavailable):
    """No usable AI_KEYS_SECRET, or a value it cannot decrypt."""


_keys = Keyring("AI_KEYS_SECRET", Unavailable, what="clubs' own AI keys")


def available() -> bool:
    return _keys.available()


def encrypt(plain: str) -> str:
    return _keys.encrypt(plain)


def decrypt(token: str | None) -> str:
    return _keys.decrypt(token)
