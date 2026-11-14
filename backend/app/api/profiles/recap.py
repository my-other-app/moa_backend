"""The morning after: "You met 12 people at DevFest", a push and an email.

When an encounter at an event is first recorded, the event's recap job is
written for 09:00 India time the morning after its networking window
closes (the first 09:00 after the end plus an hour); the dedupe key holds
the time, so a moved event gets a job for its new time and the old one
finds itself early and stands down.

The event's job puts a row in network_recaps for everyone with at least one
encounter at it (INSERT ... ON CONFLICT DO NOTHING) and, for each row it
made, a send job, in the same transaction: a second run makes no rows and
so sends nothing. A save synced after the recap ran (an offline scan, up to
72 hours late) writes the job again; it runs at once and reaches only the
people who had no recap yet.

The send job claims its row (`sent_at`) before anything goes, and writes
the email's own job in that transaction, then sends the push: one recap per
person per event however often either job runs. The push always (type
`network_recap`, data {type, event_id}); the email unless the person turned
`recap_emails` off, with a link that turns it off (and List-Unsubscribe
headers, one-click). Both name up to six of the people met, with their
headlines, and open the Network filtered to the event
(myotherapp.in/network?event=<id>, which the app opens).
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.events.models import Events
from app.api.profiles import access, service
from app.api.profiles.models import NetworkEncounters, NetworkPeople, NetworkRecaps
from app.api.users.models import Users
from app.config import settings
from app.core import jobs, public_urls
from app.core.mail import links as signed

logger = logging.getLogger(__name__)

RECAP_JOB = "network.recap"
SEND_JOB = "network.recap_send"
EMAIL_JOB = "network.recap_email"
PUSH_TYPE = "network_recap"
IST = timezone(timedelta(hours=5, minutes=30))
RECAP_HOUR = 9
NAMES = 6
BATCH = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def due_at(event: Events) -> Optional[datetime]:
    """09:00 India time, the first after the event's networking window closes."""
    from app.api.profiles.network import window_of

    window = window_of(event)
    if window is None:
        return None
    closes = window[1].astimezone(IST)
    day = closes.date() if closes.time() < time(RECAP_HOUR) else closes.date() + timedelta(days=1)
    return datetime.combine(day, time(RECAP_HOUR), IST)


async def schedule(session, event: Events) -> None:
    due = due_at(event)
    if due is None:
        return
    await jobs.enqueue(
        session, RECAP_JOB, {"event_id": event.id}, run_at=due.astimezone(timezone.utc),
        dedupe_key=f"{RECAP_JOB}:{event.id}:{int(due.timestamp())}",
    )


async def run_event(session, event_id: int, now: Optional[datetime] = None) -> int:
    """Everyone who met someone at the event and has no recap of it yet gets
    one (a send job each). Returns how many."""
    now = now or _now()
    event = await session.scalar(select(Events).where(Events.id == event_id, Events.is_deleted == False))  # noqa: E712
    if event is None:
        await session.commit()
        return 0
    due = due_at(event)
    if due is None:
        await session.commit()
        return 0
    if due > now + timedelta(minutes=1):
        # The event moved later: its new time has a job of its own.
        await schedule(session, event)
        await session.commit()
        return 0
    NP, E = NetworkPeople, NetworkEncounters
    owners = sorted(set((await session.scalars(
        select(NP.owner_id).join(E, E.network_person_id == NP.id).where(E.event_id == event.id).where(NP.owner_id.in_(
            select(Users.id).where(*access.live_person_where())
        ))
    )).all()))
    made = 0
    for start in range(0, len(owners), BATCH):
        chunk = owners[start:start + BATCH]
        got = list((await session.scalars(
            pg_insert(NetworkRecaps).values([{"person_id": p, "event_id": event.id} for p in chunk])
            .on_conflict_do_nothing(index_elements=["person_id", "event_id"]).returning(NetworkRecaps.person_id)
        )).all())
        if got:
            await jobs.enqueue_many(session, SEND_JOB, [
                ({"person_id": p, "event_id": event.id}, f"{SEND_JOB}:{event.id}:{p}") for p in got
            ])
        await session.commit()
        made += len(got)
    return made


