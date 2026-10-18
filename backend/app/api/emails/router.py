"""Designed emails for an event, the club's library of them, every message
sent, and the public file links those emails carry."""
import asyncio
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Body, File, Query, Response, UploadFile

from app.api.deliveries import service as deliveries
from app.api.emails import library_overview, service
from app.api.emails.models import EmailStage
from app.api.emails.schemas import (
    LibraryOverview,
    AudienceCount,
    AudienceRequest,
    EmailContext,
    EmailTemplateCreate,
    EmailTemplateOut,
    ImageUpload,
    MessageCreate,
    MessageDetail,
    MessageOut,
    MessagePage,
    PreviewRequest,
    PreviewResponse,
    TemplateSendRequest,
    TemplatesOut,
    TestSendRequest,
    TestSendResponse,
)
from app.api.events.certificates import availability, certificate_document
from app.api.events.models import EventRegistrationsLink, Events
from app.api.events.tickets import ticket_document
from app.core.auth.dependencies import ClubAuth
from app.core.mail import links
from app.core.mail.schema import EmailTemplateFields
from app.core.storage.images import store_image
from app.db.core import SessionDep
from app.response import CustomHTTPException
from sqlalchemy import select
from sqlalchemy.orm import joinedload

router = APIRouter(prefix="/events/{event_id}/emails")
messages_router = APIRouter(prefix="/events/{event_id}/messages")
library_router = APIRouter(prefix="/clubs/me/emails")
public_router = APIRouter(prefix="/files")

EMAIL_IMAGE_WIDTH = 1200


def _templates_out(data: dict) -> TemplatesOut:
    return TemplatesOut(
        journey=[{**s, "template": service.template_out(s["template"]), "last_message": service.message_out(s["last_message"]) if s["last_message"] else None} for s in data["journey"]],
        custom=[service.template_out(t) for t in data["custom"]],
    )


# --- an event's emails ------------------------------------------------------------


@router.get("", summary="The event's journey emails and custom ones")
async def list_templates(session: SessionDep, user: ClubAuth, event_id: int) -> TemplatesOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return _templates_out(await service.journey(session, event))


@router.get("/context", summary="What the editor needs: tokens, samples, brand")
async def context(session: SessionDep, user: ClubAuth, event_id: int) -> EmailContext:
    event = await deliveries.event_for_club(session, user, event_id)
    return EmailContext(**service.context(event, brand=await service.brand_primary(session, event)))


@router.post("", summary="A new custom email for this event")
async def create_template(session: SessionDep, user: ClubAuth, event_id: int, body: EmailTemplateCreate) -> EmailTemplateOut:
    event = await deliveries.event_for_club(session, user, event_id)
    fields = EmailTemplateFields.model_validate(body.model_dump(by_alias=True, exclude={"stage"}))
    tpl = await service.create_template(session, club_id=event.club_id, event_id=event.id, fields=fields, stage=EmailStage(body.stage), created_by=user.id)
    return EmailTemplateOut(**service.template_out(tpl))


@router.post("/preview", summary="The email as a guest would see it")
async def preview(session: SessionDep, user: ClubAuth, event_id: int, body: PreviewRequest) -> PreviewResponse:
    event = await deliveries.event_for_club(session, user, event_id)
    return PreviewResponse(**await service.preview(session, event, body.subject, body.design, registration_id=body.registration_id))


@router.post("/test", summary="Send this design to one address")
async def send_test(session: SessionDep, user: ClubAuth, event_id: int, body: TestSendRequest) -> TestSendResponse:
    event = await deliveries.event_for_club(session, user, event_id)
    return TestSendResponse(**await service.send_test(session, event, to=str(body.to), subject=body.subject, design=body.design, attachments=body.attachments, registration_id=body.registration_id))


@router.post("/audience", summary="How many guests an audience reaches")
async def audience(session: SessionDep, user: ClubAuth, event_id: int, body: AudienceRequest) -> AudienceCount:
    event = await deliveries.event_for_club(session, user, event_id)
    return AudienceCount(**await service.audience_count(session, event, body))


@router.post("/images", summary="A picture for an email")
async def upload_image(session: SessionDep, user: ClubAuth, event_id: int, file: UploadFile = File(...)) -> ImageUpload:
    event = await deliveries.event_for_club(session, user, event_id)
    # Decoding, shrinking and the S3 upload block: off the event loop.
    raw = await file.read()
    return ImageUpload(**await asyncio.to_thread(store_image, raw, service.image_prefix(event_id=event.id), max_width=EMAIL_IMAGE_WIDTH, name="email"))


@router.get("/{template_id}", summary="One email")
async def get_template(session: SessionDep, user: ClubAuth, event_id: int, template_id: UUID) -> EmailTemplateOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return EmailTemplateOut(**service.template_out(await service.template_of(session, template_id, event=event)))


