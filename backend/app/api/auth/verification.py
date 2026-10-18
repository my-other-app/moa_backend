"""Proving an email/password account owns its address.

A password account's address is only typed at sign-up. Until its owner shows
it is theirs, nothing that belongs to the address is given to the account --
above all the tickets guests booked on the website under it, which the merge
folds only into accounts that have shown it (users.service.merges_into).

The proof is a six-digit code sent to the address and typed back. Only a keyed
hash of each code is kept. A code lives fifteen minutes and five wrong tries,
and a new one replaces any before it. Codes go out at most once a minute and
ten times a day per account.

Google and Apple vouch for an address too. When one of them signs in to a
password account nobody verified, the provider is the proof: whoever set that
password never showed the address was theirs, so the password goes and every
session it opened ends (claim_for_provider). Without that, registering a
victim's address before they ever signed in with Google kept a password into
the account they would land in.
"""
import hashlib
import hmac
import html
import logging
import math
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, ForeignKey, Index, Integer, String, delete, func, select, update
from starlette import status

from app.api.auth.sessions import UserSessions
from app.api.users.models import ADDRESS_CHECKING_PROVIDERS, Users
from app.config import settings
from app.core.email import email as email_mod
from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.response import CustomHTTPException
from app.db import lock_keys

logger = logging.getLogger(__name__)

CODE_TTL = timedelta(minutes=15)
MAX_ATTEMPTS = 5
RESEND_AFTER = timedelta(seconds=60)
DAILY_LIMIT = 10
DAY = timedelta(hours=24)

#: First key of the advisory lock that keeps two sends for one account apart,
#: so a double tap cannot slip two codes past the once-a-minute rule.
_SEND_LOCK = lock_keys.VERIFICATION_SEND


class EmailVerificationCodes(AbstractSQLModel):
    """One code sent to an account's address. Rows older than a day count for
    nothing and are cleared when the account asks for its next code."""

    __tablename__ = "email_verification_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # The address it went to. A code is good only while the account still has it.
    email = Column(String(100), nullable=False)
    # HMAC-SHA256 under the app secret, over the account, the address and the
    # code. A million codes are quick to try against a bare hash; not
    # against one keyed with a secret the database does not hold.
    code_hash = Column(String(64), nullable=False)
    sent_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    expires_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    # When it stopped counting: used, replaced by a newer one, out of tries,
    # expired, or the address was verified some other way.
    closed_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_email_verification_codes_user_sent", "user_id", "sent_at"),)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _same_address(a: str | None, b: str | None) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def code_hash(user_id: int, email: str, code: str) -> str:
    message = f"email-verification:{user_id}:{(email or '').strip().lower()}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


def _refuse(status_code: int, error_code: str, message: str, **errors) -> CustomHTTPException:
    return CustomHTTPException(status_code, message=message, error_code=error_code, errors=errors or None)


def _already_verified() -> CustomHTTPException:
    return _refuse(status.HTTP_409_CONFLICT, "ALREADY_VERIFIED", "Your email is already verified.")


