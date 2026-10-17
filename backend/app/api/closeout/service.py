"""Closing an event: the report, the status clock, and retention.

Retention runs forward only — PENDING → REPORT_SENT → DELETED, with FAILED
re-entering at the report stage — so nothing is scrubbed until the closeout
report has been generated and kept and, where the event names recipients,
confirmed delivered to every one of them. A manual run skips the clock,
never the ordering.
"""
import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.orm import joinedload

from app.api.checkins.models import CheckIns
from app.api.checkins.service import WALKUP_DOMAIN
from app.api.closeout.models import DOOR_OPEN_STATUSES, LOCKED_STATUSES, STATUS_NOTE, ArchivedContacts, CloseoutReports, EventStatus, RetentionJobs, RetentionMode, RetentionState
from app.api.closeout.report import report_pdf
from app.api.deliveries import service as deliveries
from app.api.deliveries.models import Deliveries
from app.api.emails.models import MessageDeliveries, Messages
from app.api.events.documents import EVENT_TIMEZONE, ROLE_LABELS
from app.api.events.models import EventRatingsLink, EventRegistrationsLink, Events, ends_at_sql
from app.api.kiosk.models import SelfCheckInSessions
from app.api.merch.models import MerchItems
from app.config import settings
from app.core.email import email as email_mod
from app.core.storage import objects
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

SWEEP_LOCK = 773102
RETENTION_LOCK = 773103
# A closed event with nothing to scrub is archived this long after it ends.
ARCHIVE_AFTER = timedelta(days=30)

VIA_LABEL = {
    "gate": "Scanned at the gate", "kiosk": "Self check-in kiosk", "walkup": "Registered at the door", "offline_sync": "Scanned offline",
    "desk": "By hand at the desk", "phone": "From their own phone", "app": "Volunteer app",
}
STATUS_LABEL = {EventStatus.draft: "Draft", EventStatus.published: "Published", EventStatus.live: "Live", EventStatus.closed: "Closed", EventStatus.archived: "Archived"}


def ends_at(event: Events) -> datetime:
    return event.event_datetime + timedelta(hours=event.duration or 0)


def ended(event: Events, now: Optional[datetime] = None) -> bool:
    return ends_at(event) <= (now or datetime.now(timezone.utc))


def lifecycle(event: Events) -> EventStatus:
    return event.lifecycle or EventStatus.published


def assert_writable(event: Events) -> None:
    """Closed and archived events are read-only, except for the status itself."""
    if lifecycle(event) in LOCKED_STATUSES:
        raise CustomHTTPException(409, message="This event is closed, so its settings are read-only. Move its status back to Live on the closeout page to change them.", error_code="EVENT_LOCKED")


# ─── Figures and reports ──────────────────────────────────────────────────


async def figures(session, event: Events) -> dict:
    """The numbers the organiser quotes afterwards, from the same tables the
    door and the lists read, so the report and the live screens agree."""
    regs = list((await session.scalars(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False))).all())  # noqa: E712
    attended_ids = set((await session.scalars(select(CheckIns.registration_id).where(CheckIns.event_id == event.id))).all())
    attended = {r.id for r in regs if r.is_attended or r.id in attended_ids}
    by_role = []
    for key, label in ROLE_LABELS.items():
        of_role = [r for r in regs if (getattr(r.role, "value", r.role) or "attendee") == key]
        if of_role:
            by_role.append({"role": key, "label": label, "registered": len(of_role), "checked_in": sum(1 for r in of_role if r.id in attended)})
    vias = (await session.execute(select(CheckIns.via, func.count()).where(CheckIns.event_id == event.id).group_by(CheckIns.via))).all()
    merch = list((await session.scalars(select(MerchItems).options(joinedload(MerchItems.variants)).where(MerchItems.event_id == event.id).order_by(MerchItems.created_at))).unique().all())
    messages = list((await session.scalars(select(Messages).where(Messages.event_id == event.id).order_by(Messages.created_at))).all())
    rating = (await session.execute(select(func.avg(EventRatingsLink.rating), func.count()).where(EventRatingsLink.event_id == event.id, EventRatingsLink.is_deleted == False))).one()  # noqa: E712
    club = getattr(event, "club", None)
    registered = len(regs)
    checked_in = len(attended)
    # The door as a shape, frozen here so the overview can read it back once
    # retention has deleted the rows it was computed from.
    from app.api.analytics import service as analytics

    return {
        "analytics": await analytics.figures(session, event),
        "event_name": event.name, "org_name": club.name if club else "", "venue": event.location_name,
        "starts_at": event.event_datetime.isoformat(), "ends_at": ends_at(event).isoformat(),
        "registered": registered, "checked_in": checked_in,
        "turnout_pct": round(checked_in / registered * 100) if registered else 0, "no_shows": max(0, registered - checked_in),
        "by_role": by_role,
        "by_via": [{"via": v.value, "label": VIA_LABEL.get(v.value, v.value), "count": n} for v, n in vias],
        "walk_ins": sum(1 for r in regs if (r.email or "").endswith("@" + WALKUP_DOMAIN)),
        "with_phone": sum(1 for r in regs if r.phone),
        "merch": [{"name": m.name, "collected": m.collected_count, "total": sum(v.stock for v in m.variants) if m.variant_field else (m.stock or 0)} for m in merch],
        "messages": [{"subject": m.subject, "audience": str(m.audience), "sent": m.sent_count, "failed": m.failed_count, "skipped": m.skipped_count} for m in messages],
        "certificates_issued": sum(1 for r in regs if r.certificate_issued_at),
        "cards_printed": sum(1 for r in regs if r.badge_printed_at),
        "rating_avg": float(rating[0] or 0), "rating_count": int(rating[1] or 0),
    }


