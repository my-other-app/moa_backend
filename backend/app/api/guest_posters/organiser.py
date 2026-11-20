"""The organiser's side (CONTRACT.md §10): the switch and words, the
designs on the layout v2 board (kind `poster`), and the numbers.

Under /events/{event_id}/..., so the co-host gate and the team roles apply
as to every event route: the host's and a co-host's teams by role (admins
and editors change, door staff read), admins. The design routes sit at
/events/{event_id}/designs/poster/..., included before the other kinds'
/designs/{kind}/... routes, which do not know this kind.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, File, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.doc_presets import engine as preset_engine
from app.api.event_designs import router as event_designs
from app.api.events.models import EventRegistrationsLink
from app.api.guest_posters import designs, moments as mom, render, service, stats as stats_mod
from app.api.guest_posters.models import PosterSettings
from app.core.auth.dependencies import ClubAuth
from app.core.layout_v2 import poster
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/events/{event_id}", tags=["posters"])


# ─── Settings ───────────────────────────────────────────────────────────────


def settings_out(row: Optional[PosterSettings]) -> dict:
    return {
        "enabled": bool(row.enabled) if row is not None else False,
        "platform_enabled": service.platform_on(),
        "formats": service.formats_of(row),
        "moments": mom.moments_of(row),
        "available_formats": [{"key": f, "label": poster.LABELS[f], "width": poster.FORMATS[f][0], "height": poster.FORMATS[f][1]} for f in poster.ORDER],
        "poster_url_token": "{{poster_url}}",
        "updated_at": row.updated_at.isoformat() if row is not None and row.updated_at else None,
    }


class MomentBody(BaseModel):
    enabled: Optional[bool] = None
    phrase: Optional[str] = Field(default=None, max_length=200)
    caption: Optional[str] = Field(default=None, max_length=2000)


class SettingsBody(BaseModel):
    enabled: Optional[bool] = None
    formats: Optional[list[str]] = None
    moments: Optional[dict[str, MomentBody]] = None


def _checked_moments(current: dict, sent: dict[str, MomentBody]) -> dict:
    from app.core.layout_v2 import vocabulary as tk

    out = {k: dict(v) for k, v in current.items()}
    problems = {}
    for key, body in sent.items():
        if key not in mom.MOMENTS:
            problems[f"moments.{key}"] = "A moment is before, during or after."
            continue
        if body.enabled is not None:
            out[key]["enabled"] = bool(body.enabled)
        if body.phrase is not None:
            phrase = " ".join(body.phrase.split())
            if not phrase or len(phrase) > mom.PHRASE_MAX:
                problems[f"moments.{key}.phrase"] = f"A phrase of 1 to {mom.PHRASE_MAX} characters."
            else:
                out[key]["phrase"] = phrase
        if body.caption is not None:
            caption = body.caption.strip()
            if len(caption) > mom.CAPTION_MAX:
                problems[f"moments.{key}.caption"] = f"At most {mom.CAPTION_MAX} characters."
            else:
                errors = tk.parse(caption, poster.KIND).errors
                if errors:
                    problems[f"moments.{key}.caption"] = errors[0]
                else:
                    out[key]["caption"] = caption
    if problems:
        raise CustomHTTPException(422, message=next(iter(problems.values())), error_code="INVALID_POSTER_SETTINGS", errors={"fields": problems})
    return out


@router.get("/posters/settings", summary="The event's poster switch, moments and formats")
async def get_settings(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    await event_designs._event_for_editing(session, user, event_id)
    return settings_out(await session.get(PosterSettings, event_id))


@router.put("/posters/settings", summary="Switch posters on or off; the moments' phrases and captions; the formats")
async def put_settings(session: SessionDep, user: ClubAuth, event_id: int, body: SettingsBody) -> dict:
    await event_designs._event_for_editing(session, user, event_id)
    row = await session.scalar(select(PosterSettings).where(PosterSettings.event_id == event_id).with_for_update())
    current = mom.moments_of(row)
    moments = _checked_moments(current, body.moments or {}) if body.moments is not None else current
    if body.formats is not None:
        formats = [f for f in poster.ORDER if f in set(body.formats)]
        if not formats or any(f not in poster.FORMATS for f in body.formats):
            raise CustomHTTPException(422, message="Offer at least one format: square, portrait or story.", error_code="INVALID_POSTER_SETTINGS",
                                      errors={"fields": {"formats": "square, portrait, story"}})
    else:
        formats = service.formats_of(row)
    now = datetime.now(timezone.utc)
    if row is None:
        row = PosterSettings(event_id=event_id, enabled=False, created_at=now)
        session.add(row)
    if body.enabled is not None:
        row.enabled = bool(body.enabled)
    row.formats = formats
    row.moments = moments
    row.updated_by = user.id
    row.updated_at = now
    await session.commit()
    return settings_out(await session.get(PosterSettings, event_id))


@router.get("/posters/stats", summary="Posters made, shared, and the people their links brought")
async def get_stats(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    event = await event_designs._event_for_editing(session, user, event_id)
    return await stats_mod.stats(session, event)


# ─── Designs on the board ───────────────────────────────────────────────────


@router.get("/designs/poster/variants", summary="The event's poster variants, and the ones it could have")
async def list_variants(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    event = await event_designs._event_for_editing(session, user, event_id)
    return await designs.variants_out(session, event)


@router.get("/designs/poster/layout", summary="One variant's poster in one format, as the board opens it")
async def get_layout(session: SessionDep, user: ClubAuth, event_id: int, variant: str = Query("default"), format: str = Query("square")) -> dict:
    event = await event_designs._event_for_editing(session, user, event_id)
    v, fmt = designs.checked_variant(variant), designs.checked_format(format)
    return designs.board(event, v, fmt, await designs.rows_of(session, event.id))


@router.put("/designs/poster/layout", summary="Save one variant's poster in one format")
async def put_layout(session: SessionDep, user: ClubAuth, event_id: int, body: dict = Body(...),
                     variant: str = Query("default"), format: str = Query("square")) -> dict:
    event = await event_designs._event_for_editing(session, user, event_id)
    v, fmt = designs.checked_variant(variant), designs.checked_format(format)
    layout, warnings = designs.checked(body.get("layout") if isinstance(body.get("layout"), dict) else body, fmt,
                                       prefixes=event_designs._prefixes_for(event))
    await designs.save(session, event, v, {fmt: layout}, user_id=user.id)
    await session.commit()
    return {**designs.board(event, v, fmt, await designs.rows_of(session, event.id)), "warnings": warnings}


@router.delete("/designs/poster/layout", summary="Drop one format of a variant, or the whole variant")
async def delete_layout(session: SessionDep, user: ClubAuth, event_id: int, variant: str = Query(...), format: Optional[str] = Query(None)) -> dict:
    event = await event_designs._event_for_editing(session, user, event_id)
    v = designs.checked_variant(variant)
    fmt = designs.checked_format(format) if format is not None else None
    if not await designs.drop(session, event, v, fmt, user_id=user.id):
        raise CustomHTTPException(404, message="This variant has no design of its own.", error_code="NO_SUCH_VARIANT")
    await session.commit()
    return await designs.variants_out(session, event)


@router.get("/designs/poster/starters", summary="The poster starters, for the gallery")
async def list_starters(session: SessionDep, user: ClubAuth, event_id: int) -> list[dict]:
    await event_designs._event_for_editing(session, user, event_id)
    return poster.starters()


@router.post("/designs/poster/layout/starters/{starter_id:path}", summary="A poster starter to begin from (nothing is saved)")
async def use_starter(session: SessionDep, user: ClubAuth, event_id: int, starter_id: str, format: str = Query("square")) -> dict:
    await event_designs._event_for_editing(session, user, event_id)
    fmt = designs.checked_format(format)
    if starter_id not in poster.starter_ids():
        raise CustomHTTPException(404, message="No such starter")
    meta = poster.meta(starter_id)
    return {"id": starter_id, "name": meta["name"], "layout": poster.starter(starter_id, fmt), "formats": poster.starter_formats(starter_id)}


class ApplyBody(BaseModel):
    starter_id: str = Field(..., max_length=80)
    variant: str = Field("default", max_length=40)


@router.post("/designs/poster/apply", summary="Put a starter's three formats on a variant")
async def apply_starter(session: SessionDep, user: ClubAuth, event_id: int, body: ApplyBody) -> dict:
    event = await event_designs._event_for_editing(session, user, event_id)
    v = designs.checked_variant(body.variant)
    if body.starter_id not in poster.starter_ids():
        raise CustomHTTPException(404, message="No such starter")
    found = {}
    for fmt, layout in poster.starter_formats(body.starter_id).items():
        if fmt in poster.FORMATS and isinstance(layout, dict):
            found[fmt], _ = designs.checked(layout, fmt, prefixes=event_designs._prefixes_for(event))
    await designs.save(session, event, v, found, user_id=user.id, starter=body.starter_id)
    await session.commit()
    return await designs.variants_out(session, event)


class PreviewBody(BaseModel):
    layout: Optional[dict] = None
    ticket_id: Optional[str] = Field(default=None, max_length=60)


@router.post("/designs/poster/layout/preview", summary="A poster drawn exactly, for the sample guest or a registration")
async def preview(session: SessionDep, user: ClubAuth, event_id: int, body: PreviewBody, variant: str = Query("default"),
                  format: str = Query("square"), as_: str = Query("png", alias="as", pattern="^(html|png)$")) -> Response:
    event = await event_designs._event_for_editing(session, user, event_id)
    v, fmt = designs.checked_variant(variant), designs.checked_format(format)
    rows = await designs.rows_of(session, event.id)
    if body.layout is not None:
        layout, _ = designs.checked(body.layout, fmt, prefixes=event_designs._prefixes_for(event))
        resolved = designs.Resolved(layout, v, 0)
    else:
        b = designs.board(event, v, fmt, rows)
        resolved = designs.Resolved(b["layout"], v, b["revision"])
    if resolved.layout is None:
        raise CustomHTTPException(404, message="This poster has no design yet.", error_code="NO_DESIGN")
    registration, mine, values = None, None, None
    if body.ticket_id:
        registration = await session.scalar(
            select(EventRegistrationsLink.ticket_id).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.ticket_id == body.ticket_id,
                                                           EventRegistrationsLink.is_deleted == False)  # noqa: E712
        )
        if registration is None:
            raise CustomHTTPException(404, message="No such registration on this event")
        registration = await service.registration_by_ticket(session, registration)
        # Their words, never their photo: a guest's photo is theirs (the initials stand in).
        values, _ = await service.preview_values(session, registration)
    if values is None:
        from app.api.guest_posters import values as poster_values

        values = poster_values.sample_values(event, role=v.split(":", 1)[1] if v.startswith("role:") else "attendee")
    p = render.prepare(event, mine, resolved, fmt, values)
    await session.commit()
    headers = {"Cache-Control": "no-store"}
    if as_ == "html":
        html = await asyncio.to_thread(render.page_html, p, mine, event, for_browser=True)
        return Response(content=json.dumps({"html": html}), media_type="application/json", headers=headers)
    try:
        png = await render.draw(p, mine, event)
    except preset_engine.Unavailable as exc:
        preset_engine.unavailable("poster", exc)
        raise CustomHTTPException(503, message=preset_engine.PREVIEW_UNAVAILABLE, error_code="RENDER_UNAVAILABLE")
    return Response(content=png, media_type="image/png", headers=headers)


@router.post("/designs/poster/layout/images", summary="A picture for a poster design")
async def upload_image(session: SessionDep, user: ClubAuth, event_id: int, file: UploadFile = File(...),
                       purpose: str = Query("image", pattern="^(template|image)$")) -> dict:
    await event_designs._event_for_editing(session, user, event_id)
    raw = await file.read()
    return await asyncio.to_thread(event_designs._store_layout_image, raw, event_designs._layout_prefix(event_id), purpose)

