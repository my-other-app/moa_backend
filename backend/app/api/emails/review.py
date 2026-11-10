"""An event's own registration review emails, and sending one again
(feature-register/event-review-emails-contract.md, docs/APPROVALS.md §11).

The six review emails (REQUEST_*) are the club's library templates; an event
may hold its own copy of any of them (`email_templates` with `event_id` set,
one per stage: uq_email_templates_event_stage). Whatever sends one reads the
event's copy, else the club's, else MyOtherApp's (`service.request_template`).

A club that changed the words may send them to the people the email already
went to: a resend. It is the club's choice, so it is counted against the
club's emails as an announcement is (a typed message to the guests), and it
goes by email only, never as a push. Who it reaches is decided twice: when it
is pressed (the rows are written then, `start`) and again for each person
just before their copy goes (`_still_due`): anyone whose registration moved
on in between -- confirmed, declined, withdrawn, paid -- is skipped.

Each person once per resend: a row per (resend, request), claimed before it
is sent as a message delivery is (app/api/deliveries/pipeline.py), with the
provider's idempotency key `resend:{resend_id}:{request_id}`. The same
`clientRef` pressed twice is the same resend, and mails nobody again.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from app.api.approvals.models import APPROVED, DECLINED, LAPSED, PENDING, RegistrationRequests
from app.api.deliveries import pipeline
from app.api.deliveries import service as deliveries
from app.api.emails import service
from app.api.emails.models import (
    RESEND_DONE,
    RESEND_SENDING,
    EmailStage,
    EmailTemplates,
    ReviewResendRecipients,
    ReviewResends,
)
from app.api.events.models import EventRegistrationsLink, Events
from app.api.message_quota import service as quota
from app.core import jobs
from app.core.email import email as email_mod
from app.core.email import transport
from app.core.mail import suppression
from app.core.mail.schema import EMAIL_STAGE_META, REQUEST_STAGES, EmailAttachments, EmailDesign, EmailTemplateFields
from app.core.mail.starters import email_starter
from app.core.utils.db_fields import TZAwareDateTime
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

R = RegistrationRequests
G = EventRegistrationsLink
RR = ReviewResendRecipients

#: The six, in the library's order.
STAGES: list[EmailStage] = [EmailStage(s) for s in REQUEST_STAGES]
#: The ones a club may send again. Not REQUEST_UNANSWERED ("Closed before
#: review"): it goes once, by itself, to requests closed because the event
#: ended or was deleted -- closed requests are never mailed again, and the
#: event it is about is over.
RESENDABLE = frozenset({
    EmailStage.REQUEST_RECEIVED, EmailStage.REQUEST_APPROVED, EmailStage.REQUEST_APPROVED_PAY,
    EmailStage.REQUEST_DECLINED, EmailStage.REQUEST_LAPSED,
})
#: Who each reaches, as the dashboard says it.
WHO = {
    EmailStage.REQUEST_RECEIVED: "everyone still waiting for review",
    EmailStage.REQUEST_APPROVED: "everyone confirmed who has their ticket",
    EmailStage.REQUEST_APPROVED_PAY: "everyone confirmed who hasn't paid yet, while their hold lasts",
    EmailStage.REQUEST_DECLINED: "everyone you declined",
    EmailStage.REQUEST_LAPSED: "everyone whose confirmation expired unpaid",
}
#: What a resend is to the club's quota: what a typed message to the guests
#: (POST /events/{id}/messages) is (emails.service.quota_kind).
QUOTA_KIND = "announcement"
#: The job that sends a resend.
JOB = "emails.review_resend"
#: Written on a row whose person moved on between the press and their turn.
MOVED_ON = "not sent: their registration moved on since this was sent"
#: Past this, a resend still unfinished is given up (as a message's is).
RESUME_WINDOW = service.RESUME_WINDOW
#: How often a long resend writes its counts back.
PROGRESS_EVERY = service.PROGRESS_EVERY


# ─── The stage ───────────────────────────────────────────────────────────


def stage_of(raw: str) -> EmailStage:
    """The review email a path names (`REQUEST_DECLINED`, any case), or 404."""
    try:
        stage = EmailStage((raw or "").strip().upper())
    except ValueError:
        stage = None
    if stage not in STAGES:
        raise CustomHTTPException(404, message="No such registration review email.", error_code="NOT_A_REVIEW_EMAIL")
    return stage


# ─── The templates ───────────────────────────────────────────────────────


async def event_copy(session, event: Events, stage: EmailStage) -> Optional[EmailTemplates]:
    return await session.scalar(
        select(EmailTemplates).where(EmailTemplates.event_id == event.id, EmailTemplates.stage == stage).limit(1)
    )


async def _event_copies(session, event: Events) -> dict:
    rows = await session.scalars(
        select(EmailTemplates).where(EmailTemplates.event_id == event.id, EmailTemplates.stage.in_(STAGES))
    )
    return {t.stage: t for t in rows}


def _untouched(tpl: EmailTemplates) -> bool:
    """A library email still in MyOtherApp's words."""
    starter = email_starter(tpl.stage.value)
    fields = service.fields_of(tpl)
    return fields.subject == starter.subject and fields.design.model_dump(by_alias=True) == starter.design.model_dump(by_alias=True)


