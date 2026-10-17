"""A forgotten password, for an email/password app account.

The same machinery as email verification (verification.py): a six-digit
code mailed to the address, only a keyed hash of it kept, good for fifteen
minutes and five wrong tries, a new one replacing any before it, at most one a
minute and ten a day. Typing it back with a new password sets the password,
ends every other session, verifies the address (the code proves it) and
signs the person in.

Nothing here tells anyone whether an address has an account:

- Asking for a code always answers the same, at once; the work (finding the
  account, the limits, the code, the mail) happens after the response, so
  how long the answer takes says nothing either.
- Codes are kept per address, not per account: an address with no password
  account gets its code row like any other, with a code nobody is sent. So a
  wrong code counts down, locks and expires in the same steps whether there
  is an account behind the address or not; only the mailbox's owner can tell.
  The address itself is kept only as a keyed hash (address_key), so the
  table holds no addresses of people who are not users.

Only app accounts that sign in with a password take part: provider 'email',
or an account from before providers were recorded that has a password.
Google and Apple accounts never get a code.

An account with two-factor on needs its authenticator code (or a recovery
code) as well: an inbox alone does not get past a second factor.
"""
import hashlib
import hmac
import html
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import Column, ForeignKey, Index, Integer, String, and_, delete, func, or_, select, update
from starlette import status
from starlette.concurrency import run_in_threadpool

from app.api.auth import verification
from app.api.auth.sessions import UserSessions
from app.api.users.models import SigninProviders, UserTypes, Users
from app.config import settings
from app.core.email import email as email_mod
from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

CODE_TTL = verification.CODE_TTL
MAX_ATTEMPTS = verification.MAX_ATTEMPTS
DAY = verification.DAY
EMAIL_MAX = 254

#: First key of the advisory lock that keeps two requests for one address apart.
_RESET_LOCK = 7_316_205


class PasswordResetCodes(AbstractSQLModel):
    """One reset code, for an address. Rows older than a day count for
    nothing and are cleared by the next request for a code."""

    __tablename__ = "password_reset_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # HMAC-SHA256 of the address (trimmed, lower case) under the app secret.
    email_key = Column(String(64), nullable=False)
    # The account the code was sent to; none when the address has no
    # password account (then the code went nowhere).
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    # HMAC-SHA256 under the app secret over the address and the code.
    code_hash = Column(String(64), nullable=False)
    sent_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    expires_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    # When it stopped counting: used, replaced, out of tries, or expired.
    closed_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_password_reset_codes_key_sent", "email_key", "sent_at"),
        Index("ix_password_reset_codes_sent", "sent_at"),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalise(email: str | None) -> str:
    return (email or "").strip().lower()


def address_key(email: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"password-reset-address:{normalise(email)}".encode(), hashlib.sha256).hexdigest()


def code_hash(email: str, code: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"password-reset:{normalise(email)}:{code}".encode(), hashlib.sha256).hexdigest()


def _lock_id(key: str) -> int:
    """The address's half of the advisory lock key: 32 bits of its hash."""
    return int(key[:8], 16) - 2**31


@asynccontextmanager
async def session_scope():
    """A session for the work after the response. Tests swap this for their own."""
    from app.db.core import AsyncSessionLocal
    from app.db.listeners import add_loader_criteria

    async with AsyncSessionLocal() as session:
        add_loader_criteria(session)
        yield session


def password_accounts():
    """App accounts that sign in with a password."""
    return and_(
        Users.user_type == UserTypes.app_user,
        Users.is_deleted == False,  # noqa: E712
        or_(Users.provider == SigninProviders.email, and_(Users.provider.is_(None), Users.password.isnot(None))),
    )


async def find_account(session, email: str) -> Users | None:
    """The password account at an address (case and stray spaces aside);
    one written exactly so first, should two differ only in case."""
    from app.api.users.service import same_email

    address = normalise(email)
    if not address:
        return None
    return await session.scalar(
        select(Users).where(same_email(Users.email, address), password_accounts())
        .order_by((func.lower(Users.email) == Users.email).desc(), Users.id).limit(1)
    )


async def _close_open(session, key: str, now: datetime, user_id: int | None = None) -> None:
    which = PasswordResetCodes.email_key == key
    if user_id is not None:
        which = or_(which, PasswordResetCodes.user_id == user_id)
    await session.execute(
        update(PasswordResetCodes).where(which, PasswordResetCodes.closed_at.is_(None))
        .values(closed_at=now).execution_options(synchronize_session=False)
    )


