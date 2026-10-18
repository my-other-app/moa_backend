"""Issuing certificates, and everything that follows from one existing."""
import io
import logging
import re
import zipfile
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import joinedload

from app.api.certificate_templates.service import get_template_for_event
from app.api.deliveries import service as deliveries
from app.api.deliveries.models import Deliveries, DeliveryKind
from app.api.events import doc_cache
from app.api.events.certificates import (
    availability,
    certificate_job,
    certificate_number,
    current_design_version,
    ticket_id_of_certificate,
)
from app.api.events.tickets import role_of
from app.api.events.models import EventRegistrationsLink, Events
from app.api.templates_audience import RegistrationRole
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)


def _live(event_id: int) -> list:
    return [EventRegistrationsLink.event_id == event_id, EventRegistrationsLink.is_deleted == False]  # noqa: E712


def _active(event_id: int):
    return select(EventRegistrationsLink).where(*_live(event_id))


def _audience_criteria(event_id: int, audience: str, roles: list[str]) -> list:
    criteria = _live(event_id)
    if audience == "checked_in":
        return [*criteria, EventRegistrationsLink.is_attended == True]  # noqa: E712
    if audience == "roles":
        try:
            wanted = [RegistrationRole(r) for r in roles]
        except ValueError:
            raise CustomHTTPException(422, message="Pick roles from attendee, speaker and staff.", errors={"fields": {"roles": "unknown role"}})
        if not wanted:
            raise CustomHTTPException(422, message="Pick at least one role.", errors={"fields": {"roles": "Pick at least one role."}})
        return [*criteria, EventRegistrationsLink.role.in_(wanted)]
    return criteria


def _audience(event_id: int, audience: str, roles: list[str]):
    return select(EventRegistrationsLink).where(*_audience_criteria(event_id, audience, roles))


async def _design_version(session, event_id: int) -> int:
    style = await get_template_for_event(session, event_id)
    return current_design_version(style)


async def _role_versions(session, event_id: int) -> tuple[dict, int]:
    """Each role's own design version, and the event default's.

    A role can have a design of its own (a speaker's certificate); a guest
    issued one carries that design's version, not the default's -- or every
    later save of the default would mark the speakers' certificates as out of
    date while theirs never changed."""
    default = await _design_version(session, event_id)
    versions = {}
    for role in RegistrationRole:
        versions[role] = current_design_version(await get_template_for_event(session, event_id, role=role))
    return versions, default


def _version_of_role(versions: dict, default: int):
    """The design version a row's role stands for, as SQL."""
    return case(
        *[(EventRegistrationsLink.role == role, version) for role, version in versions.items()],
        else_=default,
    )


async def _count(session, *criteria) -> int:
    return await session.scalar(select(func.count()).select_from(EventRegistrationsLink).where(*criteria)) or 0


async def issue_preview(session, event: Events, audience: str, roles: list[str]) -> dict:
    criteria = _audience_criteria(event.id, audience, roles)
    versions, default = await _role_versions(session, event.id)
    issued_c = EventRegistrationsLink.certificate_issued_at.isnot(None)
    eligible = await _count(session, *criteria)
    issued = await _count(session, *criteria, issued_c)
    outdated = await _count(
        session, *criteria, issued_c, EventRegistrationsLink.certificate_revoked_at.is_(None),
        func.coalesce(EventRegistrationsLink.certificate_design_version, 0) < _version_of_role(versions, default),
    )
    return {"eligible": eligible, "already_issued": issued, "to_issue": eligible - issued, "outdated": outdated}


