"""The club dashboard's Settings -> Integrations, under /api/v1/clubs/integrations
(the club's owner and admins only: club_access.policy's `_INTEGRATIONS`).

- GET    /clubs/integrations                       allocated?, limits, the scopes in plain words
- GET    /clubs/integrations/keys                  the club's keys (never a secret)
- POST   /clubs/integrations/keys                  a new key: its secret shown once
- POST   /clubs/integrations/keys/{id}/rotate      a new secret for the key, shown once; the old one ends now
- DELETE /clubs/integrations/keys/{id}             revoke
- GET    /clubs/integrations/connected-apps        partner apps connected to the club
- DELETE /clubs/integrations/connected-apps/{id}   disconnect (every token ends now)
- GET    /clubs/integrations/usage                 requests, errors and attendee reads a day, 30 days
- GET    /clubs/integrations/connect               the consent page's request, checked
- POST   /clubs/integrations/connect               allow (or deny) it: the redirect back to the partner

The webhook endpoints' dashboard routes are app/api/webhooks_out's, under
/clubs/integrations/webhooks.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.api.audit import service as audit
from app.api.club_access import service as club_access
from app.api.partner import allocation, apps, keys, oauth, usage
from app.api.partner import scopes as catalogue
from app.core.auth.dependencies import ClubAuth
from app.core.rate_limit import account_or_address, limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/clubs/integrations", tags=["integrations"])

DISCLOSURE = "Our registration form tells people their details are shared with {name}."


class KeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    mode: str = Field("live", pattern="^(live|test)$")
    scopes: list[str] = Field(..., min_length=1, max_length=20)
    expires_at: Optional[datetime] = None
    ip_allowlist: Optional[list[str]] = Field(None, max_length=keys.MAX_ALLOWLIST)
    attendees_disclosure_ack: bool = False


class Consent(BaseModel):
    client_id: str = Field(..., max_length=64)
    redirect_uri: Optional[str] = Field(None, max_length=2000)
    response_type: str = Field("code", max_length=20)
    scope: Optional[str] = Field(None, max_length=1000)
    state: Optional[str] = Field(None, max_length=1000)
    code_challenge: str = Field(..., max_length=128)
    code_challenge_method: str = Field("S256", max_length=10)
    mode: str = Field("live", pattern="^(live|test)$")
    decision: str = Field(..., pattern="^(allow|deny)$")
    scopes: list[str] = Field(default_factory=list, max_length=20)
    attendees_disclosure_ack: bool = False


async def _club(session, user):
    return await club_access.club_of(session, user)


def _who(request: Request, user) -> tuple[Optional[int], Optional[str]]:
    member = club_access.current_member(request)
    if member is not None:
        return member.id, (member.full_name or member.email)
    return None, "The club's own account"


async def _audit(session, request, user, action: str, club_id: int, summary: str, **details) -> None:
    member = club_access.current_member(request)
    await audit.record(
        session, actor=user, action=action, entity_type="club", entity_id=club_id, summary=summary[:255],
        details={"via": "dashboard", "member_id": member.id if member else None, **details}, request=request,
    )


@router.get("", summary="Integrations: is API access on, and its limits")
async def overview(session: SessionDep, user: ClubAuth) -> dict:
    club = await _club(session, user)
    alloc = await allocation.of(session, club.id)
    return {
        "allocated": alloc.allocated,
        "source": alloc.source,
        "rate_tier": alloc.rate_tier,
        "rate_limit": alloc.rate_limit,
        "max_keys": alloc.max_keys,
        "live_keys": await keys.live_count(session, club.id),
        "scopes": catalogue.catalogue(catalogue.CLUB_SCOPES),
        "disclosure": DISCLOSURE,
        "docs_url": "https://myotherapp.in/developers",
        "api_url": "https://api.myotherapp.in/partner/v1",
    }


@router.get("/keys", summary="The club's API keys")
async def list_keys(session: SessionDep, user: ClubAuth, include_revoked: bool = Query(False)) -> dict:
    club = await _club(session, user)
    return {"keys": [keys.out(k) for k in await keys.club_keys(session, club.id, include_revoked=include_revoked)]}


@router.post("/keys", summary="Create an API key (its secret is shown once)", status_code=201)
@limiter.limit("30/hour", key_func=account_or_address)
async def create_key(request: Request, body: KeyCreate, session: SessionDep, user: ClubAuth) -> dict:
    club = await _club(session, user)
    member_id, by = _who(request, user)
    row, secret = await keys.create(
        session, club.id, name=body.name, livemode=body.mode == "live", scopes=body.scopes, expires_at=body.expires_at,
        ip_allowlist=body.ip_allowlist, attendees_ack=body.attendees_disclosure_ack, user_id=user.id, member_id=member_id, by_name=by,
    )
    await _audit(session, request, user, "partner.key.create", club.id, f"API key {row.name} created ({'live' if row.livemode else 'test'})",
                 key_id=str(row.id), scopes=row.scopes.split(), attendees_disclosure_ack=bool(row.attendees_ack_at))
    await session.commit()
    return keys.out(row, secret=secret)


@router.post("/keys/{key_id}/rotate", summary="A new secret for a key (shown once); the old one stops now")
async def rotate_key(request: Request, key_id: str, session: SessionDep, user: ClubAuth) -> dict:
    club = await _club(session, user)
    row = await keys.key_of(session, club.id, key_id)
    row, secret = await keys.rotate(session, row)
    await _audit(session, request, user, "partner.key.rotate", club.id, f"API key {row.name} rotated", key_id=str(row.id))
    await session.commit()
    return keys.out(row, secret=secret)


@router.delete("/keys/{key_id}", summary="Revoke an API key")
async def revoke_key(request: Request, key_id: str, session: SessionDep, user: ClubAuth) -> dict:
    club = await _club(session, user)
    row = await keys.key_of(session, club.id, key_id)
    await keys.revoke(session, row, by_user_id=user.id)
    await _audit(session, request, user, "partner.key.revoke", club.id, f"API key {row.name} revoked", key_id=str(row.id))
    await session.commit()
    return keys.out(row)


@router.get("/connected-apps", summary="Partner apps connected to the club")
async def connected_apps(session: SessionDep, user: ClubAuth) -> dict:
    club = await _club(session, user)
    return {"apps": [apps.connection_out(c, a) for c, a in await apps.club_connections(session, club.id)]}


@router.delete("/connected-apps/{connection_id}", summary="Disconnect a partner app (its tokens end now)")
async def disconnect_app(request: Request, connection_id: str, session: SessionDep, user: ClubAuth) -> dict:
    club = await _club(session, user)
    conn, app = await apps.disconnect(session, club.id, connection_id, by_user_id=user.id)
    await _audit(session, request, user, "partner.app.disconnected", club.id, f"{app.name if app else 'A partner app'} disconnected",
                 connection_id=str(conn.id), client_id=app.client_id if app else None)
    await session.commit()
    return {"disconnected": True, "id": str(conn.id)}


@router.get("/usage", summary="API requests a day, the last 30 days")
async def club_usage(session: SessionDep, user: ClubAuth, days: int = Query(30, ge=1, le=90)) -> dict:
    club = await _club(session, user)
    return {"days": await usage.by_day(session, club_id=club.id, days=days), "clients": await usage.by_client(session, club_id=club.id, days=days)}


# ─── The consent page ────────────────────────────────────────────────────


def _consent_refusal(e: oauth.AuthorizeError) -> CustomHTTPException:
    return CustomHTTPException(
        400, message=e.description, error_code="OAUTH_REQUEST_INVALID",
        errors={"error": e.error, "redirect_to": e.location()},
    )


async def _checked(session, params: dict, club) -> tuple:
    try:
        app, req = await oauth.check_authorization_request(session, params)
    except oauth.AuthorizeError as e:
        raise _consent_refusal(e)
    alloc = await allocation.of(session, club.id)
    if not alloc.allocated:
        raise CustomHTTPException(
            403, message="API access isn't turned on for this club, so no app can connect yet. Ask the MyOtherApp team.",
            error_code="NOT_ALLOCATED",
            errors={"redirect_to": oauth.with_params(req.redirect_uri, {"error": "access_denied", "error_description": "API access is not turned on for this club.", "state": req.state})},
        )
    return app, req


@router.get("/connect", summary="The consent page: what the partner app asks for")
async def consent_request(
    request: Request, session: SessionDep, user: ClubAuth,
    client_id: str = Query(..., max_length=64), redirect_uri: Optional[str] = Query(None, max_length=2000),
    response_type: str = Query("code", max_length=20), scope: Optional[str] = Query(None, max_length=1000),
    state: Optional[str] = Query(None, max_length=1000), code_challenge: str = Query("", max_length=128),
    code_challenge_method: Optional[str] = Query(None, max_length=10), mode: str = Query("live", max_length=10),
) -> dict:
    club = await _club(session, user)
    params = dict(request.query_params)
    app, req = await _checked(session, params, club)
    existing = await apps.live_connection(session, app.id, club.id, req.livemode)
    return {
        "app": {"name": app.name, "client_id": app.client_id, "description": app.description, "website": app.website, "logo_url": app.logo_url},
        "club": {"id": club.id, "name": club.name},
        "mode": "live" if req.livemode else "test",
        "scopes": catalogue.catalogue(req.scopes),
        "redirect_target": oauth.redirect_target(req.redirect_uri),
        "disclosure": DISCLOSURE.format(name=app.name) if "attendees:read" in req.scopes else None,
        "already_connected": existing is not None,
    }


@router.post("/connect", summary="Allow or deny the partner app: where to send the person back")
@limiter.limit("30/minute", key_func=account_or_address)
async def consent_decision(request: Request, body: Consent, session: SessionDep, user: ClubAuth) -> dict:
    club = await _club(session, user)
    params = body.model_dump(exclude={"decision", "scopes", "attendees_disclosure_ack"}, exclude_none=True)
    app, req = await _checked(session, params, club)
    if body.decision == "deny":
        return {"redirect_to": oauth.with_params(req.redirect_uri, {"error": "access_denied", "error_description": "The club declined.", "state": req.state})}
    chosen = [s for s in req.scopes if s in set(body.scopes)]
    if not chosen:
        raise CustomHTTPException(422, message="Tick at least one thing to allow, or deny.", error_code="VALIDATION", errors={"scopes": "At least one."})
    if "attendees:read" in chosen and not body.attendees_disclosure_ack:
        raise CustomHTTPException(
            422, message="To share attendees' details, confirm that your registration form tells people their details are shared.",
            error_code="DISCLOSURE_REQUIRED", errors={"attendees_disclosure_ack": DISCLOSURE.format(name=app.name)},
        )
    member_id, by = _who(request, user)
    code = await oauth.issue_code(
        session, app, req, club_id=club.id, user_id=user.id, member_id=member_id, by_name=by, scopes=chosen,
        attendees_ack=body.attendees_disclosure_ack,
    )
    await _audit(session, request, user, "partner.app.consented", club.id, f"{app.name} allowed ({'live' if req.livemode else 'test'})",
                 client_id=app.client_id, scopes=chosen, attendees_disclosure_ack=body.attendees_disclosure_ack)
    await session.commit()
    return {"redirect_to": oauth.with_params(req.redirect_uri, {"code": code, "state": req.state})}
