import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ticket_templates.models import TicketTemplates
from app.api.ticket_templates.schemas import TicketTemplateCreate, TicketTemplateUpdate
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)


async def create_ticket_template(
    session: AsyncSession,
    club_id: int,
    data: TicketTemplateCreate,
) -> TicketTemplates:
    """Create a new ticket template for a club."""
    # If setting as default, unset any existing default
    if data.is_default:
        await _unset_club_defaults(session, club_id)

    template = TicketTemplates(
        club_id=club_id,
        name=data.name,
        is_default=data.is_default,
        config=data.config.model_dump(),
    )
    session.add(template)
    await session.commit()
    await session.refresh(template)
    logger.info(f"Created ticket template {template.id} for club {club_id}")
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


async def update_ticket_template(
    session: AsyncSession,
    template_id: int,
    club_id: int,
    data: TicketTemplateUpdate,
) -> TicketTemplates:
    """Update a ticket template."""
    template = await get_ticket_template(session, template_id)

    if template.club_id != club_id:
        raise CustomHTTPException(403, "You do not own this template")

    if data.name is not None:
        template.name = data.name
    if data.config is not None:
        template.config = data.config.model_dump()
    if data.is_default is not None:
        if data.is_default:
            await _unset_club_defaults(session, club_id)
        template.is_default = data.is_default

    await session.commit()
    await session.refresh(template)
    logger.info(f"Updated ticket template {template_id}")
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
) -> dict:
    """
    Get the ticket template config for an event.
    Falls back to club default, then to system defaults.
    Returns the config dict ready to inject into the HTML template.
    """
    from app.api.events.models import Events

    event = await session.scalar(
        select(Events).where(Events.id == event_id, Events.is_deleted == False)
    )
    if not event:
        return _default_config()

    # 1. Check if event has a specific template assigned
    if hasattr(event, "ticket_template_id") and event.ticket_template_id:
        template = await session.scalar(
            select(TicketTemplates).where(
                TicketTemplates.id == event.ticket_template_id,
                TicketTemplates.is_deleted == False,
            )
        )
        if template:
            return template.config

    # 2. Fall back to club's default template
    if event.club_id:
        default_template = await session.scalar(
            select(TicketTemplates).where(
                TicketTemplates.club_id == event.club_id,
                TicketTemplates.is_default == True,
                TicketTemplates.is_deleted == False,
            )
        )
        if default_template:
            return default_template.config

    # 3. Return system defaults (matches current hardcoded ticket)
    return _default_config()


def _default_config() -> dict:
    """Return the default ticket config matching the current hardcoded theme."""
    return {
        "accent_color": "#1db9a0",
        "bg_color": "#121212",
        "text_color": "#ffffff",
        "font": "Inter",
        "logo_url": None,
        "background_image_url": None,
        "show_prizes": True,
        "show_guidelines_link": True,
        "ticket_type_label": "GENERAL",
        "custom_footer_text": None,
    }


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
