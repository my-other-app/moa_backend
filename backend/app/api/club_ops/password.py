"""A forgotten password on the clubs dashboard: the club's own account, or a
team member's (app/api/club_access).

The app's reset (app/api/auth/password_reset.py) serves app accounts only;
this is the same flow for the dashboard's two kinds of login, with its own
codes table, so neither can be used for the other:

- asking (POST /auth/club/password/forgot) answers `{"sent": true}` at once,
  whatever the address; the work happens after the response. A code row is
  kept per address whether or not a login is behind it (over a secret no six
  digits match), so a wrong code counts down and locks the same either way.
  One a minute and ten a day per address; the route is limited per caller.
- the code is good for fifteen minutes and five wrong tries; a new one
  replaces any before it.
- using it (POST /auth/club/password/reset) sets the new password (eight
  characters or more, as for a team member's) and ends every session of that
  login: the club account's own (and with it its team's, which act on its
  tokens), or the member's. A club account with two-factor on needs its
  authenticator or a recovery code as well.

When one address is both a club account and a team member somewhere, the
club account's password is the one reset (signing in with it on the
dashboard then opens the club account, as it would have).
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, func, or_, select, update
from starlette import status
from starlette.concurrency import run_in_threadpool

from app.api.auth import verification
from app.api.auth.sessions import UserSessions
from app.api.club_access.models import ClubMembers
from app.api.club_ops.models import ClubPasswordResetCodes as Codes
from app.api.clubs.models import Clubs
from app.api.users.models import UserTypes, Users
from app.config import settings
from app.core.email import email as email_mod
from app.db import lock_keys
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

CODE_TTL = verification.CODE_TTL
MAX_ATTEMPTS = verification.MAX_ATTEMPTS
DAY = verification.DAY
EMAIL_MAX = 254
_LOCK = lock_keys.CLUB_PASSWORD_RESET


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalise(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def address_key(email: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"club-password-reset-address:{normalise(email)}".encode(), hashlib.sha256).hexdigest()


def code_hash(email: str, code: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"club-password-reset:{normalise(email)}:{code}".encode(), hashlib.sha256).hexdigest()


@asynccontextmanager
async def session_scope():
    """A session for the work after the response. Tests swap this for their own."""
    from app.db.core import AsyncSessionLocal
    from app.db.listeners import add_loader_criteria

    async with AsyncSessionLocal() as session:
        add_loader_criteria(session)
        yield session


async def find_login(session, email: str) -> tuple[Optional[Users], Optional[ClubMembers]]:
    """The club account at the address, else the active team member."""
    from app.api.users.service import same_email

    address = normalise(email)
    if not address:
        return None, None
    user = await session.scalar(
        select(Users).join(Clubs, Clubs.user_id == Users.id)
        .where(
            same_email(Users.email, address), Users.user_type == UserTypes.club, Users.is_deleted == False,  # noqa: E712
            Users.password.isnot(None), Clubs.is_deleted == False,  # noqa: E712
        )
        .order_by((func.lower(Users.email) == Users.email).desc(), Users.id).limit(1)
    )
    if user is not None:
        return user, None
    member = await session.scalar(
        select(ClubMembers).where(ClubMembers.email == address, ClubMembers.status == "active", ClubMembers.password.isnot(None))
    )
    return None, member


# ─── Asking for a code ──────────────────────────────────────────────────


async def issue(session, email: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """A new code for the address: (where to send it, the name to greet, the
    code). Nones when there is no login there (its row is kept all the same)
    or the address asked too recently or too often today (nothing kept)."""
    address = normalise(email)
    key = address_key(address)
    await session.execute(select(func.pg_advisory_xact_lock(_LOCK, int(key[:8], 16) - 2**31)))
    now = _now()
    sent = [verification._aware(t) for t in await session.scalars(
        select(Codes.sent_at).where(Codes.email_key == key, Codes.sent_at > now - DAY).order_by(Codes.sent_at.desc())
    )]
    if verification._wait(sent, now) is not None:
        return None, None, None
    await session.execute(delete(Codes).where(Codes.sent_at <= now - DAY).execution_options(synchronize_session=False))
    await session.execute(
        update(Codes).where(Codes.email_key == key, Codes.closed_at.is_(None)).values(closed_at=now)
        .execution_options(synchronize_session=False)
    )
    user, member = await find_login(session, address)
    found = user is not None or member is not None
    code = verification.new_code() if found else None
    session.add(Codes(
        email_key=key, user_id=user.id if user is not None else None, member_id=member.id if member is not None else None,
        code_hash=code_hash(address, code if code is not None else secrets.token_hex(32)),
        sent_at=now, expires_at=now + CODE_TTL, attempts=0,
    ))
    await session.commit()
    if not found:
        return None, None, None
    to = user.email if user is not None else member.email
    name = (user.full_name if user is not None else member.full_name) or None
    return to, name, code


async def request_reset(email: str) -> None:
    """After the response: a code for the address, mailed when a club login
    is there to send it to. Never raises."""
    address = normalise(email)
    if not address or "@" not in address or len(address) > EMAIL_MAX:
        return
    try:
        async with session_scope() as session:
            to, name, code = await issue(session, address)
        if to and code:
            await run_in_threadpool(send_code, to, code=code, name=name)
    except Exception:  # noqa: BLE001
        logger.warning("could not issue a club password reset code", exc_info=True)


def send_code(to: str, *, code: str, name: Optional[str] = None) -> None:
    brand = settings.MAIL_FROM_NAME or "MyOtherApp"
    minutes = int(CODE_TTL.total_seconds() // 60)
    first = (name or "").strip().split(" ")[0]
    greeting = f"Hi {first}," if first else "Hi,"
    subject = f"Your {brand} club dashboard password reset code"
    where = "Type it on the club dashboard's \"Forgot password?\" page with your new password."
    ignore = "If this wasn't you, ignore this email — your password is unchanged."
    text = f"{greeting}\n\nYour code to reset your club dashboard password is:\n\n{code}\n\n{where} It expires in {minutes} minutes.\n\n{ignore}\n"
    esc = html.escape
    body = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;'
        'color:#2C333D;max-width:480px;margin:0 auto;padding:24px 16px">'
        f'<p style="font-size:20px;font-weight:700;margin:0 0 24px">{esc(brand)}</p>'
        f"<p>{esc(greeting)}</p><p>Your code to reset your club dashboard password is:</p>"
        '<p style="font-size:36px;font-weight:700;letter-spacing:8px;'
        f'font-family:Menlo,Consolas,\'Courier New\',monospace;margin:24px 0">{esc(code)}</p>'
        f"<p>{esc(where)} It expires in {minutes} minutes.</p>"
        f'<p style="color:#6B7280;font-size:13px;margin-top:32px">{esc(ignore)}</p></div>'
    )
    try:
        email_mod.send_email(recipients=[to], subject=subject, text=text, html=body)
    except Exception:  # noqa: BLE001
        logger.warning("club password reset email could not be sent", exc_info=True)


# ─── Using it ───────────────────────────────────────────────────────────


def _invalid(left: int) -> CustomHTTPException:
    return verification._refuse(
        status.HTTP_400_BAD_REQUEST, "CODE_INVALID",
        "That code isn't right. Check the email address and the code, or ask for a new one.", attempts_left=left,
    )


def _locked() -> CustomHTTPException:
    return verification._refuse(status.HTTP_400_BAD_REQUEST, "CODE_LOCKED", "Too many wrong tries. Ask for a new code.", attempts_left=0)


def _expired() -> CustomHTTPException:
    return verification._refuse(status.HTTP_400_BAD_REQUEST, "CODE_EXPIRED", "That code has expired. Ask for a new one.", attempts_left=0)


async def _wrong_try(session, row: Codes, now: datetime) -> int:
    row.attempts += 1
    left = max(0, MAX_ATTEMPTS - row.attempts)
    if left == 0:
        row.closed_at = now
    await session.commit()
    return left


async def reset(session, *, email: str, code: str, new_password: str, totp_code: Optional[str] = None) -> str:
    """Check the code and set the password. Returns "club" or "member".
    Refusals as the app's reset: CODE_INVALID (with attempts_left), CODE_LOCKED,
    CODE_EXPIRED; TOTP_REQUIRED / TOTP_INVALID for a club account with
    two-factor on; WEAK_PASSWORD (422) before any of them, as nothing is
    spent on a password that would be refused."""
    from app.api.club_access import service as club_access
    from app.core.auth.authentication import get_password_hash_async

    club_access.check_password(new_password)
    address = normalise(email)
    key = address_key(address)
    now = _now()
    row = await session.scalar(
        select(Codes).where(Codes.email_key == key).order_by(Codes.sent_at.desc(), Codes.id.desc()).limit(1).with_for_update()
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

    user, member = await find_login(session, address)
    if (user is not None and row.user_id != user.id) or (member is not None and row.member_id != member.id) or (user is None and member is None):
        # The login went (or changed hands) since the code was sent.
        row.closed_at = now
        await session.commit()
        raise _expired()

    if user is not None and getattr(user, "totp_enabled", False):
        from app.api.auth import totp as totp_service

        if not totp_code:
            raise verification._refuse(status.HTTP_401_UNAUTHORIZED, "TOTP_REQUIRED", "Enter the code from your authenticator app as well.")
        if not await totp_service.check_second_factor(session, user, totp_code):
            left = await _wrong_try(session, row, now)
            if left == 0:
                raise _locked()
            raise verification._refuse(status.HTTP_401_UNAUTHORIZED, "TOTP_INVALID", "That authenticator code is not valid.", attempts_left=left)

    hashed = await get_password_hash_async(new_password)
    if user is not None:
        user.password = hashed
        # Every token issued before now, the team's included (they act on the club account's).
        user.token_version = (user.token_version or 0) + 1
        await session.execute(
            update(UserSessions).where(UserSessions.user_id == user.id, UserSessions.revoked == False)  # noqa: E712
            .values(revoked=True).execution_options(synchronize_session=False)
        )
        kind = "club"
    else:
        member.password = hashed
        await session.execute(
            update(UserSessions).where(UserSessions.member_id == member.id, UserSessions.revoked == False)  # noqa: E712
            .values(revoked=True).execution_options(synchronize_session=False)
        )
        kind = "member"
    await session.execute(
        update(Codes).where(or_(Codes.email_key == key), Codes.closed_at.is_(None)).values(closed_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return kind
