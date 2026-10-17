"""Inviting people to a club's dashboard, letting them in, and taking access away."""
import hashlib
import html
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Request, status
from sqlalchemy import select, update

from app.api.club_access import policy
from app.api.club_access.models import ROLES, ClubMembers
from app.api.clubs.models import Clubs
from app.api.users.models import Users, UserTypes
from app.config import settings
from app.core.auth.authentication import get_password_hash, verify_password
from app.core.email import email as email_mod
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

INVITE_DAYS = 7
MIN_PASSWORD = 8
_SEEN_EVERY = timedelta(minutes=5)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _refuse(code: int, message: str, error_code: str) -> CustomHTTPException:
    return CustomHTTPException(status_code=code, message=message, error_code=error_code)


def normalise_email(value: str) -> str:
    email = (value or "").strip().lower()
    if not _EMAIL.match(email) or len(email) > 100:
        raise _refuse(422, "Enter a valid email address", "INVALID_EMAIL")
    return email


def check_password(value: str) -> str:
    if len(value or "") < MIN_PASSWORD:
        raise _refuse(422, f"Use at least {MIN_PASSWORD} characters", "WEAK_PASSWORD")
    return value


def check_role(value: str) -> str:
    if value not in ROLES:
        raise _refuse(422, "Pick admin, editor or door", "INVALID_ROLE")
    return value


def role_label(role: str) -> str:
    return policy.ROLE_INFO.get(role, (role.title(), ""))[0]


# ─── Who is asking ───────────────────────────────────────────────────────


def current_member(request: Request) -> ClubMembers | None:
    """The member behind this request, set by the auth dependency; None for the club's own account."""
    return getattr(request.state, "club_member", None)


def role_of(request: Request) -> str:
    member = current_member(request)
    return member.role if member else "owner"


async def club_of(session, user: Users) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise _refuse(status.HTTP_403_FORBIDDEN, "Only a club can manage its dashboard access", "NOT_A_CLUB")
    return club


async def check_member(session, request: Request, user: Users, member_id: int) -> ClubMembers:
    """Run on every request a member's session makes: still a member of this
    club, and the role allows this method on this path."""
    member = await session.get(ClubMembers, member_id)
    club = getattr(user, "club", None)
    if member is None or member.status != "active" or club is None or member.club_id != club.id:
        raise CustomHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            message="Your access to this club's dashboard was removed",
            error_code="ACCESS_REMOVED",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if policy.owner_only(request.method, request.url.path):
        raise _refuse(
            status.HTTP_403_FORBIDDEN,
            "Only the club's owner can change where its payments go.",
            "OWNER_ONLY",
        )
    if not policy.allows(member.role, request.method, request.url.path):
        raise _refuse(
            status.HTTP_403_FORBIDDEN,
            f"Your role ({role_label(member.role)}) can't do this. Ask the club's owner.",
            "ROLE_FORBIDDEN",
        )
    seen = _aware(member.last_seen_at)
    if seen is None or _now() - seen > _SEEN_EVERY:
        member.last_seen_at = _now()
        try:  # like the session's own touch: committed now, a GET never commits
            await session.commit()
        except Exception:
            await session.rollback()
    request.state.club_member = member
    return member


# ─── The list ────────────────────────────────────────────────────────────


async def members_of(session, club: Clubs) -> list[ClubMembers]:
    rows = await session.scalars(
        select(ClubMembers)
        .where(ClubMembers.club_id == club.id, ClubMembers.status != "removed")
        .order_by(ClubMembers.status.desc(), ClubMembers.created_at)
    )
    return list(rows)


async def _member_of_club(session, club: Clubs, member_id: int) -> ClubMembers:
    member = await session.get(ClubMembers, member_id)
    if member is None or member.club_id != club.id or member.status == "removed":
        raise _refuse(status.HTTP_404_NOT_FOUND, "No such person on this club's access list", "MEMBER_NOT_FOUND")
    return member


def to_row(member: ClubMembers) -> dict:
    expires = _aware(member.invite_expires_at)
    return dict(
        id=member.id,
        email=member.email,
        full_name=member.full_name,
        role=member.role,
        role_label=role_label(member.role),
        status=member.status,
        invite_expired=bool(member.status == "invited" and expires is not None and expires < _now()),
        invited_at=_aware(member.invited_at),
        accepted_at=_aware(member.accepted_at),
        last_seen_at=_aware(member.last_seen_at),
    )


# ─── Inviting ────────────────────────────────────────────────────────────


def invite_url(token: str) -> str:
    return f"{settings.PUBLIC_WEB_URL.rstrip('/')}/join?token={token}"


def _new_token(member: ClubMembers) -> str:
    token = secrets.token_urlsafe(32)
    member.invite_token_hash = _hash(token)
    member.invite_expires_at = _now() + timedelta(days=INVITE_DAYS)
    member.invited_at = _now()
    return token


