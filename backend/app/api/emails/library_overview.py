"""The club's email library, as the Emails page needs it.

The four journey emails and any custom ones, each with how it is set to go
out, how many events carry it, and what it has done — sends, delivered,
opened, bounced — across the club's events. Above them, the month in
figures and what goes out next by itself.
"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import INTERVAL
from sqlalchemy.orm import joinedload

from app.api.clubs.models import Clubs
from app.api.deliveries.models import DeliveryOutcome, DeliveryStatus
from app.api.emails.models import EmailStage, EmailTemplates, MessageDeliveries, Messages
from app.api.emails.service import due_at, ensure_library, fields_of, template_out
from app.api.events.models import Events

WINDOW = timedelta(days=30)
STAGE_LABEL = {"INVITE": "Invite", "PRE_EVENT": "Pre-event", "REMINDER": "Reminder", "POST_EVENT": "Post-event", "CUSTOM": "Custom"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _messages(session, club_id: int) -> list[tuple[Messages, Events]]:
    return list((await session.execute(
        select(Messages, Events).join(Events, Events.id == Messages.event_id).where(Events.club_id == club_id, Events.is_deleted == False).order_by(Messages.created_at.desc())  # noqa: E712
    )).all())


async def _outcomes(session, message_ids: list) -> dict:
    """Per message: delivered, read, bounced or complained — where the provider told us; sent/failed where it did not."""
    out = {m: dict(delivered=0, read=0, bounced=0, failed=0, sent=0) for m in message_ids}
    if not message_ids:
        return out
    q = (
        select(MessageDeliveries.message_id, MessageDeliveries.status, MessageDeliveries.outcome, func.count())
        .where(MessageDeliveries.message_id.in_(message_ids))
        .group_by(MessageDeliveries.message_id, MessageDeliveries.status, MessageDeliveries.outcome)
    )
    for mid, status, outcome, n in (await session.execute(q)).all():
        o = out[mid]
        if status == DeliveryStatus.sent:
            o["sent"] += n
        elif status in (DeliveryStatus.failed, DeliveryStatus.dead):
            o["failed"] += n
        if outcome in (DeliveryOutcome.delivered, DeliveryOutcome.read):
            o["delivered"] += n
        if outcome == DeliveryOutcome.read:
            o["read"] += n
        if outcome in (DeliveryOutcome.bounced, DeliveryOutcome.complained, DeliveryOutcome.undelivered):
            o["bounced"] += n
    return out


def _sum(rows: list, key: str) -> int:
    return sum(r[key] for r in rows)


def _figures(msgs: list[tuple[Messages, Events]], outcomes: dict) -> dict:
    os_ = [outcomes[m.id] for m, _ in msgs]
    recipients = sum(m.recipients for m, _ in msgs)
    sent = sum(m.sent_count for m, _ in msgs)
    delivered, read, bounced = _sum(os_, "delivered"), _sum(os_, "read"), _sum(os_, "bounced")
    # Delivery is known only where the provider reported back; otherwise what left the door counts.
    known = delivered + bounced
    return dict(
        messages=len(msgs), recipients=recipients, sent=sent, failed=sum(m.failed_count for m, _ in msgs),
        delivered=delivered, bounced=bounced, read=read,
        delivery_rate=round(100 * delivered / known) if known else (round(100 * sent / recipients) if recipients else None),
        open_rate=round(100 * read / delivered) if delivered else None,
    )


async def overview(session, club: Clubs) -> dict:
    now = _now()
    library = await ensure_library(session, club.id)
    msgs = await _messages(session, club.id)
    outcomes = await _outcomes(session, [m.id for m, _ in msgs])
    cur = [x for x in msgs if now - x[0].created_at <= WINDOW]
    prev = [x for x in msgs if WINDOW < now - x[0].created_at <= 2 * WINDOW]
    f_cur, f_prev = _figures(cur, outcomes), _figures(prev, outcomes)

    # Every event copy, by the library email it came from.
    copies = list((await session.scalars(
        select(EmailTemplates).where(EmailTemplates.club_id == club.id, EmailTemplates.event_id.isnot(None))
    )).all())
    by_source: dict = {}
    for c in copies:
        by_source.setdefault(c.source_id, []).append(c)
    by_stage: dict = {}
    for c in copies:
        by_stage.setdefault(c.stage, []).append(c)
    by_template_msgs: dict = {}
    for m, e in msgs:
        if m.template_id:
            by_template_msgs.setdefault(m.template_id, []).append((m, e))

    def usage(tpl: EmailTemplates) -> dict:
        mine = by_source.get(tpl.id, []) if tpl.stage is EmailStage.CUSTOM else by_stage.get(tpl.stage, [])
        changed = [c for c in mine if c.updated_at and c.created_at and c.updated_at - c.created_at > timedelta(seconds=5)]
        sends = [x for c in mine for x in by_template_msgs.get(c.id, [])]
        f = _figures(sends, outcomes)
        last = max((m.created_at for m, _ in sends), default=None)
        return dict(events=len(mine), customised=len(changed), off=sum(1 for c in mine if not c.enabled), sends=f["messages"], recipients=f["recipients"], delivery_rate=f["delivery_rate"], open_rate=f["open_rate"], bounced=f["bounced"], last_sent_at=last)

    templates = [dict(**template_out(t), label=STAGE_LABEL[t.stage.value], usage=usage(t)) for t in library]

    # What goes out next by itself: every enabled, timed copy on an upcoming event that has not gone.
    upcoming = {e.id: e for e in (await session.scalars(select(Events).options(joinedload(Events.club)).where(Events.club_id == club.id, Events.is_deleted == False, Events.event_datetime + func.cast(func.concat(Events.duration, " HOURS"), INTERVAL) > now - timedelta(days=1)))).unique().all()}  # noqa: E712
    gone = set((await session.scalars(select(Messages.template_id).where(Messages.scheduled == True, Messages.template_id.isnot(None)))).all())  # noqa: E712
    queue = []
    for c in copies:
        e = upcoming.get(c.event_id)
        if e is None or not c.enabled or c.id in gone:
            continue
        due = due_at(e, fields_of(c).trigger)
        if due is None or due < now - timedelta(hours=1):
            continue
        queue.append(dict(template_id=c.id, stage=c.stage.value, label=STAGE_LABEL[c.stage.value], name=c.name, event=dict(id=e.id, name=e.name), due_at=due, audience=fields_of(c).audience.who))
    queue.sort(key=lambda q: q["due_at"])

    recent = [dict(id=m.id, subject=m.subject or "", stage=next((c.stage.value for c in copies if c.id == m.template_id), "CUSTOM") if m.template_id else "BROADCAST", event=dict(id=e.id, name=e.name), recipients=m.recipients, sent=m.sent_count, failed=m.failed_count, scheduled=m.scheduled, created_at=m.created_at, **{k: outcomes[m.id][k] for k in ("delivered", "read", "bounced")}) for m, e in msgs[:8]]
    disabled = [t for t in library if t.stage is not EmailStage.CUSTOM and not t.enabled]
    # The emails registration requests send (app/api/approvals): sent by
    # themselves, in the club's colours, and not edited here yet.
    from app.api.approvals.notify import LIBRARY as request_emails

    automatic = [dict(**e, group="Registration requests", customisable=False) for e in request_emails]
    return dict(
        automatic=automatic,
        figures=f_cur, previous=f_prev,
        delta=dict(messages=f_cur["messages"] - f_prev["messages"], recipients=f_cur["recipients"] - f_prev["recipients"]) if (cur or prev) else None,
        templates=templates, queue=queue[:10], recent=recent,
        attention=dict(disabled_stages=[t.stage.value for t in disabled], bounced_30d=f_cur["bounced"], failed_30d=f_cur["failed"]),
        all_time=dict(messages=len(msgs), recipients=sum(m.recipients for m, _ in msgs)),
        generated_at=now,
    )


