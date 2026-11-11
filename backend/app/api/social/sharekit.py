"""The share kit, for every plan: each platform's text and poster, ready to
post by hand -- for the club's team from the app's share sheet, and for
attendees from the event page's share row.

- The team: a club's dashboard members whose sign-in address is an app
  account. When the event is published each of them gets a push
  (`share_kit`, "<event> is live — share it"), once per event, and the app
  opens GET /social/share-kit/{event_id}.
- Attendees: GET /events/{event_id}/share, public, each platform with its
  own tracking link (utm_medium=share), so what they spread is counted too.
Instagram's text says "link in bio": its captions do not open links.
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

from sqlalchemy import func, select

from app.api.closeout.models import EventStatus
from app.api.events.models import Events
from app.api.social import content, links, posters
from app.core import jobs
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

PUSH_JOB = "social.share_kit_push"
POSTERS_JOB = "social.posters"
KIT_PLATFORMS = ("instagram", "facebook", "linkedin", "x", "whatsapp")
SIZE_FOR = {"instagram": "ig_portrait", "facebook": "facebook", "linkedin": "linkedin", "x": "x", "whatsapp": "ig_square"}
SHOWN = (EventStatus.published, EventStatus.live, EventStatus.closed, EventStatus.archived)


async def team_user_ids(session, club_id: int) -> list[int]:
    """The app accounts of the club's team: active members whose sign-in address is one."""
    from app.api.club_access.models import ClubMembers
    from app.api.users.models import UserTypes, Users

    rows = await session.scalars(
        select(Users.id).join(ClubMembers, func.lower(ClubMembers.email) == func.lower(Users.email))
        .where(ClubMembers.club_id == club_id, ClubMembers.status == "active",
               Users.user_type == UserTypes.app_user, Users.is_deleted == False)  # noqa: E712
    )
    return sorted(set(rows))


async def team_club_ids(session, user) -> list[int]:
    """The clubs whose team this app user is on (by their sign-in address)."""
    from app.api.club_access.models import ClubMembers

    if not getattr(user, "email", None):
        return []
    rows = await session.scalars(
        select(ClubMembers.club_id).where(func.lower(ClubMembers.email) == user.email.lower(), ClubMembers.status == "active")
    )
    return sorted(set(rows))


def _whatsapp_text(f: content.Facts) -> str:
    return "*{event_name}*\n📅 {date}, {time}\n📍 {venue}\n🎟️ {fee}\n\nRegister: {link}"


async def kit(session, event) -> dict:
    """Each platform's text (filled), poster and link."""
    f = await content.facts(session, event, None)
    made = await posters.ensure(session, event, sorted(set(SIZE_FOR.values())))
    out = []
    for key in KIT_PLATFORMS:
        link = await links.link_for(session, event, key)
        url = links.short_url(link)
        raw = _whatsapp_text(f) if key == "whatsapp" else content.draft(key, f)
        if key == "instagram":
            raw = content.instagram_note(raw)
        out.append({"platform": key, "text": content.render(raw, f.words(link=url)), "image_url": posters.url_of(made.get(SIZE_FOR[key])),
                    "link": url})
    await session.commit()
    local = content._local(event.event_datetime)
    return {"event": {"id": event.id, "name": event.name, "date": local.isoformat() if local else None, "slug": event.slug},
            "platforms": out}


async def may_see(session, user, event) -> bool:
    """A club user who hosts or co-hosts it (or an admin), or an app user on the team of one of them."""
    from app.api.cohosts import access as cohosts
    from app.api.events.access import is_admin_user

    if is_admin_user(user):
        return True
    kind = getattr(user.user_type, "value", user.user_type)
    if kind == "club":
        return await cohosts.viewer_role(session, event, user) is not None
    clubs = set(await team_club_ids(session, user))
    if not clubs:
        return False
    return event.club_id in clubs or any(c in clubs for c in await cohosts.cohost_club_ids(session, event.id))


async def kit_for(session, user, event_id: int) -> dict:
    event = await session.get(Events, event_id)
    if event is None or event.is_deleted or not await may_see(session, user, event):
        raise CustomHTTPException(404, message="Event not found")
    return await kit(session, event)