@router.put("/{template_id}", summary="Save an email")
async def update_template(session: SessionDep, user: ClubAuth, event_id: int, template_id: UUID, body: EmailTemplateFields) -> EmailTemplateOut:
    event = await deliveries.event_for_club(session, user, event_id)
    tpl = await service.template_of(session, template_id, event=event)
    return EmailTemplateOut(**service.template_out(await service.update_template(session, tpl, body)))


@router.delete("/{template_id}", summary="Delete a custom email")
async def delete_template(session: SessionDep, user: ClubAuth, event_id: int, template_id: UUID) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    await service.remove_template(session, await service.template_of(session, template_id, event=event))
    return {"deleted": True}


@router.post("/{template_id}/duplicate", summary="A copy, as a custom email")
async def duplicate_template(session: SessionDep, user: ClubAuth, event_id: int, template_id: UUID) -> EmailTemplateOut:
    event = await deliveries.event_for_club(session, user, event_id)
    tpl = await service.template_of(session, template_id, event=event)
    return EmailTemplateOut(**service.template_out(await service.duplicate_template(session, tpl, created_by=user.id)))


@router.post("/{template_id}/save-to-library", summary="Make this the club's default for the stage")
async def save_to_library(session: SessionDep, user: ClubAuth, event_id: int, template_id: UUID) -> EmailTemplateOut:
    event = await deliveries.event_for_club(session, user, event_id)
    tpl = await service.template_of(session, template_id, event=event)
    return EmailTemplateOut(**service.template_out(await service.save_to_library(session, tpl)))


@router.post("/{template_id}/reset", summary="Back to the club's version of this email")
async def reset_template(session: SessionDep, user: ClubAuth, event_id: int, template_id: UUID) -> EmailTemplateOut:
    event = await deliveries.event_for_club(session, user, event_id)
    tpl = await service.template_of(session, template_id, event=event)
    return EmailTemplateOut(**service.template_out(await service.reset_template(session, tpl)))


@router.post("/{template_id}/send", summary="Send this email to its audience now")
async def send_template(
    session: SessionDep, user: ClubAuth, event_id: int, template_id: UUID,
    body: Optional[TemplateSendRequest] = Body(None),
) -> MessageOut:
    """The design is frozen into a message; sending happens after this
    answers. The message's counts say how it went. A `clientRef` the event
    has seen answers with that message and sends nothing again; without one,
    a second click while the first send is still going does the same."""
    event = await deliveries.event_for_club(session, user, event_id)
    tpl = await service.template_of(session, template_id, event=event)
    msg = await service.send_template(session, event, tpl, created_by=user.id, client_ref=body.client_ref if body else None)
    return MessageOut(**service.message_out(msg, user.full_name))


# --- messages -----------------------------------------------------------------------


