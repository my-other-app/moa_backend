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
from typing import Optional

from fastapi import APIRouter, File, Form, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.api.certificate_templates.models import CertificateTemplates
from app.api.certificate_templates.service import (
    _default_config as certificate_default_config,
)
from app.api.events.access import ensure_event_access, is_admin_user
from app.api.events.models import Events
from app.api.templates_audience import RegistrationRole, TemplateAudience
from app.api.templates_resolve import fork_for_event, resolve_template
from app.api.ticket_templates.models import TicketTemplates
from app.api.ticket_templates.service import _default_config as ticket_default_config
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/events/{event_id}/designs")


class DesignKind(str, enum.Enum):
    ticket = "ticket"
    certificate = "certificate"


_MODELS = {
    DesignKind.ticket: TicketTemplates,
    DesignKind.certificate: CertificateTemplates,
}
_DEFAULTS = {
    DesignKind.ticket: ticket_default_config,
    DesignKind.certificate: certificate_default_config,
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
