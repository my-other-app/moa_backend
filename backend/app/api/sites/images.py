"""The club's photo library for its site: up to MAX_IMAGES photos, in the
club's order, each with alt text, assigned to the template's image slots
(layout.slots) or filling its gallery.

Uploads go through the image pipeline every other picture uses (storage
fields): checked, decoded off the event loop, turned upright, metadata
stripped, stored under a versioned name with the IMAGE_SIZES widths beside
it for the img's srcset. The count is checked before the upload (a quick
409) and again under a lock on the site's row before the row is written, so
two uploads at once never make 21.

A change here is on the published page within the sites sweep's next run
(sweep.behind: a photo saved after the page was made); a photo the page
shows that is deleted is taken off it at once.
"""
from __future__ import annotations

import asyncio
import io
import logging

from fastapi import UploadFile
from PIL import Image
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.sites import layout as layouts
from app.api.sites.models import IMAGE_MB, MAX_IMAGES, ClubSiteImages, ClubSites
from app.core.storage import formats
from app.core.storage.fields import _field_of, variant_key
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

FULL = "SITE_IMAGES_FULL"
ALT_MAX = 200


def _field():
    return _field_of(ClubSiteImages.image)


def _clean_alt(alt: str | None) -> str | None:
    if alt is None:
        return None
    v = " ".join(alt.split())
    if len(v) > ALT_MAX:
        raise CustomHTTPException(422, message=f"The alt text is at most {ALT_MAX} characters.", error_code="SITE_IMAGE_ALT_TOO_LONG")
    return v or None


def _full() -> CustomHTTPException:
    return CustomHTTPException(409, message=f"The library holds {MAX_IMAGES} photos. Delete one to add another.", error_code=FULL, errors={"limit": MAX_IMAGES})


async def count(session: AsyncSession, club_id: int) -> int:
    return int(await session.scalar(select(func.count()).select_from(ClubSiteImages).where(ClubSiteImages.club_id == club_id)) or 0)


async def listed(session: AsyncSession, club_id: int) -> list[ClubSiteImages]:
    return list((await session.scalars(select(ClubSiteImages).where(ClubSiteImages.club_id == club_id).order_by(ClubSiteImages.position, ClubSiteImages.id))).all())


async def one(session: AsyncSession, club_id: int, image_id: int) -> ClubSiteImages:
    row = await session.scalar(select(ClubSiteImages).where(ClubSiteImages.id == image_id, ClubSiteImages.club_id == club_id))
    if row is None:
        raise CustomHTTPException(404, message="No such photo in the club's library", error_code="SITE_IMAGE_NOT_FOUND")
    return row


def _prepare(raw: bytes):
    """CPU only: the checked upload and its sizes, and the stored original's width and height."""
    prepared = _field().prepare(raw)
    with Image.open(io.BytesIO(prepared.original)) as im:
        size = im.size
    return prepared, size


def _remove_files(key: str) -> None:
    field = _field()
    for k in [key, *(variant_key(key, name) for name in field.variations)]:
        try:
            field.s3_client.delete_object(Bucket=field.bucket_name, Key=k)
        except Exception:  # noqa: BLE001
            logger.warning("site library: could not delete %s", k)


async def upload(session: AsyncSession, site: ClubSites, file: UploadFile, alt: str | None) -> ClubSiteImages:
    from app.core.layout import pool

    if not formats.may_be_picture(file.content_type):
        raise CustomHTTPException(422, message=f"The photo must be a picture: {formats.WORKS}.")
    alt = _clean_alt(alt)
    if await count(session, site.club_id) >= MAX_IMAGES:
        raise _full()
    limit = IMAGE_MB * 1024 * 1024
    if file.size is not None and file.size > limit:
        raise CustomHTTPException(413, message=f"That image is over {IMAGE_MB} MB. Export it smaller and try again.")
    raw = await file.read()
    prepared, (width, height) = await pool.run(_prepare, raw)
    key = await asyncio.to_thread(_field().store, prepared)
    try:
        # Counted again under the site row's lock: two uploads at once never pass 20 together.
        await session.execute(select(ClubSites.id).where(ClubSites.id == site.id).with_for_update())
        n = await count(session, site.club_id)
        if n >= MAX_IMAGES:
            raise _full()
        row = ClubSiteImages(club_id=site.club_id, image=key, alt=alt, position=n, width=width, height=height)
        session.add(row)
        await session.commit()
    except BaseException:
        await session.rollback()
        await asyncio.to_thread(_remove_files, key)
        raise
    await session.refresh(row)
    return row


async def set_alt(session: AsyncSession, row: ClubSiteImages, alt: str | None) -> ClubSiteImages:
    row.alt = _clean_alt(alt)
    await session.commit()
    await session.refresh(row)
    return row


async def reorder(session: AsyncSession, club_id: int, ids: list[int]) -> None:
    """Positions 0..n-1 in the order of `ids`, which must name every photo
    in the library once; one UPDATE."""
    have = set((await session.scalars(select(ClubSiteImages.id).where(ClubSiteImages.club_id == club_id))).all())
    if len(ids) != len(set(ids)) or set(ids) != have:
        raise CustomHTTPException(
            422, message="The order must list every photo in the library once.", error_code="SITE_IMAGE_ORDER_INVALID",
            errors={"missing": sorted(have - set(ids)), "unknown": sorted(set(ids) - have)},
        )
    if not ids:
        return
    await session.execute(
        update(ClubSiteImages)
        .where(ClubSiteImages.club_id == club_id, ClubSiteImages.id.in_(ids))
        .values(position=case({image_id: i for i, image_id in enumerate(ids)}, value=ClubSiteImages.id), updated_at=func.now())
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _compact(session: AsyncSession, club_id: int) -> None:
    """Positions 0..n-1 again after a delete, in one UPDATE; rows whose
    place did not change are not touched (nor their updated_at)."""
    ranked = (
        select(ClubSiteImages.id.label("id"), (func.row_number().over(order_by=(ClubSiteImages.position, ClubSiteImages.id)) - 1).label("pos"))
        .where(ClubSiteImages.club_id == club_id)
        .subquery()
    )
    await session.execute(
        update(ClubSiteImages)
        .where(ClubSiteImages.id == ranked.c.id, ClubSiteImages.position != ranked.c.pos)
        .values(position=ranked.c.pos, updated_at=ClubSiteImages.updated_at)
        .execution_options(synchronize_session=False)
    )


async def delete(session: AsyncSession, site: ClubSites, row: ClubSiteImages) -> bool:
    """The photo, its files, and every slot it was in (in every template's
    keys). Whether the published page showed it -- then it is made again now."""
    from app.api.sites import service

    shown = bool(site.published_at) and service.on_page(site.html, row)
    picture = row.image
    image_id = row.id
    await session.delete(row)
    site.layout, was_placed = layouts.without_image(site.layout, image_id)
    if was_placed:
        site.settings_at = service._now()
    await session.flush()
    await _compact(session, site.club_id)
    await session.commit()
    if picture is not None and hasattr(picture, "delete"):
        picture.delete()
    return shown
