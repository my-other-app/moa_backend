import io
import json
import logging
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.templates_audience import RegistrationRole
from app.api.templates_resolve import resolve_template
from app.api.certificate_templates.models import CertificateTemplates
from app.api.certificate_templates.schemas import CertificateTemplateConfig
from app.response import CustomHTTPException
from fastapi import UploadFile

logger = logging.getLogger(__name__)


async def create_certificate_template(
    session: AsyncSession,
    club_id: int,
    name: str,
    is_default: bool,
    config_json: str,
    logo: Optional[UploadFile] = None,
    secondary_logo: Optional[UploadFile] = None,
    background_image: Optional[UploadFile] = None,
    signature_1: Optional[UploadFile] = None,
    signature_2: Optional[UploadFile] = None,
) -> CertificateTemplates:
    """Create a new certificate template with optional image uploads."""
    config = CertificateTemplateConfig(**json.loads(config_json))

    if is_default:
        await _unset_club_defaults(session, club_id)

    template = CertificateTemplates(
        club_id=club_id,
        name=name,
        is_default=is_default,
        config=config.model_dump(),
    )

    await _apply_uploads(template, logo, secondary_logo, background_image, signature_1, signature_2)

    session.add(template)
    await session.commit()
    await session.refresh(template)

    _sync_urls_to_config(template)
    await session.commit()
    await session.refresh(template)

    logger.info(f"Created certificate template {template.id} for club {club_id}")
    return template


async def update_certificate_template(
    session: AsyncSession,
    template_id: int,
    club_id: int,
    name: Optional[str],
    is_default: Optional[bool],
    config_json: Optional[str],
    logo: Optional[UploadFile] = None,
    secondary_logo: Optional[UploadFile] = None,
    background_image: Optional[UploadFile] = None,
    signature_1: Optional[UploadFile] = None,
    signature_2: Optional[UploadFile] = None,
    remove_logo: bool = False,
    remove_secondary_logo: bool = False,
    remove_background_image: bool = False,
    remove_signature_1: bool = False,
    remove_signature_2: bool = False,
) -> CertificateTemplates:
    """Update a certificate template with optional image uploads."""
    template = await get_certificate_template(session, template_id)

    if template.club_id != club_id:
        raise CustomHTTPException(403, "You do not own this template")

    if name is not None:
        template.name = name
    if config_json is not None:
        config = CertificateTemplateConfig(**json.loads(config_json))
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
    if remove_signature_1:
        template.signature_1 = None
    if remove_signature_2:
        template.signature_2 = None

    await _apply_uploads(template, logo, secondary_logo, background_image, signature_1, signature_2)

    await session.commit()
    await session.refresh(template)

    _sync_urls_to_config(template)
    await session.commit()
    await session.refresh(template)

    logger.info(f"Updated certificate template {template_id}")
    return template


async def list_certificate_templates(
    session: AsyncSession,
    club_id: int,
) -> list[CertificateTemplates]:
    """List all certificate templates for a club."""
    query = (
        select(CertificateTemplates)
        .where(
            CertificateTemplates.club_id == club_id,
            CertificateTemplates.is_deleted == False,
        )
        .order_by(CertificateTemplates.is_default.desc(), CertificateTemplates.created_at.desc())
    )
    return list(await session.scalars(query))


async def get_certificate_template(
    session: AsyncSession,
    template_id: int,
) -> CertificateTemplates:
    """Get a single certificate template by ID."""
    template = await session.scalar(
        select(CertificateTemplates).where(
            CertificateTemplates.id == template_id,
            CertificateTemplates.is_deleted == False,
        )
    )
    if not template:
        raise CustomHTTPException(404, "Certificate template not found")
    return template


async def delete_certificate_template(
    session: AsyncSession,
    template_id: int,
    club_id: int,
) -> None:
    """Soft-delete a certificate template."""
    template = await get_certificate_template(session, template_id)
    if template.club_id != club_id:
        raise CustomHTTPException(403, "You do not own this template")
    template.is_deleted = True
    await session.commit()
    logger.info(f"Deleted certificate template {template_id}")


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
        CertificateTemplates,
        event_id=event_id,
        club_id=event.club_id,
        role=role,
    )
    if template is not None:
        return template.config

    # An explicitly assigned club template, from before events could own
    # their own design. Still honoured so existing events keep rendering
    # the way they did.
    assigned_id = getattr(event, "certificate_template_id", None)
    if assigned_id:
        assigned = await session.scalar(
            select(CertificateTemplates).where(
                CertificateTemplates.id == assigned_id,
                CertificateTemplates.is_deleted == False,
            )
        )
        if assigned is not None:
            return assigned.config

    return _default_config()



def _default_config() -> dict:
    """Return the default certificate config."""
    return CertificateTemplateConfig().model_dump()


def _extract_url(field_val) -> Optional[str]:
    if isinstance(field_val, str):
        return field_val
    if isinstance(field_val, dict) and "original" in field_val:
        return field_val["original"]
    return None


def _sync_urls_to_config(template: CertificateTemplates) -> None:
    """Sync S3 uploaded image URLs into the config JSON."""
    config = dict(template.config) if template.config else {}
    config["logo_url"] = _extract_url(template.logo)
    config["secondary_logo_url"] = _extract_url(template.secondary_logo)
    config["background_image_url"] = _extract_url(template.background_image)
    config["signature_1_url"] = _extract_url(template.signature_1)
    config["signature_2_url"] = _extract_url(template.signature_2)
    template.config = config


async def _apply_uploads(
    template: CertificateTemplates,
    logo: Optional[UploadFile],
    secondary_logo: Optional[UploadFile],
    background_image: Optional[UploadFile],
    signature_1: Optional[UploadFile],
    signature_2: Optional[UploadFile],
) -> None:
    """Apply file uploads to the template model."""
    if logo and logo.filename:
        content = io.BytesIO(await logo.read())
        template.logo = {"bytes": content, "filename": logo.filename}
    if secondary_logo and secondary_logo.filename:
        content = io.BytesIO(await secondary_logo.read())
        template.secondary_logo = {"bytes": content, "filename": secondary_logo.filename}
    if background_image and background_image.filename:
        content = io.BytesIO(await background_image.read())
        template.background_image = {"bytes": content, "filename": background_image.filename}
    if signature_1 and signature_1.filename:
        content = io.BytesIO(await signature_1.read())
        template.signature_1 = {"bytes": content, "filename": signature_1.filename}
    if signature_2 and signature_2.filename:
        content = io.BytesIO(await signature_2.read())
        template.signature_2 = {"bytes": content, "filename": signature_2.filename}


async def _unset_club_defaults(session: AsyncSession, club_id: int) -> None:
    """Unset the is_default flag for all templates of a club."""
    existing_defaults = await session.scalars(
        select(CertificateTemplates).where(
            CertificateTemplates.club_id == club_id,
            CertificateTemplates.is_default == True,
            CertificateTemplates.is_deleted == False,
        )
    )
    for t in existing_defaults:
        t.is_default = False
