"""An event's poster made to each platform's size (models.SIZES): cropped
around a focus point the club drags onto what matters, or fitted whole on a
background in the club's colour -- or a club's own picture for a size, made
to that size the same two ways.

Made with the image code uploads use (Pillow; storage.images), stored as
JPEG under events/posters/ with a versioned name, a public folder on the
media CDN (storage.media.PUBLIC_FOLDERS): Instagram and Facebook fetch a
post's picture by its address, LinkedIn and X are sent its bytes. A variant
made from the poster is made again when the poster changes (`source_key`);
one from the club's own picture stays.
"""
from __future__ import annotations

import asyncio
import io
import re
from typing import Optional

from PIL import Image, ImageOps
from sqlalchemy import select

from app.api.social.models import SIZES, PosterVariants
from app.core.storage import bucket, images, media, objects
from app.core.storage.fields import stored_key
from app.response import CustomHTTPException

COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_BACKGROUND = "#111111"
QUALITY = 88


def folder() -> str:
    return bucket.prefix("events", "posters")


def poster_key(event) -> Optional[str]:
    return stored_key(getattr(event, "poster", None))


def url_of(variant: Optional[PosterVariants]) -> Optional[str]:
    if variant is None or not variant.key:
        return None
    return media.public_url(variant.key)


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def make(raw: bytes, size: str, *, mode: str, focus: tuple[float, float], background: Optional[str]) -> bytes:
    """`raw` made to `size`: a JPEG. Blocking: run it in a thread."""
    width, height = SIZES[size]
    src = images.checked_image(raw, max_bytes=20 * 1024 * 1024)
    try:
        src = ImageOps.exif_transpose(src)
    except Exception:  # noqa: BLE001
        pass
    if src.mode not in ("RGB", "RGBA"):
        src = src.convert("RGBA")
    W, H = src.size
    if mode == "fit":
        bg = background if background and COLOUR.match(background) else DEFAULT_BACKGROUND
        canvas = Image.new("RGB", (width, height), bg)
        scale = min(width / W, height / H)
        fitted = src.resize((max(1, round(W * scale)), max(1, round(H * scale))), Image.Resampling.LANCZOS)
        at = ((width - fitted.width) // 2, (height - fitted.height) // 2)
        canvas.paste(fitted.convert("RGB"), at, fitted if fitted.mode == "RGBA" else None)
        out = canvas
    else:
        ratio = width / height
        fx, fy = _clamp(focus[0]), _clamp(focus[1])
        if W / H > ratio:
            cw, ch = H * ratio, H
        else:
            cw, ch = W, W / ratio
        x0 = min(max(fx * W - cw / 2, 0), W - cw)
        y0 = min(max(fy * H - ch / 2, 0), H - ch)
        out = src.crop((round(x0), round(y0), round(x0 + cw), round(y0 + ch))).resize((width, height), Image.Resampling.LANCZOS)
        if out.mode == "RGBA":
            flat = Image.new("RGB", out.size, background if background and COLOUR.match(background) else "#ffffff")
            flat.paste(out, mask=out.split()[3])
            out = flat
    buf = io.BytesIO()
    out.convert("RGB").save(buf, "JPEG", quality=QUALITY, optimize=True)
    return buf.getvalue()


def _store(data: bytes, size: str) -> str:
    key = f"{folder()}{media.versioned_name('jpg').rsplit('.', 1)[0]}.{size}.jpg"
    objects.put_bytes(key, data, "image/jpeg")
    return key


async def _club_colour(session, event) -> Optional[str]:
    from app.api.clubs.models import Clubs

    colour = await session.scalar(select(Clubs.brand_primary).where(Clubs.id == event.club_id))
    return colour if colour and COLOUR.match(colour) else None


async def variants(session, event_id: int) -> dict[str, PosterVariants]:
    rows = await session.scalars(select(PosterVariants).where(PosterVariants.event_id == event_id))
    return {v.size: v for v in rows}


async def _render(session, event, row: PosterVariants) -> bool:
    """(Re)make `row` from its source; False when there is no picture to make it from."""
    source = row.source_key if row.own else poster_key(event)
    if not source:
        return False
    raw = await asyncio.to_thread(objects.get_bytes, source)
    if not raw:
        return False
    data = await asyncio.to_thread(make, raw, row.size, mode=row.mode, focus=(row.focus_x, row.focus_y), background=row.background)
    row.key = await asyncio.to_thread(_store, data, row.size)
    row.source_key = source
    row.width, row.height = SIZES[row.size]
    return True


async def ensure(session, event, sizes=None) -> dict[str, PosterVariants]:
    """Every size the event has (or `sizes`), made now where missing or out of
    date with the poster. Flushed, not committed. A size with no picture to
    make it from is left out."""
    have = await variants(session, event.id)
    poster = poster_key(event)
    colour = None
    for size in sizes or SIZES:
        row = have.get(size)
        if row is not None and row.key and (row.own or row.source_key == poster):
            continue
        if row is None:
            if not poster:
                continue
            colour = colour or await _club_colour(session, event)
            w, h = SIZES[size]
            row = PosterVariants(event_id=event.id, size=size, width=w, height=h, mode="crop", focus_x=0.5, focus_y=0.5, background=colour)
            session.add(row)
        try:
            made = await _render(session, event, row)
        except CustomHTTPException:
            made = False
        if made:
            have[size] = row
        elif row.id is None or row.key is None:
            have.pop(size, None)
    await session.flush()
    return {k: v for k, v in have.items() if v.key}


async def set_mode(session, event, size: str, *, mode: str, focus: Optional[tuple[float, float]], background: Optional[str]) -> PosterVariants:
    have = await variants(session, event.id)
    row = have.get(size)
    if row is None:
        w, h = SIZES[size]
        row = PosterVariants(event_id=event.id, size=size, width=w, height=h, background=await _club_colour(session, event))
        session.add(row)
    row.mode = mode
    if focus is not None:
        row.focus_x, row.focus_y = _clamp(focus[0]), _clamp(focus[1])
    if background is not None:
        row.background = background
    if not await _render(session, event, row):
        raise CustomHTTPException(409, message="Add the event's poster first; each size is made from it.", error_code="NO_POSTER")
    await session.flush()
    return row


async def set_own(session, event, size: str, raw: bytes, *, mode: str = "crop") -> PosterVariants:
    """The club's own picture for `size`, kept (as a JPEG) and made to the size."""
    image = images.checked_image(raw)
    fmt = (image.format or "").upper()
    if fmt not in ("JPEG", "MPO", "PNG", "WEBP"):
        from app.core.storage import formats

        if fmt not in formats.CONVERTED:
            raise CustomHTTPException(422, message="Use a JPEG, PNG or WebP picture.", error_code="IMAGE_TYPE")
    buf = io.BytesIO()
    ImageOps.exif_transpose(image).convert("RGB").save(buf, "JPEG", quality=92)
    source = f"{folder()}{media.versioned_name('jpg')}"
    await asyncio.to_thread(objects.put_bytes, source, buf.getvalue(), "image/jpeg")
    have = await variants(session, event.id)
    row = have.get(size)
    if row is None:
        w, h = SIZES[size]
        row = PosterVariants(event_id=event.id, size=size, width=w, height=h)
        session.add(row)
    row.own, row.source_key, row.mode = True, source, mode
    row.focus_x = row.focus_y = 0.5
    await _render(session, event, row)
    await session.flush()
    return row


def out(row: PosterVariants) -> dict:
    return {
        "id": row.id, "size": row.size, "width": row.width, "height": row.height, "mode": row.mode,
        "focus": {"x": row.focus_x, "y": row.focus_y}, "background": row.background, "own": bool(row.own), "url": url_of(row),
    }


async def share_image_url(session, event) -> Optional[str]:
    """The link-preview picture: the 1200 x 630 made from the current poster
    (or the club's own for that size), else the poster itself. Never made
    here: a public read writes nothing."""
    row = await session.scalar(select(PosterVariants).where(PosterVariants.event_id == event.id, PosterVariants.size == "facebook"))
    if row is not None and row.key and (row.own or row.source_key == poster_key(event)):
        return url_of(row)
    key = poster_key(event)
    return media.public_url(key) if key else None