async def issue(session, event: Events, audience: str, roles: list[str]) -> dict:
    """Everyone in the audience who has none gets one, numbered and dated
    now; the ones issued with an older design are marked as refreshed. A
    guest is never issued twice, and a revoked one stays revoked.

    Two UPDATEs in the database, not a loop over every registration loaded
    as an object: an event of five thousand guests was five thousand rows
    read, changed and flushed one by one. Each row is stamped with its own
    role's design version (`_role_versions`)."""
    criteria = _audience_criteria(event.id, audience, roles)
    versions, default = await _role_versions(session, event.id)
    version = _version_of_role(versions, default)
    now = datetime.now(timezone.utc)
    total = await _count(session, *criteria)
    issued = (await session.execute(
        update(EventRegistrationsLink)
        .where(*criteria, EventRegistrationsLink.certificate_issued_at.is_(None))
        .values(certificate_issued_at=now, certificate_design_version=version)
        .execution_options(synchronize_session="fetch")
    )).rowcount or 0
    refreshed = (await session.execute(
        update(EventRegistrationsLink)
        .where(
            *criteria,
            EventRegistrationsLink.certificate_issued_at.isnot(None),
            EventRegistrationsLink.certificate_revoked_at.is_(None),
            func.coalesce(EventRegistrationsLink.certificate_design_version, 0) < version,
        )
        .values(certificate_design_version=version)
        .execution_options(synchronize_session="fetch")
    )).rowcount or 0
    await session.commit()
    return {"issued": issued, "refreshed": refreshed, "already_issued": total - issued - refreshed}


async def set_revoked(session, event: Events, registration_id, revoked: bool) -> EventRegistrationsLink:
    reg = await deliveries._registration(session, event.id, registration_id)
    if reg.certificate_issued_at is None:
        raise CustomHTTPException(409, message="No certificate has been issued to this guest.")
    reg.certificate_revoked_at = datetime.now(timezone.utc) if revoked else None
    await session.commit()
    return reg


async def summary(session, event: Events) -> dict:
    """Counted in the database rather than by loading every registration."""
    live = _live(event.id)
    versions, version = await _role_versions(session, event.id)
    held = (EventRegistrationsLink.certificate_issued_at.isnot(None), EventRegistrationsLink.certificate_revoked_at.is_(None))
    issued = await _count(session, *live, *held)
    revoked = await _count(session, *live, EventRegistrationsLink.certificate_revoked_at.isnot(None))
    outdated = await _count(
        session, *live, *held,
        func.coalesce(EventRegistrationsLink.certificate_design_version, 0) < _version_of_role(versions, version),
    )
    sends = await deliveries.summary(session, event, DeliveryKind.certificate)
    return {
        "issued": issued,
        "revoked": revoked,
        "attended": await _count(session, *live, EventRegistrationsLink.is_attended == True),  # noqa: E712
        "registrations": await _count(session, *live),
        "outdated": outdated,
        "design_version": version,
        # Unsent counts against those issued, not every registration.
        "channels": [{**c, "unsent": max(0, issued - (c["sent"] + c["failed"] + c["skipped"] + c["pending"]))} for c in sends["channels"]],
    }


def _row(reg: EventRegistrationsLink, sends: list) -> dict:
    role = getattr(reg.role, "value", reg.role) or "attendee"
    return {
        "registration_id": reg.id,
        "ticket_id": reg.ticket_id,
        "number": certificate_number(reg.ticket_id),
        "name": reg.full_name or "",
        "email": reg.email or None,
        "role": str(role),
        "status": "revoked" if reg.certificate_revoked_at else "issued",
        "issued_at": reg.certificate_issued_at,
        "revoked_at": reg.certificate_revoked_at,
        "design_version": reg.certificate_design_version or 0,
        "deliveries": sends,
    }


async def list_certificates(
    session, event: Events, *, status: Optional[str], search: Optional[str], page: int, page_size: int
) -> dict:
    q = _active(event.id).where(EventRegistrationsLink.certificate_issued_at.isnot(None))
    if status == "issued":
        q = q.where(EventRegistrationsLink.certificate_revoked_at.is_(None))
    elif status == "revoked":
        q = q.where(EventRegistrationsLink.certificate_revoked_at.isnot(None))
    if search:
        pat = f"%{search}%"
        # A certificate number is searched by the body it shares with the ticket.
        q = q.where(
            or_(
                EventRegistrationsLink.full_name.ilike(pat),
                EventRegistrationsLink.email.ilike(pat),
                EventRegistrationsLink.ticket_id.ilike(f"%{ticket_id_of_certificate(search).replace('MOA-', '')}%"),
            )
        )
    total = await session.scalar(select(func.count()).select_from(q.subquery())) or 0
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    regs = list(
        await session.scalars(
            q.order_by(EventRegistrationsLink.ticket_id).limit(page_size).offset((page - 1) * page_size)
        )
    )
    sends = await deliveries.by_registration(session, event.id, DeliveryKind.certificate, [r.id for r in regs])
    return {"items": [_row(r, sends.get(r.id, [])) for r in regs], "total": total, "page": page, "page_size": page_size}


