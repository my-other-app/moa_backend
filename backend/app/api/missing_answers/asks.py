"""Asking people for the answers they are missing (contract §2.1, §2.6).

The club picks the questions; the server works out who is askable for them
(blank or invalid, required or not), and sends each of them, on every
channel the club chose that can reach them, a personal link (§2.2):

- push: an app account with a device; type `answers_needed`, data
  {"event_id", "url"};
- email: anyone with an address that has not bounced (guests included),
  through the mail transport, each send kept here with the provider's id;
- whatsapp: the club's own WhatsApp number only, with its approved
  `details_needed` template, to people who opted in to WhatsApp and hold a
  live ticket or an open request, never to anyone who told the club to stop.

One job per person per round (`missing_answers.send`, deduped per ask,
person and round); each channel's message is a row in answer_ask_sends,
claimed (queued) before anything is sent, so a job run again sends nobody
twice. The first round is free of the club's message quota; a reminder
(round 1 or 2) counts like any message: an email reminder is checked and
counted as a `reminder` (app/api/message_quota). A WhatsApp from the club's
own number is never counted (the club pays Meta), and a push costs nothing.

An ask closes by itself at its deadline, when nobody is left to answer, or
when the event ends: worked out on every read (`settle`), so nothing waits
on a sweep. At most two reminders, at least 12 hours after the round
before; the automatic one (`remind_after_hours`) is a job of its own.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import joinedload

from app.api.events.models import Events
from app.api.missing_answers import people as people_mod
from app.api.missing_answers import service as ma
from app.api.missing_answers.models import (
    BY_CLUB,
    CHANNELS,
    CLOSED,
    DEADLINE,
    DONE,
    EMAIL,
    EVENT_ENDED,
    FAILED,
    OPEN,
    PUSH,
    QUEUED,
    SENT,
    SKIPPED,
    WHATSAPP,
    AnswerAskPeople,
    AnswerAsks,
    AnswerAskSends,
)
from app.api.missing_answers.people import Person
from app.api.missing_answers.state import Form
from app.core import jobs
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 20
MAX_REFS = 500
MESSAGE_MAX = 300
REMIND_MIN_HOURS, REMIND_MAX_HOURS = 12, 168
MAX_REMINDERS = 2
REMIND_GAP = timedelta(hours=12)
#: What a WhatsApp "details needed" is: the club's own template (§2.6).
TEMPLATE = "details_needed"
#: What an email reminder counts as against the club's messages.
REMINDER_KIND = "reminder"

SEND_JOB = "missing_answers.send"
REMIND_JOB = "missing_answers.remind"

#: Why WhatsApp is not on offer (contract §2.1).
NO_FEATURE, NOT_CONNECTED, TEMPLATE_NOT_APPROVED = "NO_FEATURE", "NOT_CONNECTED", "TEMPLATE_NOT_APPROVED"

IST = timezone(timedelta(hours=5, minutes=30))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bad(field: str, message: str, code: str = "INVALID_ASK", status: int = 422) -> CustomHTTPException:
    return CustomHTTPException(status, message=message, error_code=code, errors={field: message})


def plural(n: int, word: str = "detail") -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def when_text(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    local = dt.astimezone(IST)
    return local.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


# ─── The club's own WhatsApp (§2.6) ──────────────────────────────────────


async def whatsapp_route(session, club_id: Optional[int]):
    """(sender or None, reason or None): the club's own number with its
    approved details_needed template, or why it can't send it."""
    from app.api.club_whatsapp import crypto, templates
    from app.api.club_whatsapp import service as accounts
    from app.api.club_whatsapp.models import ACTIVE
    from app.core import whatsapp

    if club_id is None or not await accounts.plan_allows(session, club_id):
        return None, NO_FEATURE
    account = await accounts.live_account(session, club_id)
    if account is None or account.status != ACTIVE or accounts.sending_restricted(account) or not crypto.available():
        return None, NOT_CONNECTED
    language = await templates.usable_language(session, account.id, TEMPLATE)
    if language is None:
        return None, TEMPLATE_NOT_APPROVED
    try:
        keys = accounts.keys_of(account)
    except crypto.Unavailable:
        return None, NOT_CONNECTED
    from app.api.club_whatsapp import graph

    sender = whatsapp.Sender(
        phone_number_id=account.phone_number_id, token=keys.token, language=language, app_secret=keys.app_secret,
        account_id=account.id, version=graph.version(),
    )
    return sender, None


