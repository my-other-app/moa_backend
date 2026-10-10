"""Choosing which design a document is rendered from.

One resolver for both tickets and certificates, because the rule is the
same and two copies of it would drift -- as the club and event paths
already had a habit of doing.

The order, most specific first:

  1. this event's design for this person's role
  2. this event's default design
  3. the club's default design
  4. the system default

Each step is a real answer, not a guess: an event that has only ever been
designed once has a single event_default row and every role lands on it,
which is what "one design for the whole event" means.
"""

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.templates_audience import RegistrationRole, TemplateAudience

logger = logging.getLogger(__name__)


async def resolve_template(
    session: AsyncSession,
    model,
    *,
    event_id: Optional[int],
    club_id: Optional[int],
    role: Optional[RegistrationRole] = None,
):
    """The most specific design row for this event and role, or None.

    `model` is TicketTemplates or CertificateTemplates. Returning the row
    rather than its config lets a caller tell which design it got, which the
    designer needs in order to say whether an event has its own yet.
    """
    if event_id is not None:
        if role is not None:
            own = await session.scalar(
                select(model).where(
                    model.event_id == event_id,
                    model.audience == role.audience,
                    model.is_deleted == False,
                )
            )
            if own is not None:
                return own

        event_default = await session.scalar(
            select(model).where(
                model.event_id == event_id,
                model.audience == TemplateAudience.event_default,
                model.is_deleted == False,
            )
        )
        if event_default is not None:
            return event_default

    if club_id is not None:
        club_default = await session.scalar(
            select(model).where(
                model.club_id == club_id,
                model.event_id.is_(None),
                model.is_default == True,
                model.is_deleted == False,
            )
        )
        if club_default is not None:
            return club_default

    return None


async def fork_for_event(
    session: AsyncSession,
    model,
    *,
    event_id: int,
    club_id: Optional[int],
    audience: TemplateAudience,
    name: str,
):
    """Give this event its own copy of whatever it is currently using.

    Called the first time someone edits a design on an event's own page. The
    copy starts as what they were already looking at, so editing changes
    nothing until they change something -- and from then on the club's
    template can be edited freely without reaching back into an event that
    has already been designed.

    Returns the existing row untouched if the event already owns one for
    this audience, so a second edit does not fork again.
    """
    existing = await session.scalar(
        select(model).where(
            model.event_id == event_id,
            model.audience == audience,
            model.is_deleted == False,
        )
    )
    if existing is not None:
        return existing

    # Fork from the event's current design, falling back through the chain,
    # so the copy looks like what was on screen a moment ago.
    source = await resolve_template(
        session, model, event_id=event_id, club_id=club_id
    )

    copy = model(
        club_id=None,
        event_id=event_id,
        audience=audience,
        name=name,
        is_default=False,
        config=dict(source.config) if source is not None else {},
    )
    for field in ("logo", "secondary_logo", "background_image"):
        if source is not None and hasattr(source, field):
            setattr(copy, field, getattr(source, field))

    session.add(copy)
    await session.flush()
    logger.info(
        "Forked %s for event %s (%s) from %s",
        model.__name__,
        event_id,
        audience.value,
        source.id if source is not None else "system default",
    )
    return copy