def _safe_name(s: str) -> str:
    return (re.sub(r"[^A-Za-z0-9 _.-]+", "", s or "").strip()[:60]) or "guest"


class _Sink(io.RawIOBase):
    """Where the ZIP writes: it can say how much was written, never go back.
    zipfile then writes each entry's sizes after its data (a data
    descriptor), so an entry is complete the moment it is written and can
    be sent on before the next one is drawn."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._pos = 0

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        data = bytes(b)
        self._chunks.append(data)
        self._pos += len(data)
        return len(data)

    def tell(self) -> int:
        return self._pos

    def drain(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


async def bundle_jobs(session, event: Events) -> list[tuple[str, Optional[str], object]]:
    """Every issued certificate's file name, cache key and render, read from
    the database now -- so the stream that follows touches no session."""
    regs = list(
        await session.scalars(
            _active(event.id)
            .options(
                joinedload(EventRegistrationsLink.event).options(joinedload(Events.club)),
                joinedload(EventRegistrationsLink.user),
            )
            .where(
                EventRegistrationsLink.certificate_issued_at.isnot(None),
                EventRegistrationsLink.certificate_revoked_at.is_(None),
            )
            .order_by(EventRegistrationsLink.ticket_id)
        )
    )
    styles: dict = {}
    jobs = []
    for reg in regs:
        role = role_of(reg)
        if role not in styles:
            styles[role] = await get_template_for_event(session, event.id, role=role)
        job = certificate_job(reg, styles[role])
        jobs.append((f"{certificate_number(reg.ticket_id)} {_safe_name(reg.full_name)}.pdf", job.cache_key("certificate", reg.id), job.draw))
    return jobs


async def bundle_chunks(jobs: list) -> AsyncIterator[bytes]:
    """The ZIP, one certificate at a time: each is read from the bucket or
    drawn on the render pool, written, and sent before the next. The archive
    is never whole in memory, and the first bytes leave at once. Stored, not
    deflated: a PDF is compressed already."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as z:
        if not jobs:
            z.writestr("README.txt", "No certificates have been issued for this event yet.")
        for name, key, draw in jobs:
            z.writestr(name, await doc_cache.cached(key, draw))
            yield sink.drain()
    yield sink.drain()


async def bundle_zip(session, event: Events) -> bytes:
    """The whole ZIP as bytes (bundle_chunks, gathered)."""
    return b"".join([chunk async for chunk in bundle_chunks(await bundle_jobs(session, event))])


async def verify(session, code: str) -> dict:
    """Public. The code is what the QR carries, or the number printed beside it."""
    ticket_id = ticket_id_of_certificate(code)
    reg = await session.scalar(
        select(EventRegistrationsLink)
        .options(joinedload(EventRegistrationsLink.event).options(joinedload(Events.club)))
        .where(EventRegistrationsLink.ticket_id == ticket_id, EventRegistrationsLink.is_deleted == False)
    )
    if reg is None:
        return {"valid": False, "reason": "not_found"}
    held = availability(reg)
    if not held.available:
        return {"valid": False, "reason": "revoked" if held.reason == "revoked" else "not_found"}
    event = reg.event
    return {
        "valid": True,
        "reason": "ok",
        "number": certificate_number(reg.ticket_id),
        "attendee_name": reg.full_name,
        "event_name": event.name,
        "org_name": event.club.name if event.club else None,
        "issued_at": reg.certificate_issued_at or reg.attended_on,
    }


async def outdated_sends(session, event: Events) -> int:
    """Certificates emailed with a design older than the current one."""
    version = await _design_version(session, event.id)
    return await session.scalar(
        select(func.count()).select_from(Deliveries).where(
            Deliveries.event_id == event.id,
            Deliveries.kind == DeliveryKind.certificate,
            Deliveries.design_version < version,
        )
    ) or 0