async def _club_side(session, event: Events) -> dict:
    """The club's library emails of the six, made from the starters the
    first time (ensure_library: the caller commits)."""
    library = await service.ensure_library(session, event.club_id)
    return {t.stage: t for t in library if t.stage in STAGES}


def _source(stage: EmailStage, own: dict, club: dict) -> tuple[str, EmailTemplates]:
    if stage in own:
        return "event", own[stage]
    tpl = club[stage]
    return ("default" if _untouched(tpl) else "club"), tpl


async def save(session, event: Events, stage: EmailStage, fields: EmailTemplateFields, *, created_by=None) -> EmailTemplates:
    """The event's own copy of the stage, made by the first save (from the
    club's version) and changed by the next. Words only, as the library's:
    always on, no timer, no audience, the ticket only on the ticket email."""
    if event.club_id is None:
        raise CustomHTTPException(422, message="This event has no club, so it has no emails to send from.")
    fields = service.request_fixed(fields, stage)
    service.validate_fields(fields, event_id=event.id, club_id=event.club_id, stage=stage)
    tpl = await event_copy(session, event, stage)
    if tpl is not None:
        return await service.update_template(session, tpl, fields)
    club = {t.stage: t for t in await service.ensure_library(session, event.club_id)}
    source = club.get(stage)
    tpl = service._new(event.club_id, event.id, stage, fields, source_id=source.id if source is not None else None, created_by=created_by)
    session.add(tpl)
    try:
        await session.commit()
    except IntegrityError:
        # The other tab's first save, a moment ago: this one changes it.
        await session.rollback()
        tpl = await event_copy(session, event, stage)
        if tpl is None:
            raise
        return await service.update_template(session, tpl, fields)
    return tpl


async def drop(session, event: Events, stage: EmailStage) -> tuple[str, EmailTemplates]:
    """The event goes back to the club's words for the stage. Nothing to
    drop is no error. Answers what the event uses now."""
    tpl = await event_copy(session, event, stage)
    if tpl is not None:
        await session.delete(tpl)
        await session.flush()
    club = await _club_side(session, event)
    await session.commit()
    return _source(stage, {}, club)


# ─── Who a resend reaches ────────────────────────────────────────────────


def _audience(event_id: int, stage: EmailStage, now: datetime):
    """(select of request ids, the address their email goes to, the
    address's bounce stamp or None): the people the stage's email is true
    for now. None for a stage that is never sent again."""
    from app.api.ticketing.models import CLAIMED, OFFERED, WaitlistEntries as W

    if stage is EmailStage.REQUEST_RECEIVED:
        q = select(R.id).where(R.event_id == event_id, R.status == PENDING)
        return q, R.email, None
    if stage is EmailStage.REQUEST_APPROVED_PAY:
        # Confirmed, owing the payment, the hold still standing (notify's own test).
        q = (
            select(R.id).join(W, W.id == R.waitlist_entry_id)
            .where(
                R.event_id == event_id, R.status == APPROVED, W.status == OFFERED,
                or_(W.offer_expires_at.is_(None), W.offer_expires_at > now),
            )
        )
        return q, R.email, None
    if stage is EmailStage.REQUEST_APPROVED:
        # Confirmed and holding the ticket: free, or paid (the hold claimed).
        q = (
            select(R.id).join(G, G.id == R.registration_id).outerjoin(W, W.id == R.waitlist_entry_id)
            .where(
                R.event_id == event_id, R.status == APPROVED, G.is_deleted == False,  # noqa: E712
                G.ticket_revoked_at.is_(None), or_(R.waitlist_entry_id.is_(None), W.status == CLAIMED),
            )
        )
        return q, G.email, G.email_bounced_at
    if stage is EmailStage.REQUEST_DECLINED:
        return select(R.id).where(R.event_id == event_id, R.status == DECLINED), R.email, None
    if stage is EmailStage.REQUEST_LAPSED:
        return select(R.id).where(R.event_id == event_id, R.status == LAPSED), R.email, None
    return None


