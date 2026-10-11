"""How an event went, and how the club stands, as figures.

Ported from Muster's event analytics and dashboard overview. Everything here
is an aggregate: nothing names a guest, so the closeout report can freeze a
copy that outlives the guest list, and the event's overview reads that copy
once retention has run. The door is described as a shape — admissions per
quarter hour, the load on each door, when half the house was in — rather
than as one total.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, literal_column, or_, select
from sqlalchemy.orm import joinedload

from app.api.checkins import service as checkins
from app.api.checkins.models import CheckIns, CheckInVia, Doors
from app.api.checkins.service import WALKUP_DOMAIN
from app.api.closeout.models import DOOR_OPEN_STATUSES, CloseoutReports, EventStatus, RetentionJobs, RetentionMode, RetentionState
from app.api.deliveries.models import Deliveries, DeliveryChannel, DeliveryKind, DeliveryStatus
from app.api.events.documents import EVENT_TIMEZONE, ROLE_LABELS
from app.api.events.models import EventRegistrationsLink, Events
from app.api.events.tickets import design_version_of
from app.api.events.volunteer.models import Volunteer
from app.api.idcards.models import PrintJobs, PrintSource
from app.api.merch.models import MerchItems
from app.api.users.models import Users
from app.core import providers as providers_mod

logger = logging.getLogger(__name__)

# A quarter hour: fine enough to show a rush, coarse enough to read at a glance.
BUCKET = 900
# Two weeks of quarter hours — beyond a festival, the curve stops extending.
MAX_WINDOWS = 14 * 24 * 4
RECENT = 8
LIVE_LIMIT = 6
ATTENTION_LIMIT = 6
RETENTION_HORIZON = timedelta(days=7)

ROLE_COLORS = {"attendee": "#17a58f", "speaker": "#9747ff", "staff": "#ffc84a"}
VIA_LABEL = {
    "gate": "Scanned at a door", "kiosk": "Self check-in kiosk", "walkup": "Walk-up at the door", "offline_sync": "Scanned offline, synced later",
    "desk": "By hand at the desk", "phone": "From their own phone", "app": "The volunteer app",
}
SOURCE_LABEL = {"list": "On the list", "walkup": "Walk-in"}


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """Always UTC: the rows come back in the app's zone, the buckets in UTC,
    and a chart must not be handed both."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _floor(dt: datetime) -> datetime:
    return datetime.fromtimestamp(int(dt.timestamp()) // BUCKET * BUCKET, tz=timezone.utc)


def _role(value) -> str:
    return str(getattr(value, "value", value) or "attendee")


def ends_at(event: Events) -> datetime:
    return event.event_datetime + timedelta(hours=event.duration or 0)


def lifecycle(event: Events) -> EventStatus:
    return event.lifecycle or EventStatus.published


# Attended means marked present by the organiser or admitted at a door.
ATTENDED = or_(EventRegistrationsLink.is_attended == True, CheckIns.id.isnot(None))  # noqa: E712


def _regs(event_id: int):
    return select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event_id, EventRegistrationsLink.is_deleted == False)  # noqa: E712


