"""`Cache-Control: private` on an API answer to someone who said who they
are, when the route set no Cache-Control of its own.

A person's own answers (their tickets, their profile, their notifications,
an event page with their registration on it) went out with no
Cache-Control at all. Nothing in front of the API keeps a copy today.
nginx caches nothing, and under RFC 9111 3.5 a shared cache does not
store the answer to a request that carried Authorization. But a CDN put in
front of the API later must never keep one person's data and hand it to
another.

So an answer under /api/v1/ to a request carrying a credential, with no
Cache-Control, is marked `private`. A credential is an Authorization
header, the web's access_token cookie, a live room's browser or player key,
or a kiosk's token. The app and the browser may still keep the answer and
revalidate it by its ETag (etag_middleware.py); only shared caches may
not. A route that set its own policy keeps it: the public lists' `public,
max-age=300`, the exports' `no-store`, the live stream's `no-cache`.

Plain ASGI (see ProcessingTimeMiddleware): one header written into the
response's start, the body untouched.
"""
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

PREFIX = "/api/v1/"
#: Headers that say who is asking (app.core.auth, app.api.live, app.api.kiosk).
CREDENTIALS = frozenset({b"authorization", b"x-live-key", b"x-live-player", b"x-kiosk-token"})
#: The cookie the web's sign-in sets (app.api.auth.router.ACCESS_TOKEN_COOKIE_NAME).
SESSION_COOKIE = b"access_token"
PRIVATE = "private"


def carries_credentials(scope: Scope) -> bool:
    for name, value in scope.get("headers") or ():
        name = name.lower()
        if name in CREDENTIALS and value.strip():
            return True
        if name == b"cookie":
            for pair in value.split(b";"):
                key, _, token = pair.strip().partition(b"=")
                if key == SESSION_COOKIE and token:
                    return True
    return False


class PrivateCacheMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(PREFIX) or not carries_credentials(scope):
            await self.app(scope, receive, send)
            return

        async def private(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "cache-control" not in headers:
                    headers["Cache-Control"] = PRIVATE
            await send(message)

        await self.app(scope, receive, private)
