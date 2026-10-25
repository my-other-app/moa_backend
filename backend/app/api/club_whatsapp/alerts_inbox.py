"""Telling the club's inbox people that a WhatsApp message is waiting.

Only the people who can read the inbox -- the club's owner and the members
it gave the WhatsApp permission -- hear of it, and never what a message
says: a contact's display name and a count, nothing more. A conversation
the club blocked tells nobody anything.

- The bell (club_alerts kind `whatsapp_message`): one notice per
  conversation while it has unread messages from them -- "Ananya: 3 unread
  WhatsApp messages" -- raised (or brought to the top) as each arrives, and
  gone once the conversation is read or answered. The bell's list hides it
  from anyone without the inbox (club_alerts.service.listing).
- A mail to each of those people when a message has waited unread 15
  minutes (at the sweep's first pass after: every 15 minutes), at most one
  an hour each, and only when something arrived since their last one. Each
  person may turn their mails off (their inbox notifications).
- Waiting too long: a conversation whose oldest unanswered message has
  waited the club's `alert_after_hours` (4 by default, 1 to 48) raises a
  bell notice (`whatsapp_waiting`) and a mail, once for that wait -- an
  answer ends it, and the next message starts another.

The mails are the platform's own to the club (alerts._send, a durable job),
not counted against the club's message quotas. The sweep runs these from
club_whatsapp's own sweep (sweep.py), one club at a time, each in its own
transaction.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.club_whatsapp.inbox_models import DEFAULT_ALERT_HOURS, ClubWhatsAppInboxSettings, ClubWhatsAppPersonPrefs
from app.api.club_whatsapp.models import IN, ClubWhatsAppConversations, ClubWhatsAppMessages

logger = logging.getLogger(__name__)

MESSAGE_KIND = "whatsapp_message"
WAITING_KIND = "whatsapp_waiting"
#: The bell's notices only the inbox's people see.
PRIVATE_KINDS = frozenset({MESSAGE_KIND, WAITING_KIND})
#: A message unread this long is mailed about; at most one such mail an hour a person.
EMAIL_AFTER = timedelta(minutes=15)
EMAIL_EVERY = timedelta(hours=1)
#: Conversations a mail names, at most (then "and n more").
NAMED = 8
#: Conversations one sweep looks at.
BATCH = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def display_name(contact_name, profile_name, username, wa_id) -> str:
    """Who they are to the club, in a word: no message, ever."""
    if contact_name:
        return str(contact_name)[:80]
    if profile_name:
        return str(profile_name)[:80]
    if username:
        return f"@{username}"[:80]
    if wa_id:
        return f"+{wa_id}"
    return "Someone"


def hidden_kinds(request) -> frozenset:
    """The bell's kinds this viewer may not see: the inbox's, without the inbox."""
    from app.api.club_whatsapp import access

    return frozenset() if access.can_inbox(request) else PRIVATE_KINDS


def _message_key(conv_id: int) -> str:
    return f"{MESSAGE_KIND}:{conv_id}"


# ─── One conversation ────────────────────────────────────────────────────


async def sync_conversation(session, club_id: int, conv_id: int, *, arrived: bool = False) -> None:
    """Bring the conversation's alert state in line with it: `unread_since`
    (the oldest unread message's arrival) and its bell notice -- raised, or
    brought to the top when a message just `arrived`, while something is
    unread; gone once nothing is (or the club blocked them). A notice that
    the wait was too long is marked read once they are answered. Never
    raises: a notice that can't be written must not undo the message."""
    from app.api.club_alerts.models import ClubAlerts
    from app.api.club_whatsapp import inbox

    C, M, A = ClubWhatsAppConversations, ClubWhatsAppMessages, ClubAlerts
    try:
        async with session.begin_nested():
            row = (await session.execute(
                select(C.unread_count, C.blocked_at, C.awaiting_since, C.contact_name, C.profile_name, C.username, C.wa_id)
                .where(C.id == conv_id, C.club_id == club_id)
            )).first()
            if row is None:
                return
            unread, blocked_at, awaiting, *who = row
            if unread > 0 and blocked_at is None:
                latest = (
                    select(M.created_at).where(M.conversation_id == conv_id, M.direction == IN, M.type.not_in(inbox.NOT_COUNTED))
                    .order_by(M.id.desc()).limit(unread).subquery()
                )
                await session.execute(
                    update(C).where(C.id == conv_id)
                    .values(unread_since=select(func.min(latest.c.created_at)).scalar_subquery())
                    .execution_options(synchronize_session=False)
                )
                name = display_name(*who)
                title = f"{name}: {unread} unread WhatsApp message{'s' if unread != 1 else ''}"
                now = _now()
                values = dict(club_id=club_id, kind=MESSAGE_KIND, title=title[:200], body="", link=f"/whatsapp?c={conv_id}",
                              data={"conversation_id": conv_id, "count": unread}, dedupe_key=_message_key(conv_id), created_at=now)
                changes = {"title": title[:200], "data": values["data"]}
                if arrived:
                    changes.update(created_at=now, read_at=None)
                await session.execute(
                    pg_insert(A).values(**values).on_conflict_do_update(constraint="uq_club_alerts_once", set_=changes)
                )
            else:
                await session.execute(
                    update(C).where(C.id == conv_id, C.unread_since.isnot(None)).values(unread_since=None)
                    .execution_options(synchronize_session=False)
                )
                await session.execute(delete(A).where(A.club_id == club_id, A.dedupe_key == _message_key(conv_id)))
            if awaiting is None:
                # Answered: the next message starts a new wait, told of again.
                await session.execute(
                    update(C).where(C.id == conv_id, C.waiting_alerted_since.isnot(None)).values(waiting_alerted_since=None)
                    .execution_options(synchronize_session=False)
                )
            if awaiting is None or blocked_at is not None:
                await session.execute(
                    update(A).where(A.club_id == club_id, A.kind == WAITING_KIND, A.read_at.is_(None),
                                    A.dedupe_key.like(f"{WAITING_KIND}:{conv_id}:%"))
                    .values(read_at=_now()).execution_options(synchronize_session=False)
                )
    except Exception as exc:  # noqa: BLE001 - never the message
        logger.warning("club %s: the WhatsApp alert of conversation %s could not be kept (%s)", club_id, conv_id, type(exc).__name__)


async def unread_total(session, club_id: int) -> int:
    """What waits unread, for the nav's badge: blocked conversations aside."""
    C = ClubWhatsAppConversations
    return int(await session.scalar(
        select(func.coalesce(func.sum(C.unread_count), 0)).where(C.club_id == club_id, C.blocked_at.is_(None))
    ) or 0)


# ─── Who hears ───────────────────────────────────────────────────────────


@dataclass
class Person:
    #: None: the club's own account (its owner).
    member_id: Optional[int]
    email: Optional[str]
    name: str


async def inbox_people(session, club_id: int) -> list[Person]:
    """The club's owner and the members (active) with the WhatsApp permission."""
    from app.api.club_access.models import ClubMembers
    from app.api.clubs.models import Clubs
    from app.api.users.models import Users

    owner = (await session.execute(
        select(Users.email, Clubs.name).join(Clubs, Clubs.user_id == Users.id).where(Clubs.id == club_id)
    )).first()
    out = [Person(member_id=None, email=owner[0] if owner else None, name=(owner[1] if owner else None) or "The club")]
    members = (await session.execute(
        select(ClubMembers.id, ClubMembers.email, ClubMembers.full_name)
        .where(ClubMembers.club_id == club_id, ClubMembers.status == "active", ClubMembers.whatsapp == True)  # noqa: E712
        .order_by(ClubMembers.id)
    )).all()
    out += [Person(member_id=m[0], email=m[1], name=(m[2] or m[1] or "A team member")[:120]) for m in members]
    return out


async def prefs_of(session, club_id: int) -> dict[Optional[int], ClubWhatsAppPersonPrefs]:
    P = ClubWhatsAppPersonPrefs
    rows = (await session.scalars(select(P).where(P.club_id == club_id))).all()
    return {r.member_id: r for r in rows}


async def pref_row(session, club_id: int, member_id: Optional[int]) -> ClubWhatsAppPersonPrefs:
    """This person's row, made with the defaults (everything on) the first time."""
    P = ClubWhatsAppPersonPrefs
    where = [P.club_id == club_id, P.member_id.is_(None) if member_id is None else P.member_id == member_id]
    row = await session.scalar(select(P).where(*where))
    if row is None:
        await session.execute(pg_insert(P.__table__).values(club_id=club_id, member_id=member_id).on_conflict_do_nothing())
        row = await session.scalar(select(P).where(*where).execution_options(populate_existing=True))
    return row


async def alert_hours(session, club_id: int) -> int:
    row = await session.get(ClubWhatsAppInboxSettings, club_id)
    return int(row.alert_after_hours) if row is not None else DEFAULT_ALERT_HOURS


def _mail(session, to: str, subject: str, paragraphs: list[str], footer: Optional[str] = None) -> None:
    from app.api.club_whatsapp import alerts
    from app.core.after_commit import run_after_commit

    run_after_commit(session, alerts._send, to, subject, paragraphs, ("Open the WhatsApp inbox", alerts.dashboard_url("/whatsapp")),
                     footer)


def _names(rows: list[tuple[str, int]]) -> str:
    named = [f"{name} ({n})" if n else name for name, n in rows[:NAMED]]
    more = len(rows) - NAMED
    return ", ".join(named) + (f", and {more} more" if more > 0 else "")


SWITCH_OFF = "You can turn these mails off in WhatsApp -> Settings -> Notifications."


# ─── The sweep ───────────────────────────────────────────────────────────


async def mail_unread(session, now: Optional[datetime] = None) -> int:
    """Mail each inbox person whose club has a message unread for EMAIL_AFTER,
    at most once an hour, and only when one arrived since their last mail.
    How many mails were queued."""
    from app.api.clubs.models import Clubs

    now = now or _now()
    C = ClubWhatsAppConversations
    rows = (await session.execute(
        select(C.club_id, C.id, C.unread_count, C.unread_since, C.contact_name, C.profile_name, C.username, C.wa_id)
        .where(C.unread_since.isnot(None), C.unread_since <= now - EMAIL_AFTER, C.unread_count > 0, C.blocked_at.is_(None))
        .order_by(C.club_id, C.unread_since).limit(BATCH)
    )).all()
    clubs: dict[int, list] = {}
    for r in rows:
        clubs.setdefault(r[0], []).append(r)
    sent = 0
    for club_id, convs in clubs.items():
        try:
            club_name = await session.scalar(select(Clubs.name).where(Clubs.id == club_id)) or "your club"
            prefs = await prefs_of(session, club_id)
            listed = _names([(display_name(*c[4:8]), c[2]) for c in convs])
            for person in await inbox_people(session, club_id):
                pref = prefs.get(person.member_id)
                if not person.email or (pref is not None and not pref.email_messages):
                    continue
                last = _aware(pref.messages_emailed_at) if pref is not None else None
                if last is not None and (last > now - EMAIL_EVERY or not any(_aware(c[3]) > last for c in convs)):
                    continue
                _mail(session, person.email, f"WhatsApp messages are waiting for {club_name}", [
                    f"{len(convs)} WhatsApp conversation{'s have' if len(convs) != 1 else ' has'} messages unread for "
                    f"15 minutes or more: {listed}.",
                    "Open the inbox to read and answer them.",
                ], f"You get this at most once an hour. {SWITCH_OFF}")
                row = pref if pref is not None else await pref_row(session, club_id, person.member_id)
                row.messages_emailed_at = now
                sent += 1
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("whatsapp alerts: club %s's unread mail failed (%s)", club_id, type(exc).__name__)
            await session.rollback()
    return sent


async def waiting_too_long(session, now: Optional[datetime] = None) -> int:
    """Conversations whose oldest unanswered message has waited the club's
    alert_after_hours: a bell notice and a mail, once a wait. How many."""
    from app.api.club_alerts import notify
    from app.api.clubs.models import Clubs

    now = now or _now()
    C, S = ClubWhatsAppConversations, ClubWhatsAppInboxSettings
    hours = func.coalesce(S.alert_after_hours, DEFAULT_ALERT_HOURS)
    rows = (await session.execute(
        select(C.club_id, C.id, C.awaiting_since, hours, C.contact_name, C.profile_name, C.username, C.wa_id)
        .outerjoin(S, S.club_id == C.club_id)
        .where(C.awaiting_since.isnot(None), C.blocked_at.is_(None),
               C.awaiting_since <= literal(now, C.awaiting_since.type) - func.make_interval(0, 0, 0, 0, hours),
               or_(C.waiting_alerted_since.is_(None), C.waiting_alerted_since != C.awaiting_since))
        .order_by(C.club_id, C.awaiting_since).limit(BATCH)
    )).all()
    clubs: dict[int, list] = {}
    for r in rows:
        clubs.setdefault(r[0], []).append(r)
    told = 0
    for club_id, convs in clubs.items():
        try:
            club_name = await session.scalar(select(Clubs.name).where(Clubs.id == club_id)) or "your club"
            fresh = []
            for _club, conv_id, since, n_hours, *who in convs:
                # Only if the wait is still the one found (an answer meanwhile ends it).
                moved = await session.scalar(
                    update(C).where(C.id == conv_id, C.awaiting_since == since).values(waiting_alerted_since=since)
                    .returning(C.id).execution_options(synchronize_session=False)
                )
                if moved is None:
                    continue
                name = display_name(*who)
                await notify(
                    session, club_id, WAITING_KIND, title=f"{name} has waited over {n_hours} hour{'s' if n_hours != 1 else ''} for an answer"[:200],
                    link=f"/whatsapp?c={conv_id}", data={"conversation_id": conv_id, "hours": int(n_hours)},
                    dedupe_key=f"{WAITING_KIND}:{conv_id}:{int(_aware(since).timestamp())}:{int(now.timestamp())}",
                )
                fresh.append((name, int(n_hours)))
            if fresh:
                prefs = await prefs_of(session, club_id)
                n_hours = fresh[0][1]
                for person in await inbox_people(session, club_id):
                    pref = prefs.get(person.member_id)
                    if not person.email or (pref is not None and not pref.email_messages):
                        continue
                    _mail(session, person.email, f"WhatsApp messages have waited over {n_hours} hours at {club_name}", [
                        f"{_names([(n, 0) for n, _h in fresh])} {'has' if len(fresh) == 1 else 'have'} waited more than "
                        f"{n_hours} hour{'s' if n_hours != 1 else ''} for an answer on WhatsApp.",
                        "Open the inbox to answer them.",
                    ], SWITCH_OFF)
                told += len(fresh)
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("whatsapp alerts: club %s's waiting alert failed (%s)", club_id, type(exc).__name__)
            await session.rollback()
    return told


async def tidy(session) -> int:
    """Message notices left by a conversation that has nothing unread, the
    club blocked, or retention deleted (each sync clears its own; this
    catches what a failed one left). How many were removed."""
    from app.api.club_alerts.models import ClubAlerts

    A, C = ClubAlerts, ClubWhatsAppConversations
    live = select(C.id).where(
        C.club_id == A.club_id, func.concat(MESSAGE_KIND, ":", C.id) == A.dedupe_key, C.unread_count > 0, C.blocked_at.is_(None),
    ).exists()
    gone = await session.execute(delete(A).where(A.kind == MESSAGE_KIND, ~live).returning(A.id))
    n = len(gone.all())
    await session.commit()
    return n


async def run(session, now: Optional[datetime] = None) -> dict:
    """The sweep's part: waiting conversations, unread mails, stale notices."""
    now = now or _now()
    out = {"waiting": 0, "mailed": 0, "tidied": 0}
    try:
        out["waiting"] = await waiting_too_long(session, now)
        out["mailed"] = await mail_unread(session, now)
        out["tidied"] = await tidy(session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("whatsapp alerts failed (%s)", type(exc).__name__)
        await session.rollback()
    # Empty when nothing happened, so the sweep's log line stays quiet.
    return out if any(out.values()) else {}


# ─── Assigned ────────────────────────────────────────────────────────────


async def tell_assignee(session, club_id: int, member_id: Optional[int], *, contact: str, by: str) -> bool:
    """Mail the person a conversation was assigned to, unless they turned it
    off: who assigned it and the contact's name -- nothing it says."""
    from app.api.clubs.models import Clubs

    person = next((p for p in await inbox_people(session, club_id) if p.member_id == member_id), None)
    if person is None or not person.email:
        return False
    pref = (await prefs_of(session, club_id)).get(member_id)
    if pref is not None and not pref.email_assigned:
        return False
    club_name = await session.scalar(select(Clubs.name).where(Clubs.id == club_id)) or "your club"
    _mail(session, person.email, f"A WhatsApp conversation was assigned to you at {club_name}", [
        f"{by} assigned you the WhatsApp conversation with {contact}.",
        "Open the inbox to answer it.",
    ], "You can turn these mails off in WhatsApp -> Settings -> Notifications.")
    return True
