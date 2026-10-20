"""Cross-origin answers for the MCP endpoint and its OAuth endpoints.

The API's own CORS (doth.py) lets in the dashboards and the site, with their
cookies. An MCP client running in a browser page -- the MCP Inspector, a web
chat -- calls from an origin of its own, with a bearer token and no cookie:

- discovery, registration, token and revocation: any origin, never with
  credentials. Nothing there reads a cookie, and a client must be able to
  discover and connect from wherever it runs;
- /mcp: only origins in APP_MCP_ALLOWED_ORIGINS (the endpoint refuses the
  others itself, as the transport spec asks).

Plain ASGI, outside the API's CORS: on these paths it answers the preflight
itself and writes the response's CORS headers in place of any the inner one
set. Off (passing everything through) while APP_MCP_ENABLED is off.
"""
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings

_OPEN = ("/.well-known/oauth-protected-resource", "/.well-known/oauth-authorization-server", "/oauth/register", "/oauth/token", "/oauth/revoke")
_EXPOSE = "WWW-Authenticate, Retry-After, Mcp-Session-Id, Mcp-Protocol-Version"
_METHODS = "GET, POST, DELETE, OPTIONS"


def _kind(path: str) -> str | None:
    if path == "/mcp":
        return "mcp"
    if any(path == p or path.startswith(p + "/") for p in _OPEN):
        return "open"
    return None


class McpCorsMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = _kind(scope.get("path", "")) if scope["type"] == "http" and settings.MCP_ENABLED else None
        origin = Headers(scope=scope).get("origin") if kind else None
        if not kind or not origin:
            await self.app(scope, receive, send)
            return
        allowed = kind == "open" or origin.rstrip("/") in settings.mcp_allowed_origins
        headers = Headers(scope=scope)
        if scope["method"] == "OPTIONS" and headers.get("access-control-request-method"):
            if not allowed:
                await PlainTextResponse("Origin not allowed", status_code=403)(scope, receive, send)
                return
            await Response(status_code=204, headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": _METHODS,
                "Access-Control-Allow-Headers": headers.get("access-control-request-headers") or "Authorization, Content-Type",
                "Access-Control-Max-Age": "600",
                "Vary": "Origin",
            })(scope, receive, send)
            return

        async def with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                for name in [k for k in out.keys() if k.lower().startswith("access-control-")]:
                    del out[name]
                if allowed:
                    out["Access-Control-Allow-Origin"] = origin
                    out["Access-Control-Expose-Headers"] = _EXPOSE
                out["Vary"] = "Origin"
            await send(message)

        await self.app(scope, receive, with_cors)