async def recent_for(session, user, limit: int = 20) -> list[dict]:
    """GET /social/share-kits: the recent published events of the clubs the user is on the team of."""
    from app.api.cohosts import access as cohosts
    from app.api.events.access import is_admin_user

    kind = getattr(user.user_type, "value", user.user_type)
    if kind == "club" and getattr(user, "club", None) is not None:
        where = cohosts.hosted_or_cohosted(user.club.id)
    elif is_admin_user(user):
        return []
    else:
        clubs = await team_club_ids(session, user)
        if not clubs:
            return []
        from sqlalchemy import or_

        where = or_(*(cohosts.hosted_or_cohosted(c) for c in clubs))
    rows = list(await session.scalars(
        select(Events).where(where, Events.is_deleted == False, Events.lifecycle.in_(SHOWN))  # noqa: E712
        .order_by(Events.event_datetime.desc()).limit(limit)
    ))
    out = []
    for e in rows:
        local = content._local(e.event_datetime)
        out.append({"id": e.id, "name": e.name, "date": local.isoformat() if local else None, "slug": e.slug,
                    "image_url": await posters.share_image_url(session, e, or_poster=True)})
    return out


# ─── The attendees' share row ────────────────────────────────────────────

ROW = ("x", "linkedin", "facebook", "whatsapp", "copy")


def intent(platform: str, text: str, url: str) -> Optional[str]:
    if platform == "x":
        return f"https://x.com/intent/post?text={quote(text)}&url={quote(url)}"
    if platform == "linkedin":
        return f"https://www.linkedin.com/sharing/share-offsite/?url={quote(url)}"
    if platform == "facebook":
        return f"https://www.facebook.com/sharer/sharer.php?u={quote(url)}"
    if platform == "whatsapp":
        return f"https://wa.me/?text={quote(text + ' ' + url)}"
    return None


async def share_row(session, event_id: int) -> list[dict]:
    """The public share row: the club's words and a tracking link of its own for each."""
    event = await session.get(Events, event_id)
    if event is None or event.is_deleted or event.lifecycle not in (EventStatus.published, EventStatus.live):
        raise CustomHTTPException(404, message="Event not found")
    f = await content.facts(session, event, None)
    out = []
    for key in ROW:
        link = await links.link_for(session, event, key, links.SHARE)
        url = links.short_url(link)
        text = content.render("{event_name} — {date}, {time} · {venue}", f.words(link=url))
        out.append({"platform": key, "text": text, "url": url, "intent_url": intent(key, text, url)})
    await session.commit()
    return out


# ─── Jobs ─────────────────────────────────────────────────────────────────


@jobs.handler(PUSH_JOB, max_attempts=4, timeout=300.0, priority=jobs.HIGH)
async def _push_job(p: dict) -> None:
    """Each team member on the app, once per event: "<event> is live — share it"."""
    from app.api.notifications import service as inbox
    from app.api.notifications.models import Notifications
    from app.core.notifications import fanout
    from app.core.notifications.service import send_notification_to_user

    async with jobs.scope() as session:
        event = await session.get(Events, int(p["event_id"]))
        if event is None or event.is_deleted or event.lifecycle not in (EventStatus.published, EventStatus.live):
            return
        title = f"{event.name} is live — share it"[:200]
        body = "Your text and posters for each platform are ready to post."
        for user_id in await team_user_ids(session, event.club_id):
            told = await session.scalar(select(Notifications.id).where(
                Notifications.user_id == user_id, Notifications.type == "share_kit", Notifications.event_id == event.id).limit(1))
            if told is not None:
                continue
            line = await inbox.create_notification(session, user_id=user_id, title=title, description=body, type="share_kit",
                                                   event_id=event.id, from_club_id=event.club_id, data={"event_id": event.id})
            try:
                await send_notification_to_user(session, user_id, title, body,
                                                data=fanout.with_line({"type": "share_kit", "event_id": str(event.id)}, line.id))
            except Exception:  # noqa: BLE001 - the inbox line is there; a push that fails is not retried twice
                logger.info("share kit push to user %s failed", user_id)


@jobs.handler(POSTERS_JOB, max_attempts=3, timeout=300.0, concurrency=1, priority=jobs.LOW)
async def _posters_job(p: dict) -> None:
    """Every poster size of a published event, made ahead (the link preview's among them)."""
    async with jobs.scope() as session:
        event = await session.get(Events, int(p["event_id"]))
        if event is None or event.is_deleted:
            return
        await posters.ensure(session, event)
        await session.commit()