async def whatsapp_reachable(session, club_id: int, person: Person) -> Optional[str]:
    """None when a WhatsApp may go to them, else why not."""
    from app.api.club_whatsapp import inbox, optout

    if not person.whatsapp_opt_in:
        return "they did not ask for WhatsApp"
    if not person.live_ticket:
        return "no live ticket"
    wa = inbox.wa_of(person.phone)
    if wa is None or person.wa_bounced_at is not None:
        return "no number WhatsApp reaches"
    if await optout.blocked(session, club_id, wa_id=wa, scope="all"):
        return "they asked the club to stop"
    return None


# ─── Who, and how many each channel reaches ─────────────────────────────


async def _push_users(session, user_ids) -> set[int]:
    """Of these accounts, the app accounts with a live device."""
    import sqlalchemy as sa

    from app.api.users.models import UserDeviceTokens, Users, UserTypes
    from app.core.notifications.service import int_array, reachable

    ids = sorted({int(u) for u in user_ids if u})
    if not ids:
        return set()
    accounts = select(Users.id).where(Users.id == sa.any_(int_array(ids)), Users.user_type != UserTypes.guest)
    rows = (await session.scalars(
        select(UserDeviceTokens.user_id)
        .where(
            UserDeviceTokens.user_id == sa.any_(int_array(ids)), UserDeviceTokens.is_deleted == False,  # noqa: E712
            UserDeviceTokens.user_id.in_(accounts), reachable(UserDeviceTokens.user_id),
        ).distinct()
    )).all()
    return {int(u) for u in rows}


async def _mailable(session, everyone: list[Person]) -> set:
    from app.core.mail import suppression

    found = await suppression.reasons(session, [p.email for p in everyone if p.email])
    out = set()
    for p in everyone:
        if not p.email or p.email_bounced_at is not None or suppression.at_suppressed_domain(p.email):
            continue
        if suppression.normal(p.email) in found:
            continue
        out.add(p.key)
    return out


async def reach(session, event, everyone: list[Person], *, whatsapp_on: bool) -> dict:
    pushed = await _push_users(session, [p.user_id for p in everyone])
    mailed = await _mailable(session, everyone)
    wa = 0
    if whatsapp_on:
        for p in everyone:
            if await whatsapp_reachable(session, event.club_id, p) is None:
                wa += 1
    return {
        PUSH: sum(1 for p in everyone if p.user_id is not None and int(p.user_id) in pushed),
        EMAIL: sum(1 for p in everyone if p.key in mailed),
        WHATSAPP: wa,
    }


def _check_keys(form: Form, keys) -> list[str]:
    picked: list[str] = []
    for k in keys or []:
        k = str(k or "").strip()
        if k and k not in picked:
            picked.append(k)
    if not picked:
        raise _bad("questions", "Pick at least one question to ask.")
    if len(picked) > MAX_QUESTIONS:
        raise _bad("questions", f"Ask at most {MAX_QUESTIONS} questions at once.")
    stray = [k for k in picked if k not in form]
    if stray:
        raise _bad("questions", f"'{stray[0]}' isn't a question on this event's form.")
    return form.in_order(picked)


def _check_refs(refs) -> Optional[list[tuple[str, uuid.UUID]]]:
    if refs is None:
        return None
    if len(refs) > MAX_REFS:
        raise _bad("refs", f"At most {MAX_REFS} people at once.")
    out = []
    for r in refs:
        parsed = people_mod.parse_ref(r)
        if parsed is None:
            raise _bad("refs", "One of those people isn't on this event.")
        out.append(parsed)
    return out


