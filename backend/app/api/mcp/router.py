"""The OAuth side of the MCP endpoint, at the API's root, and the club
dashboard's side under /api/v1/clubs.

At the root (all 404 while APP_MCP_ENABLED is off):

- GET  /.well-known/oauth-protected-resource[/mcp]   RFC 9728
- GET  /.well-known/oauth-authorization-server        RFC 8414
- POST /oauth/register                                RFC 7591
- GET  /oauth/authorize                               the sign-in page
- POST /oauth/authorize                               sign in, pick a club, two-step, consent
- POST /oauth/token                                   authorization_code, refresh_token
- POST /oauth/revoke                                  RFC 7009

The person's steps are one form each, posting back here with what the last
step proved carried in a signed, short-lived ticket. The ticket is tied to
this browser by a cookie made when the sign-in page was shown, so a ticket
lifted from one browser is no use in another.

Under /api/v1/clubs, with the dashboard's own sign-in (docs/MCP.md has the contract):

- GET    /clubs/connected-apps
- DELETE /clubs/connected-apps/{id}
- GET    /clubs/pending-actions/{token}
- POST   /clubs/pending-actions/{token}/confirm
- POST   /clubs/pending-actions/{token}/reject
"""
import hmac
import json
import logging
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.api.audit import service as audit
from app.api.club_access import policy
from app.api.club_access import service as club_access
from app.api.club_access.models import ClubMembers
from app.api.clubs.models import Clubs
from app.api.mcp import oauth, pages
from app.api.mcp.access import client_label
from app.api.subscriptions import gates
from app.api.users.models import Users
from app.config import settings
from app.core.auth.authentication import authenticate_club_account, get_user
from app.core.auth.dependencies import ClubAuth
from app.core.auth.payloads import sign_payload, verify_payload
from app.core.rate_limit import account_or_address, limiter
from app.db.core import SessionDep

logger = logging.getLogger(__name__)

public_router = APIRouter(tags=["mcp-oauth"])

#: The cookie tying a connection's pages to one browser.
COOKIE = "moa_mcp_connect"
#: How long the sign-in page, and each step after it, may sit open.
REQUEST_SECONDS = 30 * 60
TICKET_SECONDS = 10 * 60
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def require_enabled() -> None:
    """The kill switch: with MCP off, none of this exists."""
    if not settings.MCP_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")


def _json(body: dict, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={**_NO_STORE, **(headers or {})})


async def _discard(session) -> None:
    """Whatever a refused request had begun to write, dropped (what it meant to keep, it committed)."""
    if session.new or session.dirty or session.deleted:
        await session.rollback()


def _oauth_error(e: oauth.OAuthError) -> JSONResponse:
    headers = {"WWW-Authenticate": 'Basic realm="oauth"'} if e.status == 401 else None
    return _json(e.body(), e.status, headers)


# ─── Discovery ───────────────────────────────────────────────────────────


@public_router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@public_router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
async def protected_resource_metadata():
    require_enabled()
    return _json(oauth.protected_resource_metadata(), headers={"Cache-Control": "public, max-age=300"})


@public_router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
async def authorization_server_metadata():
    require_enabled()
    return _json(oauth.authorization_server_metadata(), headers={"Cache-Control": "public, max-age=300"})


# ─── Registration, tokens, revocation ────────────────────────────────────


@public_router.post("/oauth/register", include_in_schema=False)
# Per caller address: an app registers once per install, not per request.
@limiter.limit("20/hour")
async def register(request: Request, session: SessionDep):
    require_enabled()
    try:
        body = json.loads(await request.body() or b"null")
    except ValueError:
        return _oauth_error(oauth.OAuthError("invalid_client_metadata", "Send the client's metadata as JSON."))
    try:
        out = await oauth.register_client(session, body)
    except oauth.OAuthError as e:
        await _discard(session)
        return _oauth_error(e)
    await session.commit()
    return _json(out, 201)


async def _form(request: Request) -> dict:
    """A form's fields, one value each (the last); never an uploaded file."""
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


@public_router.post("/oauth/token", include_in_schema=False)
@limiter.limit("60/minute")
async def token(request: Request, session: SessionDep):
    require_enabled()
    form = await _form(request)
    try:
        client = await oauth.authenticate_client(session, form, request.headers.get("authorization"))
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            out = await oauth.exchange_code(session, client, form)
        elif grant_type == "refresh_token":
            out = await oauth.refresh(session, client, form)
        else:
            raise oauth.OAuthError("unsupported_grant_type", "grant_type must be authorization_code or refresh_token.")
    except oauth.OAuthError as e:
        await _discard(session)
        return _oauth_error(e)
    return _json(out)


