"""Certificates: who gets one, issuing them, sending them, and checking one."""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import StreamingResponse

from app.api.certificates import service
from app.api.certificates.schemas import (
    CertificatePage,
    CertificateState,
    CertificateSummary,
    IssuePreview,
    IssueRequest,
    IssueResponse,
    Verification,
)
from app.api.deliveries import service as deliveries
from app.api.deliveries.models import DeliveryKind
from app.api.deliveries.schemas import SendResponse, SendTicketsRequest
from app.api.events.certificates import certificate_number
from app.core.auth.dependencies import ClubAuth
from app.core.net import client_address
from app.core.rate_limit import limiter
from app.db.core import SessionDep

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
    return CertificateState(registration_id=reg.id, number=certificate_number(reg.ticket_id), revoked_at=reg.certificate_revoked_at)


@router.post("/{registration_id}/reinstate", summary="Let this certificate verify again")
async def reinstate(session: SessionDep, user: ClubAuth, event_id: int, registration_id: UUID) -> CertificateState:
    event = await deliveries.event_for_club(session, user, event_id)
    reg = await service.set_revoked(session, event, registration_id, False)
    return CertificateState(registration_id=reg.id, number=certificate_number(reg.ticket_id), revoked_at=reg.certificate_revoked_at)


@router.get("/bundle.zip", summary="Every issued certificate, one PDF per guest")
async def bundle(session: SessionDep, user: ClubAuth, event_id: int) -> StreamingResponse:
    """Streamed: each certificate is sent as soon as it is read from the
    bucket or drawn, so the archive is never held whole in memory and the
    download starts at once. Everything is read from the database first."""
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


def _visitor(request: Request) -> str:
    return client_address(request) or "unknown"


@public_router.get("/certificates/{code}", summary="What a scanned certificate code says")
@limiter.limit("60/minute", key_func=_visitor)
async def verify(request: Request, session: SessionDep, code: str) -> Verification:
    """Public: no session needed. Says whether the certificate is genuine and
    still valid, whose it is, for what, and when -- nothing more."""
    return Verification(**await service.verify(session, code))