def report_out(r: CloseoutReports, by_name: Optional[str]) -> dict:
    return {"id": r.id, "filename": r.filename, "figures": r.figures or {}, "generated_at": r.generated_at, "generated_by_name": by_name, "delivered_to": list(r.delivered_to or [])}


async def generate(session, event: Events, user=None) -> CloseoutReports:
    """The report as of now, rendered and kept; nothing else changes."""
    f = await figures(session, event)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d%H%M")
    pdf = report_pdf(f, now, EVENT_TIMEZONE)
    key = os.path.join(settings.S3_BASE_PATH, "events", str(event.id), "closeout") + f"/{uuid.uuid4().hex}.pdf"
    objects.put_bytes(key, pdf, "application/pdf")
    row = CloseoutReports(event_id=event.id, pdf_key=key, filename=f"{event.slug}-closeout-{stamp}.pdf", figures=f, generated_at=now, generated_by=user.id if user is not None else None, delivered_to=[])
    session.add(row)
    await session.commit()
    return row


async def deliver(session, event: Events, report: CloseoutReports, recipients: list) -> tuple:
    """Deliver to every recipient. Partial delivery counts as failure: the
    caller must not scrub anything unless `failed` is empty."""
    pdf = objects.get_bytes(report.pdf_key)
    if not pdf:
        raise CustomHTTPException(500, message="The report file is missing from storage.")
    delivered, failed = [], []
    for to in recipients:
        try:
            email_mod.send_email_with_attachment(
                recipients=to, subject=f"Closeout report — {event.name}",
                body_text=f"The closeout report for {event.name} is attached. Guest data is scrubbed once this report is confirmed delivered to every recipient.",
                body_html=f"<p>The closeout report for <strong>{event.name}</strong> is attached.</p><p>Guest data is scrubbed once this report is confirmed delivered to every recipient.</p>",
                attachment_bytes=pdf, attachment_filename=report.filename,
            )
            delivered.append(to)
        except Exception as exc:  # noqa: BLE001 - every provider failure is a delivery failure
            failed.append({"to": to, "error": str(exc)[:300]})
    report.delivered_to = list(delivered)
    await session.commit()
    return delivered, failed


async def list_reports(session, event: Events) -> list:
    rows = list((await session.scalars(select(CloseoutReports).options(joinedload(CloseoutReports.by)).where(CloseoutReports.event_id == event.id).order_by(CloseoutReports.generated_at.desc()).limit(50))).all())
    return [report_out(r, (r.by.full_name or r.by.email) if r.by is not None else "Retention") for r in rows]


async def report_file(session, event: Events, report_id: UUID) -> tuple:
    r = await session.get(CloseoutReports, report_id)
    if r is None or r.event_id != event.id:
        raise CustomHTTPException(404, message="That report could not be found.")
    pdf = objects.get_bytes(r.pdf_key)
    if not pdf:
        raise CustomHTTPException(404, message="The report file is missing from storage.")
    return pdf, r.filename


# ─── Settings and the status clock ────────────────────────────────────────


def settings_of(event: Events) -> dict:
    return {
        "status": lifecycle(event).value, "status_clock": bool(event.status_clock), "retention_mode": (event.retention_mode or RetentionMode.keep).value,
        "retention_hours": event.retention_hours or 72, "report_recipients": list(event.report_recipients or []), "keep_contacts": bool(event.keep_contacts),
        "starts_at": event.event_datetime, "ends_at": ends_at(event),
        "statuses": [{"key": s.value, "label": STATUS_LABEL[s], "note": STATUS_NOTE[s]} for s in EventStatus],
    }