async def targets(session, event, form: Form, keys: list[str], *, include_requests: bool, refs) -> list[Person]:
    """Everyone (or those of `refs`) for whom at least one asked question is askable."""
    everyone = await people_mod.load(session, event, requests=include_requests, refs=refs)
    return [p for p in everyone if form.askable(p.answers, keys)]


async def preview(session, event, body) -> dict:
    form = Form(event.additional_details)
    keys = _check_keys(form, body.questions)
    refs = _check_refs(body.refs)
    found = await targets(session, event, form, keys, include_requests=body.include_requests, refs=refs)
    sender, reason = await whatsapp_route(session, event.club_id)
    return {
        "people": len(found),
        "reach": await reach(session, event, found, whatsapp_on=sender is not None),
        "whatsapp": {"available": sender is not None, "reason": reason},
    }


# ─── Making an ask ───────────────────────────────────────────────────────


async def _existing(session, event_id: int, client_ref: Optional[str]) -> Optional[AnswerAsks]:
    if not client_ref:
        return None
    return await session.scalar(select(AnswerAsks).where(AnswerAsks.event_id == event_id, AnswerAsks.client_ref == client_ref))


def _clean_message(text: Optional[str]) -> str:
    lines = [" ".join(line.split()) for line in str(text or "").replace("\r\n", "\n").split("\n")]
    out = "\n".join(lines).strip()
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    if not out:
        raise _bad("message", "Write a short message to go with it.")
    if len(out) > MESSAGE_MAX:
        raise _bad("message", f"Keep the message under {MESSAGE_MAX} characters.")
    return out


async def create(session, event, body, *, actor) -> dict:
    """The ask, its people, a queued message on each channel for each of
    them, and a job each that sends them, in one transaction. A `client_ref`
    seen before returns that ask."""
    now = _now()
    existing = await _existing(session, event.id, body.client_ref)
    if existing is not None:
        return await out_one(session, event, existing)
    if ma.event_over(event, now):
        raise CustomHTTPException(409, message="This event is over, so nobody can be asked now.", error_code="EVENT_ENDED")
    form = Form(event.additional_details)
    keys = _check_keys(form, body.questions)
    refs = _check_refs(body.refs)
    message = _clean_message(body.message)
    channels = [c for c in CHANNELS if c in set(body.channels or [])]
    if not channels:
        raise _bad("channels", "Pick at least one way to send it.")
    deadline = body.deadline
    if deadline is not None:
        if deadline <= now:
            raise _bad("deadline", "The deadline has to be in the future.")
        end = ma.ends_at(event)
        if end is not None and deadline > end:
            raise _bad("deadline", "The deadline can't be after the event ends.")
    hours = body.remind_after_hours
    if hours is not None and not REMIND_MIN_HOURS <= hours <= REMIND_MAX_HOURS:
        raise _bad("remind_after_hours", f"Remind after {REMIND_MIN_HOURS} to {REMIND_MAX_HOURS} hours.")
    if WHATSAPP in channels:
        sender, reason = await whatsapp_route(session, event.club_id)
        if sender is None:
            raise CustomHTTPException(
                422, message="WhatsApp can't be sent from your number right now.", error_code="WHATSAPP_UNAVAILABLE",
                errors={"channels": reason},
            )
    found = await targets(session, event, form, keys, include_requests=body.include_requests, refs=refs)
    if not found:
        raise CustomHTTPException(409, message="Nobody has those questions to answer.", error_code="NOTHING_TO_ASK")
    ask = AnswerAsks(
        id=uuid.uuid4(), event_id=event.id, created_at=now, created_by_user_id=getattr(actor, "user_id", None),
        created_by_member_id=getattr(actor, "member_id", None), created_by_name=(getattr(actor, "name", None) or None),
        questions=[{"key": k, "label": form.label(k)} for k in keys], channels=channels, message=message,
        deadline=deadline, remind_after_hours=hours, include_requests=bool(body.include_requests),
        refs=[p.ref for p in found] if refs is not None else None, client_ref=body.client_ref, status=OPEN,
        reminders_sent=0, last_round_at=now,
        next_reminder_at=(now + timedelta(hours=hours)) if hours is not None else None,
    )
    if ask.next_reminder_at is not None and deadline is not None and ask.next_reminder_at >= deadline:
        ask.next_reminder_at = None
    session.add(ask)
    try:
        await session.flush()
    except Exception:
        await session.rollback()
        again = await _existing(session, event.id, body.client_ref)
        if again is not None:
            return await out_one(session, event, again)
        raise
    people_rows = [{"ask_id": ask.id, "ref_kind": p.kind, "ref_id": p.id, "event_id": event.id, "added_at": now} for p in found]
    for i in range(0, len(people_rows), 1000):
        await session.execute(pg_insert(AnswerAskPeople.__table__).values(people_rows[i:i + 1000]).on_conflict_do_nothing())
    await _round(session, ask, found, 0, now)
    if ask.next_reminder_at is not None:
        await jobs.enqueue(session, REMIND_JOB, {"ask_id": ask.id}, run_at=ask.next_reminder_at, dedupe_key=f"missing_answers:remind:{ask.id}")
    await session.commit()
    return await out_one(session, event, ask)


