"""OAuth for partner apps, at the API's root (404 while PARTNER_API_ENABLED is off):

- GET  /partner/oauth/authorize   checks the app and its redirect, then sends the
                                  person on to the club dashboard's consent page
- POST /partner/oauth/token       authorization_code, refresh_token, client_credentials
- POST /partner/oauth/revoke      RFC 7009

Refusals are in OAuth's own JSON shape ({error, error_description}), never
the API's: these endpoints are what OAuth libraries call.
"""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.api.partner import oauth
from app.config import settings
from app.core.rate_limit import limiter
from app.db.core import SessionDep

router = APIRouter(prefix="/partner/oauth", tags=["partner-oauth"])
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _json(body: dict, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={**_NO_STORE, **(headers or {})})


def _enabled() -> bool:
    return bool(settings.PARTNER_API_ENABLED)


def _oauth_error(e: oauth.OAuthError) -> JSONResponse:
    headers = {"WWW-Authenticate": 'Basic realm="partner-oauth"'} if e.status == 401 else None
    return _json(e.body(), e.status, headers)


async def _discard(session) -> None:
    if session.new or session.dirty or session.deleted:
        await session.rollback()


async def _form(request: Request) -> dict:
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


@router.get("/authorize", include_in_schema=False)
async def authorize(request: Request, session: SessionDep):
    if not _enabled():
        return _json({"detail": "Not Found"}, 404)
    try:
        await oauth.check_authorization_request(session, request.query_params)
    except oauth.AuthorizeError as e:
        if e.location():
            return RedirectResponse(e.location(), status_code=303)
        return _json({"error": e.error, "error_description": e.description}, 400)
    # The person signs in to the club dashboard, picks scopes and allows it there.
    return RedirectResponse(f"{oauth.consent_url()}?{urlencode(list(request.query_params.multi_items()))}", status_code=303)


@router.post("/token", include_in_schema=False)
@limiter.limit("60/minute")
async def token(request: Request, session: SessionDep):
    if not _enabled():
        return _json({"detail": "Not Found"}, 404)
    form = await _form(request)
    try:
        app = await oauth.authenticate_client(session, form, request.headers.get("authorization"))
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            out = await oauth.exchange_code(session, app, form)
        elif grant_type == "refresh_token":
            out = await oauth.refresh(session, app, form)
        elif grant_type == "client_credentials":
            out = await oauth.client_credentials(session, app, form)
        else:
            raise oauth.OAuthError("unsupported_grant_type", "grant_type is authorization_code, refresh_token or client_credentials.")
    except oauth.OAuthError as e:
        await _discard(session)
        return _oauth_error(e)
    return _json(out)


@router.post("/revoke", include_in_schema=False)
@limiter.limit("60/minute")
async def revoke(request: Request, session: SessionDep):
    if not _enabled():
        return _json({"detail": "Not Found"}, 404)
    form = await _form(request)
    try:
        app = await oauth.authenticate_client(session, form, request.headers.get("authorization"))
        await oauth.revoke_token(session, app, form.get("token"))
    except oauth.OAuthError as e:
        await _discard(session)
        return _oauth_error(e)
    return _json({})