def run_at_for(event: Events) -> datetime:
    return ends_at(event) + timedelta(hours=event.retention_hours or 72)


async def ensure_job(session, event: Events) -> RetentionJobs:
    job = await session.scalar(select(RetentionJobs).where(RetentionJobs.event_id == event.id))
    if job is None:
        job = RetentionJobs(event_id=event.id, run_at=run_at_for(event), state=RetentionState.pending)
        session.add(job)
        await session.commit()
    return job


async def update_settings(session, event: Events, body) -> dict:
    data = body.model_dump(exclude_unset=True)
    if "status" in data and data["status"] is not None:
        new = EventStatus(data["status"])
        if new is not lifecycle(event):
            event.lifecycle = new
            # Moving the status by hand takes the event off the clock, unless it
            # is being published: that is the normal way onto the clock, not off it.
            if "status_clock" not in data and new is not EventStatus.published:
                event.status_clock = False
    if data.get("status_clock") is not None:
        event.status_clock = data["status_clock"]
    if data.get("retention_mode") is not None:
        event.retention_mode = RetentionMode(data["retention_mode"])
    if data.get("retention_hours") is not None:
        event.retention_hours = data["retention_hours"]
    if data.get("report_recipients") is not None:
        event.report_recipients = data["report_recipients"]
    if data.get("keep_contacts") is not None:
        event.keep_contacts = data["keep_contacts"]
    await session.commit()
    if "status" in data:
        from app.api.sites import service as sites

        await sites.republish_for_event(session, event.id)
    # Editing the hours re-times the job. DELETED stays terminal.
    job = await ensure_job(session, event)
    if job.state is not RetentionState.deleted:
        job.run_at = run_at_for(event)
        await session.commit()
    return settings_of(event)


# ─── Retention ────────────────────────────────────────────────────────────


async def preview_counts(session, event: Events) -> dict:
    regs = list((await session.scalars(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False))).all())  # noqa: E712
    ids = [r.id for r in regs]
    return {
        "registrations": len(regs),
        "walkups": sum(1 for r in regs if (r.email or "").endswith("@" + WALKUP_DOMAIN)),
        "phones": sum(1 for r in regs if r.phone),
        "answers": sum(1 for r in regs if r.additional_details),
        "deliveries": (await session.scalar(select(func.count()).select_from(Deliveries).where(Deliveries.event_id == event.id))) or 0,
        "message_deliveries": (await session.scalar(select(func.count()).select_from(MessageDeliveries).where(MessageDeliveries.registration_id.in_(ids)))) if ids else 0,
        "self_check_in_sessions": (await session.scalar(select(func.count()).select_from(SelfCheckInSessions).where(SelfCheckInSessions.event_id == event.id))) or 0,
    }


async def retention_status(session, event: Events) -> dict:
    job = await ensure_job(session, event)
    kept = await session.scalar(select(func.count()).select_from(ArchivedContacts).where(ArchivedContacts.event_id == event.id)) or 0
    return {
        "event_id": event.id, "mode": (event.retention_mode or RetentionMode.keep).value, "state": job.state.value, "run_at": job.run_at, "attempts": job.attempts,
        "last_error": job.last_error, "refusal": job.last_refusal, "refused_at": job.refused_at, "completed_at": job.completed_at, "deleted": job.deleted_counts,
        "recipients": list(event.report_recipients or []), "preview": await preview_counts(session, event), "keeps_contacts": bool(event.keep_contacts), "contacts_kept": kept,
        "ended": ended(event), "ends_at": ends_at(event),
    }


async def archive_contacts(session, event: Events, regs: list, attended: set) -> int:
    """The four fields a club asked to keep, copied out before the scrub."""
    if not event.keep_contacts or not regs:
        return 0
    have = set((await session.scalars(select(ArchivedContacts.registration_id).where(ArchivedContacts.event_id == event.id))).all())
    n = 0
    for r in regs:
        if r.id in have:
            continue
        session.add(ArchivedContacts(
            club_id=event.club_id, event_id=event.id, registration_id=r.id, name=r.full_name or "", email=r.email, phone=r.phone,
            role=str(getattr(r.role, "value", r.role) or "attendee"), attended=r.id in attended, event_name=event.name, event_ended_at=ends_at(event),
        ))
        n += 1
    await session.flush()
    return n