async def _round(session, ask: AnswerAsks, found: list[Person], round_: int, now: datetime) -> None:
    """A queued message on each of the ask's channels for each of them, and a job each."""
    rows = [
        {"id": uuid.uuid4(), "ask_id": ask.id, "ref_kind": p.kind, "ref_id": p.id, "channel": c, "round": round_,
         "status": QUEUED, "created_at": now}
        for p in found for c in (ask.channels or [])
    ]
    for i in range(0, len(rows), 1000):
        await session.execute(pg_insert(AnswerAskSends.__table__).values(rows[i:i + 1000]).on_conflict_do_nothing())
    await jobs.enqueue_many(session, SEND_JOB, [
        ({"ask_id": ask.id, "ref_kind": p.kind, "ref_id": p.id, "round": round_},
         f"missing_answers:{ask.id}:{p.kind}:{p.id}:{round_}")
        for p in found
    ])


# ─── Reading asks ────────────────────────────────────────────────────────


async def _people_of(session, ask_ids: list) -> dict:
    rows = (await session.execute(
        select(AnswerAskPeople).where(AnswerAskPeople.ask_id.in_(ask_ids))
    )).scalars().all()
    out: dict = {}
    for r in rows:
        out.setdefault(r.ask_id, []).append(r)
    return out


async def _send_counts(session, ask_ids: list) -> dict:
    """{ask id: (sent people, failed people)}: a person counts as sent once
    anything reached them, as failed when something failed and nothing did."""
    S = AnswerAskSends
    person = (
        select(
            S.ask_id, S.ref_kind, S.ref_id,
            func.bool_or(S.status == SENT).label("sent"), func.bool_or(S.status == FAILED).label("failed"),
        )
        .where(S.ask_id.in_(ask_ids)).group_by(S.ask_id, S.ref_kind, S.ref_id).subquery()
    )
    rows = (await session.execute(
        select(person.c.ask_id, func.count().filter(person.c.sent), func.count().filter(and_(person.c.failed, ~person.c.sent)))
        .group_by(person.c.ask_id)
    )).all()
    return {a: (int(s or 0), int(f or 0)) for a, s, f in rows}


def _still(form: Form, ask: AnswerAsks, rows: list, everyone: dict) -> list[Person]:
    keys = ma.ask_keys(ask)
    out = []
    for r in rows:
        p = everyone.get((r.ref_kind, r.ref_id))
        if p is not None and form.askable(p.answers, keys):
            out.append(p)
    return out


