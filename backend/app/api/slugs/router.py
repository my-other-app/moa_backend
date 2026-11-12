"""An admin changing a club's or an event's slug, when the club asks.

    PATCH /api/v1/admin/clubs/{club_id}/slug     {"slug": "gdg-cloud-tvm"}
    PATCH /api/v1/admin/events/{event_id}/slug   {"slug": "devfest-kochi-2026"}

Admins with role owner or support (MODERATE); a viewer gets 403
INSUFFICIENT_ADMIN_ROLE. 200 {"slug", "aliases", "url", "site_url"};
422 SLUG_INVALID / SLUG_RESERVED, 409 SLUG_TAKEN, 404 for no such (or a
deleted) club or event. The old slug keeps answering everywhere (service.py).
"""
from fastapi import APIRouter, Request

from app.api.slugs import service
from app.api.slugs.models import CLUB, EVENT
from app.api.slugs.schemas import SlugIn, SlugOut
from app.core.auth.permissions import ModerateAuth
from app.db.core import SessionDep

router = APIRouter(prefix="/admin", tags=["admin-slugs"])


@router.patch("/clubs/{club_id}/slug", summary="Change a club's address (its slug and website subdomain); the old one keeps working (admin)")
async def change_club_slug(club_id: int, body: SlugIn, request: Request, session: SessionDep, admin: ModerateAuth) -> SlugOut:
    return SlugOut(**await service.change(session, CLUB, club_id, body.slug, admin, request=request))


@router.patch("/events/{event_id}/slug", summary="Change an event's address (its slug); the old one keeps working (admin)")
async def change_event_slug(event_id: int, body: SlugIn, request: Request, session: SessionDep, admin: ModerateAuth) -> SlugOut:
    return SlugOut(**await service.change(session, EVENT, event_id, body.slug, admin, request=request))
