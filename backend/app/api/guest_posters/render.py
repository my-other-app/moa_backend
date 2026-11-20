"""A guest's poster as a PNG: worked out, kept by fingerprint, drawn by the
jobs queue (CONTRACT.md §9).

`prepare` reads everything a format's poster prints -- the resolved layout,
the guest's values, the brand colours, each picture by what names it (the
photo by its hash and framing) -- and its fingerprint. Same inputs, same
fingerprint, same PNG: never drawn twice. `draw` makes it: the layout v2
builder's page, the render service's PDF, pdfium's PNG at 1080 px.

Drawn by the job `guest_posters.render`: LOW priority and one at a time per
runner, so the mails and check-ins someone is waiting on claim first and
card printing (which does not use the queue) is never behind a queue of
posters. A render that fails marks the format `failed`, and the guest is told
to try again in a moment; nothing else is touched.

The state of each format lives on the guest's row, `renders[format]`:
{fingerprint, key?, state: queued|ready|failed, at, moment?, error?}, each
format written on its own (jsonb ||), so a job and a request never undo each
other's.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import JSONB

from app.api.doc_presets import layouts
from app.api.guest_posters import photos
from app.api.guest_posters.models import GuestPosters
from app.core import jobs
from app.core.layout_v2 import poster

logger = logging.getLogger(__name__)

JOB = "guest_posters.render"
#: A queued render not done in this long is asked for again.
QUEUED_FOR = 300.0
#: A failed render answers "try again in a moment" for this long.
FAILED_FOR = 20.0
PNG_WIDTH = 1080


@dataclass
class Prepared:
    fmt: str
    layout: Optional[dict]
    variant: str
    revision: int
    values: dict
    palette: dict
    #: picture name -> ("key", key) | ("url", url) | ("poster_photo", key, framing) | None
    refs: dict
    fingerprint: str
    frame: Optional[dict]
    framing: dict
    title: str = ""
    starter: Optional[str] = None
    extra: dict = field(default_factory=dict)


def _refs(layout: dict, event, poster_row) -> dict:
    """The layout's pictures (layouts.picture_refs, without a style: the club's
    logo, the event's poster image, the design's uploads); its guest photo is
    the guest's own upload with their framing, or the initials tile."""
    refs = layouts.picture_refs(layout, {}, event, None, None)
    if "guest_photo" in refs:
        refs["guest_photo"] = None
    return refs


def _names(refs: dict, poster_row, fmt: str, frame: Optional[dict], framing: dict) -> dict:
    out = {}
    for name, ref in refs.items():
        if name == "guest_photo":
            sha = getattr(poster_row, "photo_sha", None) if poster_row is not None else None
            out[name] = ["poster_photo", sha, framing, frame and [frame["width"], frame["height"]]] if sha else None
        elif ref is None:
            out[name] = None
        else:
            from app.core.storage.bucket import key_from_url

            out[name] = ref[1] if ref[0] == "key" else (key_from_url(ref[1]) or ref[1])
    return out


def prepare(event, poster_row, resolved, fmt: str, values: dict) -> Prepared:
    """Everything a format's poster prints, and its fingerprint. Pure: nothing is read."""
    from app.api.events import doc_cache
    from app.core import layout_v2

    layout = resolved.layout
    palette = layouts.colors({}, event)
    frame = photos.frame_of(layout)
    framing = photos.framing_for(getattr(poster_row, "framing", None), fmt)
    refs = _refs(layout or {}, event, poster_row)
    names = _names(refs, poster_row, fmt, frame, framing)
    title = f"{values.get('name') or 'Poster'} · {event.name or ''}".strip(" ·")
    ctx = {"qr": values.get("share_url") or "", "colors": palette, "images": names, "title": title}
    try:
        design = str(layout_v2.fingerprint(layout, values, ctx, kind=poster.KIND)) if layout else "none"
    except Exception:  # noqa: BLE001 - a layout that cannot be read still has a key; drawing it fails
        design = "unreadable"
    fp = doc_cache.fingerprint("poster", design, resolved.variant, resolved.revision, resolved.starter, fmt)
    return Prepared(fmt=fmt, layout=layout, variant=resolved.variant, revision=resolved.revision, values=values,
                    palette=palette, refs=refs, fingerprint=fp, frame=frame, framing=framing, title=title, starter=resolved.starter)


def pictures(p: Prepared, poster_row, event) -> dict:
    """Every picture as a data: URI ("" for none). Blocking."""
    others = {k: v for k, v in p.refs.items() if k != "guest_photo"}
    images, _ = layouts.pictures(others, event_id=event.id)
    if "guest_photo" in p.refs:
        uri = ""
        key = getattr(poster_row, "photo_key", None)
        if key and p.frame:
            raw = photos.read(key)
            cut = photos.crop(raw, float(p.frame["width"]), float(p.frame["height"]), p.framing) if raw else None
            uri = photos.data_uri(cut) if cut else ""
        images["guest_photo"] = uri
    return images


def page_html(p: Prepared, poster_row, event, *, for_browser: bool = False) -> str:
    """The poster's page (the builder's), with its pictures. Blocking."""
    from app.core import layout_v2

    ctx = {"qr": p.values.get("share_url") or "", "colors": p.palette, "images": pictures(p, poster_row, event), "title": p.title}
    return layout_v2.build_html(p.layout, p.values, context=ctx, kind=poster.KIND, for_browser=for_browser)


