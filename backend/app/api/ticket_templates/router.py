from typing import List
from fastapi import APIRouter

from app.api.ticket_templates import service
from app.api.ticket_templates.schemas import (
    TicketTemplateCreate,
    TicketTemplateUpdate,
    TicketTemplateResponse,
)
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep

router = APIRouter(prefix="/ticket-templates")


@router.post("/create", summary="Create a ticket template")
async def create_ticket_template(
    data: TicketTemplateCreate,
    user: ClubAuth,
    session: SessionDep = SessionDep,
) -> TicketTemplateResponse:
    """Create a new ticket template for your club."""
    from app.api.clubs.models import Clubs
    from sqlalchemy import select

    club = await session.scalar(
        select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False)
    )
    if not club:
        from app.response import CustomHTTPException
        raise CustomHTTPException(404, "Club not found")

    template = await service.create_ticket_template(session, club.id, data)
    return template


@router.get("/list", summary="List ticket templates")
async def list_ticket_templates(
    user: ClubAuth,
    session: SessionDep = SessionDep,
) -> List[TicketTemplateResponse]:
    """List all ticket templates for your club."""
    from app.api.clubs.models import Clubs
    from sqlalchemy import select

    club = await session.scalar(
        select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False)
    )
    if not club:
        from app.response import CustomHTTPException
        raise CustomHTTPException(404, "Club not found")

    return await service.list_ticket_templates(session, club.id)


@router.put("/{template_id}", summary="Update a ticket template")
async def update_ticket_template(
    template_id: int,
    data: TicketTemplateUpdate,
    user: ClubAuth,
    session: SessionDep = SessionDep,
) -> TicketTemplateResponse:
    """Update your ticket template."""
    from app.api.clubs.models import Clubs
    from sqlalchemy import select

    club = await session.scalar(
        select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False)
    )
    if not club:
        from app.response import CustomHTTPException
        raise CustomHTTPException(404, "Club not found")

    return await service.update_ticket_template(session, template_id, club.id, data)


@router.delete("/{template_id}", summary="Delete a ticket template")
async def delete_ticket_template(
    template_id: int,
    user: ClubAuth,
    session: SessionDep = SessionDep,
) -> dict:
    """Soft-delete a ticket template."""
    from app.api.clubs.models import Clubs
    from sqlalchemy import select

    club = await session.scalar(
        select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False)
    )
    if not club:
        from app.response import CustomHTTPException
        raise CustomHTTPException(404, "Club not found")

    await service.delete_ticket_template(session, template_id, club.id)
    return {"message": "Template deleted successfully"}