def _no_email(address):
    return or_(address.is_(None), func.trim(address) == "")


def _reachable(address, bounced):
    """An address, never bounced here, on no list of the platform's."""
    out = [~_no_email(address), suppression.not_suppressed(address)]
    if bounced is not None:
        out.append(bounced.is_(None))
    return out


async def count(session, event: Events, stage: EmailStage, now: Optional[datetime] = None) -> dict:
    """{eligible, without_email}: who a resend would reach now, and those it
    can't for want of an address. Nothing for a stage never sent again."""
    if stage not in RESENDABLE:
        return {"eligible": 0, "without_email": 0}
    q, address, bounced = _audience(event.id, stage, now or pipeline.now())
    eligible = await session.scalar(select(func.count()).select_from(q.where(*_reachable(address, bounced)).subquery()))
    without = await session.scalar(select(func.count()).select_from(q.where(_no_email(address)).subquery()))
    return {"eligible": int(eligible or 0), "without_email": int(without or 0)}


# ─── The section ─────────────────────────────────────────────────────────


async def _latest_resends(session, event: Events) -> dict:
    rows = await session.scalars(
        select(ReviewResends).where(ReviewResends.event_id == event.id).order_by(ReviewResends.created_at.desc())
    )
    out: dict = {}
    for r in rows:
        out.setdefault(r.stage, r)
    return out


async def overview(session, event: Events) -> list[dict]:
    """The six, in order: which words the event uses, who a resend would
    reach and the last one. Makes the club's library emails the first time
    (the route commits)."""
    if event.club_id is None:
        raise CustomHTTPException(422, message="This event has no club, so it has no emails to send from.")
    own = await _event_copies(session, event)
    club = await _club_side(session, event)
    last = await _latest_resends(session, event)
    now = pipeline.now()
    out = []
    for stage in STAGES:
        source, tpl = _source(stage, own, club)
        meta = EMAIL_STAGE_META[stage.value]
        out.append({
            "stage": stage.value,
            "label": meta["label"],
            "when": meta.get("when"),
            "who": WHO.get(stage),
            "source": source,
            "template": service.template_out(tpl),
            "club_template_id": club[stage].id if stage in club else None,
            "resendable": stage in RESENDABLE,
            "eligible": (await count(session, event, stage, now))["eligible"],
            "last_resend": resend_out(last[stage.value]) if stage.value in last else None,
        })
    return out


# ─── Pressing "Send to N people" ─────────────────────────────────────────


async def existing(session, event: Events, client_ref) -> Optional[ReviewResends]:
    return await session.scalar(select(ReviewResends).where(ReviewResends.event_id == event.id, ReviewResends.client_ref == client_ref))


