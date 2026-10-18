import json
import logging
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.templates_audience import RegistrationRole
from app.api.templates_images import CERTIFICATE_FIELDS, shown, stored
from app.core.storage.fields import upload
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

    # The uploads' keys into the config the renderer reads.
    _sync_urls_to_config(template)
    session.add(template)
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

    # The uploads' keys into the config the renderer reads.
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
        return shown(template.config, template, fields=FIELDS)

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
            return shown(assigned.config, assigned, fields=FIELDS)

    return _default_config()



def _default_config() -> dict:
    """Return the default certificate config."""
    return CertificateTemplateConfig().model_dump()


FIELDS = CERTIFICATE_FIELDS


def _sync_urls_to_config(template: CertificateTemplates) -> None:
    """Sync the uploaded images' keys into the config JSON.

    Keys, not the presigned URLs this used to copy: those lasted an hour,
    and the document printed from them lost its logo and artwork when they
    ran out. The columns are what an upload or a removal changes here, so
    they decide every field (app.api.templates_images.stored)."""
    template.config = stored(template.config, template, fields=FIELDS, touched=FIELDS)


def response_of(template: CertificateTemplates) -> dict:
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
        "signature_1": template.signature_1,
        "signature_2": template.signature_2,
    }


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
        template.logo = await upload(CertificateTemplates.logo, {"bytes": await logo.read(), "filename": logo.filename})
    if secondary_logo and secondary_logo.filename:
        template.secondary_logo = await upload(CertificateTemplates.secondary_logo, {"bytes": await secondary_logo.read(), "filename": secondary_logo.filename})
    if background_image and background_image.filename:
        template.background_image = await upload(CertificateTemplates.background_image, {"bytes": await background_image.read(), "filename": background_image.filename})
    if signature_1 and signature_1.filename:
        template.signature_1 = await upload(CertificateTemplates.signature_1, {"bytes": await signature_1.read(), "filename": signature_1.filename})
    if signature_2 and signature_2.filename:
        template.signature_2 = await upload(CertificateTemplates.signature_2, {"bytes": await signature_2.read(), "filename": signature_2.filename})


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