def _in_words(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = math.ceil(seconds / 60)
    if minutes < 90:
        return f"{minutes} minutes"
    return f"{math.ceil(seconds / 3600)} hours"


# ─── Sending ────────────────────────────────────────────────────────────


async def _sent_in_last_day(session, user_id: int, now: datetime) -> list[datetime]:
    rows = await session.scalars(
        select(EmailVerificationCodes.sent_at)
        .where(EmailVerificationCodes.user_id == user_id, EmailVerificationCodes.sent_at > now - DAY)
        .order_by(EmailVerificationCodes.sent_at.desc())
    )
    return [_aware(t) for t in rows]


def _wait(sent: list[datetime], now: datetime) -> tuple[int, str] | None:
    """Seconds until another code may go out, and why; None when it may now."""
    if sent and now - sent[0] < RESEND_AFTER:
        return max(1, math.ceil((sent[0] + RESEND_AFTER - now).total_seconds())), "CODE_RECENTLY_SENT"
    if len(sent) >= DAILY_LIMIT:
        # The window frees up when the oldest send that still counts turns a day old.
        return max(1, math.ceil((sent[DAILY_LIMIT - 1] + DAY - now).total_seconds())), "CODE_DAILY_LIMIT"
    return None


async def issue(session, user: Users) -> str:
    """A new code for the account's address, replacing any before it, and the
    code itself for the mail. Flushed; the caller commits.

    Refused with 409 when the address is already verified and 429, with
    `retry_after` in seconds, when the account asked too recently or too
    often today."""
    if user.email_verified:
        raise _already_verified()
    await session.execute(select(func.pg_advisory_xact_lock(_SEND_LOCK, user.id)))
    now = _now()
    wait = _wait(await _sent_in_last_day(session, user.id, now), now)
    if wait is not None:
        seconds, reason = wait
        if reason == "CODE_RECENTLY_SENT":
            message = f"We just sent you a code. You can ask for another in {_in_words(seconds)}."
        else:
            message = f"You've asked for a lot of codes today. Try again in {_in_words(seconds)}."
        raise _refuse(status.HTTP_429_TOO_MANY_REQUESTS, reason, message, retry_after=seconds)

    await session.execute(
        delete(EmailVerificationCodes)
        .where(EmailVerificationCodes.user_id == user.id, EmailVerificationCodes.sent_at <= now - DAY)
        .execution_options(synchronize_session=False)
    )
    await _close_open_codes(session, user.id, now)
    code = new_code()
    session.add(
        EmailVerificationCodes(
            user_id=user.id,
            email=user.email,
            code_hash=code_hash(user.id, user.email, code),
            sent_at=now,
            expires_at=now + CODE_TTL,
            attempts=0,
        )
    )
    await session.flush()
    return code


async def _close_open_codes(session, user_id: int, now: datetime) -> None:
    await session.execute(
        update(EmailVerificationCodes)
        .where(EmailVerificationCodes.user_id == user_id, EmailVerificationCodes.closed_at.is_(None))
        .values(closed_at=now)
        .execution_options(synchronize_session=False)
    )


def send_code_email(to: str, *, code: str, name: str | None = None) -> None:
    """The mail with the code. Run in the background: the request that asked
    for it does not wait on the mail provider, and a mail that could not go
    out is logged, never raised -- the account can ask for another."""
    brand = settings.MAIL_FROM_NAME or "MyOtherApp"
    minutes = int(CODE_TTL.total_seconds() // 60)
    first = (name or "").strip().split(" ")[0]
    greeting = f"Hi {first}," if first else "Hi,"
    text = (
        f"{greeting}\n\n"
        f"Your {brand} verification code is:\n\n{code}\n\n"
        f"Type it into the app to verify this email address. It expires in {minutes} minutes.\n\n"
        f"If this wasn't you, ignore this email and nothing will change.\n"
    )
    esc = html.escape
    body = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;'
        'color:#2C333D;max-width:480px;margin:0 auto;padding:24px 16px">'
        f'<p style="font-size:20px;font-weight:700;margin:0 0 24px">{esc(brand)}</p>'
        f"<p>{esc(greeting)}</p>"
        "<p>Type this code into the app to verify your email address:</p>"
        '<p style="font-size:36px;font-weight:700;letter-spacing:8px;'
        f'font-family:Menlo,Consolas,\'Courier New\',monospace;margin:24px 0">{esc(code)}</p>'
        f"<p>It expires in {minutes} minutes.</p>"
        '<p style="color:#6B7280;font-size:13px;margin-top:32px">'
        "If this wasn't you, ignore this email and nothing will change.</p>"
        "</div>"
    )
    try:
        email_mod.send_email(recipients=[to], subject=f"{code} is your {brand} verification code", text=text, html=body)
    except Exception:
        logger.warning("verification code email to %s could not be sent", to, exc_info=True)


# ─── Checking ───────────────────────────────────────────────────────────


async def verify(session, user: Users, code: str) -> datetime:
    """Check a typed code against the account's latest one. On a match the
    address is verified and every open code closed (flushed; the caller
    commits, after folding guests in). On anything else a refusal is raised,
    with the wrong try already committed and `attempts_left` in the errors."""
    if user.email_verified:
        raise _already_verified()
    now = _now()
    row = await session.scalar(
        select(EmailVerificationCodes)
        .where(EmailVerificationCodes.user_id == user.id)
        .order_by(EmailVerificationCodes.sent_at.desc(), EmailVerificationCodes.id.desc())
        .limit(1)
        .with_for_update()
    )
    expired = _refuse(status.HTTP_400_BAD_REQUEST, "CODE_EXPIRED", "That code has expired. Ask for a new one.", attempts_left=0)
    if row is None:
        raise _refuse(status.HTTP_400_BAD_REQUEST, "CODE_EXPIRED", "Ask for a code first.", attempts_left=0)
    if row.attempts >= MAX_ATTEMPTS:
        raise _refuse(status.HTTP_400_BAD_REQUEST, "CODE_LOCKED", "Too many wrong tries. Ask for a new code.", attempts_left=0)
    if row.closed_at is not None:
        raise expired
    if _aware(row.expires_at) <= now or not _same_address(row.email, user.email):
        row.closed_at = now
        await session.commit()
        raise expired

    typed = re.sub(r"[\s-]", "", code or "")
    if hmac.compare_digest(code_hash(user.id, user.email, typed), row.code_hash):
        user.email_verified_at = now
        await _close_open_codes(session, user.id, now)
        await session.flush()
        return now

    row.attempts += 1
    left = max(0, MAX_ATTEMPTS - row.attempts)
    if left == 0:
        row.closed_at = now
    await session.commit()
    if left == 0:
        raise _refuse(status.HTTP_400_BAD_REQUEST, "CODE_LOCKED", "Too many wrong tries. Ask for a new code.", attempts_left=0)
    raise _refuse(
        status.HTTP_400_BAD_REQUEST, "CODE_INVALID",
        f"That code isn't right. {left} {'try' if left == 1 else 'tries'} left.", attempts_left=left,
    )


# ─── Google and Apple ───────────────────────────────────────────────────


async def claim_for_provider(session, user: Users, *, method: str) -> bool:
    """Google or Apple has just vouched that the person signing in owns
    `user.email`. Stamps the address verified; and when the account is a
    password account nobody verified, takes it back from that password.

    The password, any second factor set up behind it and every session go,
    and the event is recorded on the Security page. The account itself --
    its tickets and profile -- stays, now with its owner. Returns whether it
    was taken back. Flushed; the caller commits."""
    if user.email_verified_at is not None:
        return False
    now = _now()
    user.email_verified_at = now
    if getattr(user.provider, "value", user.provider) in ADDRESS_CHECKING_PROVIDERS:
        # Made through the provider itself, before accounts carried the stamp.
        await session.flush()
        return False

    user.password = None
    user.totp_enabled = False
    user.totp_secret = None
    user.totp_recovery_codes = None
    # Ends every access and refresh token issued before now.
    user.token_version = (user.token_version or 0) + 1
    await session.execute(
        update(UserSessions)
        .where(UserSessions.user_id == user.id, UserSessions.revoked == False)  # noqa: E712
        .values(revoked=True)
        .execution_options(synchronize_session=False)
    )
    await _close_open_codes(session, user.id, now)
    from app.api.auth import password_reset

    await password_reset.close_for_user(session, user.id, now)

    from app.api.security import service as security

    await security.record(session, request=None, identifier=user.email, user=user, method=method, result="reclaimed")
    logger.warning("%s sign-in took back unverified password account %s", method, user.id)
    return True
