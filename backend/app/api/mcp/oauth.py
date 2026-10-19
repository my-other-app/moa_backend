"""The authorization server for the club MCP endpoint.

The MCP authorization spec (revision 2026-07-28) asks the endpoint to be an
OAuth 2.1 resource server and points clients at an authorization server.
This backend is both, for its own endpoint only:

- discovery: protected resource metadata (RFC 9728) and authorization
  server metadata (RFC 8414);
- clients: registered by themselves (RFC 7591), or known by the https URL of
  their metadata document (OAuth Client ID Metadata Documents), fetched once
  and kept for as long as it says it may be;
- the authorization code grant with PKCE, S256 only, and the resource
  indicator (RFC 8707) binding every token to https://.../mcp;
- tokens: opaque, stored as sha256 digests, bound to the grant (club, person,
  client, scopes). Access tokens last APP_MCP_ACCESS_TOKEN_MINUTES; refresh
  tokens rotate on every use, and one presented a second time ends the grant;
- revocation (RFC 7009).

The person's side -- signing in, picking the club, consenting -- is router.py
and pages.py. Every refusal here is an `OAuthError`, answered in OAuth's own
JSON shape, never the API's.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import socket
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import httpx
from sqlalchemy import select

from app.api.mcp import scopes as catalogue
from app.api.mcp.access import AccessEnded, end_grant, principal_for
from app.api.mcp.models import McpAuthCodes, McpClients, McpGrants, McpTokens
from app.config import settings

logger = logging.getLogger(__name__)

#: Prefixes, so a leaked token is recognisable (and secret scanners can find one).
ACCESS = "moa_at_"
REFRESH = "moa_rt_"
CODE = "moa_ac_"
SECRET = "moa_cs_"
CODE_TTL = timedelta(minutes=5)
AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
GRANT_TYPES = ("authorization_code", "refresh_token")

#: A client metadata document: at most this big, read within this long, and
#: trusted for between these (Cache-Control max-age, else an hour).
DOC_MAX_BYTES = 16 * 1024
DOC_TIMEOUT = 5.0
DOC_MIN_AGE = timedelta(minutes=5)
DOC_MAX_AGE = timedelta(hours=24)
DOC_DEFAULT_AGE = timedelta(hours=1)

_PKCE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
#: Schemes no redirect may use: they run code, read files, or are not a place an app listens.
_NEVER_SCHEMES = frozenset({
    "http", "javascript", "data", "file", "vbscript", "about", "blob", "filesystem", "ftp", "ws", "wss",
    "mailto", "tel", "sms", "view-source", "chrome", "chrome-extension", "moz-extension", "intent",
})
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def digest(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


class OAuthError(Exception):
    """A refusal in OAuth's terms: `error` from the RFCs' lists, and a sentence."""

    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status

    def body(self) -> dict:
        return {"error": self.error, "error_description": self.description}


# ─── Addresses and discovery ─────────────────────────────────────────────


def issuer() -> str:
    """The authorization server's identifier: the API's own origin."""
    return settings.PUBLIC_API_URL.rstrip("/")


def resource_url() -> str:
    """The MCP endpoint's canonical URI, every token's audience."""
    return f"{issuer()}/mcp"


def resource_metadata_url() -> str:
    return f"{issuer()}/.well-known/oauth-protected-resource/mcp"


def dashboard_url(path: str = "") -> str:
    return settings.MCP_DASHBOARD_URL.rstrip("/") + path


def _canonical(uri: str) -> str:
    parts = urlsplit((uri or "").strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


def same_resource(a: str, b: str) -> bool:
    return bool(a) and bool(b) and _canonical(a) == _canonical(b)


def protected_resource_metadata() -> dict:
    return {
        "resource": resource_url(),
        "authorization_servers": [issuer()],
        "scopes_supported": list(catalogue.ALL),
        "bearer_methods_supported": ["header"],
        "resource_name": "MyOtherApp for clubs",
    }


def authorization_server_metadata() -> dict:
    base = issuer()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "scopes_supported": list(catalogue.ALL),
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": list(GRANT_TYPES),
        "token_endpoint_auth_methods_supported": list(AUTH_METHODS),
        "revocation_endpoint_auth_methods_supported": list(AUTH_METHODS),
        "code_challenge_methods_supported": ["S256"],
        "client_id_metadata_document_supported": bool(settings.MCP_CLIENT_METADATA_DOCUMENTS),
        "authorization_response_iss_parameter_supported": True,
    }


# ─── Redirect URIs ───────────────────────────────────────────────────────


def check_redirect_uri(uri) -> str:
    """An https address; http only back to this machine (a desktop app's
    loopback listener); or a native app's own scheme (cursor://...). Never a
    fragment, never a scheme that runs code or opens a file."""
    if not isinstance(uri, str) or not uri.strip() or len(uri) > 2000:
        raise OAuthError("invalid_redirect_uri", "Each redirect URI must be a URL.")
    parts = urlsplit(uri)
    scheme = parts.scheme.lower()
    if parts.fragment:
        raise OAuthError("invalid_redirect_uri", "A redirect URI cannot have a fragment.")
    if scheme == "https":
        if not parts.hostname:
            raise OAuthError("invalid_redirect_uri", f"{uri} has no host.")
    elif scheme == "http":
        if (parts.hostname or "").lower() not in _LOOPBACK:
            raise OAuthError("invalid_redirect_uri", "Plain http is allowed only for localhost; use https.")
    elif not _SCHEME.match(scheme) or scheme in _NEVER_SCHEMES or not (parts.netloc or parts.path):
        raise OAuthError("invalid_redirect_uri", f"{uri} is not a redirect URI this server allows.")
    return uri


def is_loopback(uri: str) -> bool:
    parts = urlsplit(uri)
    return parts.scheme.lower() == "http" and (parts.hostname or "").lower() in _LOOPBACK


def redirect_allowed(registered: list, presented: str | None) -> bool:
    """Exactly one of the registered URIs. A loopback one may come back on
    any port (RFC 8252 7.3): a desktop app picks a free port each time."""
    if not presented:
        return False
    for uri in registered or []:
        if uri == presented:
            return True
        if is_loopback(uri) and is_loopback(presented):
            a, b = urlsplit(uri), urlsplit(presented)
            if (a.hostname, a.path, a.query) == (b.hostname, b.path, b.query):
                return True
    return False


def redirect_target(uri: str) -> str:
    """Where a redirect goes, as the consent page names it: the host, or a native app's scheme."""
    parts = urlsplit(uri)
    if parts.scheme.lower() in ("http", "https"):
        return parts.netloc.lower()
    return f"{parts.scheme}://{parts.netloc or parts.path.split('/')[0]}"