async def start(session, event: Events, stage: EmailStage, *, client_ref, created_by=None, created_by_name=None) -> ReviewResends:
    """The resend, one pending row per person it is for, and the job that
    sends them, in one transaction. A clientRef the event has seen answers
    that resend and writes nothing (two at once: the unique index decides).
    Refused whole (402 MESSAGE_QUOTA_EXCEEDED) when the club's emails left
    this month, plan and credits, can't cover everyone; the per-person check
    at send time stays, for sends racing it."""
    earlier = await existing(session, event, client_ref)
    if earlier is not None:
        return earlier
    if stage not in RESENDABLE:
        raise CustomHTTPException(
            422, message="This email goes once, by itself, and can't be sent again.", error_code="NOT_RESENDABLE",
        )
    if not getattr(event, "requires_approval", False):
        raise CustomHTTPException(
            422, message="These emails only go out for events that review registrations.", error_code="NOT_REVIEWED",
        )
    now = pipeline.now()
    q, address, bounced = _audience(event.id, stage, now)
    eligible = (await count(session, event, stage, now))["eligible"]
    if not eligible:
        raise CustomHTTPException(422, message="Nobody to send this to right now.", error_code="NOBODY")
    await _ensure_room(session, event, needed=eligible, at=now)
    own = await event_copy(session, event, stage)
    if own is not None:
        source, tpl = "event", own
    else:
        source, tpl = _source(stage, {}, await _club_side(session, event))
    fields = service.request_fixed(service.fields_of(tpl), stage)
    resend = ReviewResends(
        id=uuid.uuid4(), event_id=event.id, stage=stage.value, client_ref=client_ref, source=source, template_id=tpl.id,
        template_version=tpl.version or 1, subject=fields.subject, design=fields.design.model_dump(by_alias=True),
        status=RESEND_SENDING, recipients=0, sent_count=0, failed_count=0, skipped_count=0,
        created_by=created_by, created_by_name=(created_by_name or None) and created_by_name[:120], created_at=now, updated_at=now,
    )
    session.add(resend)
    try:
        await session.flush()
        table = RR.__table__
        rows = q.where(*_reachable(address, bounced)).with_only_columns(
            func.gen_random_uuid(), literal(resend.id), R.id, literal("pending"), literal(0),
            literal(now, TZAwareDateTime()), literal(now, TZAwareDateTime()),
        )
        inserted = await session.execute(
            pg_insert(table).from_select(["id", "resend_id", "request_id", "status", "attempts", "created_at", "updated_at"], rows)
            .on_conflict_do_nothing()
        )
        resend.recipients = inserted.rowcount or 0
        if not resend.recipients:
            # The last of them moved on a moment ago.
            await session.rollback()
            raise CustomHTTPException(422, message="Nobody to send this to right now.", error_code="NOBODY")
        await enqueue(session, resend.id)
        await session.commit()
    except IntegrityError:
        # The same clientRef, committed a moment ago by the other click.
        await session.rollback()
        earlier = await existing(session, event, client_ref)
        if earlier is None:
            raise
        return earlier
    return resend


#: The refusal when the club's emails can't cover a whole resend.
QUOTA_CODE = "MESSAGE_QUOTA_EXCEEDED"


async def room_for(session, club_id: Optional[int], at: Optional[datetime] = None) -> Optional[int]:
    """How many club emails can still go this month: what is left of the
    plan's allowance plus the credits bought or granted. None: no limit (no
    plan limit, or limits not enforced). The same arithmetic as
    quota.held_back, for a whole resend at once."""
    from app.api.message_quota import credits
    from app.api.message_quota.models import MessageUsage
    from app.api.subscriptions import gates

    if club_id is None or not gates.enforcing():
        return None
    terms = await quota.terms_for(session, club_id)
    limit = terms.limit(quota.EMAIL)
    if limit is None:
        return None
    used = await session.scalar(
        select(MessageUsage.plan_used).where(
            MessageUsage.club_id == club_id, MessageUsage.period == quota.period_of(at), MessageUsage.channel == quota.EMAIL,
        )
    )
    return max(0, limit - int(used or 0)) + await credits.bought_remaining(session, club_id, quota.EMAIL, at)


async def _ensure_room(session, event: Events, *, needed: int, at: datetime) -> None:
    """Refused as a whole when the club's emails can't cover everyone: a
    resend held back person by person would reach some and not others."""
    room = await room_for(session, event.club_id, at)
    if room is None or room >= needed:
        return
    raise CustomHTTPException(
        402,
        message=(
            f"This would send {needed:,} emails and your club has {room:,} left this month. "
            "Buy credits to send it, or send it next month."
        ),
        error_code=QUOTA_CODE,
        errors={"needed": needed, "available": room},
    )


async def enqueue(session, resend_id) -> None:
    await jobs.enqueue(session, JOB, {"resend_id": str(resend_id)}, dedupe_key=f"emails:review-resend:{resend_id}")