async def _head_count(session, event_id: int) -> tuple[int, int]:
    """Registered and checked in, counted the way the closeout report counts."""
    row = (
        await session.execute(
            select(func.count(func.distinct(EventRegistrationsLink.id)), func.count(func.distinct(EventRegistrationsLink.id)).filter(ATTENDED))
            .select_from(EventRegistrationsLink)
            .outerjoin(CheckIns, CheckIns.registration_id == EventRegistrationsLink.id)
            .where(EventRegistrationsLink.event_id == event_id, EventRegistrationsLink.is_deleted == False)  # noqa: E712
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


# ─── The shape of the door ────────────────────────────────────────────────


async def figures(session, event: Events, now: Optional[datetime] = None) -> dict:
    """The door as a shape, from live rows. Aggregates only, every date an ISO
    string, so the dict can be frozen into a report as it is."""
    now = _now(now)
    bucket = func.to_timestamp(func.floor(func.extract("epoch", CheckIns.checked_in_at) / BUCKET) * BUCKET).label("bucket")
    buckets = (
        await session.execute(
            select(bucket, CheckIns.door_id, func.count()).where(CheckIns.event_id == event.id).group_by(literal_column("1"), CheckIns.door_id).order_by(literal_column("1"))
        )
    ).all()
    doors = (
        await session.execute(
            select(CheckIns.door_id, Doors.name, Doors.kind, func.min(CheckIns.door_label), func.count(), func.min(CheckIns.checked_in_at), func.max(CheckIns.checked_in_at))
            .select_from(CheckIns)
            .outerjoin(Doors, Doors.id == CheckIns.door_id)
            .where(CheckIns.event_id == event.id)
            .group_by(CheckIns.door_id, Doors.name, Doors.kind, Doors.sort_order)
            .order_by(Doors.sort_order.nulls_last(), func.count().desc())
        )
    ).all()
    types = (
        await session.execute(
            select(EventRegistrationsLink.role, func.count(func.distinct(EventRegistrationsLink.id)), func.count(func.distinct(EventRegistrationsLink.id)).filter(ATTENDED))
            .select_from(EventRegistrationsLink)
            .outerjoin(CheckIns, CheckIns.registration_id == EventRegistrationsLink.id)
            .where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False)  # noqa: E712
            .group_by(EventRegistrationsLink.role)
        )
    ).all()
    sends = (
        await session.execute(
            select(Deliveries.channel, Deliveries.status, func.count()).where(Deliveries.event_id == event.id, Deliveries.kind == DeliveryKind.ticket).group_by(Deliveries.channel, Deliveries.status)
        )
    ).all()
    vias = (await session.execute(select(CheckIns.via, func.count()).where(CheckIns.event_id == event.id).group_by(CheckIns.via))).all()
    cards = (
        await session.execute(
            select(func.coalesce(func.sum(PrintJobs.cards).filter(PrintJobs.error.is_(None)), 0), func.count().filter(PrintJobs.error.isnot(None)))
            .where(PrintJobs.event_id == event.id, PrintJobs.source != PrintSource.test)
        )
    ).one()
    contacts = (
        await session.execute(
            select(
                func.count(func.nullif(EventRegistrationsLink.phone, "")),
                func.count().filter(and_(EventRegistrationsLink.email.isnot(None), ~EventRegistrationsLink.email.like(f"%@{WALKUP_DOMAIN}"))),
                func.count().filter(EventRegistrationsLink.email_bounced_at.isnot(None)),
                func.count().filter(EventRegistrationsLink.badge_printed_at.isnot(None)),
            ).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False)  # noqa: E712
        )
    ).one()

    total = sum(int(n) for _, _, n in buckets)

    # Arrivals: a contiguous run of quarter hours, so the curve has no gaps to misread.
    per_window: dict[int, int] = {}
    for at, _, n in buckets:
        key = int(_floor(at).timestamp())
        per_window[key] = per_window.get(key, 0) + int(n)
    arrivals: list[dict] = []
    peak_at: Optional[int] = None
    peak = 0
    half_at: Optional[int] = None
    if per_window:
        first = min(per_window)
        # Run on to now (while the event is on) or to its close — never past the close,
        # so a report generated weeks later does not trail thousands of empty windows.
        horizon = min(int(_floor(now).timestamp()), int(_floor(ends_at(event)).timestamp()), first + MAX_WINDOWS * BUCKET)
        last = max(max(per_window), horizon)
        cumulative = 0
        for t in range(first, last + 1, BUCKET):
            admits = per_window.get(t, 0)
            cumulative += admits
            arrivals.append({"starts_at": datetime.fromtimestamp(t, tz=timezone.utc).isoformat(), "admits": admits, "cumulative": cumulative})
            if admits > peak:
                peak_at, peak = t, admits
            if half_at is None and total > 0 and cumulative * 2 >= total:
                half_at = t

    door_peak: dict[Optional[object], int] = {}
    for _, door_id, n in buckets:
        door_peak[door_id] = max(door_peak.get(door_id, 0), int(n))
    by_door = [
        {
            # A string, so the dict can be frozen into a report as JSON.
            "door_id": str(door_id) if door_id else None, "name": name or label or "No door", "kind": kind.value if kind else "door", "admits": int(n),
            "share_pct": round(int(n) / total * 1000) / 10 if total else 0.0, "last_admit_at": _iso(last_at), "peak_admits": door_peak.get(door_id, 0),
        }
        for door_id, name, kind, label, n, _, last_at in doors
    ]
    first_admit = min((r[5] for r in doors if r[5]), default=None)
    last_admit = max((r[6] for r in doors if r[6]), default=None)

    typed = {_role(role): (int(reg), int(seen)) for role, reg, seen in types}
    by_type = [
        {"role": key, "label": label, "color": ROLE_COLORS.get(key, "#979797"), "registered": typed[key][0], "checked_in": typed[key][1]}
        for key, label in ROLE_LABELS.items()
        if key in typed
    ]
    counted = {(c, s): int(n) for c, s, n in sends}
    deliveries = []
    for channel in DeliveryChannel:
        row = {"channel": channel.value, **{s.value: counted.get((channel, s), 0) for s in DeliveryStatus}}
        if sum(counted.get((channel, s), 0) for s in DeliveryStatus):
            deliveries.append(row)
    via_counts = {v.value: int(n) for v, n in vias}
    half_minutes = round((half_at - int(event.event_datetime.timestamp())) / 60) if half_at is not None else None

    return {
        "total_admitted": total,
        "arrivals": arrivals,
        "by_door": by_door,
        "by_type": by_type,
        "deliveries": deliveries,
        "via": [{"via": v.value, "label": VIA_LABEL.get(v.value, v.value), "admits": via_counts[v.value]} for v in CheckInVia if via_counts.get(v.value)],
        "cards": {"printed": int(cards[0] or 0), "failed": int(cards[1] or 0), "holders": int(contacts[3] or 0)},
        "timing": {
            "first_admit_at": _iso(first_admit), "last_admit_at": _iso(last_admit),
            "peak_starts_at": _iso(datetime.fromtimestamp(peak_at, tz=timezone.utc)) if peak_at is not None else None, "peak_admits": peak,
            "half_house_at": _iso(datetime.fromtimestamp(half_at, tz=timezone.utc)) if half_at is not None else None, "half_house_minutes": half_minutes,
        },
        "contacts": {"phone": int(contacts[0] or 0), "email": int(contacts[1] or 0), "bounced": int(contacts[2] or 0)},
    }


