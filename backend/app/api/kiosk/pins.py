"""The kiosk's staff PIN: kept as a salted PBKDF2 hash, compared in constant
time, and tried at most a few times per kiosk in a window.

A PIN is four to twelve characters, so the hash alone would not hold off
someone with the table; the limit per kiosk is what stops guessing at the
tablet. The hash keeps the PIN itself out of backups, logs and the admin.
The format is self-describing so the cost can be raised later.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import case, func, or_, update

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 120_000
#: Wrong or right, a kiosk gets this many tries in a window...
ATTEMPTS = 5
#: ...of this long, counted from the first try in it.
WINDOW = timedelta(minutes=15)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s + "=" * (-len(s) % 4))


def hash_pin(pin: str, *, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations)
    return f"{ALGORITHM}${iterations}${_b64(salt)}${_b64(digest)}"


def verify_pin(pin: str, stored: str | None) -> bool:
    """Constant time in the PIN; False for anything malformed."""
    if not stored:
        return False
    try:
        algorithm, iterations, salt, digest = stored.split("$")
        if algorithm != ALGORITHM:
            return False
        got = hashlib.pbkdf2_hmac("sha256", pin.encode(), _unb64(salt), int(iterations))
        return hmac.compare_digest(got, _unb64(digest))
    except (ValueError, TypeError):
        return False


async def hash_pin_async(pin: str) -> str:
    return await asyncio.to_thread(hash_pin, pin)


async def verify_pin_async(pin: str, stored: str | None) -> bool:
    return await asyncio.to_thread(verify_pin, pin, stored)


async def take_attempt(session, device_id) -> bool:
    """Count one try against the kiosk, in one conditional UPDATE: a fresh
    window when the last has passed, else one more while under ATTEMPTS.
    False when the kiosk is out of tries. Concurrent tries cannot both
    take the last one."""
    from app.api.kiosk.models import KioskDevices

    t = KioskDevices.__table__
    expired = or_(t.c.pin_window_at.is_(None), t.c.pin_window_at < func.now() - WINDOW)
    stmt = (
        update(t)
        .where(t.c.id == device_id, or_(expired, t.c.pin_attempts < ATTEMPTS))
        .values(
            pin_attempts=case((expired, 1), else_=t.c.pin_attempts + 1),
            pin_window_at=case((expired, func.now()), else_=t.c.pin_window_at),
        )
        .returning(t.c.pin_attempts)
    )
    row = (await session.execute(stmt)).first()
    await session.commit()
    return row is not None


async def clear_attempts(session, device_id) -> None:
    from app.api.kiosk.models import KioskDevices

    t = KioskDevices.__table__
    await session.execute(update(t).where(t.c.id == device_id).values(pin_attempts=0, pin_window_at=None))
    await session.commit()
