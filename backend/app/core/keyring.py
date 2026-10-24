"""Encrypting a club's own credentials at rest: one mechanism, one secret per kind.

A club pastes the keys to accounts it pays for -- its Razorpay
(payment_gateways), its AI provider (core/ai), its WhatsApp Business number
(club_whatsapp) -- and each is kept with Fernet under a setting of its own:
PAYMENT_KEYS_SECRET, AI_KEYS_SECRET, WHATSAPP_KEYS_SECRET. One leaked secret
should open one kind of key, not all three, so they are never shared; what is
shared is this code.

A comma-separated value is a rotation: the first key encrypts, any of them
decrypts. Without a usable key the feature is unavailable -- nothing can be
saved, and what was saved cannot be used -- and `Unavailable` (the caller's
own subclass, so each module's callers keep catching their own) says so.
The secret is never logged: only the setting's name.
"""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import settings

logger = logging.getLogger(__name__)


class Unavailable(Exception):
    """No usable secret, or a value it cannot decrypt."""


class Keyring:
    """Fernet under `settings.<setting>`, read afresh each call (tests and a
    rotation change it) and parsed once per value."""

    def __init__(self, setting: str, unavailable: type[Exception] = Unavailable, *, what: str = "club keys"):
        self.setting = setting
        self.unavailable = unavailable
        self.what = what
        self._cache: tuple[str, MultiFernet | None] | None = None

    def _raw(self) -> str:
        return (getattr(settings, self.setting, "") or "").strip()

    def fernet(self) -> MultiFernet | None:
        raw = self._raw()
        if self._cache is not None and self._cache[0] == raw:
            return self._cache[1]
        fernet = None
        if raw:
            try:
                fernet = MultiFernet([Fernet(part.strip().encode()) for part in raw.split(",") if part.strip()])
            except (ValueError, TypeError):
                # Never the value itself: it is the key to every club's secret of this kind.
                logger.error("APP_%s is not a Fernet key; %s are unavailable", self.setting, self.what)
                fernet = None
        self._cache = (raw, fernet)
        return fernet

    def available(self) -> bool:
        return self.fernet() is not None

    def encrypt(self, plain: str) -> str:
        fernet = self.fernet()
        if fernet is None:
            raise self.unavailable(f"{self.setting} is not set")
        return fernet.encrypt(plain.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str | None) -> str:
        fernet = self.fernet()
        if fernet is None:
            raise self.unavailable(f"{self.setting} is not set")
        if not token:
            raise self.unavailable("nothing saved")
        try:
            return fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise self.unavailable(f"the saved value cannot be decrypted with {self.setting}") from exc
