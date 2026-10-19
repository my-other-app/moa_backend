"""What an assistant may only ask for: a person approves it, or not.

Publishing an event and sending an announcement reach people outside the
club, so no token does either. A `request_*` tool makes a pending action
with a one-time token and hands back https://club-dashboard.../confirm/{token};
someone on the club's team opens it, signed in to the dashboard as usual,
sees exactly what will happen (`describe`) and confirms or rejects it. The
token is good once, for a day; only its digest is kept. A newer request for
the same thing supersedes an older one still waiting.

Confirming does what the dashboard itself does, as the person confirming --
with the plan's limits, their role (policy.allows_action) and the event as it
is *then*: an event published meanwhile is not published twice.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app.api.audit import service as audit
from app.api.club_access import policy
from app.api.club_access.models import ClubMembers
from app.api.closeout.models import EventStatus
from app.api.events.models import Events
from app.api.mcp.models import AnnouncementDrafts, McpPendingActions
from app.api.mcp.oauth import dashboard_url, digest, new_token
from app.api.users.models import Users
from app.config import settings
from app.core import public_urls
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

PREFIX = "moa_pa_"
LIFETIME = timedelta(hours=24)
AUDIENCE_WORDS = {"all": "everyone registered", "attendees": "those who checked in", "non_attendees": "those who registered but did not check in"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def confirm_url(token: str) -> str:
    return dashboard_url(f"/confirm/{token}")


def _refuse(status: int, message: str, code: str) -> CustomHTTPException:
    return CustomHTTPException(status_code=status, message=message, error_code=code)


async def draft_of(session, club_id: int, draft_id) -> AnnouncementDrafts | None:
    try:
        key = UUID(str(draft_id))
    except (TypeError, ValueError):
        return None
    draft = await session.get(AnnouncementDrafts, key)
    return draft if draft is not None and draft.club_id == club_id else None


# ─── Asking ──────────────────────────────────────────────────────────────


async def request(session, principal, *, kind: str, target_type: str, target_id, payload: dict, request=None) -> tuple[McpPendingActions, str]:
    """A new pending action, and its token (shown once, never stored). Not committed."""
    now = _now()
    await session.execute(
        update(McpPendingActions)
        .where(McpPendingActions.club_id == principal.club.id, McpPendingActions.kind == kind,
               McpPendingActions.target_id == str(target_id), McpPendingActions.status == "pending")
        .values(status="superseded", decided_at=now)
    )
    token = new_token(PREFIX)
    row = McpPendingActions(
        club_id=principal.club.id, grant_id=principal.grant.id, kind=kind, target_type=target_type, target_id=str(target_id),
        payload=payload, token_hash=digest(token), status="pending", expires_at=now + LIFETIME,
        requested_by_user_id=principal.user.id, requested_by_member_id=principal.member.id if principal.member else None,
        client_name=principal.client_name[:200],
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session, actor=principal.user, action="mcp.action.requested", entity_type=target_type, entity_id=target_id,
        summary=f"{principal.client_name} asked to {kind.replace('_', ' ')}"[:255],
        details={"via": "mcp", "client": principal.client_name, "action_id": str(row.id), "kind": kind,
                 "member_id": principal.member.id if principal.member else None},
        request=request,
    )
    return row, token


def requested_out(row: McpPendingActions, token: str) -> dict:
    what = {"publish_event": "publish the event", "send_announcement": "send the announcement"}[row.kind]
    return {
        "status": "pending_approval", "action_id": str(row.id), "kind": row.kind,
        "confirm_url": confirm_url(token), "expires_at": row.expires_at.isoformat(),
        "next_step": f"Nothing has happened yet. Someone on the club's team must open the confirm_url, signed in to the dashboard, and approve it to {what}. It expires in 24 hours.",
    }


# ─── The dashboard's side ────────────────────────────────────────────────


async def by_token(session, club_id: int | None, token: str) -> McpPendingActions:
    """The club's pending action behind a token. Another club's, or none, is 404 alike."""
    row = await session.scalar(
        select(McpPendingActions).where(McpPendingActions.token_hash == digest(token or "")).with_for_update()
    ) if token and token.startswith(PREFIX) else None
    if row is None or club_id is None or row.club_id != club_id:
        raise _refuse(404, "This link is not valid, or it is for another club.", "ACTION_NOT_FOUND")
    if row.status == "pending" and _aware(row.expires_at) <= _now():
        row.status = "expired"
        await session.commit()
    return row


async def _person(session, user_id, member_id) -> dict | None:
    if member_id:
        member = await session.get(ClubMembers, member_id)
        if member is not None:
            return {"name": member.full_name, "email": member.email, "role": member.role}
    if user_id:
        return {"name": None, "email": None, "role": "owner"}
    return None


async def describe(session, row: McpPendingActions, *, role: str) -> dict:
    """What confirming would do, for the dashboard's confirm page."""
    target: dict = {}
    effects: list[str] = []
    title = "Approve an assistant's request"
    if row.kind == "publish_event":
        event = await session.scalar(select(Events).where(Events.id == int(row.target_id), Events.club_id == row.club_id))
        if event is not None:
            from app.api.clubs.models import ClubUsersLink

            followers = await session.scalar(
                select(func.count()).select_from(ClubUsersLink).where(
                    ClubUsersLink.club_id == row.club_id, ClubUsersLink.is_following == True, ClubUsersLink.is_deleted == False  # noqa: E712
                )
            ) or 0
            title = f"Publish “{event.name}”"
            target = {
                "type": "event", "id": event.id, "name": event.name, "status": (event.lifecycle or EventStatus.published).value,
                "deleted": bool(event.is_deleted), "starts_at": _aware(event.event_datetime).isoformat(),
                "venue": event.location_name, "online": bool(event.is_online), "paid": bool(event.has_fee),
                "fee": float(event.reg_fee) if event.has_fee and event.reg_fee is not None else None, "capacity": event.max_participants,
                "dashboard_url": dashboard_url(f"/events/{event.id}"), "public_url": public_urls.event_url(event.slug),
            }
            effects = [
                "The event becomes public: on the club's page, the website and the MyOtherApp app.",
                "Registration opens as the event is set up.",
                f"The club's followers ({followers}) are told about the new event.",
            ]
    elif row.kind == "send_announcement":
        draft = await session.get(AnnouncementDrafts, UUID(row.target_id))
        event = await session.get(Events, draft.event_id) if draft is not None else None
        if draft is not None and event is not None:
            from app.api.clubs import announcements_page

            reach = (await announcements_page.audience(session, event))["by_audience"][draft.audience]
            title = f"Send “{draft.title}”"
            target = {
                "type": "announcement", "id": str(draft.id), "status": draft.status, "title": draft.title, "body": draft.body,
                "audience": draft.audience, "event": {"id": event.id, "name": event.name},
                "people": reach["people"], "reachable": reach["reachable"],
            }
            effects = [
                f"A push notification and an inbox message go to {AUDIENCE_WORDS[draft.audience]} of “{event.name}”: "
                f"{reach['people']} people, {reach['reachable']} of them with the app.",
                "It counts toward the plan's announcements this month.",
            ]
    expired = row.status == "expired"
    can_decide = row.status == "pending" and policy.allows_action(role, row.kind) and bool(target)
    return {
        "id": str(row.id), "kind": row.kind, "status": row.status, "title": title, "effects": effects, "target": target,
        "requested": {"app": row.client_name, "by": await _person(session, row.requested_by_user_id, row.requested_by_member_id), "at": _aware(row.created_at).isoformat()},
        "expires_at": _aware(row.expires_at).isoformat(), "expired": expired,
        "can_decide": can_decide,
        "cannot_decide_reason": None if can_decide or row.status != "pending" else (
            "This action is no longer possible." if not target else "Your role can't approve this. Ask an admin or the club's owner."
        ),
        "decided": {"at": _aware(row.decided_at).isoformat(), "by": await _person(session, row.decided_by_user_id, row.decided_by_member_id)} if row.decided_at and row.status in ("confirmed", "rejected", "failed") else None,
        "result": row.result,
    }


