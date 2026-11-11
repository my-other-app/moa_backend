"""Facebook Pages and Instagram business or creator accounts, through one
Meta app (META_APP_ID, META_APP_SECRET) and the Graph API at
SOCIAL_GRAPH_VERSION (v26.0, released 29 July 2026; Meta retires a version
about two years after it ships).

Sign-in: Facebook Login (for Business, when META_LOGIN_CONFIG_ID names a
configuration), the code exchanged for a user token and that for a
long-lived one, then the Pages the person manages (GET /me/accounts) with
each Page's own token -- a Page token from a long-lived user token does not
expire. Instagram is the business or creator account linked to one of those
Pages (`instagram_business_account`), posted to with that Page's token.

Posting:
- Facebook: POST /{page-id}/photos (`url` of the poster, `message`), or
  /{page-id}/feed for text alone; the post's address from `permalink_url`.
- Instagram: POST /{ig-user-id}/media (`image_url`, `caption`) makes a
  container; GET /{container-id}?fields=status_code until FINISHED; POST
  /{ig-user-id}/media_publish (`creation_id`). The container's id is saved
  as soon as it exists (`progress`), so a retry publishes the same one.
  The rolling 24-hour limit: GET /{ig-user-id}/content_publishing_limit.
- Lookups (an answer lost): /{page-id}/published_posts, /{ig-user-id}/media.

Every call carries the token as a bearer header and Meta's appsecret_proof
(an app may require it); none is logged.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.api.social import http as h
from app.api.social.adapters.base import Candidate, Creds, Picture, Published, SignIn, Tokens, same_text
from app.config import settings

GRAPH = "https://graph.facebook.com"
DIALOG = "https://www.facebook.com"
#: Seconds between looks at an Instagram container (the suite sets these to nothing).
POLL_DELAYS: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0, 8.0)

# Meta's error codes: what each means for a post.
_AUTH_CODES = {"102", "190", "463", "467"}
_RATE_CODES = {"4", "9", "17", "32", "613", "80001", "80002", "80005"}
_BUSY_CODES = {"1", "2"}
_PERMISSION_CODES = {"3", "10"}
_IMAGE_SUBCODES = {"2207004", "2207005", "2207009", "2207052", "2207083", "36003", "36001"}
_LIMIT_SUBCODES = {"2207042"}


def version() -> str:
    return (settings.SOCIAL_GRAPH_VERSION or "v26.0").strip()


def api_version() -> str:
    return f"Graph API {version()}"


def url(path: str) -> str:
    return f"{GRAPH}/{version()}/{path.lstrip('/')}"


def _app_id() -> str:
    return (settings.META_APP_ID or "").strip()


def _app_secret() -> str:
    return (settings.META_APP_SECRET or "").strip()


def proof(token: str) -> str:
    """Meta's appsecret_proof: HMAC-SHA256 of the token under the app secret."""
    return hmac.new(_app_secret().encode(), token.encode(), hashlib.sha256).hexdigest()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def error(r: httpx.Response) -> h.PlatformError:
    body = h.json_of(r)
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = None if err.get("code") is None else str(err.get("code"))
    sub = None if err.get("error_subcode") is None else str(err.get("error_subcode"))
    message = err.get("error_user_msg") or err.get("message") or f"Meta answered {r.status_code}."
    tag = f"meta_{code}" + (f"_{sub}" if sub else "") if code else None
    if code in _AUTH_CODES:
        return h.PlatformError(h.AUTH, message, code=tag, status=r.status_code)
    if sub in _LIMIT_SUBCODES or code in _RATE_CODES:
        return h.PlatformError(h.RATE, message, code=tag, status=r.status_code, retry_after=h.retry_after(r))
    if sub in _IMAGE_SUBCODES or code in ("9004", "324", "36003"):
        return h.PlatformError(h.IMAGE, message, code=tag, status=r.status_code)
    if code in _BUSY_CODES or r.status_code >= 500 or err.get("is_transient"):
        return h.PlatformError(h.TRANSIENT, message, code=tag, status=r.status_code)
    if code in _PERMISSION_CODES or (code and code.isdigit() and 200 <= int(code) <= 299):
        return h.PlatformError(h.PERMISSION, message, code=tag, status=r.status_code)
    if code == "368":
        return h.PlatformError(h.REFUSED, message, code=tag, status=r.status_code)
    return h.by_status(r, message, tag)