async def met_at_event(session, person_id: int, event_id: int) -> list[dict]:
    """The people this person met at the event that they may still see, first met first."""
    NP, E = NetworkPeople, NetworkEncounters
    rows = (await session.execute(
        select(NP.person_id).join(E, E.network_person_id == NP.id)
        .where(NP.owner_id == person_id, E.event_id == event_id, *access.shown_person_where(NP.person_id, person_id))
        .order_by(E.met_at, NP.id)
    )).all()
    ids = list(dict.fromkeys(r[0] for r in rows))
    found = await service.people(session, ids)
    rels = await access.relations(session, person_id, ids)
    return [service.brief(found[i], rels[i]) for i in ids if i in found]


def wording(count: int, event_name: str) -> str:
    return f"You met {count} {'person' if count == 1 else 'people'} at {event_name}"


def names_line(people: list[dict]) -> str:
    names = [p["full_name"] for p in people[:3]]
    rest = len(people) - len(names)
    if rest > 0:
        return ", ".join(names) + f" and {rest} other{'s' if rest != 1 else ''}"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def network_link(event_id: int) -> str:
    return public_urls.site_url(f"/network?event={event_id}")


async def send(session, person_id: int, event_id: int) -> bool:
    """One person's recap, once: claimed first, the email's job written with
    the claim, then the push. True when it was theirs to send."""
    claimed = await session.scalar(
        update(NetworkRecaps).where(
            NetworkRecaps.person_id == person_id, NetworkRecaps.event_id == event_id, NetworkRecaps.sent_at.is_(None),
        ).values(sent_at=_now()).returning(NetworkRecaps.person_id)
    )
    if claimed is None:
        await session.commit()
        return False
    person = await access.load_person(session, user_id=person_id)
    event = await session.scalar(select(Events).where(Events.id == event_id))
    met = await met_at_event(session, person_id, event_id) if person is not None and event is not None else []
    if person is None or event is None or not met:
        await session.commit()
        return False
    if access.settings_of(person.profile)["recap_emails"] and person.email:
        await jobs.enqueue(session, EMAIL_JOB, {"person_id": person_id, "event_id": event_id}, dedupe_key=f"{EMAIL_JOB}:{event_id}:{person_id}")
    await session.commit()
    from app.core.notifications.service import send_notification_to_user

    try:
        await send_notification_to_user(
            session, person_id, wording(len(met), event.name), f"{names_line(met)}. See who you met.",
            data={"type": PUSH_TYPE, "event_id": str(event_id), "url": network_link(event_id)},
        )
    except Exception:  # noqa: BLE001 - claimed: a push that failed is not sent twice
        logger.warning("network recap %s/%s: the push could not be sent", event_id, person_id, exc_info=True)
    return True


# ─── The email ──────────────────────────────────────────────────────────


def unsubscribe_token(user_id: int) -> str:
    return signed.sign(f"network-recap-unsubscribe:{int(user_id)}")


def unsubscribe_ok(user_id: int, token: str) -> bool:
    return signed.verify(f"network-recap-unsubscribe:{int(user_id)}", token or "")


def unsubscribe_url(user_id: int) -> str:
    query = urlencode({"u": int(user_id), "t": unsubscribe_token(user_id)})
    return f"{settings.PUBLIC_API_URL.rstrip('/')}/api/v1/network/recap-emails/unsubscribe?{query}"


