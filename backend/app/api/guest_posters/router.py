"""The guest's poster endpoints (CONTRACT.md §6): /api/v1/posters/{ticket_id}...

Holder only -- the account the registration belongs to, or the poster
link's signature (`?s=`) -- as the ticket page reads a ticket. The club, its
co-hosts and admins see the counts on the Posters page, never a guest's photo.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, File, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field

from app.api.guest_posters import photos, render, service
from app.core.auth.dependencies import OptionalAuth
from app.core.layout_v2 import poster
from app.core.rate_limit import limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/posters", tags=["posters"])

#: Every guest route, per ticket: never per address, which behind the proxy
#: can be one address for every guest at once (app.core.rate_limit).
PER_TICKET = "300/minute"


def _ticket_key(request: Request) -> str:
    return "poster:" + str((request.path_params or {}).get("ticket_id") or "")[:60]


async def _guest(session, ticket_id: str, user, s: Optional[str], *, mint: bool = True):
    registration = await service.holder(session, ticket_id, user, s)
    return await service.guest(session, registration, mint=mint)


def _format(fmt: str, g) -> str:
    if fmt not in poster.FORMATS or fmt not in g.formats:
        raise CustomHTTPException(422, message="This poster is not offered in that format.", error_code="INVALID_FORMAT")
    return fmt


@router.get("/{ticket_id}", summary="Whether this guest has a poster now, and everything the poster page shows")
@limiter.limit(PER_TICKET, key_func=_ticket_key)
async def availability(request: Request, session: SessionDep, ticket_id: str, s: Optional[str] = Query(None, max_length=64),
                       user: OptionalAuth = None) -> dict:
    g, mine = await _guest(session, ticket_id, user, s)
    out = await service.availability(session, g, mine)
    await session.commit()
    return out


@router.post("/{ticket_id}/photo", summary="The guest's photo for their poster (cleaned; metadata removed)")
@limiter.limit(PER_TICKET, key_func=_ticket_key)
async def upload_photo(request: Request, session: SessionDep, ticket_id: str, file: UploadFile = File(...),
                       s: Optional[str] = Query(None, max_length=64), user: OptionalAuth = None) -> dict:
    g, mine = await _guest(session, ticket_id, user, s)
    service.needs(g)
    raw = await file.read(photos.MAX_BYTES + 1)
    if len(raw) > photos.MAX_BYTES:
        raise CustomHTTPException(413, message="That photo is over 12 MB. Use a smaller one.", error_code="PHOTO_TOO_LARGE")
    await service.set_photo(session, g, mine, raw)
    g, mine = await _guest(session, ticket_id, user, s)
    out = await service.availability(session, g, mine)
    await session.commit()
    return out


@router.get("/{ticket_id}/photo", summary="The guest's own photo")
@limiter.limit(PER_TICKET, key_func=_ticket_key)
async def get_photo(request: Request, session: SessionDep, ticket_id: str, s: Optional[str] = Query(None, max_length=64),
                    user: OptionalAuth = None) -> Response:
    registration = await service.holder(session, ticket_id, user, s)
    mine = await service.poster_row(session, registration, create=False)
    key = mine.photo_key if mine is not None else None
    await session.commit()
    data = await asyncio.to_thread(photos.read, key) if key else None
    if not data:
        raise CustomHTTPException(404, message="No photo yet.", error_code="NO_PHOTO")
    return Response(content=data, media_type=photos.content_type(key), headers={"Cache-Control": "private, no-store"})


@router.delete("/{ticket_id}/photo", summary="Remove the guest's photo and every poster made with it, now")
@limiter.limit(PER_TICKET, key_func=_ticket_key)
async def delete_photo(request: Request, session: SessionDep, ticket_id: str, s: Optional[str] = Query(None, max_length=64),
                       user: OptionalAuth = None) -> dict:
    registration = await service.holder(session, ticket_id, user, s)
    mine = await service.poster_row(session, registration, create=False)
    if mine is not None:
        await service.drop_photo(session, registration.event_id, mine)
    g, mine = await service.guest(session, registration, mint=True)
    out = await service.availability(session, g, mine)
    await session.commit()
    return out


class FramingBody(BaseModel):
    format: Optional[str] = Field(default=None, description="square, portrait or story; omitted: every format")
    zoom: float = Field(default=1.0)
    x: float = Field(default=0.5)
    y: float = Field(default=0.45)


@router.put("/{ticket_id}/framing", summary="Where the photo sits in the frame: zoom, and the photo's point at the frame's centre")
@limiter.limit(PER_TICKET, key_func=_ticket_key)
async def put_framing(request: Request, session: SessionDep, ticket_id: str, body: FramingBody,
                      s: Optional[str] = Query(None, max_length=64), user: OptionalAuth = None) -> dict:
    g, mine = await _guest(session, ticket_id, user, s)
    service.needs(g)
    if body.format is not None:
        _format(body.format, g)
    if not mine.photo_key:
        raise CustomHTTPException(409, message="Add a photo first.", error_code="NO_PHOTO")
    try:
        framing = photos.checked_framing(body.model_dump())
    except ValueError as exc:
        raise CustomHTTPException(422, message="Zoom is from 1 to 5; x and y from 0 to 1.", error_code="INVALID_FRAMING", errors={"field": str(exc)})
    await service.set_framing(session, mine, body.format, framing)
    g, mine = await _guest(session, ticket_id, user, s)
    out = await service.availability(session, g, mine)
    await session.commit()
    return out


class RenderBody(BaseModel):
    format: str


@router.post("/{ticket_id}/render", summary="Ask for the poster's PNG in a format: ready, or being drawn")
@limiter.limit(PER_TICKET, key_func=_ticket_key)
async def ask_render(request: Request, response: Response, session: SessionDep, ticket_id: str, body: RenderBody,
                     s: Optional[str] = Query(None, max_length=64), user: OptionalAuth = None) -> dict:
    g, mine = await _guest(session, ticket_id, user, s)
    service.needs(g)
    fmt = _format(body.format, g)
    status, out = await service.ask_render(session, g, mine, fmt)
    await session.commit()
    response.status_code = status
    return out


@router.get("/{ticket_id}/render/{fmt}", summary="Whether the poster in a format is drawn yet")
@limiter.limit(PER_TICKET, key_func=_ticket_key)
async def render_state(request: Request, session: SessionDep, ticket_id: str, fmt: str,
                       s: Optional[str] = Query(None, max_length=64), user: OptionalAuth = None) -> dict:
    g, mine = await _guest(session, ticket_id, user, s)
    service.needs(g)
    out = await service.render_status(session, g, mine, _format(fmt, g))
    await session.commit()
    return out


@router.get("/{ticket_id}/image/{fmt}", summary="The poster's PNG, once drawn")
@limiter.limit(PER_TICKET, key_func=_ticket_key)
async def image(request: Request, session: SessionDep, ticket_id: str, fmt: str, s: Optional[str] = Query(None, max_length=64),
                v: Optional[str] = Query(None, max_length=40), download: bool = Query(False), user: OptionalAuth = None) -> Response:
    g, mine = await _guest(session, ticket_id, user, s)
    service.needs(g)
    fmt = _format(fmt, g)
    p = await service.prepared(session, g, mine, fmt)
    state = (mine.renders or {}).get(fmt) or {}
    await session.commit()
    if render.status(mine, p) != "ready":
        raise CustomHTTPException(404, message="This poster is not drawn yet.", error_code="POSTER_NOT_READY")
    etag = f'"{p.fingerprint}"'
    headers = {"Cache-Control": "private, max-age=300", "ETag": etag}
    if download:
        slug = (g.event.slug or str(g.event.id))[:80]
        headers["Content-Disposition"] = f'attachment; filename="im-attending-{slug}-{fmt}.png"'
    if etag in (request.headers.get("if-none-match") or ""):
        return Response(status_code=304, headers=headers)
    data = await asyncio.to_thread(photos.read, state.get("key"))
    if not data:
        await render.set_state(session, mine.id, fmt, {"fingerprint": None, "state": "gone"})
        await session.commit()
        raise CustomHTTPException(404, message="This poster is not drawn yet.", error_code="POSTER_NOT_READY")
    return Response(content=data, media_type="image/png", headers=headers)


class EventBody(BaseModel):
    action: str = Field(..., max_length=20)
    format: Optional[str] = Field(default=None, max_length=10)


@router.post("/{ticket_id}/events", summary="What the guest did with their poster (counted, nothing else)", status_code=204)
@limiter.limit(PER_TICKET, key_func=_ticket_key)
async def record_event(request: Request, session: SessionDep, ticket_id: str, body: EventBody,
                       s: Optional[str] = Query(None, max_length=64), user: OptionalAuth = None) -> Response:
    registration = await service.holder(session, ticket_id, user, s)
    if body.action not in service.ACTIONS:
        raise CustomHTTPException(422, message="action: download, share, copy_caption or copy_link.", error_code="INVALID_ACTION")
    mine = await service.poster_row(session, registration, create=False)
    if mine is not None:
        await service.record(session, mine, body.action)
    await session.commit()
    return Response(status_code=204)
