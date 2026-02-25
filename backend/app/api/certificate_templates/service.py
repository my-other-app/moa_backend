from typing import List
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.certificate_templates.models import CertificateTemplates
from app.api.certificate_templates.schemas import (
    CertificateTemplateCreate,
    CertificateTemplateUpdate,
)
from app.response import CustomHTTPException


async def create_certificate_template(
    session: AsyncSession, club_id: int, data: CertificateTemplateCreate
) -> CertificateTemplates:
    # If this is set as default, unset others for this club
    if data.is_default:
        await _unset_defaults(session, club_id)

    template = CertificateTemplates(
        club_id=club_id,
        name=data.name,
        is_default=data.is_default,
        config=data.config.model_dump(),
    )
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template


async def get_certificate_template(session: AsyncSession, template_id: int) -> CertificateTemplates:
    template = await session.scalar(
        select(CertificateTemplates).where(
            CertificateTemplates.id == template_id,
            CertificateTemplates.is_deleted == False,
        )
    )
    if not template:
        raise CustomHTTPException(404, "Certificate template not found")
    return template


async def list_certificate_templates(
    session: AsyncSession, club_id: int
) -> List[CertificateTemplates]:
    result = await session.execute(
        select(CertificateTemplates)
        .where(
            CertificateTemplates.club_id == club_id,
            CertificateTemplates.is_deleted == False,
        )
        .order_by(CertificateTemplates.created_at.desc())
    )
    return list(result.scalars().all())


async def update_certificate_template(
    session: AsyncSession, template_id: int, club_id: int, data: CertificateTemplateUpdate
) -> CertificateTemplates:
    template = await get_certificate_template(session, template_id)
    if template.club_id != club_id:
        raise CustomHTTPException(403, "Not authorized to modify this template")

    if data.is_default and not template.is_default:
        await _unset_defaults(session, club_id)

    update_data = data.model_dump(exclude_unset=True)
    if "config" in update_data:
        # Merge new config with existing
        current_config = template.config or {}
        new_config = update_data.pop("config")
        current_config.update(new_config)
        template.config = current_config

    for key, value in update_data.items():
        setattr(template, key, value)

    await session.commit()
    await session.refresh(template)
    return template


async def delete_certificate_template(
    session: AsyncSession, template_id: int, club_id: int
) -> None:
    template = await get_certificate_template(session, template_id)
    if template.club_id != club_id:
        raise CustomHTTPException(403, "Not authorized to delete this template")

    template.is_deleted = True
    await session.commit()


async def _unset_defaults(session: AsyncSession, club_id: int) -> None:
    """Helper to unset is_default on all other templates for a club"""
    result = await session.execute(
        select(CertificateTemplates).where(
            CertificateTemplates.club_id == club_id,
            CertificateTemplates.is_default == True,
            CertificateTemplates.is_deleted == False,
        )
    )
    existing_defaults = result.scalars().all()
    for t in existing_defaults:
        t.is_default = False
    if existing_defaults:
        await session.flush()


async def get_template_for_event(
    session: AsyncSession,
    event_id: int,
) -> dict:
    """
    Get the certificate template config for an event.
    Falls back to club default, then to system defaults.
    """
    from app.api.events.models import Events

    event = await session.scalar(
        select(Events).where(Events.id == event_id, Events.is_deleted == False)
    )
    if not event:
        return _default_config()

    if hasattr(event, "certificate_template_id") and event.certificate_template_id:
        template = await session.scalar(
            select(CertificateTemplates).where(
                CertificateTemplates.id == event.certificate_template_id,
                CertificateTemplates.is_deleted == False,
            )
        )
        if template:
            return template.config

    if event.club_id:
        default_template = await session.scalar(
            select(CertificateTemplates).where(
                CertificateTemplates.club_id == event.club_id,
                CertificateTemplates.is_default == True,
                CertificateTemplates.is_deleted == False,
            )
        )
        if default_template:
            return default_template.config

    return _default_config()


def _default_config() -> dict:
    from app.api.certificate_templates.schemas import CertificateTemplateConfig
    return CertificateTemplateConfig().model_dump()
