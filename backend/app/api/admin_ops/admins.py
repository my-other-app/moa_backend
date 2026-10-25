"""Admin accounts: who they are, adding one by email, taking one away.

Adding (owner only, MANAGE_ADMINS) promotes the account the address belongs
to -- an app account, or a guest record -- or makes one, and mails a link to
set a password (ADMIN_INVITE_DAYS). A club's account is never promoted: the
account *is* the club. Taking admin access away makes the account an app
account again and ends its sessions; nobody can remove themselves, and the
last owner stays.

Every change is in the admin audit log. A new admin has no two-factor
grace (app.api.admin_ops.two_factor): they set it up before they use the
portal.
"""
from __future__ import annotations

import hashlib
import html as html_mod
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from app.api.admin_ops.models import AdminInvites
from app.api.audit import service as audit
from app.api.users.models import AdminRoles, UserTypes, Users
from app.config import settings
from app.core import jobs
from app.core.auth.dependencies import AdminAuth
from app.core.auth.permissions import ManageAdminsAuth, role_of
from app.core.rate_limit import limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/admins", tags=["admin-accounts"])
public_router = APIRouter(prefix="/admin/invites", tags=["admin-accounts"])

ADMIN_INVITE_DAYS = 7
MIN_ADMIN_PASSWORD = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(at):
    return at.isoformat() if at else None


def _type(user) -> str:
    return getattr(user.user_type, "value", user.user_type)