def _check_decidable(row: McpPendingActions, role: str) -> None:
    if row.status == "expired":
        raise _refuse(410, "This request expired. Ask the assistant again.", "ACTION_EXPIRED")
    if row.status != "pending":
        raise _refuse(409, f"This request was already {row.status}.", "ACTION_DECIDED")
    if not policy.allows_action(role, row.kind):
        raise _refuse(403, "Your role can't approve this. Ask an admin or the club's owner.", "ROLE_FORBIDDEN")


async def confirm(session, row: McpPendingActions, *, user: Users, member: ClubMembers | None, request=None) -> dict:
    """Do it, as the person confirming."""
    if not settings.MCP_ENABLED:
        raise _refuse(503, "Connected assistants are switched off right now, so their requests can't be approved.", "MCP_DISABLED")
    role = member.role if member is not None else "owner"
    _check_decidable(row, role)
    if row.kind == "publish_event":
        result = await _publish(session, row, user=user)
    elif row.kind == "send_announcement":
        result = await _send(session, row, user=user)
    else:  # pragma: no cover - the check constraint holds the kinds
        raise _refuse(422, "Unknown request.", "ACTION_UNKNOWN")
    row.status, row.result = "confirmed", result
    row.decided_at, row.decided_by_user_id, row.decided_by_member_id = _now(), user.id, member.id if member else None
    await audit.record(
        session, actor=user, action="mcp.action.confirmed", entity_type=row.target_type, entity_id=row.target_id,
        summary=f"Approved {row.client_name}'s request to {row.kind.replace('_', ' ')}"[:255],
        details={"via": "dashboard", "requested_via": "mcp", "client": row.client_name, "action_id": str(row.id), "kind": row.kind,
                 "member_id": member.id if member else None, "result": result},
        request=request,
    )
    await session.commit()
    return await describe(session, row, role=role)