async def close_for_user(session, user_id: int, now: datetime | None = None) -> None:
    """Every open reset code sent to this account stops counting (its
    password has been taken away, or set some other way)."""
    await session.execute(
        update(PasswordResetCodes).where(PasswordResetCodes.user_id == user_id, PasswordResetCodes.closed_at.is_(None))
        .values(closed_at=now or _now()).execution_options(synchronize_session=False)
    )


# ─── Asking for a code ──────────────────────────────────────────────────


async def issue(session, email: str) -> tuple[Users | None, str | None]:
    """A new code for the address, replacing any before it: (the account,
    the code to mail). (None, None) when the address has no password account
    (its row is kept, over a secret nothing can match) or asked too recently
    or too often today (nothing is kept). Committed when a row was made."""
    address = normalise(email)
    key = address_key(address)
    await session.execute(select(func.pg_advisory_xact_lock(_RESET_LOCK, _lock_id(key))))
    now = _now()
    sent = [verification._aware(t) for t in await session.scalars(
        select(PasswordResetCodes.sent_at)
        .where(PasswordResetCodes.email_key == key, PasswordResetCodes.sent_at > now - DAY)
        .order_by(PasswordResetCodes.sent_at.desc())
    )]
    if verification._wait(sent, now) is not None:
        return None, None  # nothing written; the lock goes with the session
    # Anyone's rows a day old count for nothing any more.
    await session.execute(delete(PasswordResetCodes).where(PasswordResetCodes.sent_at <= now - DAY).execution_options(synchronize_session=False))
    await _close_open(session, key, now)
    account = await find_account(session, address)
    # With no account, the row is kept all the same (so the address counts
    # down and locks like any other), over a secret no six digits can match.
    code = verification.new_code() if account is not None else None
    session.add(PasswordResetCodes(
        email_key=key, user_id=account.id if account is not None else None,
        code_hash=code_hash(address, code if code is not None else secrets.token_hex(32)),
        sent_at=now, expires_at=now + CODE_TTL, attempts=0,
    ))
    await session.commit()
    return account, code


async def request_reset(email: str) -> None:
    """After the response to "forgot my password": a code for the address,
    and the mail when a password account is there to send it to. Never raises."""
    address = normalise(email)
    if not address or "@" not in address or len(address) > EMAIL_MAX:
        return
    try:
        async with session_scope() as session:
            account, code = await issue(session, address)
            to, name = (account.email, account.full_name) if account is not None else (None, None)
        if account is not None and code:
            await run_in_threadpool(send_reset_email, to, code=code, name=name)
    except Exception:  # noqa: BLE001
        logger.warning("could not issue a password reset code", exc_info=True)


def send_reset_email(to: str, *, code: str, name: str | None = None) -> None:
    """The mail with the code, branded like the verification one. Logged,
    never raised, when it cannot go out: the person can ask again."""
    brand = settings.MAIL_FROM_NAME or "MyOtherApp"
    minutes = int(CODE_TTL.total_seconds() // 60)
    first = (name or "").strip().split(" ")[0]
    greeting = f"Hi {first}," if first else "Hi,"
    subject = f"Your {brand} password reset code"
    ignore = "If this wasn't you, ignore this email — your password is unchanged."
    text = (
        f"{greeting}\n\n"
        f"Your {brand} password reset code is:\n\n{code}\n\n"
        f"Type it into the app with your new password. It expires in {minutes} minutes.\n\n"
        f"{ignore}\n"
    )
    esc = html.escape
    body = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;'
        'color:#2C333D;max-width:480px;margin:0 auto;padding:24px 16px">'
        f'<p style="font-size:20px;font-weight:700;margin:0 0 24px">{esc(brand)}</p>'
        f"<p>{esc(greeting)}</p>"
        f"<p>{esc(subject)}:</p>"
        '<p style="font-size:36px;font-weight:700;letter-spacing:8px;'
        f'font-family:Menlo,Consolas,\'Courier New\',monospace;margin:24px 0">{esc(code)}</p>'
        f"<p>Type it into the app with your new password. It expires in {minutes} minutes.</p>"
        '<p style="color:#6B7280;font-size:13px;margin-top:32px">'
        f"{esc(ignore)}</p>"
        "</div>"
    )
    try:
        email_mod.send_email(recipients=[to], subject=subject, text=text, html=body)
    except Exception:
        logger.warning("password reset email to %s could not be sent", to, exc_info=True)


