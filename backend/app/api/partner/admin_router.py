"""Admin portal -> Integrations, under /api/v1/admin/integrations. Reading is
any admin's (VIEW); allocating, registering and suspending apps and revoking
keys change who reaches clubs' data, so they take MANAGE_CONTENT, the
capability the club records themselves take.

- GET    /admin/integrations/clubs                      clubs with an allocation (and usage)
- GET    /admin/integrations/clubs/{club_id}             one club: allocation, keys, connected apps, usage
- PUT    /admin/integrations/clubs/{club_id}             allocate: on/off, rate tier, max keys, notes
- DELETE /admin/integrations/keys/{key_id}               revoke any club's key
- GET    /admin/integrations/partner-apps                every partner app
- POST   /admin/integrations/partner-apps                register one: its client secret shown once
- GET    /admin/integrations/partner-apps/{id}           one, with its connected clubs
- PATCH  /admin/integrations/partner-apps/{id}           edit (allowed scopes, redirect URIs, tier...)
- POST   /admin/integrations/partner-apps/{id}/rotate-secret
- POST   /admin/integrations/partner-apps/{id}/suspend   ({reason}) every token refused until resumed
- POST   /admin/integrations/partner-apps/{id}/resume
- DELETE /admin/integrations/partner-apps/{id}/connections/{connection_id}   disconnect a club
- GET    /admin/integrations/usage                       the platform's requests a day, and the busiest clients
- POST   /admin/integrations/docs-links                  a private developer docs link ({purpose: self} or
                                                         {partner_app_id, label?}): its url shown once
- GET    /admin/integrations/docs-links                  every docs link (?partner_app_id= &kind= &club_id=)
- DELETE /admin/integrations/docs-links/{id}             revoke any of them

The docs links take MANAGE_CONTENT, reading included (docs_links.py).

The webhook health routes are app/api/webhooks_out's, under /admin/integrations/webhooks.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.audit import service as audit
from app.api.clubs.models import Clubs
from app.api.partner import allocation, apps, docs_links, keys, usage
from app.api.partner import scopes as catalogue
from app.api.partner.models import PartnerAllocations, PartnerApiKeys
from app.core.auth.dependencies import AdminAuth
from app.core.auth.permissions import ContentAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/admin/integrations", tags=["admin-integrations"])


class AllocationIn(BaseModel):
    enabled: bool
    rate_tier: str = Field("standard", pattern="^(standard|high)$")
    max_keys: int = Field(allocation.DEFAULT_MAX_KEYS, ge=0, le=100)
    notes: Optional[str] = Field(None, max_length=1000)


class AppIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=500)
    website: Optional[str] = Field(None, max_length=500)
    logo_url: Optional[str] = Field(None, max_length=500)
    contact_email: Optional[str] = Field(None, max_length=200)
    redirect_uris: list[str] = Field(..., min_length=1, max_length=10)
    allowed_scopes: list[str] = Field(..., min_length=1, max_length=20)
    rate_tier: str = Field("standard", pattern="^(standard|high)$")


class AppPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=500)
    website: Optional[str] = Field(None, max_length=500)
    logo_url: Optional[str] = Field(None, max_length=500)
    contact_email: Optional[str] = Field(None, max_length=200)
    redirect_uris: Optional[list[str]] = Field(None, min_length=1, max_length=10)
    allowed_scopes: Optional[list[str]] = Field(None, min_length=1, max_length=20)
    rate_tier: Optional[str] = Field(None, pattern="^(standard|high)$")


class SuspendIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=300)


class DocsLinkIn(BaseModel):
    purpose: Optional[str] = Field(None, pattern="^(self|share)$")
    partner_app_id: Optional[str] = Field(None, max_length=64)
    label: Optional[str] = Field(None, max_length=docs_links.MAX_LABEL)


def _alloc_out(club, alloc_row, *, live_keys: int = 0) -> dict:
    return {
        "club": {"id": club.id, "name": club.name, "slug": club.slug, "verified": bool(club.is_verified)},
        "enabled": bool(alloc_row.enabled) if alloc_row else False,
        "rate_tier": alloc_row.rate_tier if alloc_row else "standard",
        "rate_limit": allocation.limits_for(alloc_row.rate_tier if alloc_row else "standard"),
        "max_keys": alloc_row.max_keys if alloc_row else allocation.DEFAULT_MAX_KEYS,
        "notes": alloc_row.notes if alloc_row else None,
        "source": alloc_row.source if alloc_row else None,
        "updated_at": alloc_row.updated_at.isoformat() if alloc_row and alloc_row.updated_at else None,
        "live_keys": live_keys,
    }


async def _record(session, request, user, action: str, entity_type: str, entity_id, summary: str, **details) -> None:
    await audit.record(session, actor=user, action=action, entity_type=entity_type, entity_id=entity_id, summary=summary[:255],
                       details={"via": "admin", **details}, request=request)


@router.get("/clubs", summary="Clubs with API access allocated (or once allocated)")
async def list_allocations(session: SessionDep, user: AdminAuth, enabled: Optional[bool] = Query(None)) -> dict:
    q = select(PartnerAllocations, Clubs).join(Clubs, Clubs.id == PartnerAllocations.club_id).where(Clubs.is_deleted == False)  # noqa: E712
    if enabled is not None:
        q = q.where(PartnerAllocations.enabled == enabled)
    rows = (await session.execute(q.order_by(PartnerAllocations.updated_at.desc()).limit(500))).all()
    counts = dict((await session.execute(
        select(PartnerApiKeys.club_id, func.count()).where(PartnerApiKeys.revoked_at.is_(None)).group_by(PartnerApiKeys.club_id)
    )).all())
    return {"clubs": [_alloc_out(c, a, live_keys=counts.get(c.id, 0)) for a, c in rows]}


async def _club(session, club_id: int):
    club = await session.get(Clubs, club_id)
    if club is None or club.is_deleted:
        raise CustomHTTPException(404, message="Club not found", error_code="CLUB_NOT_FOUND")
    return club


@router.get("/clubs/{club_id}", summary="One club's API access: allocation, keys, connected apps, usage")
async def club_detail(club_id: int, session: SessionDep, user: AdminAuth) -> dict:
    club = await _club(session, club_id)
    row = await session.get(PartnerAllocations, club.id)
    club_keys = await keys.club_keys(session, club.id, include_revoked=True)
    return {
        **_alloc_out(club, row, live_keys=sum(1 for k in club_keys if k.revoked_at is None)),
        "plan_allows": (await allocation.of(session, club.id)).allocated if row is None else None,
        "keys": [keys.out(k) for k in club_keys],
        "connected_apps": [apps.connection_out(c, a) for c, a in await apps.club_connections(session, club.id)],
        "usage": await usage.by_day(session, club_id=club.id, days=30),
    }


@router.put("/clubs/{club_id}", summary="Allocate API access to a club, or take it away")
async def put_allocation(request: Request, club_id: int, body: AllocationIn, session: SessionDep, user: ContentAuth) -> dict:
    club = await _club(session, club_id)
    row = await allocation.put(session, club.id, enabled=body.enabled, rate_tier=body.rate_tier, max_keys=body.max_keys,
                               notes=body.notes, by_user_id=user.id)
    await _record(session, request, user, "partner.allocation.set", "club", club.id,
                  f"API access {'on' if body.enabled else 'off'} for {club.name}", enabled=body.enabled, rate_tier=body.rate_tier, max_keys=body.max_keys)
    await session.commit()
    return _alloc_out(club, row, live_keys=await keys.live_count(session, club.id))


@router.delete("/keys/{key_id}", summary="Revoke a club's API key")
async def revoke_key(request: Request, key_id: str, session: SessionDep, user: ContentAuth) -> dict:
    row = await keys.key_of(session, None, key_id)
    await keys.revoke(session, row, by_user_id=user.id)
    await _record(session, request, user, "partner.key.revoke", "club", row.club_id, f"API key {row.name} revoked by MyOtherApp", key_id=str(row.id))
    await session.commit()
    return keys.out(row)


@router.get("/partner-apps", summary="Every partner app")
async def list_apps(session: SessionDep, user: AdminAuth) -> dict:
    from app.api.partner.models import PartnerApps

    rows = list((await session.scalars(select(PartnerApps).order_by(PartnerApps.created_at.desc()))).all())
    counts = await apps.connection_counts(session, [a.id for a in rows])
    used = await apps.last_used(session, [a.id for a in rows])
    return {"apps": [apps.app_out(a, connections=counts.get(a.id, 0), last_used_at=used.get(a.id)) for a in rows], "scopes": catalogue.catalogue()}


@router.post("/partner-apps", summary="Register a partner app (its client secret is shown once)", status_code=201)
async def register_app(request: Request, body: AppIn, session: SessionDep, user: ContentAuth) -> dict:
    app, secret = await apps.register(session, body.model_dump(), by_user_id=user.id)
    await _record(session, request, user, "partner.app.register", "partner_app", app.id, f"Partner app {app.name} registered",
                  client_id=app.client_id, allowed_scopes=app.allowed_scopes.split())
    await session.commit()
    return apps.app_out(app, secret=secret, connections=0)


@router.get("/partner-apps/{app_id}", summary="A partner app and its connected clubs")
async def app_detail(app_id: str, session: SessionDep, user: AdminAuth) -> dict:
    app = await apps.app_of(session, app_id)
    conns = await apps.app_connections(session, app.id)
    clubs = {c.id: c for c in (await session.scalars(select(Clubs).where(Clubs.id.in_([x.club_id for x in conns])))).all()} if conns else {}
    return {
        **apps.app_out(app, connections=len(conns)),
        "connections": [{**apps.connection_out(c), "club": {"id": c.club_id, "name": getattr(clubs.get(c.club_id), "name", None)}} for c in conns],
    }


@router.patch("/partner-apps/{app_id}", summary="Edit a partner app")
async def edit_app(request: Request, app_id: str, body: AppPatch, session: SessionDep, user: ContentAuth) -> dict:
    app = await apps.app_of(session, app_id)
    fields = body.model_dump(exclude_unset=True)
    await apps.edit(session, app, fields)
    await _record(session, request, user, "partner.app.update", "partner_app", app.id, f"Partner app {app.name} edited", fields=sorted(fields))
    await session.commit()
    return apps.app_out(app)


@router.post("/partner-apps/{app_id}/rotate-secret", summary="A new client secret (shown once); the old one stops now")
async def rotate_secret(request: Request, app_id: str, session: SessionDep, user: ContentAuth) -> dict:
    app = await apps.app_of(session, app_id)
    secret = await apps.rotate_secret(session, app)
    await _record(session, request, user, "partner.app.rotate_secret", "partner_app", app.id, f"Partner app {app.name}'s secret rotated")
    await session.commit()
    return apps.app_out(app, secret=secret)


@router.post("/partner-apps/{app_id}/suspend", summary="Suspend a partner app: every token refused until resumed")
async def suspend_app(request: Request, app_id: str, body: SuspendIn, session: SessionDep, user: ContentAuth) -> dict:
    app = await apps.app_of(session, app_id)
    await apps.set_suspended(session, app, True, body.reason)
    await _record(session, request, user, "partner.app.suspend", "partner_app", app.id, f"Partner app {app.name} suspended", reason=body.reason)
    await session.commit()
    return apps.app_out(app)


@router.post("/partner-apps/{app_id}/resume", summary="Resume a suspended partner app")
async def resume_app(request: Request, app_id: str, session: SessionDep, user: ContentAuth) -> dict:
    app = await apps.app_of(session, app_id)
    await apps.set_suspended(session, app, False)
    await _record(session, request, user, "partner.app.resume", "partner_app", app.id, f"Partner app {app.name} resumed")
    await session.commit()
    return apps.app_out(app)


@router.delete("/partner-apps/{app_id}/connections/{connection_id}", summary="Disconnect a club from a partner app")
async def admin_disconnect(request: Request, app_id: str, connection_id: str, session: SessionDep, user: ContentAuth) -> dict:
    from app.api.partner.models import PartnerConnections

    app = await apps.app_of(session, app_id)
    try:
        import uuid

        conn = await session.get(PartnerConnections, uuid.UUID(connection_id))
    except ValueError:
        conn = None
    if conn is None or conn.app_id != app.id or conn.revoked_at is not None:
        raise CustomHTTPException(404, message="No such connection.", error_code="APP_NOT_FOUND")
    await apps.end_connection(session, conn, reason="admin", by_user_id=user.id)
    await _record(session, request, user, "partner.app.disconnected", "club", conn.club_id, f"{app.name} disconnected by MyOtherApp", connection_id=str(conn.id))
    await session.commit()
    return {"disconnected": True, "id": str(conn.id)}


@router.get("/usage", summary="The platform's API requests a day, and the busiest clients")
async def platform_usage(session: SessionDep, user: AdminAuth, days: int = Query(30, ge=1, le=90)) -> dict:
    return {"days": await usage.by_day(session, days=days), "clients": await usage.by_client(session, days=days)}


# ─── Developer docs links ────────────────────────────────────────────────


@router.post("/docs-links", summary="A private developer docs link: yours, or for a partner app's developers (url shown once)",
             status_code=201)
async def create_docs_link(request: Request, body: DocsLinkIn, session: SessionDep, user: ContentAuth) -> dict:
    """`{purpose: "self"}`: your own, 7 days. `{partner_app_id, label?}`: for that app's developers, 30 days."""
    if body.partner_app_id is None and body.purpose != "self":
        raise CustomHTTPException(422, message="Ask for your own link (purpose: self), or name the partner app.", error_code="VALIDATION",
                                  errors={"partner_app_id": "Required unless purpose is self."})
    row, raw, app = await docs_links.create_for_admin(session, user, partner_app_id=body.partner_app_id, label=body.label)
    if app is not None:
        await _record(session, request, user, "partner.docs_link.create", "partner_app", app.id,
                      f"Developer docs link made for {app.name}'s developers", link_id=str(row.id), label=row.label,
                      expires_at=row.expires_at.isoformat())
    else:
        await _record(session, request, user, "partner.docs_link.create", "user", user.id, "Developer docs link made for themselves",
                      link_id=str(row.id), expires_at=row.expires_at.isoformat())
    await session.commit()
    return docs_links.made(row, raw, app=app)