async def list_resends(session, event: Events) -> list[ReviewResends]:
    return list(await session.scalars(
        select(ReviewResends).where(ReviewResends.event_id == event.id).order_by(ReviewResends.created_at.desc(), ReviewResends.id)
    ))


def resend_out(r: ReviewResends) -> dict:
    return {
        "id": r.id, "stage": r.stage, "created_at": r.created_at,
        "created_by": {"id": r.created_by, "name": r.created_by_name} if (r.created_by is not None or r.created_by_name) else None,
        "recipients": r.recipients or 0, "sent": r.sent_count or 0, "failed": r.failed_count or 0, "skipped": r.skipped_count or 0,
        "status": r.status,
    }


# ─── Sending ─────────────────────────────────────────────────────────────


@dataclass
class _Run:
    """What every person's copy of one resend shares, read once."""

    resend_id: uuid.UUID
    stage: EmailStage
    event: Events
    subject: str
    design: EmailDesign
    brand: str
    logo_url: Optional[str]
    org_name: str
    assets: dict = field(default_factory=dict)
    terms: Optional[quota.Terms] = None
    quota_terms: dict = field(default_factory=dict)


@dataclass
class _Claim:
    id: uuid.UUID
    request_id: uuid.UUID
    claimed_at: datetime
    attempts: int


async def _run_of(db: pipeline.Db, resend_id) -> Optional[_Run]:
    async with db.tx() as s:
        r = await s.get(ReviewResends, resend_id)
        if r is None:
            return None
        event = await s.scalar(
            select(Events).options(joinedload(Events.club), selectinload(Events.cohost_clubs))
            .where(Events.id == r.event_id).execution_options(include_deleted=True)
        )
        if event is None:
            return None
        club = getattr(event, "club", None)
        return _Run(
            resend_id=r.id, stage=EmailStage(r.stage), event=event, subject=r.subject,
            design=EmailDesign.model_validate(r.design or {}), brand=await service.brand_primary(s, event),
            logo_url=service.logo_url_of(event), org_name=(club.name if club else "") or "",
        )


async def _claim(db: pipeline.Db, resend_id, n: int) -> list[_Claim]:
    at = pipeline.now()
    picked = (
        select(RR.id)
        .where(RR.resend_id == resend_id, RR.status == "pending", or_(RR.next_attempt_at.is_(None), RR.next_attempt_at <= at))
        .limit(n)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            update(RR)
            .where(RR.id.in_(picked), RR.status == "pending")
            .values(status="sending", claimed_at=at, attempts=RR.attempts + 1, updated_at=at)
            .returning(RR.id, RR.request_id, RR.attempts)
            .execution_options(synchronize_session=False)
        )
    ).all()
    return [_Claim(id=r[0], request_id=r[1], claimed_at=at, attempts=r[2]) for r in rows]


async def _release(db: pipeline.Db, claims: list) -> None:
    if not claims:
        return
    await db.execute(
        update(RR)
        .where(RR.id.in_([c.id for c in claims]), RR.status == "sending")
        .values(status="pending", claimed_at=None, attempts=RR.attempts - 1, updated_at=pipeline.now())
        .execution_options(synchronize_session=False)
    )


def key_of(resend_id, request_id) -> str:
    """The provider's idempotency key, and the quota's: one person once a resend."""
    return f"resend:{resend_id}:{request_id}"


async def _record(db: pipeline.Db, run: _Run, c: _Claim, values: dict, *, dead: bool = False) -> None:
    """One short transaction: the row's result and, once it was sent, the
    email counted against the club's (once: by its key)."""
    async with db.tx() as s:
        res = await s.execute(
            update(RR)
            .where(RR.id == c.id, RR.status == "sending", RR.claimed_at == c.claimed_at)
            .values(claimed_at=None, updated_at=pipeline.now(), **values)
            .execution_options(synchronize_session=False)
        )
        if not res.rowcount:
            logger.warning("review resend row %s: the result came after its claim was given up", c.id)
        if dead and res.rowcount:
            await pipeline.alert_dead(
                s, event_id=run.event.id, club_id=run.event.club_id, event_name=run.event.name, what="review email", error=values.get("error") or "",
            )
        if values.get("status") == "sent" and run.event.club_id is not None:
            await quota.count(s, club_id=run.event.club_id, channel=quota.EMAIL, kind=QUOTA_KIND, ref=key_of(run.resend_id, c.request_id), terms=run.terms)