async def draw(p: Prepared, poster_row, event) -> bytes:
    """The PNG, 1080 px wide: the page through the render service, then pdfium."""
    from app.api.doc_presets import engine
    from app.core.layout import pool
    from app.core.layout.renderer import pdf_to_png

    html = await asyncio.to_thread(page_html, p, poster_row, event)
    pdf = await engine.pdf(html)
    return await pool.run(pdf_to_png, pdf, width_px=PNG_WIDTH)


def png_key(event_id: int, registration_id, fmt: str, fingerprint: str) -> str:
    return f"{photos.folder(event_id, registration_id)}{fmt}-{fingerprint}.png"


# ─── State ──────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age(state: dict, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat(state.get("at"))).total_seconds()
    except (TypeError, ValueError):
        return 1e9


def status(poster_row, p: Prepared, now: Optional[datetime] = None) -> str:
    """none | rendering | ready | failed, for exactly these inputs."""
    now = now or _now()
    state = ((getattr(poster_row, "renders", None) or {}) if poster_row is not None else {}).get(p.fmt) or {}
    if state.get("fingerprint") != p.fingerprint:
        return "none"
    if state.get("state") == "ready" and state.get("key"):
        return "ready"
    if state.get("state") == "queued" and _age(state, now) < QUEUED_FOR:
        return "rendering"
    if state.get("state") == "failed" and _age(state, now) < FAILED_FOR:
        return "failed"
    return "none"


async def set_state(session, poster_id, fmt: str, state: dict) -> None:
    """One format's state, written on its own."""
    await session.execute(
        update(GuestPosters).where(GuestPosters.id == poster_id).values(
            renders=GuestPosters.renders.op("||")(sa.func.jsonb_build_object(sa.literal(fmt), sa.cast(sa.literal(json.dumps(state)), JSONB))),
            updated_at=_now(),
        ).execution_options(synchronize_session=False)
    )


async def queue(session, poster_row, p: Prepared) -> None:
    """Ask for the render in the caller's transaction (it runs once that commits)."""
    await set_state(session, poster_row.id, p.fmt, {"fingerprint": p.fingerprint, "state": "queued", "at": _now().isoformat()})
    await jobs.enqueue(
        session, JOB, {"poster_id": poster_row.id, "format": p.fmt, "fingerprint": p.fingerprint},
        dedupe_key=f"poster:{poster_row.registration_id}:{p.fmt}:{p.fingerprint}",
    )


@jobs.handler(JOB, max_attempts=2, timeout=90.0, concurrency=1, priority=jobs.LOW, alert_dead=False)
async def _render_job(payload: dict) -> None:
    """Draw one format's poster for the guest's current inputs, keep it, and
    drop the format's earlier PNG. Never raises for a render that fails: it
    is marked failed, and the guest asks again."""
    from app.api.guest_posters import service

    fmt = payload.get("format")
    async with jobs.scope() as session:
        loaded = await service.for_job(session, payload.get("poster_id"))
        if loaded is None or fmt not in poster.FORMATS:
            return
        guest, poster_row = loaded
        if not guest.available or fmt not in guest.formats:
            await set_state(session, poster_row.id, fmt, {"fingerprint": payload.get("fingerprint"), "state": "failed", "at": _now().isoformat(), "error": "not available"})
            await session.commit()
            return
        p = await service.prepared(session, guest, poster_row, fmt)
        await session.commit()  # nothing held while it is drawn
        if p.layout is None:
            await set_state(session, poster_row.id, fmt, {"fingerprint": p.fingerprint, "state": "failed", "at": _now().isoformat(), "error": "no design"})
            await session.commit()
            return
        try:
            png = await draw(p, poster_row, guest.event)
            key = png_key(guest.event.id, poster_row.registration_id, fmt, p.fingerprint)
            await asyncio.to_thread(photos_put, key, png)
        except Exception as exc:  # noqa: BLE001 - the render service down, a page that cannot be drawn
            logger.warning("a poster could not be drawn (%s, %s): %s", poster_row.id, fmt, exc)
            await set_state(session, poster_row.id, fmt, {"fingerprint": p.fingerprint, "state": "failed", "at": _now().isoformat(),
                                                          "error": type(exc).__name__})
            await session.commit()
            return
        old = ((poster_row.renders or {}).get(fmt) or {}).get("key")
        await set_state(session, poster_row.id, fmt, {"fingerprint": p.fingerprint, "key": key, "state": "ready", "at": _now().isoformat(),
                                                      "moment": guest.moment, "variant": p.variant})
        await session.execute(
            update(GuestPosters).where(GuestPosters.id == poster_row.id).values(
                render_count=GuestPosters.render_count + 1,
                made_at=sa.func.coalesce(GuestPosters.made_at, _now()),
            ).execution_options(synchronize_session=False)
        )
        await session.commit()
        if old and old != key and photos.is_ours(old, guest.event.id, poster_row.registration_id):
            try:
                await asyncio.to_thread(photos.delete, [old])
            except Exception:  # noqa: BLE001 - an old PNG left behind goes with the sweep
                logger.info("an earlier poster PNG could not be deleted: %s", old)


def photos_put(key: str, png: bytes) -> None:
    from app.core.storage import objects

    objects.put_bytes(key, png, "image/png")