async def _call(c: httpx.AsyncClient, method: str, path: str, token: Optional[str], *, params: Optional[dict] = None,
                data: Optional[dict] = None, sent: bool = False) -> dict:
    params = dict(params or {})
    if token:
        params["appsecret_proof"] = proof(token)
    try:
        r = await c.request(method, url(path), params=params, data=data, headers=_auth(token) if token else None)
    except httpx.HTTPError as exc:
        raise h.unreachable(exc, sent=sent) from None
    if r.status_code >= 300:
        raise error(r)
    return h.json_of(r)


def _expiry(seconds) -> Optional[datetime]:
    try:
        n = int(seconds)
    except (TypeError, ValueError):
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=n) if n > 0 else None


# ─── Sign-in ──────────────────────────────────────────────────────────────


class _Meta:
    platform = ""
    SCOPES: tuple[str, ...] = ()

    @staticmethod
    def api_version() -> str:
        return api_version()

    def authorize_url(self, *, state: str, redirect_uri: str, challenge: Optional[str] = None) -> str:
        params = {"client_id": _app_id(), "redirect_uri": redirect_uri, "state": state, "response_type": "code"}
        config = (settings.META_LOGIN_CONFIG_ID or "").strip()
        if config:
            params["config_id"] = config
        else:
            params["scope"] = ",".join(self.SCOPES)
        return f"{DIALOG}/{version()}/dialog/oauth?{urlencode(params)}"

    async def _person(self, code: str, redirect_uri: str) -> tuple[str, str, Optional[datetime], list[dict], set[str]]:
        """The code to a long-lived user token; who it is; the Pages they
        manage; the permissions they granted."""
        async with h.client() as c:
            short = await _call(c, "GET", "oauth/access_token", None, params={
                "client_id": _app_id(), "client_secret": _app_secret(), "redirect_uri": redirect_uri, "code": code,
            })
            if not short.get("access_token"):
                raise h.PlatformError(h.REFUSED, "Meta answered the sign-in without a token.", code="no_token")
            long = await _call(c, "GET", "oauth/access_token", None, params={
                "grant_type": "fb_exchange_token", "client_id": _app_id(), "client_secret": _app_secret(),
                "fb_exchange_token": short["access_token"],
            })
            user_token = long.get("access_token") or short["access_token"]
            expires = _expiry(long.get("expires_in"))
            me = await _call(c, "GET", "me", user_token, params={"fields": "id,name"})
            granted = await _call(c, "GET", "me/permissions", user_token)
            scopes = {p.get("permission") for p in granted.get("data") or [] if isinstance(p, dict) and p.get("status") == "granted"}
            pages = await _call(c, "GET", "me/accounts", user_token, params={
                "fields": "id,name,access_token,tasks,picture{url},instagram_business_account{id,username,name,profile_picture_url}",
                "limit": 100,
            })
        return str(me.get("id") or ""), user_token, expires, [p for p in pages.get("data") or [] if isinstance(p, dict)], scopes

    def _missing(self, scopes: set[str]) -> bool:
        # With a Login for Business configuration the permissions are the configuration's.
        return not (settings.META_LOGIN_CONFIG_ID or "").strip() and not set(self.SCOPES) <= scopes

    async def refresh(self, creds: Creds) -> Optional[Tokens]:
        """A Page's token does not expire; nothing to refresh."""
        return None

    async def revoke(self, creds: Creds) -> None:
        """Take back what the person granted the app (DELETE
        /{user-id}/permissions with the app's own token). The caller does it
        only when no other live connection rests on the same person."""
        if not creds.connector_id:
            return
        async with h.client() as c:
            try:
                await c.delete(url(f"{creds.connector_id}/permissions"), params={"access_token": f"{_app_id()}|{_app_secret()}"})
            except httpx.HTTPError:
                pass