@dataclass
class _Ready:
    req: RegistrationRequests
    address: str
    reg: Optional[EventRegistrationsLink] = None
    entry: object = None
    type_name: Optional[str] = None


async def _still_due(s, run: _Run, c: _Claim) -> tuple[Optional[_Ready], Optional[str]]:
    """The person as they are now, if the stage's email is still true for
    them, or why they are skipped."""
    from app.api.ticketing import types as types_mod
    from app.api.ticketing.models import WaitlistEntries

    q, address, bounced = _audience(run.event.id, run.stage, pipeline.now())
    found = (await s.execute(q.add_columns(address, bounced if bounced is not None else literal(None)).where(R.id == c.request_id))).first()
    if found is None:
        return None, MOVED_ON
    _id, addr, bounced_at = found
    if not (addr or "").strip():
        return None, "no email address"
    if bounced_at is not None:
        return None, "that address bounced — correct it to send again"
    listed = (await suppression.reasons(s, [addr])).get(suppression.normal(addr))
    if listed is not None:
        return None, suppression.why(listed)
    req = await s.scalar(select(R).where(R.id == c.request_id).execution_options(populate_existing=True))
    ready = _Ready(req=req, address=addr.strip())
    if run.stage is EmailStage.REQUEST_APPROVED:
        ready.reg = await s.scalar(
            select(G).options(
                joinedload(G.event).joinedload(Events.club), joinedload(G.event).selectinload(Events.cohost_clubs), joinedload(G.user),
            ).where(G.id == req.registration_id)
        )
        if ready.reg is None:
            return None, MOVED_ON
    elif run.stage is EmailStage.REQUEST_APPROVED_PAY:
        ready.entry = await s.get(WaitlistEntries, req.waitlist_entry_id)
    if req.ticket_type_id is not None:
        ready.type_name = (await types_mod.names(s, [req.ticket_type_id])).get(req.ticket_type_id)
    return ready, None


async def _compose(db: pipeline.Db, run: _Run, ready: _Ready) -> service.Outgoing:
    if run.stage is EmailStage.REQUEST_APPROVED:
        # The "Confirmed · ticket" email, ticket attached (request_ticket_for's shape).
        return await service.compose(
            db, ready.reg.event, ready.reg, run.subject, run.design, EmailAttachments(ticket_pdf=True, certificate_pdf=False),
            brand=run.brand, logo_url=run.logo_url, cache=run.assets,
        )
    from app.api.approvals import notify

    values = notify._values(run.event, ready.req, ready.type_name, entry=ready.entry)
    return await service.compose(
        db, run.event, None, run.subject, run.design, EmailAttachments(), brand=run.brand, logo_url=run.logo_url, values=values, cache=run.assets,
    )


async def _handle(db: pipeline.Db, run: _Run, c: _Claim) -> None:
    """Worker: one person. Checked again, sent once, recorded."""
    async with db.tx() as s:
        ready, skip = await _still_due(s, run, c)
        if skip is None and run.event.club_id is not None:
            # Past the club's emails for the month (and no credits): held back, as an announcement is.
            run.terms, skip = await quota.check(s, run.event.club_id, quota.EMAIL, QUOTA_KIND, terms=run.terms, cache=run.quota_terms)
    if skip is not None:
        await _record(db, run, c, dict(status="skipped", error=skip[:400], next_attempt_at=None))
        return
    try:
        out = await _compose(db, run, ready)
        response = await pipeline.send_with_backoff(
            email_mod.send_email, recipients=[ready.address], subject=out.subject, text=out.text, html=out.html,
            attachments=out.attachments, sender_name=run.org_name or None, idempotency_key=key_of(run.resend_id, c.request_id),
            check_suppression=False,
        )
    except transport.MailDeferred as exc:
        error = (str(exc) or exc.__class__.__name__)[:300]
        if c.attempts >= pipeline.max_tries():
            await _record(db, run, c, dict(status="dead", error=f"gave up after {c.attempts} tries: {error}"[:400], next_attempt_at=None), dead=True)
        else:
            await _record(db, run, c, dict(status="pending", error=f"will try again: {error}"[:400], next_attempt_at=pipeline.now() + pipeline.backoff(c.attempts)))
        return
    except Exception as exc:  # noqa: BLE001 - the row says what happened
        error = (str(exc) or exc.__class__.__name__)[:400]
        logger.warning("review resend %s to %s failed: %s", run.resend_id, c.request_id, error)
        await _record(db, run, c, dict(status="failed", error=error, next_attempt_at=None))
        return
    response = response or {}
    await _record(db, run, c, dict(status="sent", provider_id=response.get("MessageId"), sent_at=pipeline.now(), error=None, next_attempt_at=None))