def _close(ask: AnswerAsks, reason: str, now: datetime) -> None:
    ask.status, ask.closed_at, ask.closed_reason, ask.next_reminder_at = CLOSED, now, reason, None


def due_reason(ask: AnswerAsks, event, still: int, now: datetime) -> Optional[str]:
    """Why an open ask has closed by itself now, or None."""
    if ask.status != OPEN:
        return None
    if ask.deadline is not None and ask.deadline <= now:
        return DEADLINE
    if ma.event_over(event, now):
        return EVENT_ENDED
    if still == 0:
        return DONE
    return None


def ask_out(ask: AnswerAsks, rows: list, sent_failed: tuple, still: int) -> dict:
    return {
        "id": ask.id,
        "created_at": ask.created_at,
        "created_by": ask.created_by_name or "",
        "questions": list(ask.questions or []),
        "channels": list(ask.channels or []),
        "message": ask.message,
        "deadline": ask.deadline,
        "remind_after_hours": ask.remind_after_hours,
        "status": ask.status,
        "closed_reason": ask.closed_reason,
        "counts": {
            "people": len(rows),
            "sent": sent_failed[0],
            "failed": sent_failed[1],
            "opened": sum(1 for r in rows if r.opened_at is not None),
            "answered": sum(1 for r in rows if r.answered_at is not None),
            "still_askable": still,
        },
        "reminders_sent": int(ask.reminders_sent or 0),
        "next_reminder_at": ask.next_reminder_at if ask.status == OPEN else None,
    }


async def settle(session, event, asks: list[AnswerAsks], now: Optional[datetime] = None) -> list[dict]:
    """The asks as the dashboard reads them, each closed first if its time
    has come (its deadline, nobody left, the event over)."""
    if not asks:
        return []
    now = now or _now()
    form = Form(event.additional_details)
    ids = [a.id for a in asks]
    by_ask = await _people_of(session, ids)
    wanted = {(r.ref_kind, r.ref_id) for rows in by_ask.values() for r in rows}
    everyone = {p.key: p for p in await people_mod.load(session, event, refs=list(wanted))} if wanted else {}
    counts = await _send_counts(session, ids)
    out = []
    changed = False
    for ask in asks:
        rows = by_ask.get(ask.id, [])
        still = len(_still(form, ask, rows, everyone))
        reason = due_reason(ask, event, still, now)
        if reason is not None:
            _close(ask, reason, now)
            changed = True
        out.append(ask_out(ask, rows, counts.get(ask.id, (0, 0)), still))
    if changed:
        await session.commit()
    return out


async def out_one(session, event, ask: AnswerAsks) -> dict:
    return (await settle(session, event, [ask]))[0]


async def list_asks(session, event) -> dict:
    asks = list((await session.scalars(
        select(AnswerAsks).where(AnswerAsks.event_id == event.id).order_by(AnswerAsks.created_at.desc(), AnswerAsks.id.desc())
    )).all())
    return {"asks": await settle(session, event, asks)}


