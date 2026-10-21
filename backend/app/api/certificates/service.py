"""Issuing certificates, and everything that follows from one existing."""
import asyncio
import hashlib
import io
import logging
import re
import zipfile
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import joinedload

from app.api.certificate_templates.service import get_template_for_event
from app.api.certificates.models import CertificateFiles
from app.api.deliveries import service as deliveries
from app.api.deliveries.models import Deliveries, DeliveryKind
from app.api.events import doc_cache
from app.api.events.certificates import (
    CERTIFICATE_PREFIX,
    availability,
    certificate_context,
    certificate_job,
    certificate_style,
    current_design_version,
    number_of,
    ticket_id_of_certificate,
)
from app.api.events.documents import image_width, in_layout_mode, layout_png
from app.api.events.tickets import role_of
from app.api.events.models import EventRegistrationsLink, Events
from app.api.templates_audience import RegistrationRole
from app.api.templates_images import CERTIFICATE_FIELDS, embedded
from app.config import settings
from app.core.layout import pool
from app.core.storage import objects
from app.core.storage.bucket import prefix
from app.core.utils.keys import CERTIFICATE_CODE, TICKET_ALPHABET
from app.core.utils.pdf import generate_image_bytes
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
        "number": number_of(reg),
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
        # A certificate's own number, or for an older one, the body its
        # number shares with the ticket.
        q = q.where(
            or_(
                EventRegistrationsLink.full_name.ilike(pat),
                EventRegistrationsLink.email.ilike(pat),
                EventRegistrationsLink.certificate_no.ilike(f"%{search.strip()}%"),
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
        jobs.append((f"{number_of(reg)} {_safe_name(reg.full_name)}.pdf", job.cache_key("certificate", reg.id), job.draw))
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


# ─── The public verifier ────────────────────────────────────────────────────

#: Looked for in place of a code that cannot be a number, so a malformed
#: code costs the same lookup, and gets the same answer, as one not found.
_NO_SUCH_NUMBER = "CERT-?"
#: The copies of one certificate fetched and hashed in one ask, at most.
FILES_PER_ASK = 10
#: How long listing a certificate's copies in the bucket may take.
LIST_TIMEOUT = 5.0
_FLAT_CODE = re.compile(rf"^(?:CERT|MOA)([{TICKET_ALPHABET}]{{5}}|[0-9A-F]{{1,7}})([{TICKET_ALPHABET}]{{5}})$")


def code_number(code: str) -> Optional[str]:
    """The certificate number a typed, scanned or pasted code names, upper
    case, or None when it cannot be one.

    Takes the number printed on a certificate (CERT-XXXXX-XXXXX), what an
    older certificate's QR held (certificate:MOA-XXXXX-XXXXX, its ticket id)
    and the ticket id alone, in any case and with spaces anywhere. Whatever
    else is never looked up as typed.
    """
    text = re.sub(r"\s", "", code or "")[:80]
    if text.lower().startswith("certificate:"):
        text = text[len("certificate:"):]
    found = CERTIFICATE_CODE.match(text.upper())
    if found:
        return f"{CERTIFICATE_PREFIX}-{found.group(1)}"
    # Typed without its dashes (CERT7K3QFX9PWM): the last five are the second group.
    flat = _FLAT_CODE.match(text.upper().replace("-", ""))
    return f"{CERTIFICATE_PREFIX}-{flat.group(1)}-{flat.group(2)}" if flat else None


async def certificate_by_number(session, number: Optional[str]) -> Optional[EventRegistrationsLink]:
    """The live registration whose certificate has this number, with its
    event and club, in one lookup on two unique indexes.

    Its own number first. A number made from a ticket id finds only a
    registration with no number of its own, whose certificate was printed
    before numbers were drawn separately: one that has its own never answers
    to its ticket id, so the verifier tells no one who holds a ticket.
    """
    number = number or _NO_SUCH_NUMBER
    ticket_id = "MOA-" + number.split("-", 1)[1]
    return await session.scalar(
        select(EventRegistrationsLink)
        .options(joinedload(EventRegistrationsLink.event).joinedload(Events.club))
        .where(
            EventRegistrationsLink.is_deleted == False,  # noqa: E712
            or_(
                EventRegistrationsLink.certificate_no == number,
                and_(EventRegistrationsLink.certificate_no.is_(None), EventRegistrationsLink.ticket_id == ticket_id),
            ),
        )
        .order_by(EventRegistrationsLink.certificate_no.is_(None))
        .limit(1)
    )


async def held_certificate(session, code: str) -> Optional[EventRegistrationsLink]:
    """The registration holding a valid certificate under this code, or None."""
    reg = await certificate_by_number(session, code_number(code))
    return reg if reg is not None and availability(reg).available else None


def _club_facts(event) -> dict:
    club = getattr(event, "club", None)
    return {
        "org_name": club.name if club is not None else None,
        # The club's page on the site, only while the public may see it.
        "org_slug": club.slug if club is not None and club.listed and not club.is_deleted else None,
    }


async def verify(session, code: str) -> dict:
    """Public. Whether the certificate is genuine and still valid, whose,
    for what, from whom and when -- nothing more. A revoked one says which
    event and club took it back, not whose it was."""
    reg = await certificate_by_number(session, code_number(code))
    if reg is None:
        return {"valid": False, "reason": "not_found"}
    held = availability(reg)
    if held.reason == "revoked":
        return {"valid": False, "reason": "revoked", "number": number_of(reg), "event_name": reg.event.name, **_club_facts(reg.event)}
    if not held.available:
        return {"valid": False, "reason": "not_found"}
    return {
        "valid": True,
        "reason": "ok",
        "number": number_of(reg),
        "attendee_name": reg.full_name,
        "event_name": reg.event.name,
        **_club_facts(reg.event),
        "issued_at": reg.certificate_issued_at or reg.attended_on,
    }


async def preview_png(session, reg: EventRegistrationsLink) -> bytes:
    """The certificate as a PNG: the render the app previews
    (GET /events/certificates/{ticket}/image), same design, same engine."""
    style = await certificate_style(session, reg)
    job = certificate_job(reg, style)
    if in_layout_mode(style):
        pdf = await doc_cache.cached(job.cache_key("certificate", reg.id), job.draw)
        return await pool.run(layout_png, style, pdf)
    context = certificate_context(reg, style)
    width = image_width(context["style"], default=1400)

    def draw() -> bytes:
        inline, _ = embedded(context["style"], fields=CERTIFICATE_FIELDS)
        return generate_image_bytes(template_path="events/certificate.html", context={**context, "style": inline}, width=width).getvalue()

    return await pool.run(draw)


async def file_digests(session, reg: EventRegistrationsLink) -> list[str]:
    """The SHA-256 of every copy of this certificate that was handed out.

    Each copy is the PDF kept under docs/certificate/{registration}/ (a
    download, an email, the bundle all hand out the one kept there; a
    redesign keeps another). What is already hashed is read from
    certificate_files; a copy kept since is fetched and hashed now, and
    remembered. With the cache off, or the bucket not answering, it is what
    was hashed before: an empty list means no file can be checked.
    """
    rows = (await session.scalars(select(CertificateFiles).where(CertificateFiles.registration_id == reg.id))).all()
    known = {row.key: row.sha256 for row in rows}
    if settings.RENDER_DOC_CACHE:
        base = prefix("docs", "certificate", str(reg.id))
        try:
            keys = await asyncio.wait_for(asyncio.to_thread(objects.list_keys, base), timeout=LIST_TIMEOUT)
        except Exception:  # noqa: BLE001 - the check is a courtesy; the verification stands without it
            logger.warning("the kept copies of certificate %s could not be listed", reg.id, exc_info=True)
            keys = []
        fresh = [k for k in keys if k not in known and k.endswith(".pdf")][:FILES_PER_ASK]
        added = False
        for key in fresh:
            try:
                data = await asyncio.to_thread(objects.fetch, key, quiet_if_missing=True)
            except Exception:  # noqa: BLE001
                logger.warning("a kept copy of certificate %s could not be read", reg.id, exc_info=True)
                data = None
            if not data:
                continue
            digest = hashlib.sha256(data).hexdigest()
            await session.execute(
                pg_insert(CertificateFiles)
                .values(registration_id=reg.id, key=key, sha256=digest, size=len(data))
                .on_conflict_do_nothing()
            )
            known[key] = digest
            added = True
        if added:
            await session.commit()
    return sorted(set(known.values()))


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
