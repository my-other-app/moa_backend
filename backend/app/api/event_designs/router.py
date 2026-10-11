"""An event's own ticket and certificate designs.

The club library at /ticket-templates and /certificate-templates still
exists and is what an event starts from. These endpoints are the event's
own copy: reading one resolves through the chain, and saving one forks it
so that event stops following the club's.

Keeping the two apart is the point. Before this, editing a club template
silently rewrote the design of every event that had already used it,
including events whose tickets were already in people's hands.
"""

import enum
import io
import os
from uuid import uuid4
from typing import Optional

from fastapi import APIRouter, File, Form, Query, Response, UploadFile
from PIL import Image
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.api.certificate_templates.models import CertificateTemplates
from app.api.agenda import service as agenda_service
from app.api.agenda.models import AgendaTemplates
from app.api.agenda.service import agenda_default_config
from app.api.idcards.models import BadgeTemplates
from app.api.idcards.service import badge_default_config
from app.api.certificate_templates.schemas import CertificateTemplateConfig
from app.api.certificate_templates.service import (
    _default_config as certificate_default_config,
)
from app.api.events.documents import (
    EVENT_TIMEZONE,
    apply_layers,
    document_values,
    image_width,
    in_layout_mode,
    layout_colors,
    layout_png,
    layout_sample_values,
    layout_values,
    qr_data_url,
    qr_png_base64,
    render_layout_document,
    sample_values,
)
from app.core.layout.engine import layout_problems, problems_as_fields
from app.core.layout.model import PageSpec, TicketLayoutFields, dump
from app.core.layout.starters import agenda_starter, badge_starter, certificate_starter, ticket_starter, AGENDA_STARTERS, BADGE_STARTERS, CERTIFICATE_STARTERS, TICKET_STARTERS
from app.core.layout.tokens import TICKET_TOKENS, field_token
from app.api.events.models import EventRegistrationsLink
from app.api.events.access import ensure_event_access, is_admin_user
from app.api.events.models import Events
from app.api.templates_audience import RegistrationRole, TemplateAudience
from app.api.templates_resolve import fork_for_event, resolve_template
from app.api.ticket_templates.models import TicketTemplates
from app.api.ticket_templates.schemas import TicketTemplateConfig
from app.core.utils.pdf import generate_image_bytes
from app.api.ticket_templates.service import _default_config as ticket_default_config
from app.config import settings
from app.core.auth.dependencies import ClubAuth
from app.core.storage import objects
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/events/{event_id}/designs")


class DesignKind(str, enum.Enum):
    ticket = "ticket"
    badge = "badge"
    certificate = "certificate"
    # The programme: the run of show as a designed screen or page. One per event, no QR.
    agenda = "agenda"


_MODELS = {
    DesignKind.ticket: TicketTemplates,
    DesignKind.badge: BadgeTemplates,
    DesignKind.certificate: CertificateTemplates,
    DesignKind.agenda: AgendaTemplates,
}
_DEFAULTS = {
    DesignKind.ticket: ticket_default_config,
    DesignKind.badge: badge_default_config,
    DesignKind.certificate: certificate_default_config,
    DesignKind.agenda: agenda_default_config,
}


class DesignResponse(BaseModel):
    """A design, and where it came from."""

    config: dict
    audience: str

    # Which level answered. The editor can then say "you are looking at the
    # club default" rather than implying the event has been designed when it
    # has not.
    source: str

    # True once the event owns this design for this audience, so the editor
    # offers "reset to the club default" only when there is something to
    # reset.
    is_own: bool
    template_id: Optional[int] = None


class DesignSave(BaseModel):
    config: dict = Field(..., description="The whole design, as the editor holds it")
    name: Optional[str] = Field(default=None, max_length=100)


def _role_for(audience: TemplateAudience) -> Optional[RegistrationRole]:
    """The role whose variant this audience asks for, if it is a role at all."""
    if audience is TemplateAudience.event_default:
        return None
    return RegistrationRole(audience.value)


