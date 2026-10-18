import time

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class ProcessingTimeMiddleware:
    """X-Process-Time-MS on every response: the time to its first byte.

    Plain ASGI, not BaseHTTPMiddleware: that one runs the app in a task of its
    own and hands every chunk of the body across a memory stream, which a
    streamed response (the live room's server-sent events) pays per frame and
    which held a frame or two of its own -- a phone that stopped reading still
    had frames made for it -- and could not tell the app the client was gone.
    This only writes a header into the response's start."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.time()

        async def timed(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Process-Time-MS"] = str(round((time.time() - start) * 1000, 2))
            await send(message)

        await self.app(scope, receive, timed)
