"""OAuth 2.0 for partner apps: the authorization code grant with PKCE (S256
only) and refresh tokens, plus client credentials for an app's own token.

The same pieces as the MCP server's (app/api/mcp/oauth.py: PKCE, digests,
redirect checks, refresh rotation with reuse ending the grant), with tables,
scopes and consent of its own:

- authorize: https://api.myotherapp.in/partner/oauth/authorize sends the
  person on to the club dashboard's consent page; that page reads and answers
  through /api/v1/clubs/integrations/connect (club_router.py), which signs the
  person in as the club, has them pick scopes and tick the DPDP line, and
  hands back the redirect with the code;
- token: /partner/oauth/token. Partner apps are confidential clients, so the
  client secret is always asked for (HTTP Basic or the form);
- revoke: /partner/oauth/revoke (RFC 7009). A refresh token ends its whole
  connection (the club is disconnected); an access token ends itself.

Tokens are opaque, stored as sha256 digests and bound to the connection
(app, club, scopes, live or test). `mode=test` on the authorize request
asks for a test connection, which works on the sandbox only.
"""
from __future__ import annotations

import base64
import hmac
import logging
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import unquote

from sqlalchemy import select

from app.api.mcp.oauth import AuthorizeError, OAuthError, pkce_matches, redirect_allowed, redirect_target, with_params  # noqa: F401
from app.api.mcp.oauth import _PKCE as PKCE_SHAPE
from app.api.partner import apps as apps_mod
from app.api.partner import scopes as catalogue
from app.api.partner.keys import digest
from app.api.partner.models import PartnerApps, PartnerAuthCodes, PartnerConnections, PartnerTokens
from app.config import settings

logger = logging.getLogger(__name__)