# ─── The live summary behind an event's overview ──────────────────────────


async def _ticket_version(session, event_id: int) -> int:
    from app.api.ticket_templates.service import get_template_for_event

    try:
        return design_version_of(await get_template_for_event(session, event_id))
    except Exception:  # a broken design must not take the overview down with it
        logger.exception("ticket design for event %s could not be resolved", event_id)
        return 0


async def tickets(session, event: Events, registered: Optional[int] = None) -> dict:
    """Whether everyone on the list holds a ticket made with the current design."""
    if registered is None:
        registered, _ = await _head_count(session, event.id)
    revoked = await session.scalar(select(func.count()).select_from(_regs(event.id).where(EventRegistrationsLink.ticket_revoked_at.isnot(None)).subquery())) or 0
    sent = Deliveries.kind == DeliveryKind.ticket
    issued = await session.scalar(select(func.count(func.distinct(Deliveries.registration_id))).where(Deliveries.event_id == event.id, sent, Deliveries.status == DeliveryStatus.sent)) or 0
    pending = await session.scalar(select(func.count()).where(Deliveries.event_id == event.id, sent, Deliveries.status == DeliveryStatus.pending)) or 0
    version = await _ticket_version(session, event.id)
    older = 0
    if version > 0:
        older = await session.scalar(
            select(func.count(func.distinct(Deliveries.registration_id))).where(Deliveries.event_id == event.id, sent, Deliveries.status == DeliveryStatus.sent, Deliveries.design_version < version)
        ) or 0
    return {"issued": int(issued), "missing": max(0, int(registered) - int(revoked) - int(issued)), "pending": int(pending), "older_design": int(older), "revoked": int(revoked), "design_version": version}


async def _recent(session, event: Events) -> list:
    rows = (
        await session.execute(
            select(CheckIns, EventRegistrationsLink.full_name, Doors.name, Users.full_name, Users.email)
            .select_from(CheckIns)
            .join(EventRegistrationsLink, EventRegistrationsLink.id == CheckIns.registration_id)
            .outerjoin(Doors, Doors.id == CheckIns.door_id)
            .outerjoin(Users, Users.id == CheckIns.checked_in_by)
            .where(CheckIns.event_id == event.id)
            .order_by(CheckIns.checked_in_at.desc(), CheckIns.id)
            .limit(RECENT)
        )
    ).all()
    out = []
    for ci, name, door, by_name, by_email in rows:
        by = (by_name or by_email) if (by_name or by_email) else ("Self check-in" if ci.via in (CheckInVia.kiosk, CheckInVia.phone) else None)
        out.append({"id": ci.id, "name": name or "Guest", "door": door or ci.door_label or "No door", "by": by, "via": ci.via.value, "checked_in_at": _iso(ci.checked_in_at)})
    return out