class InviteIn(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    full_name: str | None = Field(None, max_length=100)
    role: str = Field("viewer")


class AcceptIn(BaseModel):
    token: str = Field(..., min_length=10, max_length=200)
    password: str = Field(..., max_length=200)


def _role(value: str) -> AdminRoles:
    try:
        return AdminRoles(value)
    except ValueError:
        raise CustomHTTPException(422, message=f"Unknown role: {value}", error_code="UNKNOWN_ROLE")


def _normal(email: str) -> str:
    out = (email or "").strip().lower()
    if "@" not in out or out.startswith("@") or out.endswith("@") or " " in out:
        raise CustomHTTPException(422, message="That is not an email address", error_code="INVALID_EMAIL")
    return out


def _invite_state(invite: AdminInvites | None, now: datetime) -> str | None:
    if invite is None:
        return None
    if invite.accepted_at:
        return "accepted"
    if invite.revoked_at:
        return "revoked"
    return "expired" if invite.expires_at <= now else "pending"


async def _owners(session) -> int:
    return int(await session.scalar(
        select(func.count()).select_from(Users).where(
            Users.user_type == UserTypes.admin, Users.is_deleted == False,  # noqa: E712
            Users.admin_role == AdminRoles.owner, Users.is_active == True,  # noqa: E712
        )
    ) or 0)


# ─── The list ────────────────────────────────────────────────────────────


@router.get("", summary="Every admin account, with its role, two-factor and invite (admin)")
async def list_admins(session: SessionDep, user: AdminAuth) -> dict:
    now = _now()
    admins = (await session.scalars(
        select(Users).where(Users.user_type == UserTypes.admin, Users.is_deleted == False)  # noqa: E712
        .order_by(Users.created_at, Users.id)
    )).all()
    ids = [a.id for a in admins]
    invites: dict[int, AdminInvites] = {}
    if ids:
        for inv in (await session.scalars(
            select(AdminInvites).where(AdminInvites.user_id.in_(ids)).order_by(AdminInvites.created_at)
        )).all():
            invites[inv.user_id] = inv  # the newest wins
    items = [{
        "id": a.id,
        "full_name": a.full_name,
        "email": a.email,
        "role": role_of(a).value,
        "two_factor": bool(a.totp_enabled),
        "is_active": bool(a.is_active),
        "is_you": a.id == user.id,
        "created_at": _iso(a.created_at),
        "invite": ({
            "state": _invite_state(invites[a.id], now),
            "created_at": _iso(invites[a.id].created_at),
            "expires_at": _iso(invites[a.id].expires_at),
            "promoted": bool(invites[a.id].promoted),
        } if a.id in invites else None),
    } for a in admins]
    return {
        "items": items,
        "owners": sum(1 for a in admins if role_of(a) is AdminRoles.owner and a.is_active),
        "without_two_factor": sum(1 for a in admins if not a.totp_enabled),
        "can_manage": role_of(user) is AdminRoles.owner,
    }


# ─── Adding one ──────────────────────────────────────────────────────────


async def _account_for(session, email: str) -> Users | None:
    """The account an address belongs to: an app account first, then an
    admin (already one), then a guest record. A club's account is answered
    too, so the caller can refuse it by name."""
    rows = (await session.scalars(
        select(Users).where(func.lower(func.btrim(Users.email)) == email, Users.is_deleted == False)  # noqa: E712
    )).all()
    order = {UserTypes.admin.value: 0, UserTypes.app_user.value: 1, UserTypes.club.value: 2, UserTypes.guest.value: 3}
    rows = sorted(rows, key=lambda u: (order.get(_type(u), 9), u.id))
    return rows[0] if rows else None


async def _new_invite(session, target: Users, *, role: AdminRoles, by: Users, promoted: bool) -> str:
    now = _now()
    await session.execute(
        update(AdminInvites)
        .where(AdminInvites.user_id == target.id, AdminInvites.accepted_at.is_(None), AdminInvites.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    token = secrets.token_urlsafe(32)
    session.add(AdminInvites(
        user_id=target.id, email=(target.email or "").strip().lower(), role=role.value, token_hash=_hash(token),
        invited_by=by.id, promoted=promoted, created_at=now, expires_at=now + timedelta(days=ADMIN_INVITE_DAYS),
    ))
    await session.flush()
    return token


def invite_link(token: str) -> str:
    return f"{settings.PUBLIC_ADMIN_URL.rstrip('/')}/accept-invite?token={token}"


async def _mail_invite(session, target: Users, token: str, *, by: Users, role: AdminRoles) -> None:
    await jobs.enqueue(session, "admin_ops.invite_email", {
        "to": target.email, "name": target.full_name, "link": invite_link(token), "by": by.full_name or by.email,
        "role": role.value, "has_password": bool(target.password),
    })


@router.post("/invite", summary="Add an admin by email: promote their account, or make one (owner)", status_code=201)
async def invite_admin(body: InviteIn, session: SessionDep, user: ManageAdminsAuth, request: Request) -> dict:
    email = _normal(body.email)
    role = _role(body.role)
    target = await _account_for(session, email)
    promoted = target is not None
    if target is not None:
        kind = _type(target)
        if kind == UserTypes.admin.value:
            raise CustomHTTPException(409, message="That address is already an admin.", error_code="ALREADY_ADMIN")
        if kind == UserTypes.club.value:
            raise CustomHTTPException(
                409, message="That address signs in to a club. Use a personal address for an admin.", error_code="CLUB_ACCOUNT",
            )
        before = kind
        target.user_type = UserTypes.admin
        target.admin_role = role
        target.is_active = True
        # Its tokens were an app user's: they end, and it signs in again as an admin.
        target.token_version = (target.token_version or 0) + 1
        if body.full_name and before == UserTypes.guest.value:
            target.full_name = body.full_name.strip()[:100]
    else:
        from app.api.users.service import generate_username

        name = (body.full_name or "").strip()[:100] or email.split("@", 1)[0]
        target = Users(full_name=name, email=email, user_type=UserTypes.admin, admin_role=role, password=None)
        await generate_username(target, session)
        session.add(target)
        await session.flush()
        before = None
    token = await _new_invite(session, target, role=role, by=user, promoted=promoted)
    await _mail_invite(session, target, token, by=user, role=role)
    await audit.record(
        session, actor=user, action="admin.invite", entity_type="user", entity_id=target.id,
        summary=f"Added {email} as {role.value}" + (" (promoted)" if promoted else " (new account)"),
        details={"email": email, "role": role.value, "promoted": promoted, "was": before}, request=request,
    )
    await session.commit()
    return {"user_id": target.id, "email": email, "role": role.value, "promoted": promoted,
            "expires_in_days": ADMIN_INVITE_DAYS}


@router.post("/{user_id}/resend-invite", summary="Mail a new set-password link (owner)")
async def resend_invite(user_id: int, session: SessionDep, user: ManageAdminsAuth, request: Request) -> dict:
    target = await session.scalar(select(Users).where(Users.id == user_id, Users.is_deleted == False))  # noqa: E712
    if target is None or _type(target) != UserTypes.admin.value:
        raise CustomHTTPException(404, message="No such admin", error_code="ADMIN_NOT_FOUND")
    role = role_of(target)
    token = await _new_invite(session, target, role=role, by=user, promoted=True)
    await _mail_invite(session, target, token, by=user, role=role)
    await audit.record(session, actor=user, action="admin.invite_resent", entity_type="user", entity_id=target.id,
                       summary=f"Sent {target.email} a new set-password link", request=request)
    await session.commit()
    return {"user_id": target.id, "expires_in_days": ADMIN_INVITE_DAYS}


@router.delete("/{user_id}", summary="Take admin access away: the account becomes an app account (owner)")
async def remove_admin(user_id: int, session: SessionDep, user: ManageAdminsAuth, request: Request) -> dict:
    if user_id == user.id:
        raise CustomHTTPException(409, message="You cannot remove your own admin access.", error_code="CANNOT_REMOVE_SELF")
    target = await session.scalar(
        select(Users).where(Users.id == user_id, Users.is_deleted == False).with_for_update()  # noqa: E712
    )
    if target is None or _type(target) != UserTypes.admin.value:
        raise CustomHTTPException(404, message="No such admin", error_code="ADMIN_NOT_FOUND")
    if role_of(target) is AdminRoles.owner and target.is_active and await _owners(session) <= 1:
        raise CustomHTTPException(409, message="This is the last owner; make someone else an owner first.", error_code="LAST_OWNER")
    role = role_of(target).value
    target.user_type = UserTypes.app_user
    # Least privilege should anything make it an admin again without choosing a role.
    target.admin_role = AdminRoles.viewer
    # Every session it holds ends at its next request.
    target.token_version = (target.token_version or 0) + 1
    await session.execute(
        update(AdminInvites)
        .where(AdminInvites.user_id == target.id, AdminInvites.accepted_at.is_(None), AdminInvites.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    await audit.record(
        session, actor=user, action="admin.remove", entity_type="user", entity_id=target.id,
        summary=f"Removed admin access from {target.email} (was {role})", details={"role": role}, request=request,
    )
    await session.commit()
    return {"user_id": target.id, "removed": True}


# ─── The set-password link ───────────────────────────────────────────────


async def _invite_for(session, token: str, *, lock: bool = False) -> tuple[AdminInvites, Users]:
    q = select(AdminInvites).where(AdminInvites.token_hash == _hash(token or ""))
    if lock:
        q = q.with_for_update()
    invite = await session.scalar(q)
    now = _now()
    if invite is None or invite.revoked_at is not None:
        raise CustomHTTPException(404, message="This link is not valid. Ask an owner for a new one.", error_code="INVITE_INVALID")
    if invite.accepted_at is not None:
        raise CustomHTTPException(409, message="This link was used already. Sign in.", error_code="INVITE_USED")
    if invite.expires_at <= now:
        raise CustomHTTPException(410, message="This link has expired. Ask an owner for a new one.", error_code="INVITE_EXPIRED")
    target = await session.scalar(select(Users).where(Users.id == invite.user_id, Users.is_deleted == False))  # noqa: E712
    if target is None or _type(target) != UserTypes.admin.value:
        raise CustomHTTPException(404, message="This link is not valid. Ask an owner for a new one.", error_code="INVITE_INVALID")
    return invite, target


@public_router.get("/check", summary="Whom a set-password link is for (public)")
@limiter.limit("20/minute")
async def check_invite(request: Request, token: str, session: SessionDep) -> dict:
    invite, target = await _invite_for(session, token)
    return {"email": target.email, "full_name": target.full_name, "role": invite.role,
            "expires_at": _iso(invite.expires_at), "has_password": bool(target.password)}


@public_router.post("/accept", summary="Set the password an invited admin signs in with (public)")
@limiter.limit("10/minute")
async def accept_invite(request: Request, body: AcceptIn, session: SessionDep) -> dict:
    from app.core.auth.authentication import get_password_hash_async

    if len(body.password or "") < MIN_ADMIN_PASSWORD:
        raise CustomHTTPException(422, message=f"Use at least {MIN_ADMIN_PASSWORD} characters.", error_code="WEAK_PASSWORD")
    invite, target = await _invite_for(session, body.token, lock=True)
    target.password = await get_password_hash_async(body.password)
    # The link reached the inbox: the address is theirs.
    if getattr(target, "email_verified_at", None) is None:
        target.email_verified_at = _now()
    target.token_version = (target.token_version or 0) + 1
    invite.accepted_at = _now()
    await audit.record(session, actor=target, action="admin.invite_accepted", entity_type="user", entity_id=target.id,
                       summary=f"{target.email} set their admin password", request=request)
    await session.commit()
    return {"email": target.email, "accepted": True}


# ─── The mail ────────────────────────────────────────────────────────────


@jobs.handler("admin_ops.invite_email", max_attempts=4, timeout=60.0, priority=jobs.HIGH)
async def _invite_email_job(p: dict) -> None:
    import asyncio

    await asyncio.to_thread(send_invite_email, p)


def send_invite_email(p: dict) -> None:
    from app.core.email import email as email_mod

    name = p.get("name") or "there"
    role = p.get("role") or "admin"
    link = p["link"]
    by = p.get("by") or "An owner"
    password_line = (
        "You can sign in with your current password, or set a new one with the link."
        if p.get("has_password") else "Set your password with the link to sign in."
    )
    text = (
        f"Hi {name},\n\n{by} added you as an admin ({role}) of MyOtherApp.\n\n{password_line}\n{link}\n\n"
        f"The link works for {ADMIN_INVITE_DAYS} days. Once you sign in you will set up two-factor sign-in "
        "with an authenticator app before you can use the portal.\n"
    )
    safe = html_mod.escape
    html = (
        f"<p>Hi {safe(name)},</p><p>{safe(by)} added you as an admin (<strong>{safe(role)}</strong>) of MyOtherApp.</p>"
        f"<p>{safe(password_line)}</p><p><a href=\"{safe(link)}\">Set your password</a></p>"
        f"<p>The link works for {ADMIN_INVITE_DAYS} days. Once you sign in you will set up two-factor sign-in "
        "with an authenticator app before you can use the portal.</p>"
    )
    email_mod.send_email(recipients=[p["to"]], subject="You're an admin of MyOtherApp", text=text, html=html,
                         check_suppression=False)
