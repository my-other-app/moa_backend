"""What the speakers directory sends: the push to a speaker's followers when
an event they speak at is published, and the email inviting a speaker to
claim their profile.

`speaker_event`: once per event and person. Whenever an event is saved or
published, a job (`speakers.tell_followers`, one waiting per event) looks at
it: if it is public (published or live, listed, not over), everyone who
follows one of its speakers' active profiles and has not had this event's
`speaker_event` line gets one inbox line and one push. An event saved again,
or a second followed speaker added later, tells only the people not told yet
(the line is the record). The push data is `{type: speaker_event, event_id,
slug}`: the app opens the event. The followed clubs switch
(notification_settings.clubs) turns these off too.
"""
from __future__ import annotations

import asyncio
import html as html_mod
import logging

from sqlalchemy import and_, select

from app.api.events.models import Events, EventSpeakers, ends_at_sql
from app.api.speakers.models import ACTIVE, SpeakerFollows, SpeakerProfiles
from app.core import jobs

logger = logging.getLogger(__name__)

TELL_JOB = "speakers.tell_followers"
PUSH_JOB = "speakers.push"
CLAIM_EMAIL_JOB = "speakers.claim_email"
TYPE = "speaker_event"


async def queue_event(session, event_id: int) -> None:
    """Look at this event for its speakers' followers, after the caller's
    work (written and committed here). Cheap when nobody follows anyone on it."""
    await jobs.enqueue(session, TELL_JOB, {"event_id": int(event_id)}, dedupe_key=f"speakers:tell:{int(event_id)}")
    await session.commit()


def _audience(event_id: int):
    from app.core.notifications.topics import settings_off

    return (
        select(SpeakerFollows.user_id)
        .join(SpeakerProfiles, SpeakerProfiles.id == SpeakerFollows.profile_id)
        .join(EventSpeakers, EventSpeakers.speaker_profile_id == SpeakerProfiles.id)
        .where(
            EventSpeakers.event_id == event_id, EventSpeakers.is_deleted == False,  # noqa: E712
            SpeakerProfiles.status == ACTIVE, ~settings_off("clubs", SpeakerFollows.user_id),
        )
        .distinct()
    )


async def _public_event(session, event_id: int):
    from app.api.clubs.models import Clubs
    from app.api.discover.service import SHOWN, _club_visible

    from app.api.speakers.service import now

    return (await session.execute(
        select(Events, Clubs.name)
        .join(Clubs, Clubs.id == Events.club_id)
        .where(
            Events.id == event_id, Events.is_deleted == False, Events.lifecycle.in_(SHOWN),  # noqa: E712
            ends_at_sql() > now(), and_(*_club_visible()),
        )
    )).first()


@jobs.handler(TELL_JOB, max_attempts=5, timeout=300.0, concurrency=2, priority=jobs.NORMAL)
async def _tell_job(p: dict) -> None:
    from app.core.notifications import fanout

    event_id = int(p["event_id"])
    async with jobs.scope() as s:
        found = await _public_event(s, event_id)
        if found is None:
            await s.commit()
            return
        event, club_name = found
        followed: list[str] = []
        for name in (await s.scalars(
            select(SpeakerProfiles.name)
            .join(EventSpeakers, EventSpeakers.speaker_profile_id == SpeakerProfiles.id)
            .where(
                EventSpeakers.event_id == event_id, EventSpeakers.is_deleted == False, SpeakerProfiles.status == ACTIVE,  # noqa: E712
                select(SpeakerFollows.id).where(SpeakerFollows.profile_id == SpeakerProfiles.id).exists(),
            )
            .order_by(EventSpeakers.display_order, EventSpeakers.id)
        )).all():
            if name not in followed:
                followed.append(name)
        if not followed:
            await s.commit()
            return
        who = followed[0] if len(followed) == 1 else f"{followed[0]} and others you follow"
        title = f"🎤 {who} {'is' if len(followed) == 1 else 'are'} speaking"
        body = f"{event.name} · {club_name}"
        data = {"event_name": event.name, "speakers": followed[:5], "slug": event.slug}
        key = fanout.line_key(TYPE, event_id)
        reached = await fanout.insert_for_audience(
            s, _audience(event_id), title=title, description=body, type=TYPE, data=data,
            from_club_id=event.club_id, event_id=event_id, once=True, line_key=key,
        )
        if reached:
            # The push to the people this run told, and nobody told before.
            await jobs.enqueue(s, PUSH_JOB, {
                "user_ids": reached, "title": title, "body": body,
                "data": fanout.with_line({"type": TYPE, "event_id": str(event_id), "slug": event.slug}, key),
            })
        await s.commit()
    logger.info("Told %d speaker followers about event %s", len(reached), event_id)


@jobs.handler(PUSH_JOB, max_attempts=4, timeout=600.0, concurrency=2, priority=jobs.NORMAL)
async def _push_job(p: dict) -> None:
    from app.core.notifications.service import send_notification_to_users

    user_ids = [int(u) for u in p.get("user_ids") or []]
    if not user_ids:
        return
    async with jobs.scope() as s:
        await send_notification_to_users(session=s, user_ids=user_ids, title=p["title"], body=p["body"], data=p.get("data") or {})


# ─── The claim invite ────────────────────────────────────────────────────


@jobs.handler(CLAIM_EMAIL_JOB, max_attempts=4, timeout=60.0, priority=jobs.HIGH)
async def _claim_email_job(p: dict) -> None:
    await asyncio.to_thread(send_claim_email, p)


def send_claim_email(p: dict) -> None:
    from app.core.email import email as email_mod

    name = p.get("name") or "there"
    link, days = p["link"], int(p.get("days") or 14)
    talks = int(p.get("talks") or 0)
    spoke = f"You've spoken at {talks} event{'s' if talks != 1 else ''} on MyOtherApp. " if talks else ""
    text = (
        f"Hi {name},\n\n{spoke}Clubs list their speakers on MyOtherApp, and your speaker profile is ready for you. "
        "Claim it to keep your photo, bio and links up to date yourself, and to have one page of all your talks.\n\n"
        f"Claim your profile: {link}\n\nThe link works for {days} days. Sign in with the MyOtherApp account you want "
        "to keep it under. If this isn't you, ignore this email.\n"
    )
    safe = html_mod.escape
    html = (
        f"<p>Hi {safe(name)},</p><p>{safe(spoke)}Clubs list their speakers on MyOtherApp, and your speaker profile is ready "
        "for you. Claim it to keep your photo, bio and links up to date yourself, and to have one page of all your talks.</p>"
        f"<p><a href=\"{safe(link)}\">Claim your speaker profile</a></p>"
        f"<p>The link works for {days} days. Sign in with the MyOtherApp account you want to keep it under. "
        "If this isn't you, ignore this email.</p>"
    )
    email_mod.send_email(recipients=[p["to"]], subject="Your speaker profile on MyOtherApp", text=text, html=html,
                         idempotency_key=p.get("idempotency_key"))