async def _by_source(session, event: Events) -> list:
    walkup = EventRegistrationsLink.email.like(f"%@{WALKUP_DOMAIN}")
    row = (
        await session.execute(
            select(
                func.count(func.distinct(EventRegistrationsLink.id)).filter(walkup),
                func.count(func.distinct(EventRegistrationsLink.id)).filter(and_(walkup, ATTENDED)),
                func.count(func.distinct(EventRegistrationsLink.id)).filter(~walkup),
                func.count(func.distinct(EventRegistrationsLink.id)).filter(and_(~walkup, ATTENDED)),
            )
            .select_from(EventRegistrationsLink)
            .outerjoin(CheckIns, CheckIns.registration_id == EventRegistrationsLink.id)
            .where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False)  # noqa: E712
        )
    ).one()
    out = []
    for key, registered, seen in (("list", row[2], row[3]), ("walkup", row[0], row[1])):
        if registered:
            out.append({"source": key, "label": SOURCE_LABEL[key], "registered": int(registered), "checked_in": int(seen or 0)})
    return out


async def _merch(session, event: Events) -> list:
    items = list((await session.scalars(select(MerchItems).options(joinedload(MerchItems.variants)).where(MerchItems.event_id == event.id).order_by(MerchItems.created_at))).unique().all())
    return [
        {"item_id": m.id, "name": m.name, "collected": int(m.collected_count or 0), "total": sum(v.stock for v in m.variants) if m.variant_field else int(m.stock or 0)}
        for m in items
    ]


async def _frozen(session, event: Events) -> Optional[dict]:
    """The analytics the closeout report froze, when the guest list is gone."""
    job = await session.scalar(select(RetentionJobs).where(RetentionJobs.event_id == event.id))
    if job is None or job.state is not RetentionState.deleted:
        return None
    reports = await session.scalars(select(CloseoutReports).where(CloseoutReports.event_id == event.id).order_by(CloseoutReports.generated_at.desc()))
    for report in reports:
        frozen = (report.figures or {}).get("analytics")
        if isinstance(frozen, dict) and "arrivals" in frozen:
            return frozen
    return None


async def summary(session, event: Events, now: Optional[datetime] = None) -> dict:
    """Everything the event's overview shows, in one response, so no figure on
    the page can describe a different moment from the one beside it."""
    now = _now(now)
    registered, checked_in = await _head_count(session, event.id)
    frozen = await _frozen(session, event)
    job = await session.scalar(select(RetentionJobs).where(RetentionJobs.event_id == event.id))
    return {
        "event_id": event.id,
        "status": lifecycle(event).value,
        "status_clock": bool(event.status_clock),
        "door_open": checkins.door_open(event, now),
        "starts_at": _iso(event.event_datetime),
        "ends_at": _iso(ends_at(event)),
        "timezone": EVENT_TIMEZONE,
        "capacity": event.max_participants,
        "registered": registered,
        "checked_in": checked_in,
        "recent": await _recent(session, event),
        "by_source": await _by_source(session, event),
        "merch": await _merch(session, event),
        "tickets": await tickets(session, event, registered),
        "retention": {"mode": (event.retention_mode or RetentionMode.keep).value, "state": job.state.value if job else None, "run_at": _iso(job.run_at) if job else None},
        "analytics": frozen if frozen is not None else await figures(session, event, now),
        "from_report": frozen is not None,
        "generated_at": _iso(now),
    }


# ─── The club's dashboard ─────────────────────────────────────────────────


def _club_events(club_id: int):
    return select(Events).where(Events.club_id == club_id, Events.is_deleted == False)  # noqa: E712


async def _status_counts(session, scope) -> dict:
    rows = (await session.execute(select(Events.lifecycle, func.count()).where(scope).group_by(Events.lifecycle))).all()
    by = {(s or EventStatus.published).value: int(n) for s, n in rows}
    counts = {s.value: by.get(s.value, 0) for s in EventStatus}
    counts["total"] = sum(n for k, n in counts.items() if k != EventStatus.archived.value)
    return counts