async def scrub(session, event: Events, job: RetentionJobs) -> dict:
    """What the event gathered beyond the guests' own accounts goes; the
    registrations stay, so tickets and certificates keep working in the app."""
    regs = list((await session.scalars(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False))).all())  # noqa: E712
    ids = [r.id for r in regs]
    attended = set((await session.scalars(select(CheckIns.registration_id).where(CheckIns.event_id == event.id))).all()) | {r.id for r in regs if r.is_attended}
    counts = await preview_counts(session, event)
    counts["contacts_archived"] = await archive_contacts(session, event, regs, attended)
    await session.execute(delete(Deliveries).where(Deliveries.event_id == event.id))
    if ids:
        await session.execute(delete(MessageDeliveries).where(MessageDeliveries.registration_id.in_(ids)))
    await session.execute(delete(SelfCheckInSessions).where(SelfCheckInSessions.event_id == event.id))
    for r in regs:
        if (r.email or "").endswith("@" + WALKUP_DOMAIN):
            # A walk-up record was made at the gate and belongs to nobody's account: it goes.
            r.full_name = "Walk-up guest"
            r.email = f"removed-{uuid.uuid4().hex[:10]}@{WALKUP_DOMAIN}"
            r.is_deleted = True
        r.phone = None
        r.additional_details = None
    job.state = RetentionState.deleted
    job.completed_at = datetime.now(timezone.utc)
    job.deleted_counts = counts
    job.last_error = None
    job.last_refusal = None
    if event.status_clock and lifecycle(event) in (EventStatus.closed, EventStatus.live, EventStatus.published):
        event.lifecycle = EventStatus.archived
    await session.commit()
    return counts


def _result(state: RetentionState, refusal: Optional[str] = None, error: Optional[str] = None, deleted: Optional[dict] = None) -> dict:
    return {"state": state.value, "refusal": refusal, "error": error, "deleted": deleted}


async def run_retention(session, event: Events, *, manual: bool, confirm_slug: Optional[str] = None, user=None) -> dict:
    """Generate the report (and, where recipients are named, confirm delivery
    to every one of them), then scrub, in that order, forward only."""
    locked = await session.scalar(text(f"SELECT pg_try_advisory_lock({RETENTION_LOCK}, :eid)"), {"eid": event.id})
    if not locked:
        if manual:
            raise CustomHTTPException(409, message="A retention run for this event is already in progress.")
        return _result(RetentionState.pending, error="already running")
    try:
        job = await ensure_job(session, event)
        if job.state is RetentionState.deleted:
            if manual:
                raise CustomHTTPException(409, message="This guest list was already scrubbed. Nothing is left to remove.", error_code="RETENTION_ALREADY_DELETED")
            return _result(RetentionState.deleted, deleted=job.deleted_counts)
        if manual and (confirm_slug or "").strip() != event.slug:
            raise CustomHTTPException(422, message=f"The slug you typed does not match this event. Type {event.slug} exactly.", error_code="CONFIRMATION_MISMATCH")
        now = datetime.now(timezone.utc)
        if not manual and not ended(event, now):
            job.last_refusal = "NOT_ENDED"
            job.refused_at = now
            await session.commit()
            return _result(job.state, refusal="NOT_ENDED")
        if job.state in (RetentionState.pending, RetentionState.failed):
            report = await generate(session, event, user)
            recipients = list(event.report_recipients or [])
            delivered, failed = await deliver(session, event, report, recipients) if recipients else ([], [])
            job.attempts += 1
            job.report_id = report.id
            if failed:
                job.state = RetentionState.failed
                job.last_error = f"Report delivery failed for {len(failed)} of {len(recipients)} recipients: {failed[0]['error']}"
                await session.commit()
                if manual:
                    raise CustomHTTPException(409, message=f"{job.last_error}. Nothing was scrubbed.")
                return _result(RetentionState.failed, error=job.last_error)
            job.state = RetentionState.report_sent
            job.last_error = None
            job.last_refusal = None
            await session.commit()
        counts = await scrub(session, event, job)
        return _result(RetentionState.deleted, deleted=counts)
    finally:
        await session.execute(text(f"SELECT pg_advisory_unlock({RETENTION_LOCK}, :eid)"), {"eid": event.id})


# ─── The sweep ────────────────────────────────────────────────────────────