@public_router.post("/oauth/revoke", include_in_schema=False)
@limiter.limit("60/minute")
async def revoke(request: Request, session: SessionDep):
    require_enabled()
    form = await _form(request)
    try:
        client = await oauth.authenticate_client(session, form, request.headers.get("authorization"))
        await oauth.revoke_token(session, client, form.get("token"))
    except oauth.OAuthError as e:
        await _discard(session)
        return _oauth_error(e)
    return _json({})


# ─── The person's steps ──────────────────────────────────────────────────


def _nonce_hash(nonce: str | None) -> str:
    return oauth.digest(nonce or "")[:32]


def _seal(kind: str, data: dict, nonce: str, ttl: int) -> str:
    return sign_payload({"k": kind, "n": _nonce_hash(nonce), **data}, ttl)


def _open(token: str | None, kind: str, request: Request) -> dict | None:
    """A ticket from this browser, of this kind, still in date; None otherwise."""
    nonce = request.cookies.get(COOKIE)
    payload = verify_payload(token or "") if token else None
    if not payload or payload.get("k") != kind or not nonce:
        return None
    if not hmac.compare_digest(str(payload.get("n", "")), _nonce_hash(nonce)):
        return None
    return payload


def _expired_page():
    return pages.message(
        "This page has expired",
        "Go back to the app you were connecting and start again. (If it keeps happening, let this site keep cookies.)",
    )


def _request_of(payload: dict) -> oauth.AuthRequest:
    return oauth.AuthRequest(**payload["r"])


def _app_bits(client, req: oauth.AuthRequest) -> dict:
    return dict(
        app=client_label(client),
        target=oauth.redirect_target(req.redirect_uri),
        native=urlscheme(req.redirect_uri) != "https",
    )


def urlscheme(uri: str) -> str:
    return uri.split(":", 1)[0].lower()


@public_router.get("/oauth/authorize", include_in_schema=False)
async def authorize_page(request: Request, session: SessionDep):
    require_enabled()
    try:
        client, req = await oauth.check_authorization_request(session, request.query_params)
    except oauth.AuthorizeError as e:
        await session.commit()  # a client metadata document read on the way is kept
        if e.location():
            return RedirectResponse(e.location(), status_code=303)
        return pages.message("This app can't connect", e.description)
    await session.commit()
    nonce = request.cookies.get(COOKIE) or secrets.token_urlsafe(24)
    sealed = _seal("signin", {"r": req.payload()}, nonce, REQUEST_SECONDS)
    response = pages.signin(request=sealed, **_app_bits(client, req))
    response.set_cookie(
        COOKIE, nonce, max_age=REQUEST_SECONDS, httponly=True, secure=request.url.scheme == "https", samesite="lax", path="/oauth",
    )
    return response


async def _candidates(session, email: str, password: str) -> list[dict]:
    """Each club dashboard this email and password open: a team member's
    (their own password) and a club's own account. Usually one."""
    found: list[dict] = []
    member = await club_access.authenticate(session, email, password)
    if member is not None:
        club = await session.get(Clubs, member.club_id)
        owner = await get_user(session, club.user_id) if club is not None and not club.is_deleted and club.user_id else None
        if owner is not None and getattr(owner, "is_active", True) is not False and club.is_verified:
            found.append({"club": club.id, "user": owner.id, "member": member.id, "totp": False})
    user = await authenticate_club_account(session, email, password)
    if user is not None and user.club is not None and user.club.is_verified and getattr(user, "is_active", True) is not False:
        found.append({"club": user.club.id, "user": user.id, "member": None, "totp": bool(getattr(user, "totp_enabled", False))})
    return found


async def _choice_label(session, choice: dict) -> tuple[str, str]:
    club = await session.get(Clubs, choice["club"])
    if choice["member"]:
        member = await session.get(ClubMembers, choice["member"])
        return club.name, f"As {club_access.role_label(member.role)} ({member.email})"
    return club.name, "As the club's own account"


async def _signed_in(session, request: Request, choice: dict, client, req: oauth.AuthRequest, error: str | None = None):
    """After the password (and the code, when the account has two-step): the
    consent page, or why there will not be one."""
    club = await session.get(Clubs, choice["club"])
    member = await session.get(ClubMembers, choice["member"]) if choice["member"] else None
    role = member.role if member is not None else "owner"
    if not await gates.allows(session, club.id, "mcp"):
        locked = await gates.feature_locked(session, "mcp")
        return pages.message(
            "Your plan doesn't include this", locked.message, status=403,
            link=("See plans", locked.errors.get("upgrade_url") or gates.upgrade_url()),
        )
    offered = [s for s in req.scopes if policy.allows_scope(role, s)]
    unavailable = [s for s in req.scopes if s not in offered]
    if not offered:
        return pages.message(
            "Nothing to allow",
            f"Your role ({club_access.role_label(role)}) can't use anything {client_label(client)} asked for. Ask the club's owner.",
            status=403,
        )
    ticket = _seal("consent", {"r": req.payload(), "c": choice}, request.cookies.get(COOKIE), TICKET_SECONDS)
    who = (member.full_name or member.email) if member is not None else "the club's own account"
    return pages.consent(
        app_uri=client.client_uri, redirect_uri=req.redirect_uri, ticket=ticket, club=club.name, who=who,
        role=club_access.role_label(role), offered=offered, unavailable=unavailable, error=error, **_app_bits(client, req),
    )