async def _on_file(session, scope) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(EventRegistrationsLink).join(Events, Events.id == EventRegistrationsLink.event_id)
            .where(scope, Events.lifecycle.in_(DOOR_OPEN_STATUSES), EventRegistrationsLink.is_deleted == False)  # noqa: E712
        ) or 0
    )


async def _retention(session, scope, now: datetime) -> tuple[list, list]:
    """Jobs due within the week, and the ones that failed, for the events in scope."""
    rows = (
        await session.execute(
            select(RetentionJobs, Events).join(Events, Events.id == RetentionJobs.event_id).where(scope, Events.retention_mode == RetentionMode.delete).order_by(RetentionJobs.run_at)
        )
    ).all()
    due = [(j, e) for j, e in rows if j.state is not RetentionState.deleted and j.run_at <= now + RETENTION_HORIZON]
    failed = [(j, e) for j, e in rows if j.state is RetentionState.failed]
    return due, failed


async def _live(session, scope, now: datetime, limit: int) -> list:
    events = list((await session.scalars(_scope_query(scope).options(joinedload(Events.club)).where(Events.lifecycle == EventStatus.live).order_by(Events.event_datetime).limit(limit))).unique().all())
    out = []
    for e in events:
        registered, checked_in = await _head_count(session, e.id)
        gates = list((await session.scalars(select(Doors.name).where(Doors.event_id == e.id, Doors.active == True).order_by(Doors.sort_order, Doors.name))).all())  # noqa: E712
        volunteers = await session.scalar(select(func.count()).where(Volunteer.event_id == e.id, Volunteer.is_approved == True, Volunteer.is_deleted == False)) or 0  # noqa: E712
        out.append({
            "event_id": e.id, "name": e.name, "club": e.club.name if e.club else None, "registered": registered, "checked_in": checked_in,
            "gates": gates, "volunteers": int(volunteers), "door_open": checkins.door_open(e, now), "starts_at": _iso(e.event_datetime),
        })
    return out


def _scope_query(scope):
    return select(Events).where(scope)


async def _attention(session, scope, now: datetime, *, failed: list) -> list:
    items: list[dict] = []
    for job, event in failed:
        items.append({
            "kind": "RETENTION_FAILED", "tone": "red", "title": f"Retention failed for {event.name}",
            "body": f"{job.last_error or 'The report could not be delivered.'} Nothing was deleted; it retries next cycle.", "event_id": event.id,
        })
    if not providers_mod.email_provider().configured:
        items.append({
            "kind": "NO_PROVIDER", "tone": "amber", "title": "No email provider is configured",
            "body": "Tickets, certificates and messages are recorded as skipped rather than sent. Connect Amazon SES to send them.", "event_id": None,
        })
    drafts = list((await session.scalars(_scope_query(scope).where(Events.lifecycle == EventStatus.draft, Events.event_datetime < now).order_by(Events.event_datetime).limit(3))).all())
    for e in drafts:
        items.append({
            "kind": "DRAFT_STARTED", "tone": "amber", "title": f"{e.name} has started and is still a draft",
            "body": "The gate refuses every scan and nothing timed goes out. Publish it, or switch the status clock on under Closeout.", "event_id": e.id,
        })
    active = list((await session.scalars(_scope_query(scope).where(Events.lifecycle.in_(DOOR_OPEN_STATUSES)).order_by(Events.event_datetime).limit(10))).all())
    for e in active:
        registered, _ = await _head_count(session, e.id)
        t = await tickets(session, e, registered)
        if t["older_design"] > 0:
            items.append({
                "kind": "REDESIGNED_AFTER_ISSUE", "tone": "amber", "title": f"{e.name} tickets redesigned after issue",
                "body": f"{t['older_design']} {'guest holds' if t['older_design'] == 1 else 'guests hold'} a ticket made with an older design. Re-sending is the only way to update them.", "event_id": e.id,
            })
        elif t["missing"] > 0:
            items.append({
                "kind": "TICKETS_MISSING", "tone": "amber", "title": f"{e.name} has {'a guest' if t['missing'] == 1 else 'guests'} without a ticket",
                "body": f"{t['missing']} on the list {'has' if t['missing'] == 1 else 'have'} not been sent a ticket yet. Sending is safe to repeat.", "event_id": e.id,
            })
        bounced = await session.scalar(select(func.count()).select_from(_regs(e.id).where(EventRegistrationsLink.email_bounced_at.isnot(None)).subquery())) or 0
        if bounced:
            items.append({
                "kind": "BOUNCED", "tone": "amber", "title": f"{bounced} {'address' if bounced == 1 else 'addresses'} bounced on {e.name}",
                "body": "Nothing more is sent to them until the address is corrected on the guest list.", "event_id": e.id,
            })
    silent = list((await session.scalars(
        _scope_query(scope).where(Events.retention_mode == RetentionMode.delete, Events.lifecycle.in_((EventStatus.published, EventStatus.live, EventStatus.closed)), func.cardinality(Events.report_recipients) == 0).limit(3)
    )).all())
    for e in silent:
        items.append({
            "kind": "NO_RECIPIENTS", "tone": "amber", "title": f"{e.name} deletes its guest list but names nobody for the report",
            "body": "The closeout report will be kept here and sent to no one before the list is deleted. Add recipients under Closeout.", "event_id": e.id,
        })
    red = [i for i in items if i["tone"] == "red"]
    amber = [i for i in items if i["tone"] != "red"]
    return (red + amber)[:ATTENTION_LIMIT]