async def ask_of(session, event, ask_id, *, lock: bool = False) -> AnswerAsks:
    stmt = select(AnswerAsks).where(AnswerAsks.id == ask_id, AnswerAsks.event_id == event.id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    ask = await session.scalar(stmt)
    if ask is None:
        raise CustomHTTPException(404, message="That ask isn't on this event.", error_code="NOT_FOUND")
    return ask


async def close(session, event, ask_id) -> dict:
    ask = await ask_of(session, event, ask_id, lock=True)
    if ask.status == OPEN:
        _close(ask, BY_CLUB, _now())
        await session.commit()
    return await out_one(session, event, ask)


async def _still_askable_people(session, event, ask: AnswerAsks) -> list[Person]:
    rows = (await _people_of(session, [ask.id])).get(ask.id, [])
    if not rows:
        return []
    everyone = {p.key: p for p in await people_mod.load(session, event, refs=[(r.ref_kind, r.ref_id) for r in rows])}
    return _still(Form(event.additional_details), ask, rows, everyone)


async def remind(session, event, ask_id, *, now: Optional[datetime] = None, automatic: bool = False) -> dict:
    """A reminder to the people still askable: at most two an ask (the
    automatic one counts), each at least 12 hours after the round before.
    409 REMIND_LIMIT, or REMIND_TOO_SOON with `retry_after` seconds; 409
    CLOSED for an ask that has closed."""
    now = now or _now()
    ask = await ask_of(session, event, ask_id, lock=True)
    still = await _still_askable_people(session, event, ask)
    reason = due_reason(ask, event, len(still), now)
    if reason is not None:
        _close(ask, reason, now)
        await session.commit()
    if ask.status != OPEN:
        if automatic:
            return await out_one(session, event, ask)
        raise CustomHTTPException(409, message="This ask is closed.", error_code="CLOSED")
    if int(ask.reminders_sent or 0) >= MAX_REMINDERS:
        if automatic:
            ask.next_reminder_at = None
            await session.commit()
            return await out_one(session, event, ask)
        raise CustomHTTPException(409, message=f"An ask can be reminded at most {MAX_REMINDERS} times.", error_code="REMIND_LIMIT")
    last = ask.last_round_at or ask.created_at
    if last is not None and now - last < REMIND_GAP:
        retry = int((last + REMIND_GAP - now).total_seconds()) + 1
        if automatic:
            ask.next_reminder_at = last + REMIND_GAP
            await jobs.enqueue(session, REMIND_JOB, {"ask_id": ask.id}, run_at=ask.next_reminder_at,
                               dedupe_key=f"missing_answers:remind:{ask.id}:{ask.next_reminder_at.isoformat()}")
            await session.commit()
            return await out_one(session, event, ask)
        raise CustomHTTPException(
            409, message="The last message went less than 12 hours ago.", error_code="REMIND_TOO_SOON",
            errors={"retry_after": retry}, headers={"Retry-After": str(retry)},
        )
    round_ = int(ask.reminders_sent or 0) + 1
    await _round(session, ask, still, round_, now)
    ask.reminders_sent = round_
    ask.last_round_at = now
    if automatic or round_ >= MAX_REMINDERS:
        ask.next_reminder_at = None
    await session.commit()
    return await out_one(session, event, ask)


@jobs.handler(REMIND_JOB, max_attempts=4, timeout=120.0)
async def _remind_job(p: dict) -> None:
    async with jobs.scope() as s:
        ask = await s.get(AnswerAsks, uuid.UUID(str(p["ask_id"])))
        if ask is None or ask.status != OPEN or ask.next_reminder_at is None:
            return
        if ask.next_reminder_at > _now() + timedelta(minutes=1):
            return  # moved later: the job for that time sends it
        event = await s.scalar(select(Events).options(joinedload(Events.club)).where(Events.id == ask.event_id))
        if event is None:
            return
        await remind(s, event, ask.id, automatic=True)


# ─── Sending one person their round ───────────────────────────────────────


def _email(event, person: Person, ask: AnswerAsks, labels: list[str], link: str, round_: int):
    """(subject, text, html): the club's words, the questions, the deadline,
    and the button to the person's link. Plain, branded as the club."""
    import html as html_mod

    n = len(labels)
    name = event.name or "Your event"
    subject = (f"Reminder: {plural(n)} needed for {name}" if round_ else f"{name} needs {plural(n, 'more detail')} from you")
    first = person.first_name or "there"
    by = when_text(ask.deadline)
    parts = [f"Hi {first},", ask.message, "We need:\n" + "\n".join(f"• {label}" for label in labels)]
    if by:
        parts.append(f"Please add them by {by}.")
    parts.append(f"Add my details: {link}")
    text = "\n\n".join(parts) + "\n"
    esc = html_mod.escape
    items = "".join(f"<li>{esc(label)}</li>" for label in labels)
    message_html = "<br>".join(esc(line) for line in ask.message.split("\n"))
    html = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;'
        'color:#141614;max-width:520px;margin:0 auto;padding:24px 16px">'
        f'<p style="font-size:18px;font-weight:700;margin:0 0 16px">{esc(name)}</p>'
        f"<p>Hi {esc(first)},</p><p>{message_html}</p><p>We need:</p><ul>{items}</ul>"
        + (f"<p>Please add them by {esc(by)}.</p>" if by else "")
        + f'<p style="margin:24px 0"><a href="{esc(link, quote=True)}" style="display:inline-block;background:#141614;'
        f'color:#ffffff;text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:600">Add my details</a></p>'
        f'<p style="color:#6B716C;font-size:12px">Or open {esc(link)}</p>'
        "<p style=\"color:#6B716C;font-size:12px\">Your registration stays as it is either way.</p></div>"
    )
    return subject, text, html