def with_params(uri: str, params: dict) -> str:
    """`uri` with `params` added to its query (what was there kept), and `iss` (RFC 9207)."""
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query += [(k, v) for k, v in {**params, "iss": issuer()}.items() if v is not None]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


# ─── Clients ─────────────────────────────────────────────────────────────


def _text(value, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OAuthError("invalid_client_metadata", "Client names and URLs must be text.")
    value = value.strip()
    return value[:limit] or None


def _https_url(value, field: str) -> str | None:
    value = _text(value, 500)
    if value is None:
        return None
    if urlsplit(value).scheme.lower() != "https":
        raise OAuthError("invalid_client_metadata", f"{field} must be an https URL.")
    return value


def _redirects(value) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 10:
        raise OAuthError("invalid_redirect_uri", "Give between one and ten redirect_uris.")
    return [check_redirect_uri(u) for u in value]


def _grant_types(value) -> list[str]:
    types = value if value is not None else ["authorization_code"]
    if not isinstance(types, list) or "authorization_code" not in types or any(t not in GRANT_TYPES for t in types):
        raise OAuthError("invalid_client_metadata", "grant_types may be authorization_code and refresh_token, and must include authorization_code.")
    return list(dict.fromkeys(types))


async def register_client(session, body) -> dict:
    """RFC 7591: a client registers itself. Answered with its client_id
    (and a secret, unless it registered as a public client)."""
    if not isinstance(body, dict):
        raise OAuthError("invalid_client_metadata", "Send the client's metadata as a JSON object.")
    redirect_uris = _redirects(body.get("redirect_uris"))
    grant_types = _grant_types(body.get("grant_types"))
    response_types = body.get("response_types") or ["code"]
    if response_types != ["code"]:
        raise OAuthError("invalid_client_metadata", "The only response type is code.")
    # RFC 7591's default is client_secret_basic; MCP clients ask for none.
    method = body.get("token_endpoint_auth_method") or "client_secret_basic"
    if method not in AUTH_METHODS:
        raise OAuthError("invalid_client_metadata", f"token_endpoint_auth_method must be one of {', '.join(AUTH_METHODS)}.")
    secret = new_token(SECRET) if method != "none" else None
    kept = {k: v for k, v in body.items() if k not in ("client_secret", "client_id") and isinstance(k, str)}
    client = McpClients(
        client_id=f"mcp_{secrets.token_urlsafe(18)}",
        kind="dcr",
        client_name=_text(body.get("client_name"), 200),
        client_uri=_https_url(body.get("client_uri"), "client_uri"),
        logo_uri=_https_url(body.get("logo_uri"), "logo_uri"),
        redirect_uris=redirect_uris,
        grant_types=grant_types,
        token_endpoint_auth_method=method,
        client_secret_hash=digest(secret) if secret else None,
        software_id=_text(body.get("software_id"), 200),
        software_version=_text(body.get("software_version"), 60),
        client_metadata=_small(kept),
    )
    session.add(client)
    await session.flush()
    out = {
        "client_id": client.client_id,
        "client_id_issued_at": int(_now().timestamp()),
        "client_name": client.client_name,
        "client_uri": client.client_uri,
        "logo_uri": client.logo_uri,
        "redirect_uris": redirect_uris,
        "grant_types": grant_types,
        "response_types": ["code"],
        "token_endpoint_auth_method": method,
    }
    if secret:
        out.update(client_secret=secret, client_secret_expires_at=0)
    return {k: v for k, v in out.items() if v is not None}


def _small(data: dict) -> dict:
    """What a client sent, kept for the record when it is a few KB at most."""
    try:
        text = json.dumps(data, default=str)
    except (TypeError, ValueError):
        return {}
    return json.loads(text) if len(text) <= 8000 else {}


def is_document_url(client_id: str | None) -> bool:
    """A client_id that is the https URL of the client's metadata document."""
    if not client_id or not client_id.startswith("https://"):
        return False
    return urlsplit(client_id).path not in ("", "/")


async def client_for(session, client_id: str | None) -> McpClients | None:
    """The client behind a client_id: a registered one, or one known by its
    metadata document (read now when not known, or no longer fresh)."""
    if not client_id or len(client_id) > 512:
        return None
    if not is_document_url(client_id):
        return await session.scalar(select(McpClients).where(McpClients.client_id == client_id, McpClients.kind == "dcr"))
    if not settings.MCP_CLIENT_METADATA_DOCUMENTS:
        return None
    row = await session.scalar(select(McpClients).where(McpClients.client_id == client_id, McpClients.kind == "cimd"))
    if row is not None and _aware(row.fresh_until) and _aware(row.fresh_until) > _now():
        return row
    try:
        document, max_age = await fetch_document(client_id)
        fields = document_fields(client_id, document)
    except OAuthError as e:
        logger.info("client metadata document %s refused: %s", client_id, e.description)
        return None
    if row is None:
        row = McpClients(client_id=client_id, kind="cimd")
        session.add(row)
    for key, value in fields.items():
        setattr(row, key, value)
    row.fetched_at = _now()
    row.fresh_until = _now() + max_age
    await session.flush()
    return row


def document_fields(url: str, document) -> dict:
    """A metadata document checked as the draft asks: a JSON object whose
    client_id is its own URL, exactly, with redirect URIs and a name."""
    if not isinstance(document, dict):
        raise OAuthError("invalid_client", "The client's metadata document is not a JSON object.")
    if document.get("client_id") != url:
        raise OAuthError("invalid_client", "The client's metadata document names a different client_id.")
    method = document.get("token_endpoint_auth_method") or "none"
    if method != "none":
        raise OAuthError("invalid_client", "Only public clients (token_endpoint_auth_method none) may use a metadata document here.")
    return dict(
        client_name=_text(document.get("client_name"), 200),
        client_uri=_https_url(document.get("client_uri"), "client_uri"),
        logo_uri=_https_url(document.get("logo_uri"), "logo_uri"),
        redirect_uris=_redirects(document.get("redirect_uris")),
        grant_types=_grant_types(document.get("grant_types")),
        token_endpoint_auth_method="none",
        client_secret_hash=None,
        software_id=_text(document.get("software_id"), 200),
        software_version=_text(document.get("software_version"), 60),
        client_metadata=_small(document),
    )


def _max_age(cache_control: str | None) -> timedelta:
    match = re.search(r"max-age=(\d+)", cache_control or "")
    age = timedelta(seconds=int(match.group(1))) if match else DOC_DEFAULT_AGE
    if "no-store" in (cache_control or "") or "no-cache" in (cache_control or ""):
        age = DOC_MIN_AGE
    return max(DOC_MIN_AGE, min(DOC_MAX_AGE, age))


def _public(address: str) -> bool:
    try:
        a = ipaddress.ip_address(address)
    except ValueError:
        return False
    return a.is_global and not a.is_multicast


async def fetch_document(url: str) -> tuple[dict, timedelta]:
    """GET a client's metadata document, guarded against being turned on the
    server's own network: https on 443 only, every address the host resolves
    to public, no redirects, a size and a time limit. Swapped in tests."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment or parts.port not in (None, 443):
        raise OAuthError("invalid_client", "A client metadata document must be at an https URL on the default port.")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, parts.hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise OAuthError("invalid_client", "The client's metadata document's host does not resolve.")
    if not infos or not all(_public(info[4][0]) for info in infos):
        raise OAuthError("invalid_client", "The client's metadata document is not on a public address.")
    body = b""
    try:
        async with httpx.AsyncClient(timeout=DOC_TIMEOUT, follow_redirects=False) as http:
            async with http.stream("GET", url, headers={"Accept": "application/json", "User-Agent": "MyOtherApp-OAuth/1.0"}) as response:
                if response.status_code != 200:
                    raise OAuthError("invalid_client", f"The client's metadata document answered {response.status_code}.")
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > DOC_MAX_BYTES:
                        raise OAuthError("invalid_client", "The client's metadata document is too large.")
                cache_control = response.headers.get("cache-control")
    except httpx.HTTPError:
        raise OAuthError("invalid_client", "The client's metadata document could not be read.")
    try:
        document = json.loads(body)
    except ValueError:
        raise OAuthError("invalid_client", "The client's metadata document is not JSON.")
    return document, _max_age(cache_control)


async def authenticate_client(session, form: dict, authorization: str | None) -> McpClients:
    """The client calling the token or revocation endpoint: by its secret
    (in the Authorization header or the form) when it registered for one,
    by its client_id alone when it is public."""
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
    client = await client_for(session, client_id)
    if client is None:
        raise OAuthError("invalid_client", "Unknown client.", 401)
    if client.token_endpoint_auth_method != "none":
        if not secret or not client.client_secret_hash or not hmac.compare_digest(digest(secret), client.client_secret_hash):
            raise OAuthError("invalid_client", "The client's secret is not right.", 401)
    return client


# ─── The authorization request ───────────────────────────────────────────


@dataclass
class AuthRequest:
    """A checked authorization request, carried (signed) through the pages."""

    client_id: str
    redirect_uri: str
    code_challenge: str
    state: str | None
    scopes: list
    resource: str

    def payload(self) -> dict:
        return asdict(self)


class AuthorizeError(Exception):
    """A refused authorization request. With a redirect_uri the client is
    told by redirecting there; without one (the client or its redirect could
    not be trusted) the person is shown the error instead."""

    def __init__(self, error: str, description: str, *, redirect_uri: str | None = None, state: str | None = None):
        super().__init__(description)
        self.error = error
        self.description = description
        self.redirect_uri = redirect_uri
        self.state = state

    def location(self) -> str | None:
        if not self.redirect_uri:
            return None
        return with_params(self.redirect_uri, {"error": self.error, "error_description": self.description, "state": self.state})


async def check_authorization_request(session, params) -> tuple[McpClients, AuthRequest]:
    client = await client_for(session, params.get("client_id"))
    if client is None:
        raise AuthorizeError("invalid_client", "This app is not registered with MyOtherApp, or its details could not be read.")
    redirect_uri = params.get("redirect_uri")
    if not redirect_uri and len(client.redirect_uris or []) == 1:
        redirect_uri = client.redirect_uris[0]
    if not redirect_allowed(client.redirect_uris, redirect_uri):
        raise AuthorizeError("invalid_request", "The app asked to be sent back to an address it did not register.")
    state = params.get("state")

    def refuse(error: str, description: str):
        return AuthorizeError(error, description, redirect_uri=redirect_uri, state=state)

    if params.get("response_type") != "code":
        raise refuse("unsupported_response_type", "Only response_type=code is supported.")
    challenge = params.get("code_challenge") or ""
    if not challenge:
        raise refuse("invalid_request", "PKCE is required: send a code_challenge.")
    if params.get("code_challenge_method") != "S256":
        raise refuse("invalid_request", "Only the S256 code_challenge_method is supported.")
    if not _PKCE.match(challenge):
        raise refuse("invalid_request", "The code_challenge is not a valid S256 challenge.")
    asked = params.get("scope")
    wanted = catalogue.parse(asked) if asked else list(catalogue.ALL)
    if asked and not wanted:
        raise refuse("invalid_scope", f"None of the scopes asked for exist here. They are: {', '.join(catalogue.ALL)}.")
    resource = params.get("resource") or resource_url()
    if not same_resource(resource, resource_url()):
        raise refuse("invalid_target", f"Tokens here are only for {resource_url()}.")
    return client, AuthRequest(
        client_id=client.client_id, redirect_uri=redirect_uri, code_challenge=challenge, state=state,
        scopes=wanted, resource=resource_url(),
    )


# ─── Codes and tokens ────────────────────────────────────────────────────


async def issue_code(session, client: McpClients, request: AuthRequest, *, club_id: int, user_id: int, member_id: int | None, token_version: int | None, scopes: list) -> str:
    code = new_token(CODE)
    session.add(McpAuthCodes(
        code_hash=digest(code), client_pk=client.id, club_id=club_id, user_id=user_id, member_id=member_id,
        token_version=token_version, redirect_uri=request.redirect_uri, code_challenge=request.code_challenge,
        scopes=catalogue.join(scopes), resource=request.resource, expires_at=_now() + CODE_TTL,
    ))
    await session.flush()
    return code


def pkce_matches(verifier: str | None, challenge: str) -> bool:
    if not verifier or not _PKCE.match(verifier):
        return False
    computed = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return hmac.compare_digest(computed, challenge)


def _issue(session, grant: McpGrants, scopes: str, resource: str, *, parent_id=None) -> dict:
    """A new access token and refresh token for the grant. Not committed."""
    now = _now()
    access, refresh = new_token(ACCESS), new_token(REFRESH)
    minutes = int(settings.MCP_ACCESS_TOKEN_MINUTES)
    session.add(McpTokens(grant_id=grant.id, kind="access", token_hash=digest(access), scopes=scopes, resource=resource, expires_at=now + timedelta(minutes=minutes)))
    session.add(McpTokens(
        grant_id=grant.id, kind="refresh", token_hash=digest(refresh), scopes=scopes, resource=resource,
        expires_at=now + timedelta(days=int(settings.MCP_REFRESH_TOKEN_DAYS)), parent_id=parent_id,
    ))
    return {"access_token": access, "token_type": "Bearer", "expires_in": minutes * 60, "refresh_token": refresh, "scope": scopes}


async def exchange_code(session, client: McpClients, form: dict) -> dict:
    """grant_type=authorization_code: the code, once, within its minutes, by
    the client it was issued to, with the verifier behind its challenge. The
    grant -- the club's connected app -- is made here. A code presented a
    second time ends the grant its first use made."""
    code = form.get("code")
    if not code:
        raise OAuthError("invalid_request", "Send the authorization code.")
    row = await session.scalar(select(McpAuthCodes).where(McpAuthCodes.code_hash == digest(code)).with_for_update())
    if row is None or row.client_pk != client.id:
        raise OAuthError("invalid_grant", "The authorization code is not valid.")
    if row.used_at is not None:
        if row.grant_id is not None:
            grant = await session.get(McpGrants, row.grant_id)
            if grant is not None:
                await end_grant(session, grant, reason="code_reused")
        await session.commit()
        logger.warning("authorization code for client %s presented a second time: its grant was ended", client.client_id)
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
    if form.get("resource") and not same_resource(form["resource"], row.resource):
        await session.commit()
        raise OAuthError("invalid_target", f"Tokens here are only for {resource_url()}.")
    grant = McpGrants(
        club_id=row.club_id, user_id=row.user_id, member_id=row.member_id, client_pk=client.id,
        scopes=row.scopes, token_version=row.token_version, last_used_at=_now(),
    )
    session.add(grant)
    await session.flush()
    row.grant_id = grant.id
    try:
        await principal_for(session, grant)
    except AccessEnded as e:
        await end_grant(session, grant, reason=e.reason)
        await session.commit()
        raise OAuthError("invalid_grant", e.message)
    tokens = _issue(session, grant, row.scopes, row.resource)
    await session.commit()
    return tokens


async def refresh(session, client: McpClients, form: dict) -> dict:
    """grant_type=refresh_token: a new pair for the old refresh token, which
    is spent. Presented again after that -- two parties hold the grant, one of
    them stole it -- the grant ends, for both."""
    raw = form.get("refresh_token")
    if not raw:
        raise OAuthError("invalid_request", "Send the refresh_token.")
    row = await session.scalar(
        select(McpTokens).where(McpTokens.token_hash == digest(raw), McpTokens.kind == "refresh").with_for_update()
    )
    grant = await session.get(McpGrants, row.grant_id) if row is not None else None
    if row is None or grant is None or grant.client_pk != client.id:
        raise OAuthError("invalid_grant", "The refresh token is not valid.")
    if row.rotated_at is not None:
        await end_grant(session, grant, reason="refresh_reused")
        await session.commit()
        logger.warning("spent refresh token presented again for grant %s (client %s): grant ended", grant.id, client.client_id)
        raise OAuthError("invalid_grant", "This refresh token was already used, so the connection was ended. Connect the app again.")
    if row.revoked_at is not None or grant.revoked_at is not None:
        raise OAuthError("invalid_grant", "This connection was ended. Connect the app again.")
    if _aware(row.expires_at) <= _now():
        raise OAuthError("invalid_grant", "The refresh token has expired. Connect the app again.")
    if form.get("resource") and not same_resource(form["resource"], row.resource):
        raise OAuthError("invalid_target", f"Tokens here are only for {resource_url()}.")
    granted = row.scopes.split()
    scopes = row.scopes
    if form.get("scope"):
        asked = catalogue.parse(form["scope"])
        if not asked or any(s not in granted for s in form["scope"].split()):
            raise OAuthError("invalid_scope", "A refresh can only keep or narrow the scopes granted.")
        scopes = catalogue.join(asked)
    try:
        await principal_for(session, grant)
    except AccessEnded as e:
        await end_grant(session, grant, reason=e.reason)
        await session.commit()
        raise OAuthError("invalid_grant", e.message)
    row.rotated_at = _now()
    grant.last_used_at = _now()
    tokens = _issue(session, grant, scopes, row.resource, parent_id=row.id)
    await session.commit()
    return tokens


async def revoke_token(session, client: McpClients, raw: str | None) -> None:
    """RFC 7009. A refresh token ends its whole grant (the app is
    disconnected); an access token ends itself. An unknown token is no error."""
    if not raw:
        raise OAuthError("invalid_request", "Send the token to revoke.")
    row = await session.scalar(select(McpTokens).where(McpTokens.token_hash == digest(raw)))
    if row is None:
        return
    grant = await session.get(McpGrants, row.grant_id)
    if grant is None or grant.client_pk != client.id:
        raise OAuthError("unauthorized_client", "This token was not issued to this client.")
    if row.kind == "refresh":
        await end_grant(session, grant, reason="revoked_by_app")
    elif row.revoked_at is None:
        row.revoked_at = _now()
    await session.commit()


async def access_token(session, raw: str | None) -> tuple[McpTokens, McpGrants] | None:
    """A live access token for this endpoint, and its live grant; None otherwise."""
    if not raw or not raw.startswith(ACCESS):
        return None
    row = await session.scalar(select(McpTokens).where(McpTokens.token_hash == digest(raw), McpTokens.kind == "access"))
    if row is None or row.revoked_at is not None or _aware(row.expires_at) <= _now() or not same_resource(row.resource, resource_url()):
        return None
    grant = await session.get(McpGrants, row.grant_id)
    if grant is None or grant.revoked_at is not None:
        return None
    return row, grant
