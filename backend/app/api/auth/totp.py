"""Time-based one-time passwords for admin accounts.

A stolen admin password was the whole chain: impersonation, refunds and
deletion all sit behind it, and nothing else stood in the way.

Enrolment is two steps on purpose -- a secret is generated and only enabled
once the user proves they can read a code from it. Enabling on generation
would lock out anyone whose authenticator failed to scan.

- A code is good once. The 30-second step it was accepted for is kept on the
  account (users.totp_last_step) and a code from that step or an earlier one
  is refused: a code read over a shoulder cannot be used again within its
  window. Kept with a conditional UPDATE, so two requests with one code
  cannot both pass.
- The secret is encrypted at rest with the payment-keys Fernet key
  (APP_PAYMENT_KEYS_SECRET) when the server has one; a secret saved before
  that, or on a server without it, is the plain base32, and still works.
- Recovery codes are 64 random bits each, kept as HMAC-SHA256 digests: one
  hash to check a code, where the bcrypt hashes they replace cost eight
  bcrypts (two seconds of CPU) to enable and up to eight per wrong code.
  Codes issued before are bcrypt hashes; they keep working until used or
  replaced, and only a code shaped like one of them is ever bcrypt-checked.
  A code is spent under a row lock on the account, so it is spent once.
"""

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time

import pyotp
from sqlalchemy import or_, select, update
from sqlalchemy.orm.attributes import set_committed_value

from app.config import settings
from app.core.auth.authentication import verify_password

logger = logging.getLogger(__name__)

ISSUER = "MyOtherApp Admin"
RECOVERY_CODE_COUNT = 8
STEP_SECONDS = 30

#: A sealed (encrypted) secret starts with this; anything else is plain base32.
_SEALED = "fernet:"
#: A recovery code digest starts with this; a bcrypt hash starts with "$2".
_DIGEST = "hmac-sha256:"
_LEGACY_SHAPE = re.compile(r"^[0-9a-f]{4}-?[0-9a-f]{4}$")


def new_secret() -> str:
    return pyotp.random_base32()


def seal_secret(secret: str) -> str:
    """What is stored for `secret`: encrypted when the server has a key."""
    from app.api.payment_gateways import crypto

    if crypto.available():
        return _SEALED + crypto.encrypt(secret)
    # TODO: stored in the clear while APP_PAYMENT_KEYS_SECRET is unset; set
    # it and the next enrolment (or a sign-in with a code) seals it.
    return secret


def open_secret(stored: str | None) -> str | None:
    """The base32 secret from what is stored, or None when it cannot be read
    (sealed, and the server has no key that opens it)."""
    if not stored:
        return None
    if not stored.startswith(_SEALED):
        return stored
    from app.api.payment_gateways import crypto

    try:
        return crypto.decrypt(stored[len(_SEALED):])
    except crypto.Unavailable:
        logger.error("A two-factor secret cannot be decrypted with APP_PAYMENT_KEYS_SECRET")
        return None


def provisioning_uri(secret: str, account: str) -> str:
    """The otpauth:// URI an authenticator app scans."""
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=ISSUER)


def _clean(code: str | None) -> str:
    return (code or "").strip().replace(" ", "")


def matching_step(secret: str | None, code: str | None, now: float | None = None) -> int | None:
    """The 30-second step `code` belongs to, within one step either side (the
    usual allowance for a phone's clock), or None. Wider would meaningfully
    extend how long a shoulder-surfed code stays valid."""
    typed = _clean(code)
    if not secret or not typed.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    current = int((now if now is not None else time.time()) // STEP_SECONDS)
    for step in (current - 1, current, current + 1):
        if hmac.compare_digest(totp.at(step * STEP_SECONDS), typed):
            return step
    return None


def verify(secret: str | None, code: str | None) -> bool:
    """Whether `code` is a current code for this (plain or sealed) secret.
    Does not spend it: sign-in paths use accept_code."""
    return matching_step(open_secret(secret), code) is not None


async def accept_code(session, user, code: str | None) -> bool:
    """Whether `code` is a current authenticator code for this account that
    has not been used before; if so, it is now used. The step is recorded
    with a conditional UPDATE, so of two requests carrying one code exactly
    one passes. The caller commits."""
    from app.api.users.models import Users

    step = matching_step(open_secret(user.totp_secret), code)
    if step is None:
        return False
    won = await session.scalar(
        update(Users)
        .where(Users.id == user.id, or_(Users.totp_last_step.is_(None), Users.totp_last_step < step))
        .values(totp_last_step=step)
        .returning(Users.id)
        .execution_options(synchronize_session=False)
    )
    if won is None:
        logger.warning("A two-factor code was used again for user %s", user.id)
        return False
    set_committed_value(user, "totp_last_step", step)
    if user.totp_secret and not user.totp_secret.startswith(_SEALED):
        sealed = seal_secret(user.totp_secret)
        if sealed.startswith(_SEALED):
            # A secret from before the key was set: sealed now it is known good.
            user.totp_secret = sealed
    return True


def _digest(code: str) -> str:
    key = hashlib.sha256(b"moa-totp-recovery:" + settings.SECRET_KEY.encode()).digest()
    normal = _clean(code).lower().replace("-", "")
    return hmac.new(key, normal.encode(), hashlib.sha256).hexdigest()


def generate_recovery_codes() -> tuple[list[str], list[str]]:
    """
    Returns (plaintext, stored).

    The plaintext is shown once and never stored: xxxx-xxxx-xxxx-xxxx, 64
    random bits. What is stored is a keyed digest of each, because a recovery
    code is a password equivalent and a leaked table should not hand someone
    a way past 2FA -- and 64 random bits need no slow hash to stay out of reach.
    """
    plaintext = ["-".join(secrets.token_hex(2) for _ in range(4)) for _ in range(RECOVERY_CODE_COUNT)]
    return plaintext, [_DIGEST + _digest(code) for code in plaintext]


async def consume_recovery_code(session, user, code: str | None) -> bool:
    """Whether `code` is one of the account's unused recovery codes; if so it
    is removed. Read under FOR UPDATE on the account's row, so two requests
    with one code spend it once. An all-digit code is an authenticator code,
    never a recovery code. The caller commits."""
    from app.api.users.models import Users

    typed = _clean(code)
    if not typed or typed.isdigit():
        return False
    stored = await session.scalar(
        select(Users.totp_recovery_codes).where(Users.id == user.id).with_for_update()
    ) or []
    want = _DIGEST + _digest(typed)
    match = next((h for h in stored if isinstance(h, str) and h.startswith(_DIGEST) and hmac.compare_digest(h, want)), None)
    if match is None:
        lowered = typed.lower()
        if _LEGACY_SHAPE.match(lowered):
            # A code from before the digests: bcrypt, checked only for a code
            # of that shape, on a worker thread.
            legacy = lowered if "-" in lowered else f"{lowered[:4]}-{lowered[4:]}"
            for h in stored:
                if isinstance(h, str) and h.startswith("$2") and await asyncio.to_thread(verify_password, legacy, h):
                    match = h
                    break
    if match is None:
        return False
    # Removed rather than left usable: a recovery code is a single-use
    # bypass, and leaving it active turns one leaked code into permanent access.
    user.totp_recovery_codes = [h for h in stored if h != match]
    await session.flush()
    return True


async def check_second_factor(session, user, code: str | None) -> bool:
    """An authenticator code (used once) or a recovery code (spent)."""
    if await accept_code(session, user, code):
        return True
    return await consume_recovery_code(session, user, code)