def compose(person_name: str, event: Events, met: list[dict], unsubscribe: str) -> tuple[str, str, str]:
    """(subject, text, html): plain and branded (app.core.email.plain's look)."""
    from app.core.email import plain

    esc = html.escape
    subject = wording(len(met), event.name)
    link = network_link(event.id)
    shown = met[:NAMES]
    more = len(met) - len(shown)
    lines_text = [f"Hi {person_name}," if person_name else "Hi,", f"{subject}. Here's who:"]
    lines_text += [f"- {p['full_name']}" + (f", {p['headline']}" if p.get("headline") else "") for p in shown]
    if more > 0:
        lines_text.append(f"...and {more} more.")
    lines_text += [f"See them all, add notes and save people back: {link}", "", f"Stop these emails: {unsubscribe}"]
    text_body = "\n".join(lines_text) + "\n"

    people_html = "".join(
        f'<li style="margin:0 0 8px"><strong>{esc(p["full_name"])}</strong>'
        + (f'<br><span style="color:#6B7280">{esc(p["headline"])}</span>' if p.get("headline") else "")
        + "</li>"
        for p in shown
    )
    if more > 0:
        people_html += f'<li style="margin:0 0 8px;color:#6B7280">and {more} more</li>'
    html_body = (
        plain._WRAP
        + f'<p style="font-size:20px;font-weight:700;margin:0 0 24px">{esc(plain.brand())}</p>'
        + f"<p>{esc('Hi ' + person_name + ',' if person_name else 'Hi,')}</p>"
        + f"<p>{esc(subject)}. Here's who:</p>"
        + f'<ul style="padding-left:18px">{people_html}</ul>'
        + f'<p style="margin:24px 0"><a href="{esc(link, quote=True)}" style="{plain._BUTTON}">See who you met</a></p>'
        + '<p style="color:#6B7280;font-size:13px;margin-top:32px">You get this the morning after an event where you saved people. '
        + f'<a href="{esc(unsubscribe, quote=True)}" style="color:#6B7280">Stop these emails</a>.</p>'
        + "</div>"
    )
    return subject, text_body, html_body


async def send_email(session, person_id: int, event_id: int) -> bool:
    from app.core.email import email as email_mod

    person = await access.load_person(session, user_id=person_id)
    event = await session.scalar(select(Events).where(Events.id == event_id))
    if person is None or event is None or not person.email or not access.settings_of(person.profile)["recap_emails"]:
        return False
    met = await met_at_event(session, person_id, event_id)
    if not met:
        return False
    unsubscribe = unsubscribe_url(person_id)
    subject, text_body, html_body = compose(service.full_name_of(person).split(" ")[0], event, met, unsubscribe)
    await asyncio.to_thread(
        email_mod.send_email,
        recipients=[person.email], subject=subject, text=text_body, html=html_body,
        idempotency_key=f"network-recap:{event_id}:{person_id}",
        headers={"List-Unsubscribe": f"<{unsubscribe}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
    )
    return True


async def unsubscribe(session, user_id: int, token: str) -> bool:
    """The email's link: recap emails off. False for a link that is not one."""
    from app.api.users.models import UserProfiles
    from app.api.users.service import _ensure_profile

    if not unsubscribe_ok(user_id, token):
        return False
    if await session.scalar(select(Users.id).where(Users.id == int(user_id))) is None:
        return False
    await _ensure_profile(session, int(user_id))
    await session.execute(
        update(UserProfiles).where(UserProfiles.user_id == int(user_id), UserProfiles.is_deleted == False)  # noqa: E712
        .values(recap_emails=False).execution_options(synchronize_session=False)
    )
    await session.commit()
    return True


# ─── Jobs ───────────────────────────────────────────────────────────────


@jobs.handler(RECAP_JOB, max_attempts=6, timeout=300.0, priority=jobs.NORMAL)
async def _recap_job(p: dict) -> None:
    async with jobs.scope() as s:
        await run_event(s, int(p["event_id"]))


@jobs.handler(SEND_JOB, max_attempts=5, timeout=60.0, priority=jobs.NORMAL)
async def _send_job(p: dict) -> None:
    async with jobs.scope() as s:
        await send(s, int(p["person_id"]), int(p["event_id"]))


@jobs.handler(EMAIL_JOB, max_attempts=6, timeout=60.0, priority=jobs.NORMAL)
async def _email_job(p: dict) -> None:
    async with jobs.scope() as s:
        await send_email(s, int(p["person_id"]), int(p["event_id"]))
