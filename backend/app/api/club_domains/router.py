"""A club's own domain: the club's side (the owner, or a team member with the
admin role -- club_access.policy refuses everyone else), the Worker's
public question, and the admins' list.

Only connecting a domain is gated by the plan (`custom_domain`). Reading,
checking and removing never are: a club that drops its plan still sees its
domain, and can take it away.
"""
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.club_domains import service
from app.api.club_domains.schemas import AdminDomainItem, AdminDomainList, DomainIn, ResolveOut, SiteDomainOut
from app.api.clubs.models import Clubs
from app.api.subscriptions import gates
from app.core.auth.dependencies import AdminAuth, ClubAuth
from app.core.auth.permissions import ModerateAuth
from app.core.rate_limit import limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/clubs/site/domain", tags=["site-domain"])
public_router = APIRouter(prefix="/sites", tags=["site-domain"])
admin_router = APIRouter(prefix="/admin/club-domains", tags=["site-domain"])

#: How long the edge (and anything in between) may keep the resolver's answer.
RESOLVE_CACHE = "public, max-age=60, s-maxage=300"
UNKNOWN_CACHE = "public, max-age=60, s-maxage=60"


async def _club_for(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise CustomHTTPException(404, message="No club for this account")
    return club


@router.get("", summary="The club's own domain for its website, and whether it can have one")
async def get_domain(session: SessionDep, user: ClubAuth) -> SiteDomainOut:
    """`available` is false when the server has no Cloudflare credentials;
    `entitled` whether the club's plan includes its own domain. `domain` is
    null with none, else its host, status (pending, active, paused, failed),
    the records to add, Cloudflare's certificate status and what is in the way."""
    club = await _club_for(session, user)
    return await service.out(session, club)


@router.put("", summary="Connect a domain the club owns (www.theirclub.org) to its website")
async def put_domain(body: DomainIn, request: Request, session: SessionDep, user: ClubAuth) -> SiteDomainOut:
    """
    Made at Cloudflare, then pending until the club adds the records shown.
    A domain already connected is replaced (and removed at Cloudflare).

    403 PLAN_FEATURE_LOCKED (`required_plan`), 503 DOMAINS_UNAVAILABLE,
    422 INVALID_HOST (an IP, one of ours, not a domain), 422
    APEX_NOT_SUPPORTED (a bare domain: use www. and forward the apex),
    409 HOST_TAKEN (another club's), 502 CLOUDFLARE_ERROR (Cloudflare's own
    short message).
    """
    club = await _club_for(session, user)
    await gates.require_feature(session, club.id, service.FEATURE, actor=user)
    row = await service.connect(session, club, body.host, user, request=request)
    return await service.out(session, club, row)


@router.post("/check", summary="Ask Cloudflare about the domain now")
@limiter.limit("12/minute")
async def check_domain(request: Request, session: SessionDep, user: ClubAuth) -> SiteDomainOut:
    """404 NO_DOMAIN, 503 DOMAINS_UNAVAILABLE, 502 CLOUDFLARE_ERROR."""
    club = await _club_for(session, user)
    row = await service.check(session, club)
    return await service.out(session, club, row)


@router.delete("", summary="Take the domain away from the club's website")
async def delete_domain(request: Request, session: SessionDep, user: ClubAuth) -> SiteDomainOut:
    club = await _club_for(session, user)
    await service.disconnect(session, club, user, request=request)
    return await service.out(session, club)


@public_router.get("/resolve", summary="Whose site a host is, for the club-sites Worker", response_model=ResolveOut)
async def resolve(session: SessionDep, host: str = Query(..., max_length=300)) -> Response:
    """`www.theirclub.org` -> the club's slug and whether to serve (active)
    or redirect to `canonical_host` (paused). `{slug}.myotherapp.in` -> its
    club, with canonical_host the club's own domain while that is active.
    404 for anything else. No sign-in, nothing personal; cached at the edge."""
    found = await service.resolve(session, host)
    if found is None:
        return JSONResponse(
            {"message": "No club site at this host", "errors": None, "error_code": "UNKNOWN_HOST", "track_id": None},
            status_code=404,
            headers={"Cache-Control": UNKNOWN_CACHE},
        )
    return JSONResponse(ResolveOut(**found).model_dump(), headers={"Cache-Control": RESOLVE_CACHE})


@admin_router.get("", summary="Clubs' own domains (admin)")
async def list_domains(
    session: SessionDep,
    admin: AdminAuth,
    status: str | None = Query(None, description="pending, active, paused, failed or removed; default all but removed"),
    q: str | None = Query(None, max_length=100, description="Host, club name or slug"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> AdminDomainList:
    rows, total = await service.admin_list(session, status=status or None, q=q, offset=offset, limit=limit)
    return {"items": [service.admin_item(row) for row in rows], "total": total}


@admin_router.post("/{domain_id}/recheck", summary="Ask Cloudflare about a club's domain now (admin)")
async def recheck_domain(domain_id: int, request: Request, session: SessionDep, admin: AdminAuth) -> AdminDomainItem:
    """Any admin, viewers included: it only reads Cloudflare again, and moves
    the domain on as the sweep would. 404, 503 DOMAINS_UNAVAILABLE, 502 CLOUDFLARE_ERROR."""
    row = await service.admin_recheck(session, domain_id, admin, request=request)
    await session.refresh(row, attribute_names=["club"])
    return service.admin_item(row)


@admin_router.delete("/{domain_id}", summary="Take a club's domain away (admin)")
async def delete_domain_admin(domain_id: int, request: Request, session: SessionDep, admin: ModerateAuth) -> AdminDomainItem:
    """Removed here and at Cloudflare (best effort); the club's site goes back to its subdomain. Audited."""
    row = await service.admin_remove(session, domain_id, admin, request=request)
    await session.refresh(row, attribute_names=["club"])
    return service.admin_item(row)
