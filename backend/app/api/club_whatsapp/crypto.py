"""Encrypting a club's WhatsApp keys at rest: its access token, its app secret
and the webhook verify token we made for it.

Fernet with settings.WHATSAPP_KEYS_SECRET (app/core/keyring.py), a secret of
its own -- not PAYMENT_KEYS_SECRET or AI_KEYS_SECRET -- so one leaked secret
opens one kind of club credential. A comma-separated list is a rotation.
Without a usable key no club can connect a number, and one already connected
cannot be used: its sends go from the platform's number, and the club is told.
"""
from app.core.keyring import Keyring, Unavailable as _Unavailable


class Unavailable(_Unavailable):
    """No usable WHATSAPP_KEYS_SECRET, or a value it cannot decrypt."""


_keys = Keyring("WHATSAPP_KEYS_SECRET", Unavailable, what="clubs' own WhatsApp numbers")


def available() -> bool:
    return _keys.available()


def encrypt(plain: str) -> str:
    return _keys.encrypt(plain)


def decrypt(token: str | None) -> str:
    return _keys.decrypt(token)
