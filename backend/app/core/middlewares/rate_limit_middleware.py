"""slowapi's SlowAPIMiddleware, as plain ASGI.

The same checks, in the same order, with slowapi's own helpers: nothing when
the limiter is off or the route is exempt or limits itself (its decorator
does), else the default limits, answered with the limiter's exception
handler; the rate-limit headers, when the limiter writes them, go into the
response's start. What it no longer does is wrap the app in
BaseHTTPMiddleware (see ProcessingTimeMiddleware for what that cost).

slowapi's own ASGI version (SlowAPIASGIMiddleware) sends the response's
start again before every chunk of a body, which breaks any streamed
response; hence this one.
"""
from slowapi.middleware import _find_route_handler, _should_exempt, sync_check_limits
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        app = scope["app"]
        limiter = app.state.limiter
        if not limiter.enabled:
            await self.app(scope, receive, send)
            return
        handler = _find_route_handler(app.routes, scope)
        if _should_exempt(limiter, handler):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        error_response, inject_headers = sync_check_limits(limiter, request, handler, app)
        if error_response is not None:
            await error_response(scope, receive, send)
            return
        if not inject_headers:
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                limiter._inject_asgi_headers(MutableHeaders(scope=message), request.state.view_rate_limit)
            await send(message)

        await self.app(scope, receive, with_headers)
