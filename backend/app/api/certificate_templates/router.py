from typing import List
from fastapi import APIRouter

from app.api.certificate_templates import service
from app.api.certificate_templates.schemas import (
    CertificateTemplateCreate,
    CertificateTemplateUpdate,
    CertificateTemplateResponse,
)
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep

router = APIRouter(prefix="/certificate-templates")


@router.post("/create", summary="Create a certificate template")
async def create_certificate_template(
    data: CertificateTemplateCreate,
    user: ClubAuth,
    session: SessionDep = SessionDep,
) -> CertificateTemplateResponse:
    """Create a new certificate template for your club."""
    from app.api.clubs.models import Clubs
    from sqlalchemy import select

    club = await session.scalar(
        select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False)
    )
    if not club:
        from app.response import CustomHTTPException
        raise CustomHTTPException(404, "Club not found")

    template = await service.create_certificate_template(session, club.id, data)
    return template


@router.get("/list", summary="List certificate templates")
async def list_certificate_templates(
    user: ClubAuth,
    session: SessionDep = SessionDep,
) -> List[CertificateTemplateResponse]:
    """List all certificate templates for your club."""
    from app.api.clubs.models import Clubs
    from sqlalchemy import select

    club = await session.scalar(
        select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False)
    )
    if not club:
        from app.response import CustomHTTPException
        raise CustomHTTPException(404, "Club not found")

    return await service.list_certificate_templates(session, club.id)


@router.put("/{template_id}", summary="Update a certificate template")
async def update_certificate_template(
    template_id: int,
    data: CertificateTemplateUpdate,
    user: ClubAuth,
    session: SessionDep = SessionDep,
) -> CertificateTemplateResponse:
    """Update your certificate template."""
    from app.api.clubs.models import Clubs
    from sqlalchemy import select

    club = await session.scalar(
        select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False)
    )
    if not club:
        from app.response import CustomHTTPException
        raise CustomHTTPException(404, "Club not found")

    return await service.update_certificate_template(session, template_id, club.id, data)


@router.delete("/{template_id}", summary="Delete a certificate template")
async def delete_certificate_template(
    template_id: int,
    user: ClubAuth,
    session: SessionDep = SessionDep,
) -> dict:
    """Soft-delete a certificate template."""
    from app.api.clubs.models import Clubs
    from sqlalchemy import select

    club = await session.scalar(
        select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False)
    )
    if not club:
        from app.response import CustomHTTPException
        raise CustomHTTPException(404, "Club not found")

    await service.delete_certificate_template(session, template_id, club.id)
    return {"message": "Template deleted successfully"}