class Facebook(_Meta):
    platform = "facebook"
    SCOPES = ("pages_show_list", "pages_manage_posts", "pages_read_engagement")

    async def sign_in(self, *, code: str, redirect_uri: str, verifier: Optional[str] = None) -> SignIn:
        person, _token, _expires, pages, scopes = await self._person(code, redirect_uri)
        if self._missing(scopes):
            return SignIn(connector_id=person, scopes=",".join(sorted(scopes)), empty_reason="missing_permissions")
        found = []
        for p in pages:
            tasks = set(p.get("tasks") or ())
            if not p.get("access_token") or (tasks and not tasks & {"CREATE_CONTENT", "MANAGE"}):
                continue
            pic = (((p.get("picture") or {}).get("data") or {}).get("url")) if isinstance(p.get("picture"), dict) else None
            found.append(Candidate(external_id=str(p["id"]), name=p.get("name"), handle=None, picture=pic, token=p["access_token"]))
        return SignIn(candidates=found, connector_id=person, scopes=",".join(sorted(scopes)), empty_reason=None if found else "no_pages")

    async def publish(self, creds: Creds, text: str, picture: Optional[Picture], *, container: Optional[str] = None,
                      progress=None) -> Published:
        async with h.client() as c:
            if picture is not None:
                body = await _call(c, "POST", f"{creds.external_id}/photos", creds.token,
                                   data={"url": picture.url, "message": text, "published": "true"}, sent=True)
                post_id = str(body.get("post_id") or body.get("id") or "")
            else:
                body = await _call(c, "POST", f"{creds.external_id}/feed", creds.token, data={"message": text}, sent=True)
                post_id = str(body.get("id") or "")
            if not post_id:
                raise h.PlatformError(h.UNKNOWN, "Facebook took the post but gave no id.", code="no_id")
            link = None
            try:
                link = (await _call(c, "GET", post_id, creds.token, params={"fields": "permalink_url"})).get("permalink_url")
            except h.PlatformError:
                pass
        return Published(external_id=post_id, url=link or f"https://www.facebook.com/{post_id}")

    async def find_recent(self, creds: Creds, text: str, since: datetime) -> Optional[Published]:
        async with h.client() as c:
            body = await _call(c, "GET", f"{creds.external_id}/published_posts", creds.token, params={
                "fields": "id,message,created_time,permalink_url", "since": int(since.timestamp()) - 120, "limit": 25,
            })
        for p in body.get("data") or []:
            if isinstance(p, dict) and same_text(text, p.get("message")):
                return Published(external_id=str(p.get("id")), url=p.get("permalink_url") or f"https://www.facebook.com/{p.get('id')}")
        return None