async def _event_for_editing(session, user, event_id: int) -> Events:
    """The event, once the caller is allowed to act on it."""
    event = await session.scalar(
        select(Events)
        .options(joinedload(Events.club))
        .where(Events.id == event_id, Events.is_deleted == False)
    )
    if event is None:
        raise CustomHTTPException(404, message="Event not found")
    ensure_event_access(
        event,
        user.id,
        as_admin=is_admin_user(user),
        message="Not authorized to design this event",
    )
    return event


def _describe(template, event_id: int, audience: TemplateAudience) -> DesignResponse:
    """Turn a resolved row into the answer, naming the level it came from."""
    if template.event_id == event_id:
        source = (
            "event_default"
            if template.audience is TemplateAudience.event_default
            else "event_role"
        )
    else:
        source = "club_default"

    return DesignResponse(
        config=template.config or {},
        audience=audience.value,
        source=source,
        is_own=template.event_id == event_id and template.audience is audience,
        template_id=template.id,
    )


@router.get("/{kind}", summary="The design this event is currently using")
async def get_design(
    session: SessionDep,
    user: ClubAuth,
    event_id: int,
    kind: DesignKind,
    audience: TemplateAudience = Query(TemplateAudience.event_default),
) -> DesignResponse:
    event = await _event_for_editing(session, user, event_id)
    model = _MODELS[kind]

    template = await resolve_template(
        session,
        model,
        event_id=event_id,
        club_id=event.club_id,
        role=_role_for(audience),
    )

    if template is None:
        # Nothing anywhere up the chain, so the editor opens on the same
        # starting design a brand new club template would.
        return DesignResponse(
            config=_DEFAULTS[kind](),
            audience=audience.value,
            source="system",
            is_own=False,
        )

    return _describe(template, event_id, audience)


@router.put("/{kind}", summary="Save this event's design")
async def save_design(
    session: SessionDep,
    user: ClubAuth,
    event_id: int,
    kind: DesignKind,
    payload: DesignSave,
    audience: TemplateAudience = Query(TemplateAudience.event_default),
) -> DesignResponse:
    """Saving forks the design onto the event if it has not been already.

    An organiser who opens an event, changes one colour and saves ends up
    with an event that owns its design from then on -- and the club
    template they started from is left untouched.
    """
    event = await _event_for_editing(session, user, event_id)
    model = _MODELS[kind]

    template = await fork_for_event(
        session,
        model,
        event_id=event_id,
        club_id=event.club_id,
        audience=audience,
        name=payload.name or f"{event.name} {kind.value}",
    )
    template.config = payload.config
    if payload.name:
        template.name = payload.name

    await session.commit()
    await session.refresh(template)
    return _describe(template, event_id, audience)


@router.delete("/{kind}", summary="Stop using this event's own design")
async def reset_design(
    session: SessionDep,
    user: ClubAuth,
    event_id: int,
    kind: DesignKind,
    audience: TemplateAudience = Query(TemplateAudience.event_default),
) -> DesignResponse:
    """Drop the event's copy so it follows the club default again.

    Soft deleted like everything else here, so a reset made by mistake is
    recoverable rather than gone.
    """
    await _event_for_editing(session, user, event_id)
    model = _MODELS[kind]

    own = await session.scalar(
        select(model).where(
            model.event_id == event_id,
            model.audience == audience,
            model.is_deleted == False,
        )
    )
    if own is None:
        raise CustomHTTPException(
            404, message="This event has no design of its own to reset"
        )

    own.is_deleted = True
    await session.commit()

    return await get_design(
        session=session,
        user=user,
        event_id=event_id,
        kind=kind,
        audience=audience,
    )


# --- artwork -------------------------------------------------------------------
#
# Kept separate from the config save above. The config is saved often -- every
# nudge of a layer -- and sending the artwork with it would re-upload a
# multi-megabyte PNG on each one. Artwork changes rarely, so it gets its own
# request.


