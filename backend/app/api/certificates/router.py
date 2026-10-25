"""Certificates: who gets one, issuing them, sending them, and checking one."""
from functools import lru_cache
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse
from limits import parse_many

from app.api.certificates import service
from app.api.certificates.schemas import (
    CertificatePage,
    CertificateState,
    CertificateSummary,
    FileCheck,
    IssuePreview,
    IssueRequest,
    IssueResponse,
    Verification,
)
from app.api.deliveries import service as deliveries
from app.api.deliveries.models import DeliveryKind
from app.api.deliveries.schemas import SendResponse, SendTicketsRequest
from app.api.events.certificates import number_of
from app.config import settings
from app.core import site
from app.core.auth.dependencies import ClubAuth
from app.core.auth.permissions import EXPORT_DATA, ensure_admin_can
from app.core.net import client_address
from app.core.rate_limit import limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/events/{event_id}/certificates")
public_router = APIRouter(prefix="/verify")


@router.get("/summary", summary="How many certificates exist, and how many were sent")
async def summary(session: SessionDep, user: ClubAuth, event_id: int) -> CertificateSummary:
    event = await deliveries.event_for_club(session, user, event_id)
    return CertificateSummary(**await service.summary(session, event))


@router.post("/issue-preview", summary="Who would be issued one")
async def issue_preview(session: SessionDep, user: ClubAuth, event_id: int, body: IssueRequest) -> IssuePreview:
    event = await deliveries.event_for_club(session, user, event_id)
    return IssuePreview(**await service.issue_preview(session, event, body.audience, body.roles))


@router.post("/issue", summary="Issue certificates to the audience")
async def issue(session: SessionDep, user: ClubAuth, event_id: int, body: IssueRequest) -> IssueResponse:
    """Idempotent: a guest is never issued twice. Issuing again after a
    design change marks the ones made with the older design as refreshed."""
    event = await deliveries.event_for_club(session, user, event_id)
    return IssueResponse(**await service.issue(session, event, body.audience, body.roles))


@router.post("/send", summary="Email certificates to the guests issued one")
async def send(
    session: SessionDep, user: ClubAuth, event_id: int, body: SendTicketsRequest
) -> SendResponse:
    event = await deliveries.event_for_club(session, user, event_id)
    result = await deliveries.queue(
        session, event, DeliveryKind.certificate, registration_ids=body.registration_ids, only=body.only, force=body.only == "all", send=True,
    )
    return SendResponse(**{k: v for k, v in result.items() if k != "ids"})


@router.post("/{registration_id}/send", summary="One guest's certificate again")
async def send_one(
    session: SessionDep, user: ClubAuth, event_id: int, registration_id: UUID
) -> SendResponse:
    event = await deliveries.event_for_club(session, user, event_id)
    result = await deliveries.queue(
        session, event, DeliveryKind.certificate, registration_ids=[registration_id], only="all", force=True, send=True
    )
    return SendResponse(**{k: v for k, v in result.items() if k != "ids"})


@router.post("/{registration_id}/revoke", summary="Stop this certificate verifying")
async def revoke(session: SessionDep, user: ClubAuth, event_id: int, registration_id: UUID) -> CertificateState:
    event = await deliveries.event_for_club(session, user, event_id)
    reg = await service.set_revoked(session, event, registration_id, True)
    return CertificateState(registration_id=reg.id, number=number_of(reg), revoked_at=reg.certificate_revoked_at)


@router.post("/{registration_id}/reinstate", summary="Let this certificate verify again")
async def reinstate(session: SessionDep, user: ClubAuth, event_id: int, registration_id: UUID) -> CertificateState:
    event = await deliveries.event_for_club(session, user, event_id)
    reg = await service.set_revoked(session, event, registration_id, False)
    return CertificateState(registration_id=reg.id, number=number_of(reg), revoked_at=reg.certificate_revoked_at)


@router.get("/bundle.zip", summary="Every issued certificate, one PDF per guest")
async def bundle(session: SessionDep, user: ClubAuth, event_id: int) -> StreamingResponse:
    """Streamed: each certificate is sent as soon as it is read from the
    bucket or drawn, so the archive is never held whole in memory and the
    download starts at once. Everything is read from the database first."""
    # Every guest's certificate at once: for an admin, a data export.
    ensure_admin_can(user, EXPORT_DATA)
    event = await deliveries.event_for_club(session, user, event_id)
    jobs = await service.bundle_jobs(session, event)
    return StreamingResponse(
        service.bundle_chunks(jobs),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{event.slug}-certificates.zip"'},
    )


