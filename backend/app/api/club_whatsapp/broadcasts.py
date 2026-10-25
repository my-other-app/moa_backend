"""Broadcasts from a club's own WhatsApp number (Enterprise, `whatsapp_own`).

Two kinds, decided by the approved template's category:

- an **event update** (UTILITY): to the registrants of one of the club's
  events who ticked "Send my ticket on WhatsApp" (registration's
  `whatsapp_opt_in`), holding a live ticket (paid for, on a paid event);
- a **promotion** (MARKETING): to the people with a live consent to the
  club's updates (consent.py).

Both leave out anyone who asked the club's number to stop (optout.blocked,
scope "all"; for a promotion "marketing" too), and say how many were left
out and why. The composer maps each of the template's values to the
person's name, the event's name or date, or the club's own words, and is
shown the recipients and Meta's list-rate cost (INR, GST apart) first.

Sending is a job (`club_whatsapp.broadcast`), at CLUB_WHATSAPP_BROADCAST_MPS
a second: the audience is read when it starts (so a scheduled update reaches
whoever registered meanwhile), then people are taken a batch at a time --
never more in 24 hours than the account's messaging limit allows (unique
people the number reached in the moving day, its tickets and inbox
templates counted too); the rest wait for the next day (`held`). Meta's
"not now" codes are tried again with a backoff, 131049 (its per-person
marketing limit) once more a day later, 131050 never (the person stopped the
club's marketing: a marketing opt-out is kept, and their consent withdrawn).
A MARKETING template goes to Meta's Marketing Messages API
(`/marketing_messages`) once the account's
`marketing_messages_onboarding_status` is ONBOARDED, else to `/messages`;
a 131063 from `/messages` (marketing turned off on the Cloud API) moves the
broadcast to `/marketing_messages`. A refusal about the account or the
template stops the broadcast (the account is marked failed as for any send).

The club pays Meta for these: nothing here counts against MyOtherApp's
message quotas. Results come from Meta's status webhooks (webhook.py):
sent, delivered, read, failed by Meta's code, and skipped.

Nobody acting as the club for MyOtherApp may see or send one (access.py's
inbox rule), and nothing a broadcast says is logged or audited.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, or_, select, update

from app.api.club_whatsapp import consent, crypto, graph, inbox, optout, templates
from app.api.club_whatsapp import service as accounts
from app.api.club_whatsapp.models import (
    ACTIVE,
    FAILED_MESSAGE,
    OUT,
    ClubWhatsAppAccounts,
    ClubWhatsAppConversations,
    ClubWhatsAppMessages,
)
from app.api.club_whatsapp.reach_models import (
    CANCELLED,
    CLAIMED,
    DELIVERED,
    DONE,
    ENDPOINT_MARKETING,
    ENDPOINT_MESSAGES,
    EVENT_UPDATE,
    FAILED,
    PROMOTION,
    QUEUED,
    READ,
    RECIPIENT_FAILED,
    SCHEDULED,
    SENDING,
    SENT,
    SKIPPED,
    STATUS_ORDER,
    ClubWhatsAppBroadcastRecipients,
    ClubWhatsAppBroadcasts,
)
from app.config import settings
from app.core import jobs
from app.db import lock_keys
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

JOB = "club_whatsapp.broadcast"
SEND_LOCK = lock_keys.CLUB_WHATSAPP_BROADCAST

#: Meta's list rates for India, per delivered message, before GST (its rate card from 1 October 2026).
RATES_INR = {"UTILITY": Decimal("0.1150"), "MARKETING": Decimal("0.8631")}
GST = Decimal("0.18")
KIND_OF_CATEGORY = {"UTILITY": EVENT_UPDATE, "MARKETING": PROMOTION}
#: Unique people a day, by the account's messaging limit (Meta's TIER_*).
PER_DAY = {"TIER_50": 50, "TIER_250": 250, "TIER_1K": 1000, "TIER_2K": 2000, "TIER_10K": 10_000, "TIER_100K": 100_000}
UNLIMITED = "TIER_UNLIMITED"
#: A limit not read yet: Meta's lowest step for a new business.
DEFAULT_PER_DAY = 250
DAY = timedelta(hours=24)

SOURCES = ("name", "event_name", "event_date", "text")
TEXT_MAX = 500
#: People taken at once, tries a person gets for "not now", a job's time before it hands over to the next.
BATCH = 50
MAX_TRIES = 5
BUDGET_SECONDS = 50 * 60
#: A claim this old is a sender that died mid-send: it may have gone, so it is not sent again.
STALE_CLAIM = timedelta(minutes=10)
#: A scheduled broadcast is at most this far ahead, and at least a minute.
SCHEDULE_MAX = timedelta(days=60)

#: Meta's "not now" codes (busy, throttled, the same person too fast): tried again later.
RETRY_CODES = frozenset({"1", "2", "4", "17", "32", "613", "80007", "130429", "131000", "131016", "131048", "131056", "131057"})
CAPPED = "131049"
STOPPED = "131050"
MARKETING_OFF_ON_CLOUD = "131063"
MM_ONBOARDED = "ONBOARDED"
MM_REFRESH = timedelta(hours=24)

ESTIMATE_LABEL = ("An estimate at Meta's list rate for India, for every message delivered. Meta bills what it "
                  "delivers, in your WhatsApp Business Account's currency; GST is charged on top.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bad(field: str, message: str, code: str = "INVALID_BROADCAST", status: int = 422) -> CustomHTTPException:
    return CustomHTTPException(status_code=status, message=message, error_code=code, errors={"field": field})


def when(event) -> str:
    """The event's date as a ticket says it: "Fri 20 Nov 2026, 6:30 PM", India's time."""
    from app.api.events.documents import EVENT_TIMEZONE

    if event is None or event.event_datetime is None:
        return ""
    return event.event_datetime.astimezone(ZoneInfo(EVENT_TIMEZONE)).strftime("%a %-d %b %Y, %-I:%M %p")


def first_name(full_name: Optional[str]) -> str:
    return ((full_name or "").strip().split(" ")[0] or "there")[:60]


# ─── What the composer sends ─────────────────────────────────────────────


@dataclass
class Prepared:
    template: object
    definition: dict
    kind: str
    category: str
    event: Optional[object]
    variables: dict


def check_variables(definition: dict, given: dict, *, has_event: bool) -> dict:
    """What fills each of the template's values, checked against it:
    {"header": [v], "body": [v], "buttons": {"0": v}}, each v
    {"source": name | event_name | event_date | text, "value": "..." for text}."""
    header = definition.get("header") or None
    if header and header.get("format") in ("DOCUMENT", "IMAGE", "VIDEO", "LOCATION"):
        raise _bad("template", "Templates with a picture, video or document header can't be broadcast yet. Pick another.",
                   "INVALID_TEMPLATE")

    def one(field: str, v) -> dict:
        if not isinstance(v, dict) or v.get("source") not in SOURCES:
            raise _bad(field, "Say what fills this value: their name, the event's name or date, or your own words.")
        source = v["source"]
        if source in ("event_name", "event_date") and not has_event:
            raise _bad(field, "Pick the event whose name or date fills this value.")
        if source != "text":
            return {"source": source}
        value = " ".join(str(v.get("value") or "").split())
        if not value:
            raise _bad(field, "Write the words that fill this value.")
        if len(value) > TEXT_MAX:
            raise _bad(field, f"At most {TEXT_MAX} characters here.")
        return {"source": "text", "value": value}

    def many(field: str, values, n: int) -> list:
        values = values if isinstance(values, list) else []
        if len(values) != n:
            raise _bad(field, f"This template has {n} value{'s' if n != 1 else ''} there.")
        return [one(f"{field}.{i}", v) for i, v in enumerate(values)]

    given = given if isinstance(given, dict) else {}
    out: dict = {"header": [], "body": [], "buttons": {}}
    if header and header.get("format") == "TEXT":
        out["header"] = many("header", given.get("header") or [], int(header.get("variables") or 0))
    out["body"] = many("body", given.get("body") or [], int(definition.get("body_variables") or 0))
    buttons = given.get("buttons") if isinstance(given.get("buttons"), dict) else {}
    for b in definition.get("buttons") or []:
        if b.get("type") == "URL" and b.get("variables"):
            key = str(b["index"])
            out["buttons"][key] = one(f"buttons.{key}", buttons.get(key))
    return out


def params_for(variables: dict, *, name: Optional[str], event) -> dict:
    """The values for one person, as templates.send_components takes them."""
    def value(v: dict) -> str:
        source = v.get("source")
        if source == "name":
            return first_name(name)
        if source == "event_name":
            return (getattr(event, "name", None) or "").strip()
        if source == "event_date":
            return when(event)
        return str(v.get("value") or "")

    return {
        "header": [value(v) for v in variables.get("header") or []],
        "body": [value(v) for v in variables.get("body") or []],
        "buttons": {k: value(v) for k, v in (variables.get("buttons") or {}).items()},
    }


async def prepare(session, club_id: int, account: ClubWhatsAppAccounts, payload: dict) -> Prepared:
    """The template (approved on the club's account), its kind, the event and
    the values' mapping, checked. 409 TEMPLATE_NOT_APPROVED; 422
    INVALID_TEMPLATE (not UTILITY or MARKETING, a media header),
    INVALID_BROADCAST (`errors.field`); 404 EVENT_NOT_FOUND."""
    from app.api.events.models import Events

    name = str(payload.get("template") or "").strip()
    if not templates.NAME.match(name):
        raise _bad("template", "Pick an approved template.", "TEMPLATE_NOT_APPROVED", 409)
    row = await templates.approved(session, account.id, name, payload.get("language") or None)
    if row is None:
        raise _bad("template", "That template isn't approved on your WhatsApp account. Refresh the templates and pick an approved one.",
                   "TEMPLATE_NOT_APPROVED", 409)
    category = str(row.category or "").upper()
    kind = KIND_OF_CATEGORY.get(category)
    if kind is None:
        raise _bad("template", "Only UTILITY (an event update) or MARKETING (a promotion) templates can be broadcast.", "INVALID_TEMPLATE")
    if payload.get("kind") and payload["kind"] != kind:
        raise _bad("kind", f"That template is {category}: it goes out as {'an event update' if kind == EVENT_UPDATE else 'a promotion'}.",
                   "KIND_MISMATCH")
    event = None
    if payload.get("event_id") is not None:
        event = await session.scalar(select(Events).where(
            Events.id == int(payload["event_id"]), Events.club_id == club_id, Events.is_deleted == False,  # noqa: E712
        ))
        if event is None:
            raise _bad("event_id", "No such event of your club's.", "EVENT_NOT_FOUND", 404)
    if kind == EVENT_UPDATE and event is None:
        raise _bad("event_id", "Pick the event whose registrants get this update.")
    definition = templates.shape(row.components or [])
    variables = check_variables(definition, payload.get("variables") or {}, has_event=event is not None)
    return Prepared(template=row, definition=definition, kind=kind, category=category, event=event, variables=variables)


# ─── Who it goes to ──────────────────────────────────────────────────────


@dataclass
class Person:
    wa_id: Optional[str]
    bsuid: Optional[str]
    registration_id: Optional[object] = None
    user_id: Optional[int] = None
    name: Optional[str] = None


@dataclass
class Audience:
    people: list = field(default_factory=list)
    opted_out: int = 0
    no_consent: int = 0
    no_number: int = 0


async def _stopped(session, club_id: int, kind: str, wa_id: Optional[str], bsuid: Optional[str]) -> bool:
    if await optout.blocked(session, club_id, wa_id=wa_id, bsuid=bsuid, scope="all"):
        return True
    return kind == PROMOTION and await optout.blocked(session, club_id, wa_id=wa_id, bsuid=bsuid, scope="marketing")


async def audience(session, club_id: int, kind: str, event) -> Audience:
    """Everyone a broadcast of this kind reaches now, once each, and how many
    were left out: opted out, no consent, no number WhatsApp reaches."""
    from app.api.club_whatsapp.reach_models import ClubWhatsAppConsents as C
    from app.api.events.models import EventRegistrationsLink as R
    from app.api.events.models import Events
    from app.api.users.models import Users

    out, seen = Audience(), set()
    if kind == EVENT_UPDATE:
        rows = (await session.execute(
            select(R.id, R.user_id, R.full_name, R.phone, R.whatsapp_opt_in, R.wa_bounced_at)
            .join(Events, Events.id == R.event_id)
            .where(
                R.event_id == event.id, Events.club_id == club_id, R.is_deleted == False, Events.is_deleted == False,  # noqa: E712
                R.ticket_revoked_at.is_(None), or_(Events.has_fee == False, R.is_paid == True),  # noqa: E712
            )
            .order_by(R.created_at, R.id)
        )).all()
        for rid, user_id, full_name, phone, opted_in, bounced in rows:
            if not opted_in:
                out.no_consent += 1
                continue
            wa = inbox.wa_of(phone)
            if wa is None or bounced is not None:
                out.no_number += 1
                continue
            if wa in seen:
                continue
            seen.add(wa)
            if await _stopped(session, club_id, kind, wa, None):
                out.opted_out += 1
                continue
            out.people.append(Person(wa_id=wa, bsuid=None, registration_id=rid, user_id=user_id, name=full_name))
        return out
    rows = (await session.execute(
        select(C.registration_id, C.user_id, C.wa_id, C.bsuid, Users.full_name, Users.phone, R.full_name)
        .select_from(C).outerjoin(Users, Users.id == C.user_id).outerjoin(R, R.id == C.registration_id)
        .where(C.club_id == club_id, C.withdrawn_at.is_(None))
        .order_by(C.id)
    )).all()
    for rid, user_id, wa_id, bsuid, user_name, user_phone, reg_name in rows:
        # A follower who had no number then: their profile's now.
        wa = wa_id or inbox.wa_of(user_phone)
        if wa is None and not bsuid:
            out.no_number += 1
            continue
        key = wa or bsuid
        if key in seen:
            continue
        seen.add(key)
        if await _stopped(session, club_id, kind, wa, bsuid):
            out.opted_out += 1
            continue
        out.people.append(Person(wa_id=wa, bsuid=None if wa else bsuid, registration_id=rid, user_id=user_id, name=reg_name or user_name))
    return out


# ─── The messaging limit and the estimate ────────────────────────────────


def per_day(tier: Optional[str]) -> Optional[int]:
    """Unique people a day the account may reach; None for no limit."""
    if tier == UNLIMITED:
        return None
    return PER_DAY.get(tier or "", DEFAULT_PER_DAY)


async def used_today(session, club_id: int, now: datetime) -> tuple[int, Optional[datetime]]:
    """Unique people the club's number reached with a template in the moving
    24 hours -- broadcasts, tickets and reminders, inbox templates -- and
    when the first of them was (the day frees up from then). Counted as Meta
    counts toward the limit, but without knowing whose service window was
    open: never fewer than Meta's."""
    R, M, C = ClubWhatsAppBroadcastRecipients, ClubWhatsAppMessages, ClubWhatsAppConversations
    since = now - DAY
    people: dict[str, datetime] = {}
    for who, at in (await session.execute(
        select(func.coalesce(R.wa_id, R.bsuid), func.min(R.sent_at))
        .where(R.club_id == club_id, R.sent_at >= since).group_by(func.coalesce(R.wa_id, R.bsuid))
    )).all():
        people[who] = at
    for who, at in (await session.execute(
        select(func.coalesce(C.wa_id, C.bsuid), func.min(M.created_at)).join(C, C.id == M.conversation_id)
        .where(M.club_id == club_id, M.created_at >= since, M.direction == OUT, M.type == "template", M.status != FAILED_MESSAGE)
        .group_by(func.coalesce(C.wa_id, C.bsuid))
    )).all():
        if who is not None:
            people[who] = min(at, people.get(who, at))
    return len(people), (min(people.values()) if people else None)


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def estimate(category: str, recipients: int, *, tier: Optional[str], used: int = 0) -> dict:
    """Meta's list-rate cost for India (an estimate, GST apart) and how the
    messaging limit spreads it: how many go today, over how many days."""
    rate = RATES_INR.get(category, RATES_INR["MARKETING"])
    subtotal = rate * recipients
    gst = subtotal * GST
    limit = per_day(tier)
    first = recipients if limit is None else max(0, min(recipients, limit - used))
    days = 1 if limit is None or recipients <= first else 1 + math.ceil((recipients - first) / max(1, limit))
    return {
        "recipients": recipients, "category": category, "currency": "INR", "rate": float(rate),
        "subtotal": _money(subtotal), "gst_rate": float(GST), "gst": _money(gst), "total": _money(subtotal + gst),
        "estimate": True, "label": ESTIMATE_LABEL,
        "messaging_limit": {"tier": tier, "per_day": limit, "used_today": used, "today": first, "days": days},
    }


# ─── The composer's routes ───────────────────────────────────────────────


async def _sending_account(session, club_id: int, actor=None) -> ClubWhatsAppAccounts:
    await accounts.require_plan(session, club_id, actor=actor)
    account = await accounts.live_account(session, club_id)
    if account is None:
        raise CustomHTTPException(409, message="Connect your WhatsApp number first.", error_code="NO_WHATSAPP")
    if account.status != ACTIVE:
        raise CustomHTTPException(409, message="Your WhatsApp number isn't working right now. Check it in WhatsApp settings.",
                                  error_code="WHATSAPP_FAILED")
    return account


async def preview(session, club_id: int, payload: dict, *, actor=None, now: Optional[datetime] = None) -> dict:
    """What a broadcast would do now: its kind, who it reaches and who it
    leaves out, the estimate, and its words for the first person."""
    now = now or _now()
    account = await _sending_account(session, club_id, actor)
    prepared = await prepare(session, club_id, account, payload)
    found = await audience(session, club_id, prepared.kind, prepared.event)
    used, _first = await used_today(session, club_id, now)
    person = found.people[0] if found.people else None
    params = params_for(prepared.variables, name=person.name if person else None, event=prepared.event)
    try:
        _components, kept = templates.send_components(prepared.definition, params)
        sample = templates.render(prepared.definition, kept)
    except CustomHTTPException:
        sample = templates.render(prepared.definition, {})
    return {
        "kind": prepared.kind, "category": prepared.category, "template": prepared.template.name,
        "language": prepared.template.language, "event_id": getattr(prepared.event, "id", None),
        "recipients": len(found.people),
        "excluded": {"opted_out": found.opted_out, "no_consent": found.no_consent, "no_number": found.no_number},
        "estimate": estimate(prepared.category, len(found.people), tier=account.messaging_limit_tier, used=used),
        "sample": sample,
    }


async def create(session, club, user, payload: dict, *, member=None, actor_name: Optional[str] = None, request=None,
                 now: Optional[datetime] = None) -> ClubWhatsAppBroadcasts:
    """A broadcast, now or at `scheduled_at`: checked as the preview is, kept,
    and its job written in the same transaction. 422 NO_RECIPIENTS when
    nobody would get it; INVALID_BROADCAST for a time in the past or more
    than 60 days ahead."""
    from app.api.audit import service as audit

    now = now or _now()
    account = await _sending_account(session, club.id, user)
    prepared = await prepare(session, club.id, account, payload)
    scheduled_at = payload.get("scheduled_at")
    if scheduled_at is not None:
        scheduled_at = scheduled_at if scheduled_at.tzinfo else scheduled_at.replace(tzinfo=timezone.utc)
        if scheduled_at < now - timedelta(minutes=5):
            raise _bad("scheduled_at", "That time has passed. Pick a time ahead, or send now.")
        if scheduled_at > now + SCHEDULE_MAX:
            raise _bad("scheduled_at", "A broadcast can be scheduled at most 60 days ahead.")
        if scheduled_at <= now + timedelta(minutes=1):
            # A minute from now is now.
            scheduled_at = None
    found = await audience(session, club.id, prepared.kind, prepared.event)
    if not found.people:
        raise CustomHTTPException(422, message="Nobody would get it: nobody here asked for WhatsApp messages from your club, "
                                  "or they all asked you to stop.", error_code="NO_RECIPIENTS",
                                  errors={"opted_out": found.opted_out, "no_consent": found.no_consent, "no_number": found.no_number})
    used, _first = await used_today(session, club.id, now)
    b = ClubWhatsAppBroadcasts(
        club_id=club.id, account_id=account.id, kind=prepared.kind, template_name=prepared.template.name,
        template_language=prepared.template.language, category=prepared.category, event_id=getattr(prepared.event, "id", None),
        variables=prepared.variables, status=SCHEDULED if scheduled_at else SENDING, scheduled_at=scheduled_at,
        estimate=estimate(prepared.category, len(found.people), tier=account.messaging_limit_tier, used=used),
        created_by=getattr(user, "id", None) if member is None else None, created_by_member_id=getattr(member, "id", None),
        created_by_name=(actor_name or "")[:120] or None,
    )
    session.add(b)
    await session.flush()
    await jobs.enqueue(session, JOB, {"broadcast_id": b.id}, run_at=scheduled_at, dedupe_key=f"{JOB}:{b.id}:start")
    # What was sent and to how many; never the words.
    await audit.record(
        session, actor=user, action="club_whatsapp.broadcast", entity_type="club", entity_id=club.id,
        summary=f"{'Scheduled' if scheduled_at else 'Sent'} a WhatsApp {prepared.kind.replace('_', ' ')} ({b.template_name}) "
                f"to {len(found.people)}",
        details={"broadcast_id": b.id, "kind": b.kind, "template": b.template_name, "language": b.template_language,
                 "event_id": b.event_id, "recipients": len(found.people), "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
                 "member_id": getattr(member, "id", None)},
        request=request,
    )
    await session.commit()
    return b


async def cancel(session, club, user, broadcast_id: int, *, request=None) -> ClubWhatsAppBroadcasts:
    """A scheduled broadcast, or the rest of one being sent: nobody more gets it."""
    from app.api.audit import service as audit

    b = await get(session, club.id, broadcast_id)
    if b.status not in (SCHEDULED, SENDING):
        raise CustomHTTPException(409, message="It has finished already.", error_code="BROADCAST_FINISHED")
    now = _now()
    b.status, b.finished_at = CANCELLED, now
    R = ClubWhatsAppBroadcastRecipients
    await session.execute(update(R).where(R.broadcast_id == b.id, R.status == QUEUED)
                          .values(status=SKIPPED, skip_reason="cancelled", status_at=now).execution_options(synchronize_session=False))
    await audit.record(session, actor=user, action="club_whatsapp.broadcast_cancel", entity_type="club", entity_id=club.id,
                       summary=f"Cancelled WhatsApp broadcast {b.id}", details={"broadcast_id": b.id}, request=request)
    await session.commit()
    return b


async def get(session, club_id: int, broadcast_id: int) -> ClubWhatsAppBroadcasts:
    b = await session.scalar(select(ClubWhatsAppBroadcasts).where(
        ClubWhatsAppBroadcasts.id == broadcast_id, ClubWhatsAppBroadcasts.club_id == club_id,
    ).execution_options(populate_existing=True))
    if b is None:
        raise CustomHTTPException(404, message="No such broadcast.", error_code="BROADCAST_NOT_FOUND")
    return b


async def listed(session, club_id: int, *, offset: int = 0, limit: int = 20) -> tuple[list, int]:
    B = ClubWhatsAppBroadcasts
    rows = (await session.scalars(select(B).where(B.club_id == club_id).order_by(B.id.desc()).offset(offset).limit(limit))).all()
    total = await session.scalar(select(func.count()).select_from(B).where(B.club_id == club_id)) or 0
    return list(rows), int(total)


async def results(session, b: ClubWhatsAppBroadcasts, now: Optional[datetime] = None) -> dict:
    """Where everyone stands: queued (and waiting for the next day), sent,
    delivered, read, failed by Meta's code, and skipped by why."""
    now = now or _now()
    R = ClubWhatsAppBroadcastRecipients
    rows = (await session.execute(
        select(R.status, R.skip_reason, R.error_code, func.count(), func.min(R.error_title))
        .where(R.broadcast_id == b.id).group_by(R.status, R.skip_reason, R.error_code)
    )).all()
    if not rows and b.results:
        # The people are gone (retention); the counts as they stood then.
        return b.results
    count = {}
    failed: dict[str, dict] = {}
    skipped = {"opted_out": int(b.skipped_opted_out or 0), "no_consent": int(b.skipped_no_consent or 0),
               "no_number": int(b.skipped_no_number or 0), "cancelled": 0, "not_sent": 0}
    for status, reason, code, n, title in rows:
        count[status] = count.get(status, 0) + int(n)
        if status == RECIPIENT_FAILED:
            entry = failed.setdefault(code or "UNKNOWN", {"code": code, "title": title, "count": 0})
            entry["count"] += int(n)
        elif status == SKIPPED:
            skipped[reason or "not_sent"] = skipped.get(reason or "not_sent", 0) + int(n)
    held = int(await session.scalar(select(func.count()).select_from(R).where(R.broadcast_id == b.id, R.held == True)) or 0)  # noqa: E712
    waiting = int(await session.scalar(select(func.count()).select_from(R).where(
        R.broadcast_id == b.id, R.status == QUEUED, R.not_before > now)) or 0)
    skipped["limit_spill"] = held
    return {
        "recipients": sum(count.values()),
        "queued": count.get(QUEUED, 0) + count.get(CLAIMED, 0),
        "waiting": waiting,
        "sent": count.get(SENT, 0) + count.get(DELIVERED, 0) + count.get(READ, 0),
        "delivered": count.get(DELIVERED, 0) + count.get(READ, 0),
        "read": count.get(READ, 0),
        "failed": sum(f["count"] for f in failed.values()),
        "failed_by_reason": sorted(failed.values(), key=lambda f: (-f["count"], f["code"] or "")),
        "skipped": skipped,
    }


async def out(session, b: ClubWhatsAppBroadcasts) -> dict:
    from app.api.events.models import Events

    event = await session.get(Events, b.event_id) if b.event_id else None
    return {
        "id": b.id, "kind": b.kind, "template": b.template_name, "language": b.template_language, "category": b.category,
        "event": {"id": event.id, "name": event.name} if event is not None else None,
        "variables": b.variables or {}, "status": b.status, "scheduled_at": b.scheduled_at, "started_at": b.started_at,
        "finished_at": b.finished_at, "created_at": b.created_at, "created_by": b.created_by_name, "endpoint": b.endpoint,
        "estimate": b.estimate, "last_error": b.last_error, "results": await results(session, b),
    }


# ─── Sending ─────────────────────────────────────────────────────────────


class _Unconfirmed(Exception):
    """Sent, and no answer: it may have gone, so it is never sent again."""


async def _post(keys: graph.Keys, path: str, body: dict) -> str:
    """POST one template to Meta: its wamid, or GraphError (Meta's code)
    / graph.GraphError(unreachable) when Meta was never reached /
    _Unconfirmed when it may have gone."""
    headers, query = graph._auth(keys, None)
    try:
        async with graph._client() as client:
            r = await client.post(graph.url(path), headers=headers, params=query or None, json=body)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise graph.GraphError(f"Meta could not be reached ({exc.__class__.__name__})", unreachable=True) from None
    except httpx.HTTPError as exc:
        raise _Unconfirmed(f"Meta did not answer ({exc.__class__.__name__}); it may have gone") from None
    if r.status_code >= 300:
        raise graph._error(r)
    try:
        messages = (r.json() or {}).get("messages") or [{}]
    except ValueError:
        messages = [{}]
    return str((messages[0] or {}).get("id") or "") if isinstance(messages, list) else ""


async def endpoint_for(session, account: ClubWhatsAppAccounts, keys: graph.Keys, category: str, now: datetime) -> str:
    """Where a template of this category goes: a MARKETING one to Meta's
    Marketing Messages API once the account is ONBOARDED there (read at
    most daily), everything else to /messages."""
    if category != "MARKETING":
        return ENDPOINT_MESSAGES
    if account.mm_checked_at is None or account.mm_checked_at <= now - MM_REFRESH:
        try:
            answer = await graph.call(keys, "GET", account.waba_id, params={"fields": "marketing_messages_onboarding_status"})
            account.mm_status = (str(answer.get("marketing_messages_onboarding_status") or "").upper()[:32] or None)
        except graph.GraphError as exc:
            logger.info("whatsapp broadcast: MM status of number %s not read (%s)", account.id, exc.code)
        account.mm_checked_at = now
    return ENDPOINT_MARKETING if account.mm_status == MM_ONBOARDED else ENDPOINT_MESSAGES


def backoff(tries: int) -> timedelta:
    return timedelta(seconds=min(3600, 60 * 2 ** max(0, tries - 1)))


async def _marketing_stopped(session, club_id: int, wa_id: Optional[str], bsuid: Optional[str]) -> None:
    """Meta's 131050: the person stopped the club's marketing in WhatsApp.
    A `marketing` opt-out in the opt-out store (optout.py), and their consent
    to the club's updates withdrawn."""
    await optout.set_optout(session, club_id, wa_id=wa_id, bsuid=bsuid, scope=optout.MARKETING, source=optout.META_131050)
    await consent.withdraw(session, club_id, source=optout.META_131050, wa_id=wa_id, bsuid=bsuid)


async def _materialize(session, b: ClubWhatsAppBroadcasts, now: datetime) -> int:
    """The audience as it is now, written as recipients (the counts of who
    was left out kept on the broadcast)."""
    from app.api.events.models import Events

    event = await session.get(Events, b.event_id) if b.event_id else None
    if b.kind == EVENT_UPDATE and (event is None or event.is_deleted):
        b.status, b.finished_at, b.last_error = FAILED, now, "The event was removed before it went."
        return 0
    found = await audience(session, b.club_id, b.kind, event)
    for p in found.people:
        session.add(ClubWhatsAppBroadcastRecipients(
            broadcast_id=b.id, club_id=b.club_id, wa_id=p.wa_id, bsuid=p.bsuid, registration_id=p.registration_id,
            user_id=p.user_id, first_name=first_name(p.name), status=QUEUED, created_at=now,
        ))
    b.skipped_opted_out, b.skipped_no_consent, b.skipped_no_number = found.opted_out, found.no_consent, found.no_number
    b.materialized_at = now
    await session.flush()
    return len(found.people)


async def _claim(session, b: ClubWhatsAppBroadcasts, now: datetime) -> list:
    """The next people under the messaging limit, taken by this sender
    alone. Past the limit, everyone due waits for the day to free up."""
    R = ClubWhatsAppBroadcastRecipients
    await session.execute(select(func.pg_advisory_xact_lock(SEND_LOCK, b.club_id)))
    account = await session.get(ClubWhatsAppAccounts, b.account_id) if b.account_id else None
    limit = per_day(account.messaging_limit_tier if account is not None else None)
    room = BATCH
    if limit is not None:
        used, first = await used_today(session, b.club_id, now)
        room = min(BATCH, limit - used)
        if room <= 0:
            frees = (first or now) + DAY
            await session.execute(
                update(R).where(R.broadcast_id == b.id, R.status == QUEUED, or_(R.not_before.is_(None), R.not_before <= now))
                .values(not_before=frees, held=True).execution_options(synchronize_session=False)
            )
            await session.commit()
            return []
    # Two statements, not UPDATE ... WHERE id IN (SELECT ... LIMIT n FOR UPDATE SKIP LOCKED): with a plan
    # that scans that subquery again, the rows the UPDATE has just changed are skipped and the next n
    # come back, so it can take more than n -- more than the messaging limit leaves.
    ids = (await session.execute(
        select(R.id).where(R.broadcast_id == b.id, R.status == QUEUED, or_(R.not_before.is_(None), R.not_before <= now))
        .order_by(R.id).limit(room).with_for_update(skip_locked=True)
    )).scalars().all()
    if ids:
        await session.execute(
            update(R).where(R.id.in_(ids)).values(status=CLAIMED, claimed_at=now, tries=R.tries + 1)
            .execution_options(synchronize_session=False)
        )
    await session.commit()
    if not ids:
        return []
    return list((await session.scalars(select(R).where(R.id.in_(ids)).order_by(R.id).execution_options(populate_existing=True))).all())


class _Stop(Exception):
    """A refusal about the account or the template: the broadcast stops."""


async def _send_one(session, b: ClubWhatsAppBroadcasts, r: ClubWhatsAppBroadcastRecipients, keys: graph.Keys, row_t, definition: dict,
                    event, now: datetime) -> None:
    """One person: still wanting it, the template filled for them, sent, and
    the outcome on their row. _Stop when the account or template is refused."""
    if await _stopped(session, b.club_id, b.kind, r.wa_id, r.bsuid):
        r.status, r.skip_reason, r.status_at = SKIPPED, "opted_out", now
        return
    if b.kind == PROMOTION and not await consent.live(session, b.club_id, user_id=r.user_id, wa_id=r.wa_id, bsuid=r.bsuid):
        r.status, r.skip_reason, r.status_at = SKIPPED, "no_consent", now
        return
    try:
        components, _kept = templates.send_components(definition, params_for(b.variables or {}, name=r.first_name, event=event))
    except CustomHTTPException:
        r.status, r.error_code, r.error_title, r.status_at = RECIPIENT_FAILED, "VALUES", "A value for them was empty", now
        return
    to = {"to": r.wa_id} if r.wa_id else {"recipient": r.bsuid}
    body = {"messaging_product": "whatsapp", "recipient_type": "individual", **to, "type": "template",
            "template": {"name": row_t.name, "language": {"code": row_t.language}, "components": components}}
    for _ in range(2):
        path = f"{keys.phone_number_id}/{b.endpoint or ENDPOINT_MESSAGES}"
        try:
            wamid = await _post(keys, path, body)
        except _Unconfirmed as exc:
            r.status, r.error_code, r.error_title, r.status_at = RECIPIENT_FAILED, "UNCONFIRMED", str(exc)[:200], now
            return
        except graph.GraphError as exc:
            code = exc.code or ""
            if code == MARKETING_OFF_ON_CLOUD and b.category == "MARKETING" and b.endpoint != ENDPOINT_MARKETING:
                # Marketing is off on the Cloud API for this account: Meta's Marketing Messages API from now on.
                b.endpoint = ENDPOINT_MARKETING
                continue
            _refused(b, r, exc, now)
            if code == STOPPED:
                await _marketing_stopped(session, b.club_id, r.wa_id, r.bsuid)
            if _stops_broadcast(exc):
                raise _Stop(exc) from None
            return
        r.status, r.wamid, r.sent_at, r.status_at = SENT, wamid or None, now, now
        r.error_code = r.error_title = None
        return


def _stops_broadcast(exc: graph.GraphError) -> bool:
    code = exc.code or ""
    return (code in accounts.ACCOUNT_CODES or code in accounts.TEMPLATE_CODES
            or (code == "100" and "appsecret_proof" in (exc.message or "").lower()))


def _refused(b, r: ClubWhatsAppBroadcastRecipients, exc: graph.GraphError, now: datetime) -> None:
    """A refusal onto the person's row: "not now" tried again later (to
    MAX_TRIES), 131049 once more a day later, anything else failed."""
    code = exc.code or ""
    r.error_code = (code or None) and code[:16]
    r.error_title = (exc.message or "Meta refused it")[:200]
    r.status_at = now
    if code == CAPPED and not r.capped:
        r.capped, r.status, r.not_before = 1, QUEUED, now + DAY
        return
    if (code in RETRY_CODES or exc.unreachable) and not _stops_broadcast(exc) and r.tries < MAX_TRIES:
        r.status, r.not_before = QUEUED, now + backoff(r.tries)
        return
    r.status = RECIPIENT_FAILED


async def _stop(session, b: ClubWhatsAppBroadcasts, reason: str, now: datetime) -> None:
    R = ClubWhatsAppBroadcastRecipients
    b.status, b.finished_at, b.last_error = FAILED, now, reason[:300]
    await session.execute(update(R).where(R.broadcast_id == b.id, R.status.in_((QUEUED, CLAIMED)))
                          .values(status=SKIPPED, skip_reason="not_sent", status_at=now).execution_options(synchronize_session=False))


async def _again(session, b: ClubWhatsAppBroadcasts, at: datetime) -> None:
    """The broadcast's job, again at `at` (one per moment, however often asked)."""
    await jobs.enqueue(session, JOB, {"broadcast_id": b.id}, run_at=at, dedupe_key=f"{JOB}:{b.id}:{int(at.timestamp())}")


async def run(session, broadcast_id: int, *, now: Optional[datetime] = None, budget: float = BUDGET_SECONDS) -> dict:
    """The job: the broadcast's due people, a batch at a time, at the pace
    set, under the messaging limit, until nobody is due or its time is up;
    then its next job is written for whoever waits (the next day, a retry).
    {"sent", "failed", "skipped", "status"}."""
    clock = asyncio.get_running_loop().time
    started = clock()
    fixed_now = now
    counts = {"sent": 0, "failed": 0, "skipped": 0}
    b = await session.get(ClubWhatsAppBroadcasts, broadcast_id, populate_existing=True)
    if b is None or b.status not in (SCHEDULED, SENDING):
        return {**counts, "status": getattr(b, "status", None)}
    moment = fixed_now or _now()
    if b.scheduled_at is not None and b.status == SCHEDULED and b.scheduled_at > moment:
        await _again(session, b, b.scheduled_at)
        await session.commit()
        return {**counts, "status": b.status}
    if b.status == SCHEDULED:
        b.status = SENDING
    b.started_at = b.started_at or moment
    # The club's number as it is now (it may have connected another since).
    account = await accounts.live_account(session, b.club_id)
    if account is None or account.status != ACTIVE:
        await _stop(session, b, "Your WhatsApp number isn't connected and working, so it wasn't sent.", moment)
        await session.commit()
        return {**counts, "status": b.status}
    if not await accounts.plan_allows(session, b.club_id):
        await _stop(session, b, "Your plan no longer includes your own WhatsApp number.", moment)
        await session.commit()
        return {**counts, "status": b.status}
    b.account_id = account.id
    try:
        keys = accounts.keys_of(account)
    except crypto.Unavailable:
        await _stop(session, b, "MyOtherApp couldn't read your number's keys.", moment)
        await session.commit()
        return {**counts, "status": b.status}
    row_t = await templates.approved(session, account.id, b.template_name, b.template_language)
    if row_t is None:
        await _stop(session, b, f"The {b.template_name} template isn't approved any more.", moment)
        await session.commit()
        return {**counts, "status": b.status}
    from app.api.events.models import Events

    event = await session.get(Events, b.event_id) if b.event_id else None
    definition = templates.shape(row_t.components or [])
    if b.materialized_at is None:
        # Read once: a second runner (the sweep's resume beside a due job) waits here, then finds it read.
        await session.commit()
        await session.execute(select(func.pg_advisory_xact_lock(SEND_LOCK, b.club_id)))
        b.materialized_at = await session.scalar(select(ClubWhatsAppBroadcasts.materialized_at).where(ClubWhatsAppBroadcasts.id == b.id))
    if b.materialized_at is None:
        if not await _materialize(session, b, moment):
            if b.status == SENDING:
                b.status, b.finished_at = DONE, moment
            await session.commit()
            return {**counts, "status": b.status}
    if b.endpoint is None:
        b.endpoint = await endpoint_for(session, account, keys, b.category, moment)
    # A sender that died mid-claim: those may have gone, so they are not sent again.
    R = ClubWhatsAppBroadcastRecipients
    await session.execute(update(R).where(R.broadcast_id == b.id, R.status == CLAIMED, R.claimed_at < moment - STALE_CLAIM)
                          .values(status=RECIPIENT_FAILED, error_code="UNCONFIRMED",
                                  error_title="The sender stopped while sending; it may have gone").execution_options(synchronize_session=False))
    await session.commit()
    pace = 1.0 / float(settings.CLUB_WHATSAPP_BROADCAST_MPS) if (settings.CLUB_WHATSAPP_BROADCAST_MPS or 0) > 0 else 0.0
    stopped = None
    while clock() - started < budget:
        moment = fixed_now or _now()
        b = await session.get(ClubWhatsAppBroadcasts, broadcast_id, populate_existing=True)
        if b is None or b.status != SENDING:
            return {**counts, "status": getattr(b, "status", None)}
        batch = await _claim(session, b, moment)
        if not batch:
            break
        for r in batch:
            moment = fixed_now or _now()
            try:
                await _send_one(session, b, r, keys, row_t, definition, event, moment)
            except _Stop as exc:
                stopped = exc.args[0]
            await session.commit()
            if r.status in (SENT, SKIPPED, RECIPIENT_FAILED):
                counts[{SENT: "sent", SKIPPED: "skipped"}.get(r.status, "failed")] += 1
            if stopped is not None:
                break
            if pace:
                await asyncio.sleep(pace)
        if stopped is not None:
            break
    moment = fixed_now or _now()
    b = await session.get(ClubWhatsAppBroadcasts, broadcast_id, populate_existing=True)
    if stopped is not None:
        account = await session.get(ClubWhatsAppAccounts, b.account_id)
        if stopped.code in accounts.TEMPLATE_CODES:
            reason = f"Meta refused the {b.template_name} template ({stopped.code}): {stopped.message}"
        else:
            reason = f"Meta refused your WhatsApp account ({stopped.code}): {stopped.message}"
            if account is not None:
                _status, _code, words = graph.explain(stopped)
                await accounts.mark_failed(session, account, words, stopped.code)
        await _stop(session, b, reason, moment)
        await session.commit()
        return {**counts, "status": b.status}
    if b.status != SENDING:
        return {**counts, "status": b.status}
    waiting = await session.scalar(select(func.min(func.coalesce(R.not_before, moment))).where(R.broadcast_id == b.id, R.status == QUEUED))
    if waiting is None:
        b.status, b.finished_at = DONE, moment
    else:
        await _again(session, b, max(waiting, moment))
    await session.commit()
    return {**counts, "status": b.status}


# Paced (CLUB_WHATSAPP_BROADCAST_MPS), so a big one holds its slot for most of an hour: one slot per runner at most.
@jobs.handler(JOB, max_attempts=6, timeout=3600.0, concurrency=1)
async def _job(payload: dict) -> None:
    async with jobs.scope() as session:
        await run(session, int(payload["broadcast_id"]))


# ─── What Meta says became of it (webhook.py) ────────────────────────────


async def record_status(session, account: ClubWhatsAppAccounts, st: dict) -> bool:
    """A status for a broadcast's message: forward only (a late "delivered"
    never undoes "read"); a failure with Meta's code. 131049 puts the person
    back for one more try a day later; 131050 records that they stopped the
    club's marketing. True when it was a broadcast's."""
    R = ClubWhatsAppBroadcastRecipients
    wamid = str(st.get("id") or "")
    status = str(st.get("status") or "").lower()
    if not wamid or status not in (SENT, DELIVERED, READ, RECIPIENT_FAILED):
        return False
    r = await session.scalar(select(R).where(R.wamid == wamid, R.club_id == account.club_id))
    if r is None:
        return False
    at = inbox._ts(st.get("timestamp")) or _now()
    if status != RECIPIENT_FAILED:
        before = tuple(s for s, n in STATUS_ORDER.items() if n < STATUS_ORDER[status])
        await session.execute(update(R).where(R.id == r.id, R.status.in_(before)).values(status=status, status_at=at)
                              .execution_options(synchronize_session=False))
        return True
    error = (st.get("errors") or [None])[0] if isinstance(st.get("errors"), list) else None
    error = error if isinstance(error, dict) else {}
    code = None if error.get("code") is None else str(error.get("code"))[:16]
    detail = (error.get("error_data") or {}).get("details") if isinstance(error.get("error_data"), dict) else None
    title = str(detail or error.get("title") or error.get("message") or "WhatsApp couldn't deliver it")[:200]
    if r.status not in (CLAIMED, SENT):
        return True
    if code == CAPPED and not r.capped:
        # Meta's per-person marketing limit: once more, not before a day has passed.
        r.status, r.capped, r.not_before, r.wamid, r.error_code, r.error_title, r.status_at = QUEUED, 1, at + DAY, None, code, title, at
        b = await session.get(ClubWhatsAppBroadcasts, r.broadcast_id)
        if b is not None and b.status in (SENDING, DONE):
            b.status, b.finished_at = SENDING, None
            await _again(session, b, at + DAY)
        return True
    r.status, r.error_code, r.error_title, r.status_at = RECIPIENT_FAILED, code, title, at
    if code == STOPPED:
        await _marketing_stopped(session, account.club_id, r.wa_id, r.bsuid)
    return True


# ─── The sweep ───────────────────────────────────────────────────────────


async def resume(session, now: Optional[datetime] = None) -> int:
    """Broadcasts that should be sending and have no job (one gave up): their
    job again. Returns how many were written."""
    now = now or _now()
    B, R = ClubWhatsAppBroadcasts, ClubWhatsAppBroadcastRecipients
    due = (await session.scalars(select(B).where(or_(
        B.status == SENDING, (B.status == SCHEDULED) & (B.scheduled_at <= now),
    )).limit(200))).all()
    written = 0
    for b in due:
        if b.materialized_at is not None:
            nxt = await session.scalar(select(func.min(func.coalesce(R.not_before, now))).where(R.broadcast_id == b.id, R.status == QUEUED))
            if nxt is None or nxt > now:
                continue
        await jobs.enqueue(session, JOB, {"broadcast_id": b.id}, dedupe_key=f"{JOB}:{b.id}:resume")
        written += 1
    await session.commit()
    return written


async def forget(session, now: Optional[datetime] = None) -> int:
    """The people of each finished broadcast older than its club's retention
    period, deleted -- the counts kept on the broadcast. How many."""
    from app.api.club_whatsapp import sweep

    now = now or _now()
    B, R = ClubWhatsAppBroadcasts, ClubWhatsAppBroadcastRecipients
    deleted = 0
    for club_id in (await session.scalars(select(func.distinct(B.club_id)).where(B.finished_at.isnot(None), B.results.is_(None)))).all():
        try:
            latest = await accounts.latest_account(session, club_id)
            cutoff = sweep.retention_cutoff(latest.retention_months if latest is not None else None, now)
            for b in (await session.scalars(select(B).where(B.club_id == club_id, B.finished_at < cutoff, B.results.is_(None)))).all():
                b.results = await results(session, b, now)
                gone = await session.execute(R.__table__.delete().where(R.broadcast_id == b.id))
                deleted += int(gone.rowcount or 0)
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - one club never stops the rest
            logger.warning("whatsapp broadcasts: retention failed for club %s (%s)", club_id, type(exc).__name__)
            await session.rollback()
    return deleted