def _extract_url(value) -> Optional[str]:
    """The stored URL for an image field, whatever shape it is in."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "original" in value:
        return value["original"]
    return None


_IMAGE_FIELDS = {
    DesignKind.ticket: ("logo", "secondary_logo", "background_image"),
    DesignKind.badge: (),
    DesignKind.agenda: (),
    DesignKind.certificate: (
        "logo",
        "secondary_logo",
        "background_image",
        "signature_1",
        "signature_2",
    ),
}


def _sync_urls(template, kind: DesignKind) -> None:
    """Copy the stored image URLs into the config the renderer reads.

    The templates read `background_image_url` out of the config rather than
    the column, so an upload that skips this step stores the file and
    changes nothing on the document.
    """
    config = dict(template.config) if template.config else {}
    for field in _IMAGE_FIELDS[kind]:
        config[f"{field}_url"] = _extract_url(getattr(template, field, None))
    template.config = config


@router.put("/{kind}/artwork", summary="Replace this event's artwork")
async def save_artwork(
    session: SessionDep,
    user: ClubAuth,
    event_id: int,
    kind: DesignKind,
    audience: TemplateAudience = Query(TemplateAudience.event_default),
    logo: Optional[UploadFile] = File(None),
    secondary_logo: Optional[UploadFile] = File(None),
    background_image: Optional[UploadFile] = File(None),
    signature_1: Optional[UploadFile] = File(None),
    signature_2: Optional[UploadFile] = File(None),
    remove_logo: bool = Form(False),
    remove_secondary_logo: bool = Form(False),
    remove_background_image: bool = Form(False),
    remove_signature_1: bool = Form(False),
    remove_signature_2: bool = Form(False),
) -> DesignResponse:
    """Upload artwork for this event's design, forking it first.

    Forking before writing is the point: without it, an organiser replacing
    the background on one event would replace it on the club template and so
    on every other event still following it.

    A field left out is left alone. Sending an empty one would read as
    "clear this image", which would wipe a logo on a save that never touched
    it -- the same trap the club endpoints document.
    """
    event = await _event_for_editing(session, user, event_id)
    model = _MODELS[kind]
    allowed = _IMAGE_FIELDS[kind]

    template = await fork_for_event(
        session,
        model,
        event_id=event_id,
        club_id=event.club_id,
        audience=audience,
        name=f"{event.name} {kind.value}",
    )

    uploads = {
        "logo": logo,
        "secondary_logo": secondary_logo,
        "background_image": background_image,
        "signature_1": signature_1,
        "signature_2": signature_2,
    }
    removals = {
        "logo": remove_logo,
        "secondary_logo": remove_secondary_logo,
        "background_image": remove_background_image,
        "signature_1": remove_signature_1,
        "signature_2": remove_signature_2,
    }

    for field in allowed:
        if removals[field]:
            setattr(template, field, None)
        upload = uploads[field]
        if upload is not None and upload.filename:
            setattr(
                template,
                field,
                {"bytes": io.BytesIO(await upload.read()), "filename": upload.filename},
            )

    await session.commit()
    await session.refresh(template)

    # After the commit, so the stored URLs exist to copy.
    _sync_urls(template, kind)
    await session.commit()
    await session.refresh(template)

    return _describe(template, event_id, audience)


# --- previewing with a real attendee ------------------------------------------
#
# The designer's "Sample preview" showed Lorem Ipsum, and the only way to see
# the document an actual attendee would receive was to issue one. These two
# endpoints give the designer the real thing: the values for a chosen
# registration to substitute live, and the document itself rendered by the
# same engine as the PDF.


class SampleResponse(BaseModel):
    """What to fill the tokens with, and the QR that goes with it."""

    values: dict
    qr_data_url: str
    registration: Optional[dict] = None


async def _registration_for_preview(session, event_id: int, ticket_id: Optional[str]):
    """The chosen registration, which must belong to this event.

    Checked against the event rather than only looked up, so a club cannot
    preview its design against another event's attendee by guessing a code.
    """
    if not ticket_id:
        return None
    registration = await session.scalar(
        select(EventRegistrationsLink)
        .options(joinedload(EventRegistrationsLink.event).joinedload(Events.club))
        .where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.ticket_id == ticket_id,
            EventRegistrationsLink.is_deleted == False,
        )
    )
    if registration is None:
        raise CustomHTTPException(404, message="No such registration on this event")
    return registration


def _values_and_qr(event, kind: DesignKind, registration):
    """The token values and QR text for a real or fictional attendee."""
    if registration is not None:
        values = document_values(registration, event)
        code = registration.ticket_id
    else:
        values = sample_values(event, kind.value)
        code = values["ticket_code"]
    qr_text = f"certificate:{code}" if kind is DesignKind.certificate else code
    return values, qr_text


@router.get("/{kind}/sample", summary="Values to preview this design with")
async def sample_for_design(
    session: SessionDep,
    user: ClubAuth,
    event_id: int,
    kind: DesignKind,
    ticket_id: Optional[str] = Query(None),
) -> SampleResponse:
    """Token values for one registration, or a sample attendee without one.

    The designer substitutes these as the organiser drags, so a long real
    name is seen to fit -- or not -- before anyone is issued a document.
    """
    event = await _event_for_editing(session, user, event_id)
    registration = await _registration_for_preview(session, event_id, ticket_id)
    values, qr_text = _values_and_qr(event, kind, registration)
    return SampleResponse(
        values=values,
        qr_data_url=qr_data_url(qr_text),
        registration=(
            {
                "ticket_id": registration.ticket_id,
                "full_name": registration.full_name,
                "email": registration.email,
                "role": getattr(registration.role, "value", registration.role),
            }
            if registration is not None
            else None
        ),
    )


class PreviewRequest(BaseModel):
    config: dict = Field(..., description="The design as the editor holds it, saved or not")
    ticket_id: Optional[str] = Field(default=None)


@router.post("/{kind}/preview", summary="Render this design exactly, as a PNG")
async def preview_design(
    session: SessionDep,
    user: ClubAuth,
    event_id: int,
    kind: DesignKind,
    payload: PreviewRequest,
    audience: TemplateAudience = Query(TemplateAudience.event_default),
) -> Response:
    """The document as the attendee would get it, from an unsaved design.

    The canvas is an approximation -- a browser is not wkhtmltopdf. This is
    not: same template, same engine, same values, so "does the name fit"
    is answered by the thing that will print it.
    """
    event = await _event_for_editing(session, user, event_id)
    registration = await _registration_for_preview(session, event_id, payload.ticket_id)

    # Validated through the same schema the save path uses, so a preview
    # cannot render a config the API would refuse to store.
    schema = TicketTemplateConfig if kind is DesignKind.ticket else CertificateTemplateConfig
    try:
        style = schema(**payload.config).model_dump()
    except Exception as exc:
        raise CustomHTTPException(422, message=f"Invalid design: {exc}")

    # Artwork URLs live on the stored row, not in what the editor sends.
    current = await resolve_template(
        session, _MODELS[kind], event_id=event_id, club_id=event.club_id,
        role=_role_for(audience),
    )
    if current is not None and current.config:
        for key in ("logo_url", "secondary_logo_url", "background_image_url",
                    "signature_1_url", "signature_2_url"):
            if not style.get(key) and current.config.get(key):
                style[key] = current.config[key]

    values, qr_text = _values_and_qr(event, kind, registration)
    style = apply_layers(style, values)

    name = values["name"]
    if kind is DesignKind.ticket:
        template = "events/ticket.html"
        default_width = 600
        email_payload = {
            "ticket_id": values["ticket_code"],
            "participant_name": name,
            "event_name": values["event_name"],
            "event_date": values["event_date"],
            "event_time": values["event_time"],
            "event_location": values["venue"],
            "event_prizes": f"Prizes worth ₹{event.prize_amount}" if event.prize_amount else None,
            "organizer_name": values["club_name"],
            "contact_email": event.contact_email,
            "contact_phone": event.contact_phone,
            "qr_code": qr_png_base64(qr_text),
        }
    else:
        template = "events/certificate.html"
        default_width = 1400
        email_payload = {
            "ticket_id": values["certificate_id"],
            "participant_name": name,
            "event_name": values["event_name"],
            "event_date": values["event_date"],
            "organizer_name": values["club_name"],
            "qr_code": qr_png_base64(qr_text),
        }

    image_bytes = generate_image_bytes(
        template_path=template,
        context={
            "email_payload": email_payload,
            "style": style,
            "qr_code_url": qr_data_url(qr_text),
        },
        width=image_width(style, default=default_width),
    )
    return Response(
        content=image_bytes.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


# --- the layered design ---------------------------------------------------------
#
# The layer editor's own endpoints. The design is a canvas, layers and a page
# in the editor's wire form, stored under config["layout"]; the renderer draws
# it with the fonts the editor measured with. Saving validates it the way the
# editor does, so the API refuses exactly what the editor would.


_CATALOGUES = {DesignKind.ticket: TICKET_STARTERS, DesignKind.badge: BADGE_STARTERS, DesignKind.certificate: CERTIFICATE_STARTERS, DesignKind.agenda: AGENDA_STARTERS}


def _starter_of(kind: DesignKind, name: str, page=None) -> dict:
    if kind is DesignKind.ticket:
        return ticket_starter(name)
    if kind is DesignKind.badge:
        return badge_starter(name, page=page)
    if kind is DesignKind.agenda:
        return agenda_starter(name)
    return certificate_starter(name)


def _starter(kind: DesignKind) -> dict:
    s = _starter_of(kind, "screen" if kind is DesignKind.agenda else "classic")
    return dump(TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"]))


def _layout_prefix(event_id: int) -> str:
    """Where this event's designer uploads live. A layout may only point inside it."""
    return os.path.join(settings.S3_BASE_PATH, "events", str(event_id), "layout") + "/"