async def dashboard(session, club, now: Optional[datetime] = None) -> dict:
    """The club's home page: where its events stand, what needs a decision,
    and which doors are open right now."""
    now = _now(now)
    scope = and_(Events.club_id == club.id, Events.is_deleted == False)  # noqa: E712
    counts = await _status_counts(session, scope)
    due, failed = await _retention(session, scope, now)
    live = await _live(session, scope, now, LIVE_LIMIT)
    return {
        "stats": {
            "events": counts,
            "on_file": await _on_file(session, scope),
            "retention_due": len(due),
            "next_retention_at": next((_iso(j.run_at) for j, _ in due if j.run_at > now), None),
            "doors_open": sum(1 for e in live if e["door_open"]),
        },
        "attention": await _attention(session, scope, now, failed=failed),
        "live": live,
        "generated_at": _iso(now),
    }


async def lifetime(session, club, now: Optional[datetime] = None) -> dict:
    """What the club has done over every event it has run."""
    now = _now(now)
    rows = (
        await session.execute(
            select(Events.id, Events.event_datetime, Events.duration, Events.lifecycle, func.count(func.distinct(EventRegistrationsLink.id)), func.count(func.distinct(EventRegistrationsLink.id)).filter(ATTENDED))
            .select_from(Events)
            .outerjoin(EventRegistrationsLink, and_(EventRegistrationsLink.event_id == Events.id, EventRegistrationsLink.is_deleted == False))  # noqa: E712
            .outerjoin(CheckIns, CheckIns.registration_id == EventRegistrationsLink.id)
            .where(Events.club_id == club.id, Events.is_deleted == False)  # noqa: E712
            .group_by(Events.id, Events.event_datetime, Events.duration, Events.lifecycle)
        )
    ).all()
    run = [r for r in rows if (r[3] or EventStatus.published) is not EventStatus.draft and r[1] + timedelta(hours=r[2] or 0) <= now]
    turnouts = [r[5] / r[4] * 100 for r in run if r[4]]
    delivered = await session.scalar(
        select(func.count()).select_from(CloseoutReports).join(Events, Events.id == CloseoutReports.event_id).where(Events.club_id == club.id, func.cardinality(CloseoutReports.delivered_to) > 0)
    ) or 0
    return {
        "events_run": len(run),
        "registered": int(sum(r[4] for r in rows)),
        "guests_admitted": int(sum(r[5] for r in rows)),
        "average_turnout_pct": round(sum(turnouts) / len(turnouts)) if turnouts else None,
        "reports_delivered": int(delivered),
    }


async def platform_doors(session, now: Optional[datetime] = None) -> dict:
    """The same picture for the whole platform, for the admin overview."""
    now = _now(now)
    scope = Events.is_deleted == False  # noqa: E712
    counts = await _status_counts(session, scope)
    due, failed = await _retention(session, scope, now)
    live = await _live(session, scope, now, 12)
    clubs_live = len({e["club"] for e in live if e["club"]})
    return {
        "stats": {
            "events": counts, "on_file": await _on_file(session, scope), "retention_due": len(due),
            "next_retention_at": next((_iso(j.run_at) for j, _ in due if j.run_at > now), None), "doors_open": sum(1 for e in live if e["door_open"]),
        },
        "attention": await _attention(session, scope, now, failed=failed),
        "live": live,
        "clubs_live": clubs_live,
        "generated_at": _iso(now),
    }
