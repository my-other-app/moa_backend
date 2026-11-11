"""Where a change came from, for a webhook's `origin` (app/core/api_origin.py).

The partner API's auth dependency sets `api_key` or `partner_app` with the
client's id for its requests (`/partner/...`, left alone here). Every other
request gets its surface from `SurfaceMiddleware`, by what it carries:

- the Flutter app sends X-App-Version: `app`;
- a browser sends Origin (or Referer): the club dashboard's or the admin
  portal's address is `dashboard`; any other site (the public website, a
  club's own site) is `website`; the clubs' AI assistants (/mcp) act for the
  club's team: `dashboard`;
- neither, but signed in (an older app build): `app`; neither and not signed
  in (Razorpay's webhooks, the scanners' own calls): `system`.

Work outside a request -- a job, a sweep -- has none and is `system`.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from app.config import settings
from app.core.api_origin import Origin, current_origin, reset_origin, set_origin

SYSTEM = Origin(type="system")


def _host(url: Optional[str]) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _dashboard_hosts() -> set[str]:
    hosts = {_host(settings.PUBLIC_WEB_URL), _host(settings.PUBLIC_ADMIN_URL), _host(getattr(settings, "MCP_DASHBOARD_URL", ""))}
    return {h for h in hosts if h}


def surface(path: str, headers: dict[str, str]) -> Optional[str]:
    """The surface of a request, by its path and headers (lower-case names)."""
    if path.startswith("/partner/"):
        return None
    if path.startswith("/mcp"):
        return "dashboard"
    if headers.get("x-app-version"):
        return "app"
    page = headers.get("origin") or headers.get("referer")
    host = _host(page)
    if host:
        return "dashboard" if host in _dashboard_hosts() else "website"
    if (headers.get("authorization") or "").lower().startswith("bearer "):
        return "app"
    return "system"


class SurfaceMiddleware:
    """Pure ASGI: sets the request's origin for emit, unless the partner API
    sets its own."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        wanted = {b"x-app-version", b"origin", b"referer", b"authorization"}
        headers = {}
        for name, value in scope.get("headers") or ():
            if name in wanted:
                headers[name.decode("latin-1")] = value.decode("latin-1")
        kind = surface(scope.get("path") or "", headers)
        if kind is None:
            await self.app(scope, receive, send)
            return
        token = set_origin(Origin(type=kind))
        try:
            await self.app(scope, receive, send)
        finally:
            reset_origin(token)


def resolve(origin: Optional[Origin]) -> Origin:
    """The origin a change is written with: the caller's when it knows
    better, else the request's, else `system`."""
    if origin is not None:
        return origin
    return current_origin() or SYSTEM
