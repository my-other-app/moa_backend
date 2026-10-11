"""Issuing certificates, and everything that follows from one existing."""
import io
import logging
import re
import zipfile
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload

from app.api.certificate_templates.service import get_template_for_event
from app.api.deliveries import service as deliveries
from app.api.deliveries.models import Deliveries, DeliveryKind
from app.api.events.certificates import (
    availability,
    certificate_document,
    certificate_number,
    current_design_version,
    ticket_id_of_certificate,
)
from app.api.events.models import EventRegistrationsLink, Events
from app.api.templates_audience import RegistrationRole
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)


def _active(event_id: int):
    return select(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == event_id,
        EventRegistrationsLink.is_deleted == False,
    )


def _audience(event_id: int, audience: str, roles: list[str]):
    q = _active(event_id)
    if audience == "checked_in":
        return q.where(EventRegistrationsLink.is_attended == True)
    if audience == "roles":
        try:
            wanted = [RegistrationRole(r) for r in roles]
        except ValueError:
            raise CustomHTTPException(422, message="Pick roles from attendee, speaker and staff.", errors={"fields": {"roles": "unknown role"}})
        if not wanted:
            raise CustomHTTPException(422, message="Pick at least one role.", errors={"fields": {"roles": "Pick at least one role."}})
        return q.where(EventRegistrationsLink.role.in_(wanted))
    return q


async def _design_version(session, event_id: int) -> int:
    style = await get_template_for_event(session, event_id)
    return current_design_version(style)


async def issue_preview(session, event: Events, audience: str, roles: list[str]) -> dict:
    regs = list(await session.scalars(_audience(event.id, audience, roles)))
    version = await _design_version(session, event.id)
    issued = [r for r in regs if r.certificate_issued_at is not None]
    outdated = sum(1 for r in issued if r.certificate_revoked_at is None and (r.certificate_design_version or 0) < version)
    return {"eligible": len(regs), "already_issued": len(issued), "to_issue": len(regs) - len(issued), "outdated": outdated}


async def issue(session, event: Events, audience: str, roles: list[str]) -> dict:
    """Everyone in the audience who has none gets one, numbered and dated
    now; the ones issued with an older design are marked as refreshed. A
    guest is never issued twice, and a revoked one stays revoked."""
    regs = list(await session.scalars(_audience(event.id, audience, roles)))
    version = await _design_version(session, event.id)
    now = datetime.now(timezone.utc)
    issued = refreshed = already = 0
    for r in regs:
        if r.certificate_issued_at is None:
            r.certificate_issued_at = now
            r.certificate_design_version = version
            issued += 1
        elif r.certificate_revoked_at is None and (r.certificate_design_version or 0) < version:
            r.certificate_design_version = version
            refreshed += 1
        else:
            already += 1
    await session.commit()
    return {"issued": issued, "refreshed": refreshed, "already_issued": already}


async def set_revoked(session, event: Events, registration_id, revoked: bool) -> EventRegistrationsLink:
    reg = await deliveries._registration(session, event.id, registration_id)
    if reg.certificate_issued_at is None:
        raise CustomHTTPException(409, message="No certificate has been issued to this guest.")
    reg.certificate_revoked_at = datetime.now(timezone.utc) if revoked else None
    await session.commit()
    return reg


async def summary(session, event: Events) -> dict:
    regs = list(await session.scalars(_active(event.id)))
    version = await _design_version(session, event.id)
    issued = [r for r in regs if r.certificate_issued_at is not None and r.certificate_revoked_at is None]
    revoked = sum(1 for r in regs if r.certificate_revoked_at is not None)
    outdated = sum(1 for r in issued if (r.certificate_design_version or 0) < version)
    sends = await deliveries.summary(session, event, DeliveryKind.certificate)
    return {
        "issued": len(issued),
        "revoked": revoked,
        "attended": sum(1 for r in regs if r.is_attended),
        "registrations": len(regs),
        "outdated": outdated,
        "design_version": version,
        # Unsent counts against those issued, not every registration.
        "channels": [{**c, "unsent": max(0, len(issued) - (c["sent"] + c["failed"] + c["skipped"] + c["pending"]))} for c in sends["channels"]],
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


async def bundle_zip(session, event: Events) -> bytes:
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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if not regs:
            z.writestr("README.txt", "No certificates have been issued for this event yet.")
        for reg in regs:
            doc = await certificate_document(session, reg)
            z.writestr(f"{certificate_number(reg.ticket_id)} {_safe_name(reg.full_name)}.pdf", doc.content)
    return buf.getvalue()


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
