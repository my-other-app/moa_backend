from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADERS = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


class SecurityHeadersMiddleware:
    """Adds baseline security headers to every response.

    Deliberately does not set a Content-Security-Policy: this API also
    serves the Swagger UI at /docs (loads CDN JS/CSS) and renders HTML
    emails/PDFs, and a CSP tight enough to be meaningful needs per-route
    tuning that's out of scope here.

    Plain ASGI (see ProcessingTimeMiddleware for why): the headers are
    written into the response's start, the body passes untouched.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secured(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, secured)
