"""Encrypting a club's Razorpay secrets at rest.

Fernet with settings.PAYMENT_KEYS_SECRET (app/core/keyring.py). A
comma-separated list is a rotation: the first key encrypts, any of them
decrypts. Without a usable key the feature is unavailable: nothing can be
saved, and a saved gateway cannot be used (new orders go to the platform; an
existing order on it answers 503).
"""
from app.core.keyring import Keyring, Unavailable as _Unavailable


class Unavailable(_Unavailable):
    """No usable PAYMENT_KEYS_SECRET, or a value it cannot decrypt."""


_keys = Keyring("PAYMENT_KEYS_SECRET", Unavailable, what="club payment gateways")


def available() -> bool:
    return _keys.available()


def encrypt(plain: str) -> str:
    return _keys.encrypt(plain)


def decrypt(token: str | None) -> str:
    return _keys.decrypt(token)
