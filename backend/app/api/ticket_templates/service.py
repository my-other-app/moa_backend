import json
import logging
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.templates_audience import RegistrationRole
from app.api.templates_images import TICKET_FIELDS, shown, stored
from app.core.storage.fields import upload
from app.api.templates_resolve import resolve_template
from app.api.ticket_templates.models import TicketTemplates
from app.api.ticket_templates.schemas import TicketTemplateConfig
from app.response import CustomHTTPException
from fastapi import UploadFile

logger = logging.getLogger(__name__)


async def create_ticket_template(
    session: AsyncSession,
    club_id: int,
    name: str,
    is_default: bool,
    config_json: str,
    logo: Optional[UploadFile] = None,
    secondary_logo: Optional[UploadFile] = None,
    background_image: Optional[UploadFile] = None,
) -> TicketTemplates:
    """Create a new ticket template with optional image uploads."""
    config = TicketTemplateConfig(**json.loads(config_json))

    if is_default:
        await _unset_club_defaults(session, club_id)

    template = TicketTemplates(
        club_id=club_id,
        name=name,
        is_default=is_default,
        config=config.model_dump(),
    )

    # Handle file uploads
    await _apply_uploads(template, logo, secondary_logo, background_image)

    # The uploads' keys into the config the renderer reads.
    _sync_urls_to_config(template)
    session.add(template)
    await session.commit()
    await session.refresh(template)

    logger.info(f"Created ticket template {template.id} for club {club_id}")
    return template


async def update_ticket_template(
    session: AsyncSession,
    template_id: int,
    club_id: int,
    name: Optional[str],
    is_default: Optional[bool],
    config_json: Optional[str],
    logo: Optional[UploadFile] = None,
    secondary_logo: Optional[UploadFile] = None,
    background_image: Optional[UploadFile] = None,
    remove_logo: bool = False,
    remove_secondary_logo: bool = False,
    remove_background_image: bool = False,
) -> TicketTemplates:
    """Update a ticket template with optional image uploads."""
    template = await get_ticket_template(session, template_id)

    if template.club_id != club_id:
        raise CustomHTTPException(403, "You do not own this template")

    if name is not None:
        template.name = name
    if config_json is not None:
        config = TicketTemplateConfig(**json.loads(config_json))
        template.config = config.model_dump()
    if is_default is not None:
        if is_default:
            await _unset_club_defaults(session, club_id)
        template.is_default = is_default

    # Handle removals
    if remove_logo:
        template.logo = None
    if remove_secondary_logo:
        template.secondary_logo = None
    if remove_background_image:
        template.background_image = None

    # Handle file uploads (overwrite existing)
    await _apply_uploads(template, logo, secondary_logo, background_image)

    # The uploads' keys into the config the renderer reads.
    _sync_urls_to_config(template)
    await session.commit()
    await session.refresh(template)

    logger.info(f"Updated ticket template {template_id}")
    return template


async def list_ticket_templates(
    session: AsyncSession,
    club_id: int,
) -> list[TicketTemplates]:
    """List all ticket templates for a club."""
    query = (
        select(TicketTemplates)
        .where(
            TicketTemplates.club_id == club_id,
            TicketTemplates.is_deleted == False,
        )
        .order_by(TicketTemplates.is_default.desc(), TicketTemplates.created_at.desc())
    )
    return list(await session.scalars(query))


async def get_ticket_template(
    session: AsyncSession,
    template_id: int,
) -> TicketTemplates:
    """Get a single ticket template by ID."""
    template = await session.scalar(
        select(TicketTemplates).where(
            TicketTemplates.id == template_id,
            TicketTemplates.is_deleted == False,
        )
    )
    if not template:
        raise CustomHTTPException(404, "Ticket template not found")
    return template


async def delete_ticket_template(
    session: AsyncSession,
    template_id: int,
    club_id: int,
) -> None:
    """Soft-delete a ticket template."""
    template = await get_ticket_template(session, template_id)
    if template.club_id != club_id:
        raise CustomHTTPException(403, "You do not own this template")
    template.is_deleted = True
    await session.commit()
    logger.info(f"Deleted ticket template {template_id}")


async def get_template_for_event(
    session: AsyncSession,
    event_id: int,
    role: RegistrationRole | None = None,
) -> dict:
    """The config this event's document should be rendered from.

    Resolution lives in app/api/templates_resolve so tickets and
    certificates cannot drift apart: event+role, then the event's default,
    then the club's, then the system's.

    `role` picks the variant. Left out -- as the designer does when showing
    an event's default -- it skips straight to the event default.
    """
    from app.api.events.models import Events

    event = await session.scalar(
        select(Events).where(Events.id == event_id, Events.is_deleted == False)
    )
    if not event:
        return _default_config()

    template = await resolve_template(
        session,
        TicketTemplates,
        event_id=event_id,
        club_id=event.club_id,
        role=role,
    )
    if template is not None:
        return shown(template.config, template, fields=FIELDS)

    # An explicitly assigned club template, from before events could own
    # their own design. Still honoured so existing events keep rendering
    # the way they did.
    assigned_id = getattr(event, "ticket_template_id", None)
    if assigned_id:
        assigned = await session.scalar(
            select(TicketTemplates).where(
                TicketTemplates.id == assigned_id,
                TicketTemplates.is_deleted == False,
            )
        )
        if assigned is not None:
            return shown(assigned.config, assigned, fields=FIELDS)

    return _default_config()



def _default_config() -> dict:
    """Return the default ticket config."""
    return TicketTemplateConfig().model_dump()


FIELDS = TICKET_FIELDS


def _sync_urls_to_config(template: TicketTemplates) -> None:
    """Sync the uploaded images' keys into the config JSON.

    Keys, not the presigned URLs this used to copy: those lasted an hour,
    and the document printed from them lost its logo and artwork when they
    ran out. The columns are what an upload or a removal changes here, so
    they decide every field (app.api.templates_images.stored)."""
    template.config = stored(template.config, template, fields=FIELDS, touched=FIELDS)


def response_of(template: TicketTemplates) -> dict:
    """The template as the dashboard reads it: its config with a link signed
    now for every image (the stored config holds keys only)."""
    return {
        "id": template.id,
        "name": template.name,
        "is_default": template.is_default,
        "config": shown(template.config, template, fields=FIELDS),
        "logo": template.logo,
        "secondary_logo": template.secondary_logo,
        "background_image": template.background_image,
    }


async def _apply_uploads(
    template: TicketTemplates,
    logo: Optional[UploadFile],
    secondary_logo: Optional[UploadFile],
    background_image: Optional[UploadFile],
) -> None:
    """Apply file uploads to the template model."""
    if logo and logo.filename:
        template.logo = await upload(TicketTemplates.logo, {"bytes": await logo.read(), "filename": logo.filename})
    if secondary_logo and secondary_logo.filename:
        template.secondary_logo = await upload(TicketTemplates.secondary_logo, {"bytes": await secondary_logo.read(), "filename": secondary_logo.filename})
    if background_image and background_image.filename:
        template.background_image = await upload(TicketTemplates.background_image, {"bytes": await background_image.read(), "filename": background_image.filename})


async def _unset_club_defaults(session: AsyncSession, club_id: int) -> None:
    """Unset the is_default flag for all templates of a club."""
    existing_defaults = await session.scalars(
        select(TicketTemplates).where(
            TicketTemplates.club_id == club_id,
            TicketTemplates.is_default == True,
            TicketTemplates.is_deleted == False,
        )
    )
    for t in existing_defaults:
        t.is_default = False