# ─── Using it ───────────────────────────────────────────────────────────


def _refuse(status_code: int, error_code: str, message: str, **errors) -> CustomHTTPException:
    return verification._refuse(status_code, error_code, message, **errors)


def _invalid(left: int) -> CustomHTTPException:
    return _refuse(
        status.HTTP_400_BAD_REQUEST, "CODE_INVALID",
        "That code isn't right. Check the email address and the code, or ask for a new one.", attempts_left=left,
    )


def _locked() -> CustomHTTPException:
    return _refuse(status.HTTP_400_BAD_REQUEST, "CODE_LOCKED", "Too many wrong tries. Ask for a new code.", attempts_left=0)


def _expired() -> CustomHTTPException:
    return _refuse(status.HTTP_400_BAD_REQUEST, "CODE_EXPIRED", "That code has expired. Ask for a new one.", attempts_left=0)


async def _wrong_try(session, row: PasswordResetCodes, now: datetime) -> int:
    """Count a wrong try (committed) and say how many are left."""
    row.attempts += 1
    left = max(0, MAX_ATTEMPTS - row.attempts)
    if left == 0:
        row.closed_at = now
    await session.commit()
    return left


async def reset(session, *, email: str, code: str, new_password: str, totp_code: str | None = None) -> Users:
    """Check the code for the address and set the new password. On success:
    the password set, every session ended (token_version and the session
    rows), the address verified, every open code closed. Flushed; the caller
    folds guests in, signs the person in and commits.

    Refusals, with wrong tries already committed: CODE_INVALID (with
    `attempts_left`) for a wrong code or an address with no live code, the
    same whether or not it has an account; CODE_LOCKED after the fifth wrong
    try; CODE_EXPIRED for an old, used or replaced code; TOTP_REQUIRED or
    TOTP_INVALID (401) when the account has two-factor on, the code being
    right (a wrong authenticator code counts as a wrong try)."""
    address = normalise(email)
    key = address_key(address)
    now = _now()
    row = await session.scalar(
        select(PasswordResetCodes)
        .where(PasswordResetCodes.email_key == key)
        .order_by(PasswordResetCodes.sent_at.desc(), PasswordResetCodes.id.desc())
        .limit(1)
        .with_for_update()
    )
    if row is None or verification._aware(row.sent_at) <= now - DAY:
        raise _invalid(MAX_ATTEMPTS)
    if row.attempts >= MAX_ATTEMPTS:
        raise _locked()
    if row.closed_at is not None:
        raise _expired()
    if verification._aware(row.expires_at) <= now:
        row.closed_at = now
        await session.commit()
        raise _expired()

    typed = re.sub(r"[\s-]", "", code or "")
    if not hmac.compare_digest(code_hash(address, typed), row.code_hash):
        left = await _wrong_try(session, row, now)
        raise _locked() if left == 0 else _invalid(left)

    user = await find_account(session, address)
    if user is None or row.user_id != user.id:
        # The account went, or stopped signing in with a password, since the code was sent.
        row.closed_at = now
        await session.commit()
        raise _expired()

    if getattr(user, "totp_enabled", False):
        from app.api.auth import totp as totp_service

        if not totp_code:
            raise _refuse(status.HTTP_401_UNAUTHORIZED, "TOTP_REQUIRED", "Enter the code from your authenticator app as well.")
        if not totp_service.verify(user.totp_secret, totp_code):
            remaining = totp_service.consume_recovery_code(totp_code, user.totp_recovery_codes)
            if remaining is None:
                left = await _wrong_try(session, row, now)
                if left == 0:
                    raise _locked()
                raise _refuse(status.HTTP_401_UNAUTHORIZED, "TOTP_INVALID", "That authenticator code is not valid.", attempts_left=left)
            user.totp_recovery_codes = remaining

    from app.core.auth.authentication import get_password_hash

    user.password = get_password_hash(new_password)
    # Ends every access and refresh token issued before now, and every session.
    user.token_version = (user.token_version or 0) + 1
    await session.execute(
        update(UserSessions)
        .where(UserSessions.user_id == user.id, UserSessions.revoked == False)  # noqa: E712
        .values(revoked=True)
        .execution_options(synchronize_session=False)
    )
    if user.email_verified_at is None:
        user.email_verified_at = now
    await _close_open(session, key, now, user.id)
    await verification._close_open_codes(session, user.id, now)
    await session.flush()
    return user