async def advance_clock(session, now: Optional[datetime] = None) -> int:
    """published → live at the start; live → closed after the end; closed →
    archived once retention has run, or a month on when there is nothing to
    scrub. Only events on the clock."""
    now = now or datetime.now(timezone.utc)
    # Only the events whose status this minute can move: one that has started,
    # one past its end and grace, one closed whose contacts are scrubbed or
    # whose month is up. Every other event on the clock (every upcoming one)
    # was read here each minute and left as it was: thousands of rows read
    # and a query each for the closed ones, on the web worker, every minute.
    # The rules below still decide; this only leaves out rows they would not move.
    end_sql = ends_at_sql(Events)
    job_done = (
        select(RetentionJobs.id)
        .where(RetentionJobs.event_id == Events.id, RetentionJobs.state == RetentionState.deleted)
        .exists()
    )
    can_move = or_(
        and_(Events.lifecycle == EventStatus.published, Events.event_datetime <= now),
        and_(
            Events.lifecycle == EventStatus.live,
            end_sql + func.coalesce(Events.close_after_minutes, 240) * text("INTERVAL '1 minute'") <= now,
        ),
        and_(
            Events.lifecycle == EventStatus.closed,
            or_(
                job_done,
                and_(
                    or_(Events.retention_mode.is_(None), Events.retention_mode == RetentionMode.keep),
                    # ARCHIVE_AFTER in hours, as timedelta counts it (a "30 days" interval would follow the clock changes of the session's zone).
                    end_sql + text(f"INTERVAL '{int(ARCHIVE_AFTER.total_seconds() // 3600)} hours'") <= now,
                ),
            ),
        ),
    )
    events = list((await session.scalars(select(Events).where(Events.is_deleted == False, Events.status_clock == True, Events.lifecycle.in_([EventStatus.published, EventStatus.live, EventStatus.closed]), can_move))).all())  # noqa: E712
    jobs = {}
    if events:
        jobs = {j.event_id: j for j in (await session.scalars(select(RetentionJobs).where(RetentionJobs.event_id.in_([e.id for e in events])))).all()}
    moved = 0
    for e in events:
        before = lifecycle(e)
        end = ends_at(e)
        if e.lifecycle is EventStatus.published and now >= e.event_datetime:
            e.lifecycle = EventStatus.live
        if e.lifecycle is EventStatus.live and now >= end + timedelta(minutes=e.close_after_minutes or 240):
            e.lifecycle = EventStatus.closed
        if e.lifecycle is EventStatus.closed:
            job = jobs.get(e.id)
            done = job is not None and job.state is RetentionState.deleted
            if done or ((e.retention_mode or RetentionMode.keep) is RetentionMode.keep and now >= end + ARCHIVE_AFTER):
                e.lifecycle = EventStatus.archived
        if e.lifecycle is not before:
            moved += 1
    if moved:
        await session.commit()
    return moved


async def sweep(session, now: Optional[datetime] = None) -> dict:
    """Called every minute: move the clocks, then run every due retention job."""
    now = now or datetime.now(timezone.utc)
    moved = await advance_clock(session, now)
    due = list((await session.execute(
        select(RetentionJobs, Events).join(Events, Events.id == RetentionJobs.event_id).options(joinedload(Events.club))
        .where(Events.retention_mode == RetentionMode.delete, Events.is_deleted == False, RetentionJobs.state.in_([RetentionState.pending, RetentionState.report_sent, RetentionState.failed]), RetentionJobs.run_at <= now)  # noqa: E712
        .limit(50)
    )).all())
    ran = 0
    for _job, event in due:
        try:
            await run_retention(session, event, manual=False)
            ran += 1
        except Exception:  # noqa: BLE001
            logger.exception("retention run failed for event %s", event.id)
            await session.rollback()
    return {"moved": moved, "ran": ran}


async def _check_counters(session) -> None:
    """Every few hours, recount any going/follower counter that disagrees with its rows."""
    from app.api.discover import counters

    if not counters.due():
        return
    try:
        fixed = await counters.reconcile(session)
        if fixed["events"] or fixed["clubs"]:
            logger.warning("recounted %d going count(s) and %d follower count(s) that disagreed with their rows", fixed["events"], fixed["clubs"])
    except Exception:  # noqa: BLE001
        logger.exception("counter reconcile failed")
        await session.rollback()


async def sweep_loop(interval: int = 60) -> None:
    """Runs for the life of the process; one worker at a time."""
    await asyncio.sleep(7)
    while True:
        try:
            async with deliveries.session_scope() as session:
                if await session.scalar(text(f"SELECT pg_try_advisory_lock({SWEEP_LOCK})")):
                    try:
                        r = await sweep(session)
                        if r["moved"] or r["ran"]:
                            logger.info("closeout sweep moved %d status(es), ran %d retention job(s)", r["moved"], r["ran"])
                        await _check_counters(session)
                    finally:
                        await session.execute(text(f"SELECT pg_advisory_unlock({SWEEP_LOCK})"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("closeout sweep failed")
        await asyncio.sleep(interval)


__all__ = ["DOOR_OPEN_STATUSES"]
