import time
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp


class ProcessingTimeMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        start_time = time.time()

        response = await call_next(request)

        processing_time = (time.time() - start_time) * 1000

        response.headers["X-Process-Time-MS"] = str(round(processing_time, 2))

        return response