class Instagram(_Meta):
    platform = "instagram"
    SCOPES = ("instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement")

    async def sign_in(self, *, code: str, redirect_uri: str, verifier: Optional[str] = None) -> SignIn:
        person, _token, _expires, pages, scopes = await self._person(code, redirect_uri)
        if self._missing(scopes):
            return SignIn(connector_id=person, scopes=",".join(sorted(scopes)), empty_reason="missing_permissions")
        found, seen = [], set()
        for p in pages:
            ig = p.get("instagram_business_account")
            if not isinstance(ig, dict) or not ig.get("id") or not p.get("access_token") or ig["id"] in seen:
                continue
            seen.add(ig["id"])
            found.append(Candidate(
                external_id=str(ig["id"]), name=ig.get("name") or ig.get("username"), handle=ig.get("username"),
                picture=ig.get("profile_picture_url"), token=p["access_token"],
            ))
        return SignIn(candidates=found, connector_id=person, scopes=",".join(sorted(scopes)),
                      empty_reason=None if found else "no_instagram_business")

    async def quota(self, creds: Creds) -> Optional[tuple[int, int]]:
        """(used, allowed) of the rolling 24 hours, as Instagram counts them."""
        async with h.client() as c:
            body = await _call(c, "GET", f"{creds.external_id}/content_publishing_limit", creds.token,
                               params={"fields": "quota_usage,config"})
        rows = body.get("data") or []
        if not rows or not isinstance(rows[0], dict):
            return None
        used = int(rows[0].get("quota_usage") or 0)
        total = int(((rows[0].get("config") or {}).get("quota_total")) or 100)
        return used, total

    async def _status(self, c, creds: Creds, container: str) -> str:
        body = await _call(c, "GET", container, creds.token, params={"fields": "status_code,status"})
        return str(body.get("status_code") or "")

    async def publish(self, creds: Creds, text: str, picture: Optional[Picture], *, container: Optional[str] = None,
                      progress=None) -> Published:
        if picture is None:
            raise h.PlatformError(h.IMAGE, "Instagram needs a picture with every post.", code="no_image")
        ig = creds.external_id
        async with h.client() as c:
            if container:
                status = await self._status(c, creds, container)
                if status == "PUBLISHED":
                    # Published by an attempt whose answer was lost: find it, never publish again.
                    raise h.PlatformError(h.UNKNOWN, "Instagram already published this post.", code="container_published")
                if status in ("EXPIRED", "ERROR", ""):
                    container = None
            if not container:
                made = await _call(c, "POST", f"{ig}/media", creds.token, data={"image_url": picture.url, "caption": text})
                container = str(made.get("id") or "")
                if not container:
                    raise h.PlatformError(h.TRANSIENT, "Instagram gave no container for the picture.", code="no_container")
                if progress is not None:
                    await progress(container)
            for delay in (0.0, *POLL_DELAYS):
                if delay:
                    await asyncio.sleep(delay)
                status = await self._status(c, creds, container)
                if status == "FINISHED":
                    break
                if status == "ERROR":
                    raise h.PlatformError(h.IMAGE, "Instagram could not use the picture (it needs a JPEG, 4:5 to 1.91:1).", code="ig_container_error")
                if status == "EXPIRED":
                    raise h.PlatformError(h.TRANSIENT, "Instagram's container expired before it was published.", code="ig_container_expired")
                if status == "PUBLISHED":
                    raise h.PlatformError(h.UNKNOWN, "Instagram already published this post.", code="container_published")
            else:
                raise h.PlatformError(h.TRANSIENT, "Instagram is still processing the picture.", code="ig_processing")
            done = await _call(c, "POST", f"{ig}/media_publish", creds.token, data={"creation_id": container}, sent=True)
            media = str(done.get("id") or "")
            if not media:
                raise h.PlatformError(h.UNKNOWN, "Instagram published without giving the post's id.", code="no_id")
            link = None
            try:
                link = (await _call(c, "GET", media, creds.token, params={"fields": "permalink"})).get("permalink")
            except h.PlatformError:
                pass
        return Published(external_id=media, url=link)

    async def find_recent(self, creds: Creds, text: str, since: datetime) -> Optional[Published]:
        async with h.client() as c:
            body = await _call(c, "GET", f"{creds.external_id}/media", creds.token,
                               params={"fields": "id,caption,timestamp,permalink", "limit": 25})
        floor = since - timedelta(minutes=2)
        for p in body.get("data") or []:
            if not isinstance(p, dict):
                continue
            try:
                when = datetime.fromisoformat(str(p.get("timestamp")).replace("+0000", "+00:00"))
            except ValueError:
                when = None
            if when is not None and when < floor:
                continue
            if same_text(text, p.get("caption")):
                return Published(external_id=str(p.get("id")), url=p.get("permalink"))
        return None


FACEBOOK = Facebook()
INSTAGRAM = Instagram()


# ─── Meta's callbacks: deauthorise and data deletion ───────────────────────


def _b64(part: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def parse_signed_request(signed: Optional[str]) -> Optional[dict]:
    """Meta's signed_request, checked: `<sig>.<payload>`, both base64url,
    the signature HMAC-SHA256 of the payload's text under the app secret.
    None for anything that is not one, signed by our app, with HMAC-SHA256."""
    import json

    secret = _app_secret()
    if not signed or not secret or "." not in signed:
        return None
    sig_part, payload_part = signed.split(".", 1)
    try:
        sig = _b64(sig_part)
        payload = json.loads(_b64(payload_part))
    except (ValueError, TypeError):
        return None
    expected = hmac.new(secret.encode(), payload_part.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    if not isinstance(payload, dict) or str(payload.get("algorithm", "")).upper() != "HMAC-SHA256" or not payload.get("user_id"):
        return None
    return payload