async def _after_password(session, request: Request, choice: dict, client, req: oauth.AuthRequest):
    if choice["totp"]:
        ticket = _seal("totp", {"r": req.payload(), "c": choice}, request.cookies.get(COOKIE), TICKET_SECONDS)
        return pages.second_factor(app=client_label(client), ticket=ticket)
    return await _signed_in(session, request, choice, client, req)


@public_router.post("/oauth/authorize", include_in_schema=False)
# Per caller address, like /auth/token; one account's password is guarded by its own lockout.
@limiter.limit("20/minute")
async def authorize_step(request: Request, session: SessionDep):
    require_enabled()
    raw = await request.form()
    form = {k: v for k, v in raw.items() if isinstance(v, str)}
    ticked = {v for v in raw.getlist("scope") if isinstance(v, str)}
    step = form.get("step")
    if step == "signin":
        return await _step_signin(session, request, form)
    kinds = {"pick": "pick", "totp": "totp", "consent": "consent"}
    if step not in kinds:
        return pages.message("Something went wrong", "That step is not one this page knows.")
    payload = _open(form.get("ticket"), kinds[step], request)
    if payload is None:
        return _expired_page()
    req = _request_of(payload)
    client = await oauth.client_for(session, req.client_id)
    if client is None or not oauth.redirect_allowed(client.redirect_uris, req.redirect_uri):
        return pages.message("This app can't connect", "The app is no longer registered here. Start again from the app.")
    if step == "pick":
        choices = payload.get("choices") or []
        try:
            choice = choices[int(form.get("choice", ""))]
        except (ValueError, IndexError):
            return _expired_page()
        return await _after_password(session, request, choice, client, req)
    if step == "totp":
        return await _step_totp(session, request, form, payload["c"], client, req)
    return await _step_consent(session, request, form, ticked, payload["c"], client, req)


async def _step_signin(session, request: Request, form: dict):
    from app.api.security import service as security

    payload = _open(form.get("request"), "signin", request)
    if payload is None:
        return _expired_page()
    req = _request_of(payload)
    client = await oauth.client_for(session, req.client_id)
    if client is None or not oauth.redirect_allowed(client.redirect_uris, req.redirect_uri):
        return pages.message("This app can't connect", "The app is no longer registered here. Start again from the app.")
    email = (form.get("email") or "").strip()
    password = form.get("password") or ""
    ip = security.client_ip(request)

    def again(error: str, status: int):
        return pages.signin(request=form.get("request"), email=email, error=error, status=status, **_app_bits(client, req))

    wait = await security.lockout_remaining(session, email, ip)
    if wait:
        await security.record(session, request=request, identifier=email, user=None, method="mcp", result="blocked")
        await session.commit()
        return again(f"Too many wrong passwords. Try again in {max(1, wait // 60)} min.", 429)
    choices = await _candidates(session, email, password) if email and password else []
    if not choices:
        await security.record(session, request=request, identifier=email, user=None, method="mcp", result="wrong_password")
        await session.commit()
        return again("That email and password don't open a club dashboard.", 401)
    owner = await session.get(Users, choices[0]["user"])
    await security.record(session, request=request, identifier=email, user=owner, method="mcp", result="signed_in")
    await session.commit()
    if len(choices) > 1:
        ticket = _seal("pick", {"r": req.payload(), "choices": choices}, request.cookies.get(COOKIE), TICKET_SECONDS)
        labels = [(i, *await _choice_label(session, c)) for i, c in enumerate(choices)]
        return pages.pick_club(app=client_label(client), ticket=ticket, choices=labels)
    return await _after_password(session, request, choices[0], client, req)


async def _step_totp(session, request: Request, form: dict, choice: dict, client, req: oauth.AuthRequest):
    from app.api.auth import totp as totp_service
    from app.api.security import service as security

    user = await session.get(Users, choice["user"])
    ip = security.client_ip(request)
    ticket = form.get("ticket")
    if await security.lockout_remaining(session, user.email, ip):
        return pages.second_factor(app=client_label(client), ticket=ticket, error="Too many wrong codes. Try again later.", status=429)
    if not await totp_service.check_second_factor(session, user, (form.get("code") or "").strip()):
        await security.record(session, request=request, identifier=user.email, user=user, method="mcp", result="totp_invalid")
        await session.commit()
        return pages.second_factor(app=client_label(client), ticket=ticket, error="That code is not valid.", status=401)
    await session.commit()
    return await _signed_in(session, request, choice, client, req)