ACCESS = "moa_pat_"
REFRESH = "moa_prt_"
CODE = "moa_pac_"
CODE_TTL = timedelta(minutes=5)
#: What an app's own token (client credentials) may be for.
APP_TOKEN_SCOPES = frozenset({"clubs:create"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def consent_url() -> str:
    return (settings.PARTNER_CONSENT_URL or "").strip() or settings.MCP_DASHBOARD_URL.rstrip("/") + "/integrations/connect"


def endpoints() -> dict:
    base = settings.PUBLIC_API_URL.rstrip("/")
    return {
        "authorization_endpoint": f"{base}/partner/oauth/authorize",
        "token_endpoint": f"{base}/partner/oauth/token",
        "revocation_endpoint": f"{base}/partner/oauth/revoke",
    }


# ─── The authorization request ───────────────────────────────────────────


@dataclass
class AuthRequest:
    client_id: str
    redirect_uri: str
    code_challenge: str
    state: Optional[str]
    scopes: list
    livemode: bool

    def payload(self) -> dict:
        return asdict(self)


async def check_authorization_request(session, params) -> tuple[PartnerApps, AuthRequest]:
    """The app, and the request as checked. AuthorizeError otherwise: with a
    redirect_uri when the app and its address can be trusted (the app is
    told), without one when they cannot (the person is shown it)."""
    app = await apps_mod.by_client_id(session, params.get("client_id"))
    if app is None:
        raise AuthorizeError("invalid_client", "This app is not registered with MyOtherApp.")
    redirect_uri = params.get("redirect_uri")
    if not redirect_uri and len(app.redirect_uris or []) == 1:
        redirect_uri = app.redirect_uris[0]
    if not redirect_allowed(app.redirect_uris, redirect_uri):
        raise AuthorizeError("invalid_request", "The app asked to be sent back to an address it did not register.")
    state = params.get("state")

    def refuse(error: str, description: str):
        return AuthorizeError(error, description, redirect_uri=redirect_uri, state=state)

    if app.status != "active":
        raise refuse("unauthorized_client", "This app is suspended by MyOtherApp.")
    if params.get("response_type") != "code":
        raise refuse("unsupported_response_type", "Only response_type=code is supported.")
    challenge = params.get("code_challenge") or ""
    if not challenge:
        raise refuse("invalid_request", "PKCE is required: send a code_challenge.")
    if params.get("code_challenge_method") != "S256":
        raise refuse("invalid_request", "Only the S256 code_challenge_method is supported.")
    if not PKCE_SHAPE.match(challenge):
        raise refuse("invalid_request", "The code_challenge is not a valid S256 challenge.")
    allowed = set(catalogue.parse(app.allowed_scopes)) - APP_TOKEN_SCOPES
    asked = params.get("scope")
    wanted = catalogue.parse(asked) if asked else sorted(allowed, key=catalogue.ALL.index)
    wanted = [s for s in wanted if s in allowed]
    if not wanted:
        raise refuse("invalid_scope", "None of the scopes asked for are ones this app may use.")
    mode = (params.get("mode") or "live").lower()
    if mode not in ("live", "test"):
        raise refuse("invalid_request", "mode is live or test.")
    return app, AuthRequest(
        client_id=app.client_id, redirect_uri=redirect_uri, code_challenge=challenge, state=state, scopes=wanted,
        livemode=mode == "live",
    )


async def issue_code(
    session, app: PartnerApps, req: AuthRequest, *, club_id: int, user_id: int, member_id: Optional[int],
    by_name: Optional[str], scopes: list, attendees_ack: bool,
) -> str:
    code = new_token(CODE)
    session.add(PartnerAuthCodes(
        code_hash=digest(code), app_id=app.id, club_id=club_id, user_id=user_id, member_id=member_id,
        connected_by_name=(by_name or "")[:120] or None, redirect_uri=req.redirect_uri, code_challenge=req.code_challenge,
        scopes=catalogue.join(scopes), livemode=req.livemode, attendees_ack=bool(attendees_ack), expires_at=_now() + CODE_TTL,
    ))
    await session.flush()
    return code


# ─── Clients ─────────────────────────────────────────────────────────────


async def authenticate_client(session, form: dict, authorization: Optional[str]) -> PartnerApps:
    """The app calling the token or revocation endpoint, by its secret (HTTP
    Basic or the form). A suspended app is refused here too."""
    client_id, secret = form.get("client_id"), form.get("client_secret")
    if authorization and authorization[:6].lower() == "basic ":
        try:
            pair = base64.b64decode(authorization[6:].strip()).decode("utf-8")
            basic_id, basic_secret = (unquote(p) for p in pair.split(":", 1))
        except (ValueError, UnicodeDecodeError):
            raise OAuthError("invalid_client", "The Authorization header is not valid Basic credentials.", 401)
        if client_id and client_id != basic_id:
            raise OAuthError("invalid_client", "Two different client_ids were sent.", 401)
        client_id, secret = basic_id, basic_secret
    app = await apps_mod.by_client_id(session, client_id)
    if app is None or not secret or not hmac.compare_digest(digest(secret), app.client_secret_hash or ""):
        raise OAuthError("invalid_client", "Unknown client, or the client secret is not right.", 401)
    if app.status != "active":
        raise OAuthError("invalid_client", "This app is suspended by MyOtherApp.", 401)
    return app


# ─── Tokens ──────────────────────────────────────────────────────────────


def _issue(session, app: PartnerApps, conn: Optional[PartnerConnections], scopes: str, livemode: bool, *, parent_id=None, refresh: bool = True) -> dict:
    now = _now()
    minutes = int(settings.PARTNER_ACCESS_TOKEN_MINUTES)
    access = new_token(ACCESS)
    session.add(PartnerTokens(
        app_id=app.id, connection_id=conn.id if conn else None, kind="access", token_hash=digest(access), scopes=scopes,
        livemode=livemode, expires_at=now + timedelta(minutes=minutes),
    ))
    out = {"access_token": access, "token_type": "Bearer", "expires_in": minutes * 60, "scope": scopes, "livemode": livemode}
    if conn is not None:
        out["club_id"] = str(conn.club_id)
    if refresh:
        raw = new_token(REFRESH)
        session.add(PartnerTokens(
            app_id=app.id, connection_id=conn.id if conn else None, kind="refresh", token_hash=digest(raw), scopes=scopes,
            livemode=livemode, expires_at=now + timedelta(days=int(settings.PARTNER_REFRESH_TOKEN_DAYS)), parent_id=parent_id,
        ))
        out["refresh_token"] = raw
    return out


async def _club_still_reachable(session, club_id: int) -> Optional[str]:
    """Why the club cannot be acted for now, or None."""
    from app.api.clubs.models import Clubs
    from app.api.partner import allocation

    club = await session.get(Clubs, club_id)
    if club is None or club.is_deleted:
        return "This club no longer exists."
    if not (await allocation.of(session, club_id)).allocated:
        return "API access isn't turned on for this club."
    return None


async def exchange_code(session, app: PartnerApps, form: dict) -> dict:
    """grant_type=authorization_code: once, within minutes, by the app it was
    issued to, with the verifier. The connection is made here (a club's earlier
    connection to the same app, in the same mode, ends). A code used twice
    ends the connection its first use made."""
    code = form.get("code")
    if not code:
        raise OAuthError("invalid_request", "Send the authorization code.")
    row = await session.scalar(select(PartnerAuthCodes).where(PartnerAuthCodes.code_hash == digest(code)).with_for_update())
    if row is None or row.app_id != app.id:
        raise OAuthError("invalid_grant", "The authorization code is not valid.")
    if row.used_at is not None:
        if row.connection_id is not None:
            conn = await session.get(PartnerConnections, row.connection_id)
            if conn is not None:
                await apps_mod.end_connection(session, conn, reason="code_reused")
        await session.commit()
        logger.warning("partner authorization code for %s presented a second time: its connection was ended", app.client_id)
        raise OAuthError("invalid_grant", "This authorization code was already used.")
    row.used_at = _now()
    if _aware(row.expires_at) <= _now():
        await session.commit()
        raise OAuthError("invalid_grant", "The authorization code has expired.")
    if form.get("redirect_uri") and form.get("redirect_uri") != row.redirect_uri:
        await session.commit()
        raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request.")
    if not pkce_matches(form.get("code_verifier"), row.code_challenge):
        await session.commit()
        raise OAuthError("invalid_grant", "The code_verifier does not match the code_challenge.")
    why = await _club_still_reachable(session, row.club_id)
    if why:
        await session.commit()
        raise OAuthError("invalid_grant", why)
    # Only what the app may still ask for: the admin may have narrowed it since.
    allowed = set(catalogue.parse(app.allowed_scopes))
    scopes = catalogue.join(s for s in catalogue.parse(row.scopes) if s in allowed)
    if not scopes:
        await session.commit()
        raise OAuthError("invalid_scope", "None of the scopes granted are ones this app may still use.")
    earlier = await apps_mod.live_connection(session, app.id, row.club_id, row.livemode)
    if earlier is not None:
        await apps_mod.end_connection(session, earlier, reason="replaced")
    conn = PartnerConnections(
        app_id=app.id, club_id=row.club_id, scopes=scopes, livemode=row.livemode, via="consent",
        connected_by_user_id=row.user_id, connected_by_member_id=row.member_id, connected_by_name=row.connected_by_name,
        attendees_ack_at=_now() if row.attendees_ack and "attendees:read" in scopes.split() else None, last_used_at=_now(),
    )
    session.add(conn)
    await session.flush()
    row.connection_id = conn.id
    tokens = _issue(session, app, conn, scopes, row.livemode)
    await _audit_connection(session, conn, app, row.user_id)
    await session.commit()
    return tokens


async def _audit_connection(session, conn: PartnerConnections, app: PartnerApps, user_id: Optional[int]) -> None:
    from app.api.audit import service as audit
    from app.api.users.models import Users

    actor = await session.get(Users, user_id) if user_id else None
    await audit.record(
        session, actor=actor, action="partner.app.connected", entity_type="club", entity_id=conn.club_id,
        summary=f"{app.name} connected ({'live' if conn.livemode else 'test'})"[:255],
        details={"via": "partner_api", "client_id": app.client_id, "app": app.name, "scopes": conn.scopes.split(),
                 "connection_id": str(conn.id), "connected_by": conn.connected_by_name, "livemode": conn.livemode},
    )


async def refresh(session, app: PartnerApps, form: dict) -> dict:
    """grant_type=refresh_token: a new pair, the old refresh token spent.
    Presented again after that, the connection ends for both holders."""
    raw = form.get("refresh_token")
    if not raw:
        raise OAuthError("invalid_request", "Send the refresh_token.")
    row = await session.scalar(
        select(PartnerTokens).where(PartnerTokens.token_hash == digest(raw), PartnerTokens.kind == "refresh").with_for_update()
    )
    conn = await session.get(PartnerConnections, row.connection_id) if row is not None and row.connection_id else None
    if row is None or conn is None or row.app_id != app.id:
        raise OAuthError("invalid_grant", "The refresh token is not valid.")
    if row.rotated_at is not None:
        await apps_mod.end_connection(session, conn, reason="refresh_reused")
        await session.commit()
        logger.warning("spent partner refresh token presented again for connection %s (%s): ended", conn.id, app.client_id)
        raise OAuthError("invalid_grant", "This refresh token was already used, so the connection was ended. Connect again.")
    if row.revoked_at is not None or conn.revoked_at is not None:
        raise OAuthError("invalid_grant", "This connection was ended. Connect again.")
    if _aware(row.expires_at) <= _now():
        raise OAuthError("invalid_grant", "The refresh token has expired. Connect again.")
    why = await _club_still_reachable(session, conn.club_id)
    if why:
        raise OAuthError("invalid_grant", why)
    granted = row.scopes.split()
    allowed = set(catalogue.parse(app.allowed_scopes))
    scopes = [s for s in granted if s in allowed]
    if form.get("scope"):
        asked = catalogue.parse(form["scope"])
        if not asked or any(s not in granted for s in form["scope"].split()):
            raise OAuthError("invalid_scope", "A refresh can only keep or narrow the scopes granted.")
        scopes = [s for s in asked if s in allowed]
    if not scopes:
        raise OAuthError("invalid_scope", "None of the scopes granted are ones this app may still use.")
    row.rotated_at = _now()
    conn.last_used_at = _now()
    tokens = _issue(session, app, conn, catalogue.join(scopes), bool(row.livemode), parent_id=row.id)
    await session.commit()
    return tokens


async def client_credentials(session, app: PartnerApps, form: dict) -> dict:
    """grant_type=client_credentials: the app's own token, for no club: to
    list the clubs connected to it and, with clubs:create, to make one. Live
    unless the form says mode=test. No refresh token: ask again."""
    allowed = set(catalogue.parse(app.allowed_scopes)) & APP_TOKEN_SCOPES
    asked = catalogue.parse(form.get("scope")) if form.get("scope") else sorted(allowed)
    if form.get("scope") and any(s not in allowed for s in (form.get("scope") or "").split()):
        raise OAuthError("invalid_scope", "An app's own token can only carry clubs:create, when the app may use it.")
    mode = (form.get("mode") or "live").lower()
    if mode not in ("live", "test"):
        raise OAuthError("invalid_request", "mode is live or test.")
    tokens = _issue(session, app, None, catalogue.join(asked), mode == "live", refresh=False)
    await session.commit()
    return tokens


async def revoke_token(session, app: PartnerApps, raw: Optional[str]) -> None:
    if not raw:
        raise OAuthError("invalid_request", "Send the token to revoke.")
    row = await session.scalar(select(PartnerTokens).where(PartnerTokens.token_hash == digest(raw)))
    if row is None:
        return
    if row.app_id != app.id:
        raise OAuthError("unauthorized_client", "This token was not issued to this client.")
    if row.kind == "refresh" and row.connection_id is not None:
        conn = await session.get(PartnerConnections, row.connection_id)
        if conn is not None:
            await apps_mod.end_connection(session, conn, reason="revoked_by_app")
    elif row.revoked_at is None:
        row.revoked_at = _now()
    await session.commit()


async def access_token(session, raw: Optional[str]) -> Optional[tuple[PartnerTokens, PartnerApps, Optional[PartnerConnections]]]:
    """A live access token, its app and its connection (None for an app's own token)."""
    if not raw or not raw.startswith(ACCESS):
        return None
    row = await session.scalar(select(PartnerTokens).where(PartnerTokens.token_hash == digest(raw), PartnerTokens.kind == "access"))
    if row is None or row.revoked_at is not None or _aware(row.expires_at) <= _now():
        return None
    app = await session.get(PartnerApps, row.app_id)
    if app is None:
        return None
    conn = None
    if row.connection_id is not None:
        conn = await session.get(PartnerConnections, row.connection_id)
        if conn is None or conn.revoked_at is not None:
            return None
    return row, app, conn