def _layout_keys(data: dict) -> dict:
    """Every object key a layout points at, by the field that holds it."""
    keys = {}
    template_key = (data.get("canvas") or {}).get("templateKey")
    if template_key:
        keys["canvas.templateKey"] = template_key
    for i, layer in enumerate(data.get("layers") or []):
        if layer.get("kind") == "image" and layer.get("imageKey"):
            keys[f"layers.{i}.imageKey"] = layer["imageKey"]
    return keys


def _layout_dto(event, kind: DesignKind, audience: TemplateAudience, template, style: dict, agenda: Optional[list] = None) -> dict:
    """The design and everything the editor needs around it."""
    layout = style.get("layout") if in_layout_mode(style) else None
    data = layout or {**_starter(kind), "version": 0}
    colors = layout_colors(style, getattr(event, "club", None))
    image_urls = {key: objects.presigned_url(key) for key in set(_layout_keys(data).values())}
    template_key = (data.get("canvas") or {}).get("templateKey")
    club = getattr(event, "club", None)
    fields = [f.get("label") for f in (event.additional_details or []) if isinstance(f, dict) and f.get("label")]
    tokens = [{"key": t["key"], "group": t["group"], "label": t["label"], "sample": t["sample"], "note": t.get("note")} for t in TICKET_TOKENS]
    tokens += [{"key": field_token(f), "group": "Guest fields", "label": f, "sample": f"‹{f}›", "note": None} for f in fields]
    return {
        "eventId": event.id,
        "kind": kind.value,
        "audience": audience.value,
        "canvas": data["canvas"],
        "layers": data["layers"],
        "page": data["page"],
        "version": data.get("version", 0),
        "isOwn": template is not None and template.event_id == event.id and template.audience is audience,
        "fromStarter": layout is None,
        "templateUrl": image_urls.get(template_key) if template_key else style.get("background_image_url"),
        "imageUrls": image_urls,
        "brand": {
            "orgName": club.name if club else "",
            "primary": colors["primary"],
            "secondary": colors["secondary"],
            "logoUrl": style.get("logo_url"),
        },
        "eventIconUrl": None,
        "starters": _CATALOGUES[kind],
        "tokens": tokens,
        "qrRequired": kind in (DesignKind.ticket, DesignKind.badge),
        "sampleValues": layout_sample_values(event, kind.value),
        # The run of show an agenda layer draws on the stage: the event's own sessions, or samples while it has none.
        "agenda": agenda or [],
        "timezone": EVENT_TIMEZONE,
    }