def send_invite_email(member: ClubMembers, *, club_name: str, token: str) -> None:
    link = invite_url(token)
    role = role_label(member.role)
    what = policy.ROLE_INFO[member.role][1]
    greeting = f"Hi {member.full_name}," if member.full_name else "Hi,"
    text = (
        f"{greeting}\n\n{club_name} added you to its dashboard on MyOtherApp as {role}.\n{what}\n\n"
        f"Set your password and sign in: {link}\n\nThe link works for {INVITE_DAYS} days. "
        f"If you weren't expecting this, ignore it and nothing happens.\n"
    )
    esc = html.escape
    body = (
        f"<p>{esc(greeting)}</p>"
        f"<p><strong>{esc(club_name)}</strong> added you to its dashboard on MyOtherApp as <strong>{esc(role)}</strong>.<br>{esc(what)}</p>"
        f"<p><a href=\"{esc(link)}\">Set your password and sign in</a></p>"
        f"<p>The link works for {INVITE_DAYS} days. If you weren't expecting this, ignore it and nothing happens.</p>"
    )
    try:
        email_mod.send_email(recipients=[member.email], subject=f"You're invited to {club_name} on MyOtherApp", text=text, html=body)
    except Exception:
        # The owner can still copy the link from the page; a failed mail is not a failed invite.
        logger.warning("invite email to %s could not be sent", member.email, exc_info=True)


async def invite(session, club: Clubs, *, email: str, full_name: str | None, role: str) -> tuple[ClubMembers, str]:
    email = normalise_email(email)
    check_role(role)
    live = await session.scalar(select(ClubMembers).where(ClubMembers.email == email, ClubMembers.status != "removed"))
    if live is not None:
        if live.club_id == club.id:
            raise _refuse(status.HTTP_409_CONFLICT, "This person already has access", "ALREADY_MEMBER")
        raise _refuse(status.HTTP_409_CONFLICT, "This address already signs in to another club's dashboard", "MEMBER_ELSEWHERE")
    club_login = await session.scalar(
        select(Users.id).where(Users.email == email, Users.user_type == UserTypes.club, Users.is_deleted == False)  # noqa: E712
    )
    if club_login is not None:
        raise _refuse(status.HTTP_409_CONFLICT, "This address is a club's own login; use another address", "CLUB_LOGIN_EMAIL")
    member = ClubMembers(
        club_id=club.id, email=email, full_name=(full_name or "").strip()[:80] or None, role=role, status="invited",
    )
    token = _new_token(member)
    session.add(member)
    await session.flush()
    return member, token


async def resend(session, club: Clubs, member_id: int) -> tuple[ClubMembers, str]:
    member = await _member_of_club(session, club, member_id)
    if member.status != "invited":
        raise _refuse(status.HTTP_409_CONFLICT, "This person has already joined", "ALREADY_JOINED")
    token = _new_token(member)
    await session.flush()
    return member, token


async def set_role(session, club: Clubs, member_id: int, role: str) -> ClubMembers:
    member = await _member_of_club(session, club, member_id)
    member.role = check_role(role)
    await session.flush()
    return member


async def remove(session, club: Clubs, member_id: int) -> ClubMembers:
    from app.api.auth.sessions import UserSessions

    member = await _member_of_club(session, club, member_id)
    member.status = "removed"
    member.removed_at = _now()
    member.invite_token_hash = None
    member.invite_expires_at = None
    # Their open sessions end now; the dependency would refuse them anyway.
    await session.execute(update(UserSessions).where(UserSessions.member_id == member.id).values(revoked=True))
    await session.flush()
    return member


# ─── Joining ─────────────────────────────────────────────────────────────


async def by_token(session, token: str) -> tuple[ClubMembers, Clubs]:
    member = await session.scalar(select(ClubMembers).where(ClubMembers.invite_token_hash == _hash(token or ""), ClubMembers.status == "invited"))
    club = await session.get(Clubs, member.club_id) if member else None
    if member is None or club is None or club.is_deleted:
        raise _refuse(status.HTTP_404_NOT_FOUND, "This invite link is not valid. Ask the club to send a new one.", "INVITE_NOT_FOUND")
    return member, club


def invite_expired(member: ClubMembers) -> bool:
    expires = _aware(member.invite_expires_at)
    return expires is None or expires < _now()


async def accept(session, token: str, *, full_name: str | None, password: str) -> ClubMembers:
    member, _club = await by_token(session, token)
    if invite_expired(member):
        raise _refuse(status.HTTP_410_GONE, "This invite has expired. Ask the club to send a new one.", "INVITE_EXPIRED")
    check_password(password)
    name = (full_name or "").strip()[:80]
    if name:
        member.full_name = name
    member.password = get_password_hash(password)
    member.status = "active"
    member.accepted_at = _now()
    member.invite_token_hash = None
    member.invite_expires_at = None
    await session.flush()
    return member


# ─── Signing in ──────────────────────────────────────────────────────────


async def authenticate(session, email: str, password: str) -> ClubMembers | None:
    """The active member with this email and password, or None."""
    address = (email or "").strip().lower()
    if "@" not in address:
        return None
    member = await session.scalar(select(ClubMembers).where(ClubMembers.email == address, ClubMembers.status == "active"))
    if member is None or not member.password or not verify_password(password, member.password):
        return None
    return member


async def change_password(session, member: ClubMembers, *, current: str, new: str) -> None:
    if not member.password or not verify_password(current or "", member.password):
        raise _refuse(status.HTTP_400_BAD_REQUEST, "The current password is not right", "WRONG_PASSWORD")
    member.password = get_password_hash(check_password(new))
    await session.flush()
