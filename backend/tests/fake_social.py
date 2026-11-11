"""In-memory Facebook (Graph API), Instagram, LinkedIn and X, for the suite.

`install(monkeypatch)` saves every platform's settings (made-up app ids and
secrets, a fresh SOCIAL_KEYS_SECRET), points app/api/social/http.py's
transport at this fake through httpx's MockTransport and returns it. Nothing
reaches the network: a request the fake does not know fails the test.

Each call is recorded (`calls`, with what was sent) so a test can check the
request's shape; `fail(method, path, ...)` makes the next matching call
answer an error (or raise), as the platform would.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs

import httpx
from cryptography.fernet import Fernet

from app.api.social import http as social_http
from app.config import settings

META_APP_ID, META_APP_SECRET = "test-meta-app", "test-meta-secret"
LI_ID, LI_SECRET = "test-li-client", "test-li-secret"
X_ID, X_SECRET = "test-x-client", "test-x-secret"
#: The tokens the fake hands out: a test asserts none of them ever leaves.
TOKENS = ("SHORT-USER-TOKEN", "LONG-USER-TOKEN", "PAGE-TOKEN-1", "PAGE-TOKEN-2", "LI-ACCESS-TOKEN", "LI-REFRESH-TOKEN",
          "X-ACCESS-TOKEN", "X-REFRESH-TOKEN", "X-ACCESS-TOKEN-2", "X-REFRESH-TOKEN-2")


@dataclass
class Call:
    method: str
    host: str
    path: str
    params: dict
    form: dict
    json: Optional[dict]
    headers: dict
    raw: bytes = b""


@dataclass
class Fake:
    calls: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    pages: list = field(default_factory=lambda: [
        {"id": "page-1", "name": "Circuit Society", "access_token": "PAGE-TOKEN-1", "tasks": ["CREATE_CONTENT", "MANAGE"],
         "picture": {"data": {"url": "https://fb.example/p1.jpg"}},
         "instagram_business_account": {"id": "ig-1", "username": "circuitsoc", "name": "Circuit Society", "profile_picture_url": "https://ig.example/1.jpg"}},
    ])
    granted: Optional[list] = None
    container_status: list = field(default_factory=lambda: ["FINISHED"])
    ig_quota: int = 0
    fb_posts: list = field(default_factory=list)
    ig_media: list = field(default_factory=list)
    li_posts: list = field(default_factory=list)
    tweets: list = field(default_factory=list)
    li_orgs: list = field(default_factory=lambda: ["urn:li:organization:777"])
    x_refreshes: int = 0
    n: int = 0

    def fail(self, method: str, path: str, status: int = 500, body: Optional[dict] = None, *, raise_: Optional[Exception] = None,
             times: int = 1, headers: Optional[dict] = None) -> None:
        """The next `times` calls of `method` to a path matching `path` (a regex) answer this."""
        for _ in range(times):
            self.failures.append((method, re.compile(path), status, body, raise_, headers or {}))

    def by(self, method: str, path: str) -> list[Call]:
        rx = re.compile(path)
        return [c for c in self.calls if c.method == method and rx.search(c.path)]

    def sent_anything(self) -> bool:
        return any(c.method in ("POST", "PUT") and (c.path.endswith(("/photos", "/feed", "/media", "/media_publish")) or "/rest/posts" in c.path
                                                    or c.path == "/2/tweets") for c in self.calls)

    def _id(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.n}"

    # ── The platforms ───────────────────────────────────────────────────

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        params = {k: v for k, v in request.url.params.items()}
        raw = request.content or b""
        ctype = request.headers.get("content-type", "")
        form, body = {}, None
        if raw and "application/x-www-form-urlencoded" in ctype:
            form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
        elif raw and "application/json" in ctype:
            body = json.loads(raw)
        self.calls.append(Call(request.method, host, path, params, form, body, dict(request.headers), raw))
        for i, (method, rx, status, fbody, exc, headers) in enumerate(self.failures):
            if method == request.method and rx.search(path):
                self.failures.pop(i)
                if exc is not None:
                    raise exc
                return httpx.Response(status, json=fbody or {}, headers=headers)
        if host == "graph.facebook.com":
            return self._graph(request.method, path, params, form)
        if host in ("www.linkedin.com", "api.linkedin.com"):
            return self._linkedin(request.method, host, path, params, form, body)
        if host == "api.x.com":
            return self._x(request.method, path, params, form, body, raw)
        raise AssertionError(f"the fake platforms do not know {request.method} {host}{path}")

    def _graph(self, method, path, params, form):
        assert path.startswith("/v26.0/"), path
        p = path[len("/v26.0/"):]
        ok = lambda body: httpx.Response(200, json=body)  # noqa: E731
        if p == "oauth/access_token":
            if params.get("grant_type") == "fb_exchange_token":
                assert params["fb_exchange_token"] == "SHORT-USER-TOKEN"
                return ok({"access_token": "LONG-USER-TOKEN", "token_type": "bearer", "expires_in": 5183944})
            assert params["client_secret"] == META_APP_SECRET and params["code"]
            return ok({"access_token": "SHORT-USER-TOKEN", "token_type": "bearer", "expires_in": 3600})
        if p == "me":
            return ok({"id": "meta-user-1", "name": "Asha Nair"})
        if p == "me/permissions":
            granted = self.granted if self.granted is not None else [
                "pages_show_list", "pages_manage_posts", "pages_read_engagement", "instagram_basic", "instagram_content_publish"]
            return ok({"data": [{"permission": g, "status": "granted"} for g in granted]})
        if p == "me/accounts":
            return ok({"data": self.pages})
        parts = p.split("/")
        if method == "DELETE" and len(parts) == 2 and parts[1] == "permissions":
            return ok({"success": True})
        if method == "POST" and len(parts) == 2 and parts[1] in ("photos", "feed"):
            post_id = f"{parts[0]}_{self._id('')}"
            self.fb_posts.append({"id": post_id, "message": form.get("message"), "created_time": "2026-11-11T10:00:00+0000",
                                  "permalink_url": f"https://www.facebook.com/{post_id}"})
            return ok({"id": self._id("photo-"), "post_id": post_id} if parts[1] == "photos" else {"id": post_id})
        if method == "GET" and len(parts) == 2 and parts[1] == "published_posts":
            return ok({"data": list(self.fb_posts)})
        if method == "POST" and len(parts) == 2 and parts[1] == "media":
            return ok({"id": self._id("container-")})
        if method == "POST" and len(parts) == 2 and parts[1] == "media_publish":
            media = self._id("igmedia-")
            self.ig_media.append({"id": media, "caption": self._caption, "timestamp": "2099-01-01T00:00:00+0000",
                                  "permalink": f"https://www.instagram.com/p/{media}/"})
            return ok({"id": media})
        if method == "GET" and len(parts) == 2 and parts[1] == "content_publishing_limit":
            return ok({"data": [{"quota_usage": self.ig_quota, "config": {"quota_total": 100, "quota_duration": 86400}}]})
        if method == "GET" and len(parts) == 2 and parts[1] == "media":
            return ok({"data": list(self.ig_media)})
        if method == "GET" and len(parts) == 1:
            fields = params.get("fields", "")
            if "status_code" in fields:
                status = self.container_status.pop(0) if len(self.container_status) > 1 else self.container_status[0]
                return ok({"id": parts[0], "status_code": status})
            if "permalink_url" in fields:
                return ok({"id": parts[0], "permalink_url": f"https://www.facebook.com/{parts[0]}"})
            if "permalink" in fields:
                return ok({"id": parts[0], "permalink": f"https://www.instagram.com/p/{parts[0]}/"})
        raise AssertionError(f"the fake Graph API does not know {method} {path}")

    @property
    def _caption(self) -> Optional[str]:
        made = self.by("POST", r"/media$")
        return made[-1].form.get("caption") if made else None

    def _linkedin(self, method, host, path, params, form, body):
        if host == "www.linkedin.com":
            if path == "/oauth/v2/accessToken":
                assert form["client_secret"] == LI_SECRET
                if form["grant_type"] == "refresh_token":
                    return httpx.Response(200, json={"access_token": "LI-ACCESS-TOKEN", "expires_in": 5184000, "refresh_token": "LI-REFRESH-TOKEN"})
                return httpx.Response(200, json={"access_token": "LI-ACCESS-TOKEN", "expires_in": 5184000, "scope": form.get("scope", "")})
            if path == "/oauth/v2/revoke":
                return httpx.Response(200, json={})
            if path.startswith("/dms-uploads/"):
                return httpx.Response(201)
        if path == "/v2/userinfo":
            return httpx.Response(200, json={"sub": "li-member-9", "name": "Asha Nair", "picture": "https://li.example/a.jpg"})
        if path == "/rest/organizationAcls":
            return httpx.Response(200, json={"elements": [{"organization": o, "role": "ADMINISTRATOR", "state": "APPROVED"} for o in self.li_orgs]})
        if path.startswith("/rest/organizations/"):
            return httpx.Response(200, json={"localizedName": "Circuit Society", "vanityName": "circuit-society"})
        if path == "/rest/images" and params.get("action") == "initializeUpload":
            return httpx.Response(200, json={"value": {"uploadUrl": "https://www.linkedin.com/dms-uploads/up1", "image": "urn:li:image:IMG1"}})
        if path == "/rest/posts" and method == "POST":
            urn = f"urn:li:share:{self._id('7')}"
            self.li_posts.append({"id": urn, "commentary": body.get("commentary"), "createdAt": 4102444800000})
            return httpx.Response(201, headers={"x-restli-id": urn})
        if path == "/rest/posts" and method == "GET":
            return httpx.Response(200, json={"elements": list(self.li_posts)})
        raise AssertionError(f"the fake LinkedIn does not know {method} {host}{path}")

    def _x(self, method, path, params, form, body, raw):
        if path == "/2/oauth2/token":
            if form.get("grant_type") == "refresh_token":
                self.x_refreshes += 1
                return httpx.Response(200, json={"access_token": "X-ACCESS-TOKEN-2", "refresh_token": "X-REFRESH-TOKEN-2", "expires_in": 7200})
            assert form.get("code_verifier"), "the PKCE verifier goes with the code"
            return httpx.Response(200, json={"access_token": "X-ACCESS-TOKEN", "refresh_token": "X-REFRESH-TOKEN", "expires_in": 7200,
                                             "scope": "tweet.read tweet.write users.read media.write offline.access"})
        if path == "/2/oauth2/revoke":
            return httpx.Response(200, json={"revoked": True})
        if path == "/2/users/me":
            return httpx.Response(200, json={"data": {"id": "x-user-5", "name": "Circuit Society", "username": "circuitsoc",
                                                      "profile_image_url": "https://x.example/a.jpg"}})
        if path == "/2/media/upload":
            assert b'name="media_category"' in raw and b"tweet_image" in raw, "the picture goes as a tweet_image"
            return httpx.Response(200, json={"data": {"id": "18800000001", "media_key": "3_18800000001"}})
        if path == "/2/tweets" and method == "POST":
            tid = self._id("19000")
            self.tweets.append({"id": tid, "text": body["text"]})
            return httpx.Response(201, json={"data": {"id": tid, "text": body["text"]}})
        if re.match(r"^/2/users/[^/]+/tweets$", path):
            return httpx.Response(200, json={"data": list(self.tweets)})
        raise AssertionError(f"the fake X does not know {method} {path}")


def install(monkeypatch, *, linkedin_pages: bool = True) -> Fake:
    fake = Fake()
    monkeypatch.setattr(settings, "SOCIAL_KEYS_SECRET", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "META_APP_ID", META_APP_ID)
    monkeypatch.setattr(settings, "META_APP_SECRET", META_APP_SECRET)
    monkeypatch.setattr(settings, "META_LOGIN_CONFIG_ID", "")
    monkeypatch.setattr(settings, "LINKEDIN_CLIENT_ID", LI_ID)
    monkeypatch.setattr(settings, "LINKEDIN_CLIENT_SECRET", LI_SECRET)
    monkeypatch.setattr(settings, "LINKEDIN_PAGES_ENABLED", linkedin_pages)
    monkeypatch.setattr(settings, "X_CLIENT_ID", X_ID)
    monkeypatch.setattr(settings, "X_CLIENT_SECRET", X_SECRET)
    monkeypatch.setattr(settings, "SOCIAL_GRAPH_VERSION", "v26.0")
    monkeypatch.setattr(social_http, "TRANSPORT", httpx.MockTransport(fake.handler))
    return fake
