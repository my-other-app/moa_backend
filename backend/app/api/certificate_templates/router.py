from typing import List, Optional
from fastapi import APIRouter, Form, File, UploadFile

from app.api.certificate_templates import service
from app.api.certificate_templates.schemas import CertificateTemplateResponse
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep


router = APIRouter(prefix="/certificate-templates")


async def _get_club_id(session, user_id: int) -> int:
    from app.api.clubs.models import Clubs
    from sqlalchemy import select
    from app.response import CustomHTTPException

    club = await session.scalar(
        select(Clubs).where(Clubs.user_id == user_id, Clubs.is_deleted == False)
    )
    if not club:
        raise CustomHTTPException(404, "Club not found")
    return club.id


@router.post("/create", summary="Create a certificate template")
async def create_certificate_template(
    user: ClubAuth,
    session: SessionDep = SessionDep,
    name: str = Form(...),
    is_default: bool = Form(False),
    config: str = Form("{}"),
    logo: Optional[UploadFile] = File(None),
    secondary_logo: Optional[UploadFile] = File(None),
    background_image: Optional[UploadFile] = File(None),
    signature_1: Optional[UploadFile] = File(None),
    signature_2: Optional[UploadFile] = File(None),
) -> CertificateTemplateResponse:
    """Create a new certificate template with file uploads."""
    club_id = await _get_club_id(session, user.id)
    template = await service.create_certificate_template(
        session, club_id, name, is_default, config,
        logo=logo, secondary_logo=secondary_logo,
        background_image=background_image,
        signature_1=signature_1, signature_2=signature_2,
    )
    return template


@router.get("/list", summary="List certificate templates")
async def list_certificate_templates(
    user: ClubAuth,
    session: SessionDep = SessionDep,
) -> List[CertificateTemplateResponse]:
    """List all certificate templates for your club."""
    club_id = await _get_club_id(session, user.id)
    return await service.list_certificate_templates(session, club_id)


@router.put("/{template_id}", summary="Update a certificate template")
async def update_certificate_template(
    template_id: int,
    user: ClubAuth,
    session: SessionDep = SessionDep,
    name: Optional[str] = Form(None),
    is_default: Optional[bool] = Form(None),
    config: Optional[str] = Form(None),
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
) -> CertificateTemplateResponse:
    """Update your certificate template with optional file uploads."""
    club_id = await _get_club_id(session, user.id)
    return await service.update_certificate_template(
        session, template_id, club_id, name, is_default, config,
        logo=logo, secondary_logo=secondary_logo,
        background_image=background_image,
        signature_1=signature_1, signature_2=signature_2,
        remove_logo=remove_logo, remove_secondary_logo=remove_secondary_logo,
        remove_background_image=remove_background_image,
        remove_signature_1=remove_signature_1, remove_signature_2=remove_signature_2,
    )


@router.delete("/{template_id}", summary="Delete a certificate template")
async def delete_certificate_template(
    template_id: int,
    user: ClubAuth,
    session: SessionDep = SessionDep,
) -> dict:
    """Soft-delete a certificate template."""
    club_id = await _get_club_id(session, user.id)
    await service.delete_certificate_template(session, template_id, club_id)
    return {"message": "Template deleted successfully"}
