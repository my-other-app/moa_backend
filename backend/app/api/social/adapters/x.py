"""X (Twitter): API v2, OAuth 2.0 with PKCE as a confidential client
(X_CLIENT_ID, X_CLIENT_SECRET), pay per use.

Sign-in: x.com/i/oauth2/authorize with `tweet.read tweet.write users.read
media.write offline.access` and an S256 challenge; the code (with its
verifier) for tokens at POST /2/oauth2/token, Basic-authenticated. An access
token lasts two hours; the refresh token (offline.access) is replaced on
every refresh, so a refresh is done holding the account's row.

Posting: POST /2/media/upload (multipart: `media`, `media_category`
tweet_image) gives the media id; POST /2/tweets with `media.media_ids`. The
post's address is x.com/{username}/status/{id}. A lost answer is looked up
in GET /2/users/{id}/tweets since the attempt (X rewrites links to t.co, so
the words are compared without them).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.api.social import http as h
from app.api.social.adapters.base import Candidate, Creds, Picture, Published, SignIn, Tokens, same_text
from app.config import settings

AUTHORIZE = "https://x.com/i/oauth2/authorize"
API = "https://api.x.com"
VERSION = "X API v2"
SCOPES = ("tweet.read", "tweet.write", "users.read", "media.write", "offline.access")


def new_verifier() -> str:
    """A PKCE code verifier: 43 to 128 unreserved characters."""
    return secrets.token_urlsafe(64)[:96]


def challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _basic() -> dict:
    raw = f"{(settings.X_CLIENT_ID or '').strip()}:{(settings.X_CLIENT_SECRET or '').strip()}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def error(r: httpx.Response) -> h.PlatformError:
    body = h.json_of(r)
    message = body.get("detail") or body.get("title") or body.get("error_description") or f"X answered {r.status_code}."
    errors = body.get("errors") if isinstance(body.get("errors"), list) else []
    if not body.get("detail") and errors and isinstance(errors[0], dict):
        message = errors[0].get("message") or message
    if r.status_code == 429:
        reset = r.headers.get("x-rate-limit-reset")
        wait = None
        try:
            wait = max(0.0, float(reset) - datetime.now(timezone.utc).timestamp()) if reset else None
        except ValueError:
            pass
        return h.PlatformError(h.RATE, message, code="x_rate_limited", status=429, retry_after=wait)
    if r.status_code == 403 and "duplicate" in str(message).lower():
        return h.PlatformError(h.REFUSED, "X refused a post it already has (the same words).", code="x_duplicate", status=403)
    if r.status_code == 400 and body.get("error") == "invalid_grant":
        return h.PlatformError(h.AUTH, message, code="x_invalid_grant", status=400)
    return h.by_status(r, message, f"x_{r.status_code}")


def _expiry(seconds) -> Optional[datetime]:
    try:
        n = int(seconds)
    except (TypeError, ValueError):
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=n) if n > 0 else None


class XAdapter:
    platform = "x"
    SCOPES = SCOPES

    @staticmethod
    def api_version() -> str:
        return VERSION

    def authorize_url(self, *, state: str, redirect_uri: str, challenge: Optional[str] = None) -> str:
        params = {
            "response_type": "code", "client_id": (settings.X_CLIENT_ID or "").strip(), "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge or "", "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE}?{urlencode(params)}"

    async def _token(self, data: dict) -> dict:
        async with h.client() as c:
            try:
                r = await c.post(f"{API}/2/oauth2/token", data={**data, "client_id": (settings.X_CLIENT_ID or "").strip()}, headers=_basic())
            except httpx.HTTPError as exc:
                raise h.unreachable(exc) from None
        if r.status_code >= 300:
            raise error(r)
        body = h.json_of(r)
        if not body.get("access_token"):
            raise h.PlatformError(h.REFUSED, "X answered without a token.", code="no_token")
        return body

    async def sign_in(self, *, code: str, redirect_uri: str, verifier: Optional[str] = None) -> SignIn:
        body = await self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier or ""})
        token = body["access_token"]
        async with h.client() as c:
            try:
                r = await c.get(f"{API}/2/users/me", params={"user.fields": "profile_image_url,username,name"}, headers=_bearer(token))
            except httpx.HTTPError as exc:
                raise h.unreachable(exc) from None
        if r.status_code >= 300:
            raise error(r)
        me = h.json_of(r).get("data") or {}
        if not me.get("id"):
            raise h.PlatformError(h.REFUSED, "X did not say who signed in.", code="no_user")
        return SignIn(
            candidates=[Candidate(
                external_id=str(me["id"]), name=me.get("name"), handle=me.get("username"), picture=me.get("profile_image_url"),
                token=token, refresh_token=body.get("refresh_token"), expires_at=_expiry(body.get("expires_in")),
            )],
            connector_id=str(me["id"]), scopes=body.get("scope"),
        )

    async def refresh(self, creds: Creds) -> Optional[Tokens]:
        if not creds.refresh_token:
            return None
        body = await self._token({"grant_type": "refresh_token", "refresh_token": creds.refresh_token})
        return Tokens(token=body["access_token"], refresh_token=body.get("refresh_token") or creds.refresh_token,
                      expires_at=_expiry(body.get("expires_in")))

    async def revoke(self, creds: Creds) -> None:
        async with h.client() as c:
            for token, hint in ((creds.token, "access_token"), (creds.refresh_token, "refresh_token")):
                if not token:
                    continue
                try:
                    await c.post(f"{API}/2/oauth2/revoke", data={"token": token, "token_type_hint": hint,
                                                                 "client_id": (settings.X_CLIENT_ID or "").strip()}, headers=_basic())
                except httpx.HTTPError:
                    pass

    async def publish(self, creds: Creds, text: str, picture: Optional[Picture], *, container: Optional[str] = None,
                      progress=None) -> Published:
        from app.core.storage import objects

        async with h.client() as c:
            body: dict = {"text": text}
            if picture is not None:
                data = await asyncio.to_thread(objects.get_bytes, picture.key)
                if not data:
                    raise h.PlatformError(h.IMAGE, "The picture could not be read for X.", code="image_missing")
                try:
                    r = await c.post(f"{API}/2/media/upload", headers=_bearer(creds.token),
                                     files={"media": ("poster.jpg", data, "image/jpeg")}, data={"media_category": "tweet_image"})
                except httpx.HTTPError as exc:
                    raise h.unreachable(exc) from None
                if r.status_code >= 300:
                    raise error(r)
                media = (h.json_of(r).get("data") or {}).get("id")
                if not media:
                    raise h.PlatformError(h.TRANSIENT, "X took the picture without giving its id.", code="no_media_id")
                body["media"] = {"media_ids": [str(media)]}
            try:
                r = await c.post(f"{API}/2/tweets", headers=_bearer(creds.token), json=body)
            except httpx.HTTPError as exc:
                raise h.unreachable(exc, sent=True) from None
            if r.status_code >= 300:
                raise error(r)
        tweet = (h.json_of(r).get("data") or {}).get("id")
        if not tweet:
            raise h.PlatformError(h.UNKNOWN, "X took the post without giving its id.", code="no_id")
        who = creds.handle or "i"
        return Published(external_id=str(tweet), url=f"https://x.com/{who}/status/{tweet}")

    async def find_recent(self, creds: Creds, text: str, since: datetime) -> Optional[Published]:
        start = (since - timedelta(minutes=2)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with h.client() as c:
            try:
                r = await c.get(f"{API}/2/users/{creds.external_id}/tweets", headers=_bearer(creds.token),
                                params={"start_time": start, "max_results": 10, "tweet.fields": "created_at"})
            except httpx.HTTPError as exc:
                raise h.unreachable(exc) from None
        if r.status_code >= 300:
            raise error(r)
        for t in h.json_of(r).get("data") or []:
            if isinstance(t, dict) and same_text(text, t.get("text")):
                who = creds.handle or "i"
                return Published(external_id=str(t.get("id")), url=f"https://x.com/{who}/status/{t.get('id')}")
        return None


X = XAdapter()