async def reject(session, row: McpPendingActions, *, user: Users, member: ClubMembers | None, request=None) -> dict:
    role = member.role if member is not None else "owner"
    _check_decidable(row, role)
    row.status = "rejected"
    row.decided_at, row.decided_by_user_id, row.decided_by_member_id = _now(), user.id, member.id if member else None
    await audit.record(
        session, actor=user, action="mcp.action.rejected", entity_type=row.target_type, entity_id=row.target_id,
        summary=f"Rejected {row.client_name}'s request to {row.kind.replace('_', ' ')}"[:255],
        details={"via": "dashboard", "requested_via": "mcp", "client": row.client_name, "action_id": str(row.id), "kind": row.kind,
                 "member_id": member.id if member else None},
        request=request,
    )
    await session.commit()
    return await describe(session, row, role=role)


async def _fail(session, row: McpPendingActions, message: str, code: str, status: int = 409):
    row.status, row.decided_at, row.result = "failed", _now(), {"error": code, "message": message}
    await session.commit()
    raise _refuse(status, message, code)


async def _publish(session, row: McpPendingActions, *, user: Users) -> dict:
    from app.api.closeout import service as closeout
    from app.api.closeout.schemas import SettingsPatch
    from app.core.notifications.triggers import notify_followers_of_new_event, notify_users_by_interest

    event = await session.scalar(
        select(Events).where(Events.id == int(row.target_id), Events.club_id == row.club_id, Events.is_deleted == False)  # noqa: E712
        .execution_options(populate_existing=True)
    )
    if event is None:
        await _fail(session, row, "The event no longer exists.", "EVENT_NOT_FOUND", 404)
    if event.lifecycle is not EventStatus.draft:
        await _fail(session, row, f"The event is not a draft any more (it is {event.lifecycle.value}).", "EVENT_NOT_DRAFT")
    await closeout.update_settings(session, event, SettingsPatch(status="published"))
    # Published now, it is new to its followers: told once, as creating a
    # published event in the dashboard tells them (each job deduped per event).
    try:
        from app.api.clubs.models import Clubs
        from app.api.interests.models import Interests

        club = await session.get(Clubs, event.club_id)
        await notify_followers_of_new_event(session=session, club_id=club.id, club_name=club.name, event_id=event.id, event_name=event.name)
        loaded = await session.scalar(
            select(Events).where(Events.id == event.id).options(selectinload(Events.interests).joinedload(Interests.category))
            .execution_options(populate_existing=True)
        )
        topics = loaded.topics if loaded is not None else []
        if topics:
            await notify_users_by_interest(
                session=session, category_id=[t["id"] for t in topics], category_name=topics[0]["name"],
                club_id=club.id, event_id=event.id, event_name=event.name,
            )
    except Exception:  # noqa: BLE001 - telling followers must never undo the publish
        logger.exception("telling followers about event %s after an approved publish failed", event.id)
    return {"event_id": event.id, "status": "published", "public_url": public_urls.event_url(event.slug)}


async def _send(session, row: McpPendingActions, *, user: Users) -> dict:
    from app.api.subscriptions import gates
    from app.core.notifications.triggers import notify_event_participants

    draft = await session.get(AnnouncementDrafts, UUID(row.target_id))
    if draft is None or draft.club_id != row.club_id:
        await _fail(session, row, "The announcement draft no longer exists.", "DRAFT_NOT_FOUND", 404)
    if draft.status != "draft":
        await _fail(session, row, f"The announcement was already {draft.status}.", "DRAFT_NOT_PENDING")
    event = await session.scalar(select(Events).where(Events.id == draft.event_id, Events.club_id == row.club_id, Events.is_deleted == False))  # noqa: E712
    if event is None:
        await _fail(session, row, "The announcement's event no longer exists.", "EVENT_NOT_FOUND", 404)
    # The plan counts announcements per month, as the dashboard's send does.
    # Refused here, the request stays pending: upgrade, then approve it.
    await gates.require_within(session, event.club_id, "announcements_per_month", actor=user)
    sent, people = await notify_event_participants(
        session=session, event_id=event.id, title=draft.title, body=draft.body, audience=draft.audience, club_id=event.club_id,
    )
    draft.status, draft.sent_at = "sent", _now()
    return {"draft_id": str(draft.id), "event_id": event.id, "people": len(people), "pushed": sent}