async def _mark(session, row_id, status: str, *, error: Optional[str] = None, provider_id: Optional[str] = None) -> None:
    await session.execute(
        update(AnswerAskSends).where(AnswerAskSends.id == row_id, AnswerAskSends.status == QUEUED)
        .values(status=status, error=(error or None) and error[:300], provider_id=provider_id,
                sent_at=_now() if status == SENT else None)
        .execution_options(synchronize_session=False)
    )


async def _push(session, event, person: Person, ask: AnswerAsks, n: int, link: str) -> tuple[str, Optional[str]]:
    from app.core.notifications.service import send_notification_to_user

    if person.user_id is None or not await _push_users(session, [person.user_id]):
        return SKIPPED, "the app is not on their phone"
    title = f"{event.name} needs {plural(n, 'more detail')}"
    data = {"type": "answers_needed", "event_id": str(event.id), "url": link}
    try:
        ok = await send_notification_to_user(session, int(person.user_id), title, ask.message, data=data)
    except Exception as exc:  # noqa: BLE001 - the other channels still go
        logger.warning("ask %s: the push to %s failed", ask.id, person.ref, exc_info=True)
        return FAILED, str(exc)[:300] or "the push failed"
    return (SENT, None) if ok else (SKIPPED, "no device took it")


async def _mail(session, event, person: Person, ask: AnswerAsks, labels: list[str], link: str, round_: int, row_id):
    from app.api.message_quota import service as quota
    from app.core.email import email as email_mod
    from app.core.email import transport
    from app.core.mail import suppression

    if not person.email:
        return SKIPPED, "no email address", None, None
    if person.email_bounced_at is not None:
        return SKIPPED, "that address bounced", None, None
    found = (await suppression.reasons(session, [person.email])).get(suppression.normal(person.email))
    if found is not None:
        return SKIPPED, suppression.why(found), None, None
    if suppression.at_suppressed_domain(person.email):
        return SKIPPED, "that address takes no mail", None, None
    terms = None
    if round_ > 0:
        # A reminder counts like any message, and waits past Free's allowance.
        terms, held = await quota.check(session, event.club_id, quota.EMAIL, REMINDER_KIND)
        if held:
            return SKIPPED, held, None, None
    subject, text, html = _email(event, person, ask, labels, link, round_)
    org = event.club.name if getattr(event, "club", None) is not None else None
    # The read is over before the provider's call: no connection held meanwhile.
    await session.commit()
    try:
        response = await asyncio.to_thread(
            email_mod.send_email, recipients=[person.email], subject=subject, text=text, html=html, sender_name=org,
            idempotency_key=f"answers-{row_id}", check_suppression=False,
        )
    except transport.MailDeferred:
        raise
    except Exception as exc:  # noqa: BLE001
        return FAILED, (str(exc) or exc.__class__.__name__)[:300], None, None
    return SENT, None, (response or {}).get("MessageId"), terms