@router.get("", summary="The certificates issued, by number")
async def list_certificates(
    session: SessionDep,
    user: ClubAuth,
    event_id: int,
    status: Optional[str] = Query(None, pattern="^(issued|revoked)$"),
    search: Optional[str] = Query(None, max_length=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> CertificatePage:
    event = await deliveries.event_for_club(session, user, event_id)
    return CertificatePage(**await service.list_certificates(session, event, status=status, search=search, page=page, page_size=page_size))


# ─── The public verifier ────────────────────────────────────────────────────
#
# Anyone may check a certificate, by its number, its QR or a link ending in
# either; never by a name or an address, so the verifier is no directory of
# who went where. Each ask is counted before anything is looked up, a miss
# as much as a match (settings.CERTIFICATE_VERIFY_LIMIT), per person: the
# site renders its result pages on Vercel, from addresses every visitor
# shares, so the visitor it names in X-Visitor-IP is believed -- only
# behind a valid X-Site-Token (app/core/site.py) -- and the site as a whole
# has a bucket of its own on top (CERTIFICATE_VERIFY_SITE_LIMIT). Anyone
# else is counted by their own address. Nothing here is ever cached.

NO_STORE = {"Cache-Control": "no-store"}


def verifier(request: Request) -> str:
    """Who is checking: the visitor the site's server names, else the caller."""
    return site.visitor_ip(request) or client_address(request) or "unknown"


@lru_cache(maxsize=None)
def _items(rate: str):
    return tuple(parse_many(rate))


def _spent(rate: str, scope: str, who: str) -> bool:
    """Count one ask against `rate` for `who`; True when it is over."""
    for item in _items(rate):
        if not limiter.limiter.hit(item, scope, who):
            return True
    return False


def _count(request: Request, scope: str) -> None:
    if not limiter.enabled:
        return
    over = _spent(settings.CERTIFICATE_VERIFY_LIMIT, scope, verifier(request))
    bucket = site.token_id(request)
    if not over and bucket is not None:
        over = _spent(settings.CERTIFICATE_VERIFY_SITE_LIMIT, f"{scope}:site", bucket)
    if over:
        raise CustomHTTPException(
            429, message="Too many certificate checks from here. Wait a minute, then try again.", error_code="TOO_MANY_CHECKS",
        )


@public_router.get("/certificates/{code}", summary="What a certificate's number, QR or link says")
async def verify(request: Request, response: Response, session: SessionDep, code: str) -> Verification:
    """Public: no session needed. Says whether the certificate is genuine and
    still valid, whose, for what, from whom and when -- nothing more. A code
    that cannot be a number gets the same answer as one not found."""
    _count(request, "certificates:verify")
    response.headers.update(NO_STORE)
    return Verification(**await service.verify(session, code))


@public_router.get("/certificates/{code}/image", summary="A valid certificate as a picture, to compare with the one shown")
async def verify_image(request: Request, session: SessionDep, code: str) -> Response:
    """The certificate the number names, drawn as the app previews it, while
    it is valid; 404 for anything else, revoked or not found alike."""
    _count(request, "certificates:verify-image")
    reg = await service.held_certificate(session, code)
    if reg is None:
        raise CustomHTTPException(404, message="No certificate has this number.")
    png = await service.preview_png(session, reg)
    return Response(content=png, media_type="image/png", headers={**NO_STORE, "X-Content-Type-Options": "nosniff"})


@public_router.get("/certificates/{code}/files", summary="The SHA-256 of each copy of a valid certificate handed out")
async def verify_files(request: Request, response: Response, session: SessionDep, code: str) -> FileCheck:
    """For checking a file in the browser: the page hashes the file there
    and compares, and the file is never sent. Revoked or not found: none."""
    _count(request, "certificates:verify-files")
    response.headers.update(NO_STORE)
    reg = await service.held_certificate(session, code)
    digests = await service.file_digests(session, reg) if reg is not None else []
    return FileCheck(available=bool(digests), sha256=digests)