async def finish(db: pipeline.Db, resend_id) -> Optional[ReviewResends]:
    """The counts as they stand, and done once nothing is left to send."""
    async with db.tx() as s:
        counts = dict((await s.execute(select(RR.status, func.count()).where(RR.resend_id == resend_id).group_by(RR.status))).all())
        values = dict(
            sent_count=counts.get("sent", 0), failed_count=counts.get("failed", 0) + counts.get("dead", 0),
            skipped_count=counts.get("skipped", 0), updated_at=pipeline.now(),
        )
        if not counts.get("pending", 0) and not counts.get("sending", 0):
            values["status"] = RESEND_DONE
        await s.execute(update(ReviewResends).where(ReviewResends.id == resend_id).values(**values).execution_options(synchronize_session=False))
        return await s.get(ReviewResends, resend_id, populate_existing=True)


async def _run_resend(resend_id, session=None) -> Optional[ReviewResends]:
    db = deliveries._db(session)
    run = await _run_of(db, resend_id)
    if run is None:
        return None
    last = time.monotonic()

    async def claim(n: int):
        nonlocal last
        if time.monotonic() - last >= PROGRESS_EVERY:
            last = time.monotonic()
            await finish(db, resend_id)
        return await _claim(db, resend_id, n)

    await pipeline.pump(claim, lambda c: _handle(db, run, c), release=lambda claims: _release(db, claims))
    return await finish(db, resend_id)


async def run_resend(resend_id) -> None:
    """One resend, sent in this process unless it already is."""
    await pipeline.run(("review_resend", resend_id), lambda: _run_resend(resend_id))


@jobs.handler(JOB, max_attempts=6, timeout=3600.0, concurrency=2)
async def _resend_job(payload: dict) -> None:
    """Send what is left of one resend. Each person's row is claimed before
    their copy goes, so the job running twice mails nobody twice."""
    await run_resend(uuid.UUID(str(payload["resend_id"])))


async def resume(session, now: Optional[datetime] = None) -> dict:
    """The minute sweep's look at resends (emails.service.resume): claims of
    workers that died put back, rows tried too often given up, what was cut
    off started again, and what is now too late left alone."""
    now = now or pipeline.now()
    db = deliveries._db(session)
    stale = (await db.execute(
        update(RR).where(RR.status == "sending", RR.claimed_at < now - pipeline.STALE_CLAIM)
        .values(status="pending", claimed_at=None, next_attempt_at=now, updated_at=now)
        .returning(RR.id).execution_options(synchronize_session=False)
    )).all()
    dead = (await db.execute(
        update(RR).where(RR.status == "pending", RR.attempts >= pipeline.max_tries())
        .values(status="dead", error=f"gave up after {pipeline.max_tries()} tries", updated_at=now)
        .returning(RR.id).execution_options(synchronize_session=False)
    )).all()
    unfinished = (await db.execute(select(ReviewResends.id, ReviewResends.created_at).where(ReviewResends.status == RESEND_SENDING))).all()
    started = late = 0
    for resend_id, created_at in unfinished:
        if created_at < now - RESUME_WINDOW:
            await db.execute(
                update(RR).where(RR.resend_id == resend_id, RR.status.in_(("pending", "sending")))
                .values(status="failed", error="not sent: too late to send by the time it could be — send it again if it still matters", claimed_at=None, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            await finish(db, resend_id)
            late += 1
        elif not pipeline.running(("review_resend", resend_id)):
            async with db.tx() as s:
                await enqueue(s, resend_id)
            started += 1
    return {"stale": len(stale), "dead": len(dead), "started": started, "late": late}