async def _whatsapp(session, event, person: Person, n: int, ask: AnswerAsks, sender) -> tuple[str, Optional[str], Optional[str]]:
    from app.core import whatsapp

    if sender is None:
        return SKIPPED, "the club's WhatsApp can't send it now", None
    why = await whatsapp_reachable(session, event.club_id, person)
    if why:
        return SKIPPED, why, None
    template = whatsapp.details_needed(
        name=person.full_name, event=event.name, count=n, by=when_text(ask.deadline) or "the event",
        suffix=people_mod.link_suffix(event.id, person.ref),
    )
    await session.commit()
    try:
        sent = await whatsapp.send(person.phone, template, sender=sender)
    except whatsapp.Undeliverable as exc:
        return SKIPPED, str(getattr(exc, "reason", None) or exc)[:300], None
    except whatsapp.Deferred:
        raise
    except whatsapp.WhatsAppError as exc:
        return FAILED, str(getattr(exc, "reason", None) or exc)[:300], None
    return SENT, None, (sent or {}).get("MessageId")


async def send_one(ask_id, ref_kind: str, ref_id, round_: int) -> None:
    """One person's round: each queued channel sent, skipped (with why) or
    failed. A provider that says "later" raises, and the job is tried again
    for what is still queued."""
    from app.api.message_quota import service as quota

    async with jobs.scope() as s:
        ask = await s.get(AnswerAsks, ask_id)
        if ask is None:
            return
        event = await s.scalar(select(Events).options(joinedload(Events.club)).where(Events.id == ask.event_id).execution_options(include_deleted=True))
        rows = list((await s.scalars(
            select(AnswerAskSends).where(
                AnswerAskSends.ask_id == ask_id, AnswerAskSends.ref_kind == ref_kind, AnswerAskSends.ref_id == ref_id,
                AnswerAskSends.round == round_, AnswerAskSends.status == QUEUED,
            ).order_by(AnswerAskSends.channel)
        )).all())
        if not rows or event is None:
            await s.commit()
            return
        person = await people_mod.load_one(s, event, ref_kind, ref_id)
        form = Form(event.additional_details)
        keys = form.askable(person.answers, ma.ask_keys(ask)) if person is not None else []
        why_not = None
        if person is None:
            why_not = "they are no longer registered"
        elif ask.status != OPEN or not ma.live(ask, event, _now()):
            why_not = "the ask had closed"
        elif not keys:
            why_not = "they had already answered"
        if why_not:
            for r in rows:
                await _mark(s, r.id, SKIPPED, error=why_not)
            await s.commit()
            return
        link = people_mod.personal_link(event.id, person.ref)
        labels = [form.label(k) for k in keys]
        sender = None
        if any(r.channel == WHATSAPP for r in rows):
            sender, _reason = await whatsapp_route(s, event.club_id)
        row_ids = {r.channel: r.id for r in rows}
        await s.commit()
        for channel in (PUSH, WHATSAPP, EMAIL):
            row_id = row_ids.get(channel)
            if row_id is None:
                continue
            if channel == PUSH:
                status, error = await _push(s, event, person, ask, len(keys), link)
                await _mark(s, row_id, status, error=error)
            elif channel == WHATSAPP:
                status, error, provider_id = await _whatsapp(s, event, person, len(keys), ask, sender)
                await _mark(s, row_id, status, error=error, provider_id=provider_id)
            else:
                status, error, provider_id, terms = await _mail(s, event, person, ask, labels, link, round_, row_id)
                await _mark(s, row_id, status, error=error, provider_id=provider_id)
                if status == SENT and round_ > 0:
                    await quota.count(s, club_id=event.club_id, channel=quota.EMAIL, kind=REMINDER_KIND, terms=terms,
                                      ref=f"answers:{row_id}")
            await s.commit()


@jobs.handler(SEND_JOB, max_attempts=6, timeout=120.0)
async def _send_job(p: dict) -> None:
    await send_one(uuid.UUID(str(p["ask_id"])), str(p["ref_kind"]), uuid.UUID(str(p["ref_id"])), int(p.get("round") or 0))