async def _agenda_for(session, event, kind: DesignKind) -> Optional[list]:
    if kind is not DesignKind.agenda:
        return None
    return agenda_service.entries_json(await agenda_service.entries_for(session, event))


async def _current(session, event, kind: DesignKind, audience: TemplateAudience):
    template = await resolve_template(
        session, _MODELS[kind], event_id=event.id, club_id=event.club_id, role=_role_for(audience),
    )
    style = dict(template.config or {}) if template is not None else _DEFAULTS[kind]()
    return template, style


@router.get("/{kind}/layout", summary="The layered design the editor works on")
async def get_layout(
    session: SessionDep,
    user: ClubAuth,
    event_id: int,
    kind: DesignKind,
    audience: TemplateAudience = Query(TemplateAudience.event_default),
) -> dict:
    event = await _event_for_editing(session, user, event_id)
    template, style = await _current(session, event, kind, audience)
    return _layout_dto(event, kind, audience, template, style, await _agenda_for(session, event, kind))


@router.get("/{kind}/layout/starters/{name}", summary="A complete design to start from")
async def get_starter(
    session: SessionDep,
    user: ClubAuth,
    event_id: int,
    kind: DesignKind,
    name: str,
) -> dict:
    event = await _event_for_editing(session, user, event_id)
    if name not in _CATALOGUES[kind]:
        raise CustomHTTPException(404, message="No such starter")
    # A badge starter is drawn for the page the designer is on, so the
    # insert and label starters fit the stock already chosen.
    page = None
    if kind is DesignKind.badge:
        _, style = await _current(session, event, kind, TemplateAudience.event_default)
        layout = style.get("layout") if in_layout_mode(style) else None
        if layout and layout.get("page"):
            page = PageSpec.model_validate(layout["page"])
    s = _starter_of(kind, name, page)
    return dump(TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"]))


def _validate_layout(kind: DesignKind, body: dict, *, event_id: int) -> TicketLayoutFields:
    """Shape first, then meaning -- each refused with the field to fix."""
    try:
        layout = TicketLayoutFields.model_validate(body)
    except Exception as exc:
        raise CustomHTTPException(422, message="This design has a problem.", errors={"detail": str(exc)[:2000]})
    # Keys are plain strings in the JSON; one pointing outside this event's
    # folder would let a design print another club's upload.
    prefix = _layout_prefix(event_id)
    foreign = {field: "Upload the image through this event's designer." for field, key in _layout_keys(body).items() if not key.startswith(prefix)}
    if foreign:
        raise CustomHTTPException(422, message="Some images in this design were not uploaded here.", errors={"fields": foreign})
    problems = layout_problems(layout.canvas, layout.layers, layout.page, qr_required=kind in (DesignKind.ticket, DesignKind.badge))
    if problems:
        raise CustomHTTPException(
            422,
            message="Some parts of this design need attention before it can be used.",
            errors={"fields": problems_as_fields(problems)},
        )
    return layout


@router.put("/{kind}/layout", summary="Save the layered design")
async def save_layout(
    session: SessionDep,
    user: ClubAuth,
    event_id: int,
    kind: DesignKind,
    body: dict,
    audience: TemplateAudience = Query(TemplateAudience.event_default),
) -> dict:
    """Forks the design onto the event on first save, like the config does,
    and bumps the version so a document can say which design it was made from."""
    event = await _event_for_editing(session, user, event_id)
    layout = _validate_layout(kind, body, event_id=event_id)

    template = await fork_for_event(
        session, _MODELS[kind], event_id=event_id, club_id=event.club_id,
        audience=audience, name=f"{event.name} {kind.value}",
    )
    config = dict(template.config or {})
    previous = (config.get("layout") or {}).get("version", 0)
    config["layout"] = {**dump(layout), "version": previous + 1}
    template.config = config
    await session.commit()
    await session.refresh(template)
    return _layout_dto(event, kind, audience, template, dict(template.config), await _agenda_for(session, event, kind))


@router.post("/{kind}/layout/preview", summary="The layered design rendered exactly, as a PNG")
async def preview_layout(
    session: SessionDep,
    user: ClubAuth,
    event_id: int,
    kind: DesignKind,
    body: dict,
    audience: TemplateAudience = Query(TemplateAudience.event_default),
) -> Response:
    """The unsaved design through the real renderer, for a real attendee
    (`ticket_id`) or the sample one. A design that would be refused on save
    is refused here too, so the preview never shows something the attendee
    could not receive."""
    event = await _event_for_editing(session, user, event_id)
    ticket_id = body.pop("ticket_id", None)
    layout = _validate_layout(kind, body, event_id=event_id)
    template, style = await _current(session, event, kind, audience)
    style = {**style, "layout": dump(layout)}

    registration = await _registration_for_preview(session, event_id, ticket_id)
    if registration is not None:
        values = layout_values(registration, event, kind.value)
        code = registration.ticket_id
    else:
        values = layout_sample_values(event, kind.value)
        code = values["admission_no"]
    qr_text = f"certificate:{code}" if kind is DesignKind.certificate else code

    entries = await agenda_service.entries_for(session, event) if kind is DesignKind.agenda else None
    pdf = render_layout_document(style, values, qr_text, title=f"{kind.value} preview", agenda=entries, club=getattr(event, "club", None))
    return Response(content=layout_png(style, pdf), media_type="image/png", headers={"Cache-Control": "no-store"})


MAX_IMAGE_BYTES = 5 * 1024 * 1024
# Templates are stored at this width so files stay light; the editor's X and Y read in the stored size.
STORED_MAX_WIDTH = 2400
_EXT = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}


