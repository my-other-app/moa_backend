"""Telling the club, and writing it down.

- The activity log (audit.record): every connect, disconnect, schedule,
  approval and post, with who did it -- never a token, never the text.
- The club's bell (club_alerts): `social_disconnected` (an account needs
  reconnecting, with the posts waiting on it), `social_expiring` (access
  ends within seven days), `social_failed` (a post failed and needs a
  person). The owner is mailed too, once a day per kind; owners and admins
  who use the app get a push.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select

from app.config import settings

logger = logging.getLogger(__name__)

LABELS = {"facebook": "Facebook", "instagram": "Instagram", "linkedin": "LinkedIn", "linkedin_page": "LinkedIn Page", "x": "X"}
SETTINGS_PATH = "/settings/social"


def promote_path(event_id: int) -> str:
    return f"/events/{event_id}/promote"


def dashboard_url(path: str) -> str:
    return f"{settings.PUBLIC_WEB_URL.rstrip('/')}{path}"


async def record(session, *, actor, action: str, club_id: int, summary: str, details: Optional[dict] = None, request=None) -> None:
    from app.api.audit import service as audit

    user = getattr(actor, "user", actor)
    extra = actor.details() if hasattr(actor, "details") else {}
    await audit.record(
        session, actor=user, action=f"social.{action}", entity_type="club", entity_id=club_id,
        summary=summary[:255], details={**(details or {}), **extra}, request=request,
    )


def send(to: str, subject: str, paragraphs: list[str], button, footer: Optional[str]) -> None:
    """The mail, raising when the provider did not take it (a durable job retries it)."""
    from app.core.email import email as email_mod
    from app.core.email import plain

    email_mod.send_email(
        recipients=[to], subject=subject,
        text=plain.text(paragraphs, button=button, footer=footer),
        html=plain.render(paragraphs, button=button, footer=footer),
    )


def _send(to: str, subject: str, paragraphs: list[str], button, footer: Optional[str]) -> None:
    try:
        send(to, subject, paragraphs, button, footer)
    except Exception:  # noqa: BLE001
        logger.warning("social notice (%s) could not be sent", subject, exc_info=True)


async def admins_on_the_app(session, club_id: int) -> list[int]:
    """The app accounts of the club's admins (team members whose sign-in
    address is an app account's)."""
    from app.api.club_access.models import ClubMembers
    from app.api.users.models import UserTypes, Users

    rows = await session.scalars(
        select(Users.id).join(ClubMembers, func.lower(ClubMembers.email) == func.lower(Users.email))
        .where(ClubMembers.club_id == club_id, ClubMembers.status == "active", ClubMembers.role == "admin",
               Users.user_type == UserTypes.app_user, Users.is_deleted == False)  # noqa: E712
    )
    return sorted(set(rows))


async def tell(session, club_id: int, kind: str, *, title: str, body: str, link: str, dedupe_key: str,
               data: Optional[dict] = None, mail: bool = True, push: bool = True) -> bool:
    """A notice in the bell (once per `dedupe_key`), and with a new one a mail
    to the owner and a push to admins on the app. In the caller's
    transaction. True when the notice was new."""
    from app.api.club_alerts import notify
    from app.core.after_commit import run_after_commit

    made = await notify(session, club_id, kind, title=title, body=body, link=link, data=data or {}, dedupe_key=dedupe_key)
    if made is None:
        return False
    if mail:
        from app.api.subscriptions.notices import owner_address

        to = await owner_address(session, club_id)
        if to:
            run_after_commit(session, _send, to, title, [body], ("Open MyOtherApp", dashboard_url(link)), None)
    if push:
        from app.core import jobs

        users = await admins_on_the_app(session, club_id)
        if users:
            await jobs.enqueue(session, PUSH_JOB, {"user_ids": users, "title": title, "body": body,
                                                   "url": dashboard_url(link), "kind": kind, "club_id": club_id})
    return True


PUSH_JOB = "social.admin_push"


async def account_needs_reconnect(session, account, waiting: int) -> None:
    label = LABELS.get(account.platform, account.platform)
    posts = f" — {waiting} post{'s' if waiting != 1 else ''} waiting" if waiting else ""
    await tell(
        session, account.club_id, "social_disconnected",
        title=f"{label} disconnected{posts}",
        body=f"Reconnect {label} ({account.name or account.handle or 'your account'}) to send what is waiting.",
        link=SETTINGS_PATH, data={"account_id": str(account.id), "platform": account.platform, "waiting": waiting},
        dedupe_key=f"social_disconnected:{account.id}:{datetime.now(timezone.utc).date().isoformat()}",
    )


async def account_expiring(session, account, days: int) -> None:
    label = LABELS.get(account.platform, account.platform)
    ends = account.token_expires_at.date().isoformat() if account.token_expires_at else ""
    await tell(
        session, account.club_id, "social_expiring",
        title=f"{label} access ends in {days} day{'s' if days != 1 else ''}",
        body=f"Reconnect {label} ({account.name or account.handle or 'your account'}) before {ends} to keep posting.",
        link=SETTINGS_PATH, data={"account_id": str(account.id), "platform": account.platform, "expires_at": ends},
        dedupe_key=f"social_expiring:{account.id}:{ends}",
    )


async def post_failed(session, target, post) -> None:
    label = LABELS.get(target.platform, target.platform)
    await tell(
        session, target.club_id, "social_failed",
        title=f"A {label} post failed — share it manually",
        body=f"\"{post.name}\" did not go out on {label}: {target.error_message or 'the platform refused it'}",
        link=promote_path(target.event_id),
        data={"post_id": str(post.id), "target_id": str(target.id), "event_id": target.event_id, "platform": target.platform,
              "action": target.error_action},
        dedupe_key=f"social_failed:{target.id}:{target.attempts}",
    )


# ─── The push to admins ──────────────────────────────────────────────────


def _register() -> None:
    from app.core import jobs

    @jobs.handler(PUSH_JOB, max_attempts=4, timeout=120.0, priority=jobs.HIGH)
    async def _admin_push(p: dict) -> None:
        from app.core.notifications.service import send_notification_to_users

        async with jobs.scope() as session:
            await send_notification_to_users(
                session, [int(u) for u in p.get("user_ids") or []], p.get("title") or "", p.get("body") or "",
                data={"type": "social_alert", "kind": str(p.get("kind") or ""), "url": str(p.get("url") or ""),
                      "club_id": str(p.get("club_id") or "")},
            )


_register()