async def _step_consent(session, request: Request, form: dict, ticked: set, choice: dict, client, req: oauth.AuthRequest):
    if form.get("decision") != "allow":
        return RedirectResponse(
            oauth.with_params(req.redirect_uri, {"error": "access_denied", "error_description": "The person declined.", "state": req.state}),
            status_code=303,
        )
    club = await session.get(Clubs, choice["club"])
    user = await session.get(Users, choice["user"])
    member = await session.get(ClubMembers, choice["member"]) if choice["member"] else None
    # Everything again, as it is now: the person may have lost access, or
    # the club its plan, since the page was shown.
    if club is None or club.is_deleted or not club.is_verified or user is None or (member is not None and member.status != "active"):
        return pages.message("You can't connect this club", "Your access to this club's dashboard has changed. Start again from the app.", status=403)
    if not await gates.allows(session, club.id, "mcp"):
        locked = await gates.feature_locked(session, "mcp")
        return pages.message("Your plan doesn't include this", locked.message, status=403, link=("See plans", gates.upgrade_url()))
    role = member.role if member is not None else "owner"
    offered = [s for s in req.scopes if policy.allows_scope(role, s)]
    chosen = [s for s in offered if s in ticked]
    if not chosen:
        return await _signed_in(session, request, choice, client, req, error="Tick at least one thing to allow, or cancel.")
    code = await oauth.issue_code(
        session, client, req, club_id=club.id, user_id=user.id, member_id=member.id if member else None,
        token_version=None if member is not None else user.token_version, scopes=chosen,
    )
    await audit.record(
        session, actor=user, action="mcp.app.connected", entity_type="club", entity_id=club.id,
        summary=f"{client_label(client)} connected to {club.name}"[:255],
        details={"via": "mcp", "client": client_label(client), "client_id": client.client_id, "scopes": chosen,
                 "member_id": member.id if member else None, "member_email": member.email if member else None},
        request=request,
    )
    await session.commit()
    return RedirectResponse(oauth.with_params(req.redirect_uri, {"code": code, "state": req.state}), status_code=303)



# ─── The club dashboard's side ───────────────────────────────────────────

club_router = APIRouter(prefix="/clubs", tags=["mcp"])


@club_router.get("/connected-apps", summary="AI assistants connected to the club")
async def connected_apps(request: Request, session: SessionDep, user: ClubAuth) -> dict:
    """Every app connected to the club (owners and admins), or the ones the
    signed-in member connected. docs/MCP.md has the shape."""
    from app.api.mcp import connected

    club = await club_access.club_of(session, user)
    return await connected.apps(session, club.id, role=club_access.role_of(request), member=club_access.current_member(request))


@club_router.delete("/connected-apps/{grant_id}", summary="Disconnect an AI assistant")
async def disconnect_app(request: Request, grant_id: str, session: SessionDep, user: ClubAuth) -> dict:
    """Ends the connection and every token it holds, now; its waiting requests are rejected."""
    from app.api.mcp import connected

    club = await club_access.club_of(session, user)
    await connected.disconnect(
        session, club.id, grant_id, user=user, role=club_access.role_of(request), member=club_access.current_member(request), request=request,
    )
    return {"disconnected": True, "id": grant_id}


@club_router.get("/pending-actions/{token}", summary="What an assistant asked for, before approving it")
@limiter.limit("60/minute", key_func=account_or_address)
async def pending_action(request: Request, token: str, session: SessionDep, user: ClubAuth) -> dict:
    from app.api.mcp import actions

    club = await club_access.club_of(session, user)
    row = await actions.by_token(session, club.id, token)
    return await actions.describe(session, row, role=club_access.role_of(request))


@club_router.post("/pending-actions/{token}/confirm", summary="Approve what an assistant asked for")
@limiter.limit("30/minute", key_func=account_or_address)
async def confirm_pending_action(request: Request, token: str, session: SessionDep, user: ClubAuth) -> dict:
    from app.api.mcp import actions

    club = await club_access.club_of(session, user)
    row = await actions.by_token(session, club.id, token)
    return await actions.confirm(session, row, user=user, member=club_access.current_member(request), request=request)


@club_router.post("/pending-actions/{token}/reject", summary="Turn down what an assistant asked for")
@limiter.limit("30/minute", key_func=account_or_address)
async def reject_pending_action(request: Request, token: str, session: SessionDep, user: ClubAuth) -> dict:
    from app.api.mcp import actions

    club = await club_access.club_of(session, user)
    row = await actions.by_token(session, club.id, token)
    return await actions.reject(session, row, user=user, member=club_access.current_member(request), request=request)