@router.post("/{kind}/layout/images", summary="A template or a picture for the layered design")
async def upload_layout_image(
    session: SessionDep,
    user: ClubAuth,
    event_id: int,
    kind: DesignKind,
    file: UploadFile = File(...),
    purpose: str = Query("image", pattern="^(template|image)$"),
) -> dict:
    """Stored under the event and answered with the key the layout saves and
    a URL to show it now. Nothing is written to the design: the editor holds
    the key until the person saves."""
    await _event_for_editing(session, user, event_id)
    raw = await file.read()
    if len(raw) > MAX_IMAGE_BYTES:
        raise CustomHTTPException(413, message="That image is over 5 MB. Export it smaller and try again.")
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception:
        raise CustomHTTPException(422, message="That file is not an image we can read. Use PNG, JPEG or WebP.")
    fmt = (image.format or "").upper()
    if fmt not in _EXT:
        raise CustomHTTPException(422, message="Use PNG, JPEG or WebP.")
    if image.width > STORED_MAX_WIDTH:
        image = image.resize((STORED_MAX_WIDTH, round(image.height * STORED_MAX_WIDTH / image.width)), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        if fmt == "JPEG":
            image.convert("RGB").save(out, "JPEG", quality=90)
        else:
            image.save(out, "PNG")
            fmt = "PNG"
        raw = out.getvalue()
    ext = _EXT[fmt]
    key = f"{_layout_prefix(event_id)}{purpose}-{uuid4().hex}.{ext}"
    objects.put_bytes(key, raw, "image/jpeg" if ext == "jpg" else f"image/{ext}")
    return {"key": key, "url": objects.presigned_url(key), "width": image.width, "height": image.height}