@messages_router.get("", summary="Every message sent to this event's guests")
async def list_messages(session: SessionDep, user: ClubAuth, event_id: int, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> MessagePage:
    event = await deliveries.event_for_club(session, user, event_id)
    data = await service.list_messages(session, event, page=page, page_size=page_size)
    return MessagePage(items=[service.message_out(m) for m in data["items"]], total=data["total"], page=data["page"], page_size=data["page_size"])


@messages_router.post("", summary="Email the guests something now")
async def create_message(session: SessionDep, user: ClubAuth, event_id: int, body: MessageCreate) -> MessageOut:
    event = await deliveries.event_for_club(session, user, event_id)
    if body.client_ref is not None:
        # The composer's second click: the first send is the answer.
        earlier = await service.existing_message(session, event, body.client_ref)
        if earlier is not None:
            return MessageOut(**service.message_out(earlier, user.full_name))
    msg = await service.send_broadcast(
        session, event, subject=body.subject, body=body.body, design=body.design, attachments=body.attachments, audience=body.audience,
        client_ref=body.client_ref, created_by=user.id,
    )
    return MessageOut(**service.message_out(msg, user.full_name))


@messages_router.get("/{message_id}", summary="One message and who it reached")
async def get_message(
    session: SessionDep, user: ClubAuth, event_id: int, message_id: UUID,
    problem: bool = False, search: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
) -> MessageDetail:
    event = await deliveries.event_for_club(session, user, event_id)
    msg = await service.message_of(session, event, message_id)
    data = await service.message_deliveries(session, msg, problem=problem, search=search, page=page, page_size=page_size)
    return MessageDetail(message=service.message_out(msg), **data)


# --- the club's library --------------------------------------------------------------


@library_router.get("", summary="The club's email library")
async def list_library(session: SessionDep, user: ClubAuth) -> list[EmailTemplateOut]:
    club = await service.club_of(session, user)
    return [EmailTemplateOut(**service.template_out(t)) for t in await service.ensure_library(session, club.id)]


@library_router.post("", summary="A new custom email in the library")
async def create_library_template(session: SessionDep, user: ClubAuth, body: EmailTemplateCreate) -> EmailTemplateOut:
    club = await service.club_of(session, user)
    fields = EmailTemplateFields.model_validate(body.model_dump(by_alias=True, exclude={"stage"}))
    tpl = await service.create_template(session, club_id=club.id, event_id=None, fields=fields, stage=EmailStage(body.stage), created_by=user.id)
    return EmailTemplateOut(**service.template_out(tpl))


@library_router.post("/preview", summary="A library email with sample values")
async def preview_library(session: SessionDep, user: ClubAuth, body: PreviewRequest) -> PreviewResponse:
    club = await service.club_of(session, user)
    return PreviewResponse(**await service.preview(session, service.sample_event(club), body.subject, body.design))


@library_router.get("/context", summary="What the library editor needs")
async def library_context(session: SessionDep, user: ClubAuth) -> EmailContext:
    club = await service.club_of(session, user)
    return EmailContext(**service.context(service.sample_event(club), brand="#1db9a0"))


@library_router.post("/images", summary="A picture for a library email")
async def upload_library_image(session: SessionDep, user: ClubAuth, file: UploadFile = File(...)) -> ImageUpload:
    club = await service.club_of(session, user)
    raw = await file.read()
    return ImageUpload(**await asyncio.to_thread(store_image, raw, service.image_prefix(club_id=club.id), max_width=EMAIL_IMAGE_WIDTH, name="email"))


@library_router.get("/overview", summary="The library with how each email is used and what it did, the month in figures, and what goes out next")
async def library_overview_endpoint(session: SessionDep, user: ClubAuth) -> LibraryOverview:
    club = await service.club_of(session, user)
    return LibraryOverview(**await library_overview.overview(session, club))


@library_router.get("/{template_id}", summary="One library email")
async def get_library_template(session: SessionDep, user: ClubAuth, template_id: UUID) -> EmailTemplateOut:
    club = await service.club_of(session, user)
    return EmailTemplateOut(**service.template_out(await service.template_of(session, template_id, club_id=club.id)))


@library_router.put("/{template_id}", summary="Save a library email")
async def update_library_template(session: SessionDep, user: ClubAuth, template_id: UUID, body: EmailTemplateFields) -> EmailTemplateOut:
    club = await service.club_of(session, user)
    tpl = await service.template_of(session, template_id, club_id=club.id)
    return EmailTemplateOut(**service.template_out(await service.update_template(session, tpl, body)))


@library_router.delete("/{template_id}", summary="Delete a custom library email")
async def delete_library_template(session: SessionDep, user: ClubAuth, template_id: UUID) -> dict:
    club = await service.club_of(session, user)
    await service.remove_template(session, await service.template_of(session, template_id, club_id=club.id))
    return {"deleted": True}


@library_router.post("/{template_id}/duplicate", summary="A copy in the library")
async def duplicate_library_template(session: SessionDep, user: ClubAuth, template_id: UUID) -> EmailTemplateOut:
    club = await service.club_of(session, user)
    tpl = await service.template_of(session, template_id, club_id=club.id)
    return EmailTemplateOut(**service.template_out(await service.duplicate_template(session, tpl, created_by=user.id)))


# --- files a guest opens from an email ---------------------------------------------------


async def _registration_by_ticket(session, ticket_id: str) -> EventRegistrationsLink:
    reg = await session.scalar(
        select(EventRegistrationsLink)
        .options(joinedload(EventRegistrationsLink.event).joinedload(Events.club), joinedload(EventRegistrationsLink.user))
        .where(EventRegistrationsLink.ticket_id == ticket_id, EventRegistrationsLink.is_deleted == False)
    )
    if reg is None:
        raise CustomHTTPException(404, message="No such ticket")
    return reg


def _check(kind: str, ticket_id: str, s: str) -> None:
    if not links.file_token_ok(kind, ticket_id, s):
        raise CustomHTTPException(403, message="This link is not valid.")


@public_router.get("/tickets/{ticket_id}.pdf", summary="A guest's ticket, from the link in their email")
async def ticket_file(session: SessionDep, ticket_id: str, s: str = Query("")) -> Response:
    _check("ticket", ticket_id, s)
    reg = await _registration_by_ticket(session, ticket_id)
    if reg.ticket_revoked_at is not None:
        raise CustomHTTPException(410, message="This ticket has been revoked.")
    doc = await ticket_document(session, reg)
    return Response(content=doc.content, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{doc.filename}"'})


@public_router.get("/certificates/{ticket_id}.pdf", summary="A guest's certificate, from the link in their email")
async def certificate_file(session: SessionDep, ticket_id: str, s: str = Query("")) -> Response:
    _check("certificate", ticket_id, s)
    reg = await _registration_by_ticket(session, ticket_id)
    if not availability(reg).available:
        raise CustomHTTPException(410, message="This certificate is not available.")
    doc = await certificate_document(session, reg)
    return Response(content=doc.content, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{doc.filename}"'})
