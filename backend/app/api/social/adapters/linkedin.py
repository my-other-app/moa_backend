"""LinkedIn: a member's own profile, and a club's Page (an organisation),
through one LinkedIn app (LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET) and
the versioned REST API (LinkedIn-Version LINKEDIN_API_VERSION, 202610;
LinkedIn sunsets a version about a year after release).

Sign-in: OAuth 2.0's authorization code. A member: `openid profile
w_member_social` (Sign In with LinkedIn using OpenID Connect + Share on
LinkedIn), who they are from /v2/userinfo. A Page: `r_organization_admin
w_organization_social r_organization_social` (the Community Management
API, behind LINKEDIN_PAGES_ENABLED until its review passes), the Pages the
member administers from /rest/organizationAcls -- none and the member is
told only a Page admin can connect it. Access lasts 60 days; a refresh
token comes only where LinkedIn grants one.

Posting: the picture's upload slot from /rest/images?action=initializeUpload,
the bytes PUT there, then POST /rest/posts with the image's urn; the post's
urn comes back in `x-restli-id`. A lost answer is looked up with the posts
by author for a Page (r_organization_social); a member's own posts cannot
be read (r_member_social is for approved partners only), so such a post is
never sent again by itself: the club is asked.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, urlencode

import httpx

from app.api.social import http as h
from app.api.social.adapters.base import Candidate, Creds, Picture, Published, SignIn, Tokens, same_text
from app.config import settings

AUTHORIZE = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN = "https://www.linkedin.com/oauth/v2/accessToken"
REVOKE = "https://www.linkedin.com/oauth/v2/revoke"
API = "https://api.linkedin.com"

# LinkedIn's "little" text: these must be escaped to be read as themselves.
_RESERVED = re.compile(r"([\\|{}@\[\]()<>*_~])")
# A # that does not begin a hashtag.
_LONE_HASH = re.compile(r"#(?!\w)")


def version() -> str:
    return (settings.LINKEDIN_API_VERSION or "202610").strip()


def little(text: str) -> str:
    """Our text as LinkedIn's commentary: its reserved characters escaped,
    a hashtag left a hashtag."""
    return _LONE_HASH.sub(r"\\#", _RESERVED.sub(r"\\\1", text or ""))


def _id() -> str:
    return (settings.LINKEDIN_CLIENT_ID or "").strip()


def _secret() -> str:
    return (settings.LINKEDIN_CLIENT_SECRET or "").strip()


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": version(),
        "X-Restli-Protocol-Version": "2.0.0",
    }


def error(r: httpx.Response) -> h.PlatformError:
    body = h.json_of(r)
    message = body.get("message") or body.get("error_description") or f"LinkedIn answered {r.status_code}."
    code = body.get("code") or body.get("serviceErrorCode") or body.get("error")
    tag = f"linkedin_{code}" if code else None
    if r.status_code == 403 and "organization" in str(message).lower():
        return h.PlatformError(h.PERMISSION, "Only a Page admin can post as the Page.", code="not_page_admin", status=403)
    if r.status_code == 422 and "image" in str(message).lower():
        return h.PlatformError(h.IMAGE, message, code=tag, status=422)
    if r.status_code == 409:
        return h.PlatformError(h.TRANSIENT, message, code=tag or "conflict", status=409)
    return h.by_status(r, message, str(tag) if tag else None)


def _expiry(seconds) -> Optional[datetime]:
    try:
        n = int(seconds)
    except (TypeError, ValueError):
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=n) if n > 0 else None


class _LinkedIn:
    platform = ""
    SCOPES: tuple[str, ...] = ()

    @staticmethod
    def api_version() -> str:
        return f"LinkedIn REST {version()}"

    def authorize_url(self, *, state: str, redirect_uri: str, challenge: Optional[str] = None) -> str:
        params = {"response_type": "code", "client_id": _id(), "redirect_uri": redirect_uri, "state": state, "scope": " ".join(self.SCOPES)}
        return f"{AUTHORIZE}?{urlencode(params)}"

    async def _tokens(self, data: dict) -> dict:
        async with h.client() as c:
            try:
                r = await c.post(TOKEN, data={**data, "client_id": _id(), "client_secret": _secret()})
            except httpx.HTTPError as exc:
                raise h.unreachable(exc) from None
        if r.status_code >= 300:
            raise error(r)
        body = h.json_of(r)
        if not body.get("access_token"):
            raise h.PlatformError(h.REFUSED, "LinkedIn answered without a token.", code="no_token")
        return body

    async def _get(self, c, path: str, token: str, params: Optional[dict] = None) -> httpx.Response:
        try:
            r = await c.get(f"{API}{path}", params=params, headers=_headers(token))
        except httpx.HTTPError as exc:
            raise h.unreachable(exc) from None
        if r.status_code >= 300:
            raise error(r)
        return r

    async def refresh(self, creds: Creds) -> Optional[Tokens]:
        if not creds.refresh_token:
            return None
        body = await self._tokens({"grant_type": "refresh_token", "refresh_token": creds.refresh_token})
        return Tokens(
            token=body["access_token"], refresh_token=body.get("refresh_token") or creds.refresh_token,
            expires_at=_expiry(body.get("expires_in")), refresh_expires_at=_expiry(body.get("refresh_token_expires_in")),
        )

    async def revoke(self, creds: Creds) -> None:
        async with h.client() as c:
            try:
                await c.post(REVOKE, data={"client_id": _id(), "client_secret": _secret(), "token": creds.token})
            except httpx.HTTPError:
                pass

    def author(self, creds: Creds) -> str:
        raise NotImplementedError

    async def _upload(self, c, creds: Creds, picture: Picture) -> str:
        from app.core.storage import objects

        data = await asyncio.to_thread(objects.get_bytes, picture.key)
        if not data:
            raise h.PlatformError(h.IMAGE, "The picture could not be read for LinkedIn.", code="image_missing")
        try:
            r = await c.post(f"{API}/rest/images", params={"action": "initializeUpload"}, headers=_headers(creds.token),
                             json={"initializeUploadRequest": {"owner": self.author(creds)}})
        except httpx.HTTPError as exc:
            raise h.unreachable(exc) from None
        if r.status_code >= 300:
            raise error(r)
        value = h.json_of(r).get("value") or {}
        upload, image = value.get("uploadUrl"), value.get("image")
        if not upload or not image:
            raise h.PlatformError(h.TRANSIENT, "LinkedIn gave no place to upload the picture.", code="no_upload_url")
        try:
            put = await c.put(upload, content=data, headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "image/jpeg"})
        except httpx.HTTPError as exc:
            raise h.unreachable(exc) from None
        if put.status_code >= 300:
            raise error(put)
        return str(image)

    async def publish(self, creds: Creds, text: str, picture: Optional[Picture], *, container: Optional[str] = None,
                      progress=None) -> Published:
        async with h.client() as c:
            body = {
                "author": self.author(creds),
                "commentary": little(text),
                "visibility": "PUBLIC",
                "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
                "lifecycleState": "PUBLISHED",
                "isReshareDisabledByAuthor": False,
            }
            if picture is not None:
                body["content"] = {"media": {"id": await self._upload(c, creds, picture)}}
            try:
                r = await c.post(f"{API}/rest/posts", headers=_headers(creds.token), json=body)
            except httpx.HTTPError as exc:
                raise h.unreachable(exc, sent=True) from None
            if r.status_code >= 300:
                raise error(r)
            urn = r.headers.get("x-restli-id") or r.headers.get("x-linkedin-id") or ""
        if not urn:
            raise h.PlatformError(h.UNKNOWN, "LinkedIn took the post but gave no id.", code="no_id")
        return Published(external_id=urn, url=f"https://www.linkedin.com/feed/update/{urn}/")


class Member(_LinkedIn):
    platform = "linkedin"
    SCOPES = ("openid", "profile", "w_member_social")

    def author(self, creds: Creds) -> str:
        return f"urn:li:person:{creds.external_id}"

    async def sign_in(self, *, code: str, redirect_uri: str, verifier: Optional[str] = None) -> SignIn:
        body = await self._tokens({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
        token = body["access_token"]
        async with h.client() as c:
            me = h.json_of(await self._get(c, "/v2/userinfo", token))
        if not me.get("sub"):
            raise h.PlatformError(h.REFUSED, "LinkedIn did not say who signed in.", code="no_member")
        return SignIn(
            candidates=[Candidate(
                external_id=str(me["sub"]), name=me.get("name"), handle=None, picture=me.get("picture"), token=token,
                refresh_token=body.get("refresh_token"), expires_at=_expiry(body.get("expires_in")),
                refresh_expires_at=_expiry(body.get("refresh_token_expires_in")),
            )],
            connector_id=str(me["sub"]), scopes=body.get("scope"),
        )

    async def find_recent(self, creds: Creds, text: str, since: datetime) -> Optional[Published]:
        raise h.LookupUnsupported("LinkedIn does not let apps read a member's posts.")


class Page(_LinkedIn):
    platform = "linkedin_page"
    SCOPES = ("r_organization_admin", "w_organization_social", "r_organization_social")

    def author(self, creds: Creds) -> str:
        return f"urn:li:organization:{creds.external_id}"

    async def sign_in(self, *, code: str, redirect_uri: str, verifier: Optional[str] = None) -> SignIn:
        body = await self._tokens({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
        token = body["access_token"]
        found = []
        async with h.client() as c:
            acls = h.json_of(await self._get(c, "/rest/organizationAcls", token, {
                "q": "roleAssignee", "role": "ADMINISTRATOR", "state": "APPROVED", "count": 50,
            }))
            for row in acls.get("elements") or []:
                org = str((row or {}).get("organization") or "")
                org_id = org.rsplit(":", 1)[-1] if org.startswith("urn:li:organization:") else ""
                if not org_id or any(f.external_id == org_id for f in found):
                    continue
                try:
                    info = h.json_of(await self._get(c, f"/rest/organizations/{quote(org_id)}", token))
                except h.PlatformError:
                    info = {}
                found.append(Candidate(
                    external_id=org_id, name=info.get("localizedName"), handle=info.get("vanityName"), picture=None, token=token,
                    refresh_token=body.get("refresh_token"), expires_at=_expiry(body.get("expires_in")),
                    refresh_expires_at=_expiry(body.get("refresh_token_expires_in")),
                ))
        return SignIn(candidates=found, scopes=body.get("scope"), empty_reason=None if found else "not_page_admin")

    async def find_recent(self, creds: Creds, text: str, since: datetime) -> Optional[Published]:
        async with h.client() as c:
            r = await self._get(c, "/rest/posts", creds.token, {
                "author": self.author(creds), "q": "author", "count": 10, "sortBy": "CREATED",
            })
        floor = (since - timedelta(minutes=2)).timestamp() * 1000
        for p in h.json_of(r).get("elements") or []:
            if not isinstance(p, dict) or (p.get("createdAt") or 0) < floor:
                continue
            if same_text(text, p.get("commentary")):
                urn = str(p.get("id"))
                return Published(external_id=urn, url=f"https://www.linkedin.com/feed/update/{urn}/")
        return None


MEMBER = Member()
PAGE = Page()