@router.get("/docs-links", summary="Every developer docs link (never their tokens)")
async def list_docs_links(
    session: SessionDep, user: ContentAuth, partner_app_id: Optional[str] = Query(None, max_length=64),
    kind: Optional[str] = Query(None, pattern="^(club|partner_app|admin)$"), club_id: Optional[int] = Query(None),
) -> dict:
    rows = await docs_links.all_links(session, partner_app_id=partner_app_id, kind=kind, club_id=club_id)
    clubs, found_apps = await docs_links.names(session, rows)
    return {"links": [docs_links.out(r, club=clubs.get(r.club_id), app=found_apps.get(r.partner_app_id)) for r in rows]}


@router.delete("/docs-links/{link_id}", summary="Revoke any developer docs link")
async def revoke_docs_link(request: Request, link_id: str, session: SessionDep, user: ContentAuth) -> dict:
    row = await docs_links.link_of(session, link_id)
    already = row.revoked_at is not None
    await docs_links.revoke(session, row, by_user_id=user.id)
    if not already:
        if row.kind == "club":
            entity_type, entity_id = "club", row.club_id
        elif row.kind == "partner_app":
            entity_type, entity_id = "partner_app", row.partner_app_id
        else:
            entity_type, entity_id = "user", row.created_by_user_id
        await _record(session, request, user, "partner.docs_link.revoke", entity_type, entity_id,
                      "Developer docs link revoked by MyOtherApp", link_id=str(row.id), kind=row.kind, label=row.label)
    await session.commit()
    return {"revoked": True}
