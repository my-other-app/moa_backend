import asyncio
import gc
import traceback
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import HTMLResponse
from starlette import status
from sqlalchemy.exc import StatementError
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.router import api_router
from app.config import settings
from app.db.listeners import *
from app.response import ErrorResponse, CustomHTTPException
from app.core.utils.discord import notify_error
from app.core.middlewares.process_time_middleware import ProcessingTimeMiddleware
from app.core.middlewares.security_headers_middleware import SecurityHeadersMiddleware
from app.core.rate_limit import limiter
from app.core import redact
# Tells the public site when an event's or a club's page changes (inert without APP_SITE_REVALIDATE_SECRET).
from app.core import site_revalidate  # noqa: F401

# A club webhook's token never reaches the access log whole.
redact.install()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: seed badge definitions
    try:
        from app.db.core import AsyncSessionLocal
        from app.api.badges.seed import seed_badges
        async with AsyncSessionLocal() as session:
            await seed_badges(session)
    except Exception as e:
        print(f"Warning: Badge seeding failed: {e}")
    # Timed journey emails: one sweep a minute, one worker at a time.
    from app.api.emails.service import sweep_loop

    sweeper = asyncio.create_task(sweep_loop())
    from app.api.closeout.service import sweep_loop as closeout_sweep_loop

    closer = asyncio.create_task(closeout_sweep_loop())
    from app.api.live.service import sweep_loop as feedback_sweep_loop

    feedback = asyncio.create_task(feedback_sweep_loop())
    # Billing: graces running out and trials about to bill, once an hour.
    from app.api.subscriptions.sweep import sweep_loop as billing_sweep_loop

    billing = asyncio.create_task(billing_sweep_loop())
    # What import and startup built (models, routes, schemas, the S3 service
    # model: some 740,000 objects) lives as long as the process. Frozen out
    # of the collector's reach, a full collection no longer walks all of it:
    # on the discover benchmark those walks were the 100+ ms tail of requests
    # that allocate a lot (scripts/bench/RESULTS.md).
    gc.collect()
    gc.freeze()
    yield
    sweeper.cancel()
    closer.cancel()
    feedback.cancel()
    billing.cancel()


application = FastAPI(default_response_class=ORJSONResponse, lifespan=lifespan)

application.state.limiter = limiter
application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

application.include_router(router=api_router)
# The short tracking link lives at the root: api.myotherapp.in/l/<code> fits on a poster.
from app.api.links.router import public_router as public_links_router  # noqa: E402

application.include_router(router=public_links_router, tags=["tracking-links"])
# Innermost: each request's plan-gate scope, so one request resolves a club's
# entitlements once and knows whether an admin is behind it
# (app/api/subscriptions/gates.py).
from app.api.subscriptions.gates import RequestScope as PlanGateScope  # noqa: E402

application.add_middleware(PlanGateScope)
# print(settings.cors_origins)
# if settings.DEBUG:
application.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
application.add_middleware(ProcessingTimeMiddleware)
application.add_middleware(SecurityHeadersMiddleware)
application.add_middleware(SlowAPIMiddleware)
# Outermost, so the rate limiter and everything after it see the caller
# rather than Docker's bridge address (app.core.net).
from app.core.net import ClientAddressMiddleware  # noqa: E402

application.add_middleware(ClientAddressMiddleware)

# @application.exception_handler(ValidationError)
# async def validation_exception_handler2(request, exc):
#     errors = {}

#     for error in exc.errors():
#         current = errors

#         if len(error["loc"]) <= 1:
#             current[error["loc"][0]] = error["msg"]
#             break

#         keys = error["loc"][1:]
#         for loc in keys[:-1]:
#             current = current.setdefault(loc, {})
#         current[keys[-1]] = error["msg"]

#     return ErrorResponse(
#         message="Invalid request",
#         errors=errors,
#     ).get_response(status.HTTP_422_UNPROCESSABLE_ENTITY)


@application.exception_handler(Exception)
async def http_exception_handler(request: Request, exc: Exception):
    track_id = str(uuid.uuid4())
    try:
        await notify_error(request, exc, track_id)
    except Exception as e:
        print("Error while sending error notification")
        traceback.print_exc()
    return ErrorResponse(
        message="Internal Server Error",
        errors={"error": "An error occurred while processing the request"},
        track_id=track_id,
    ).get_response(status.HTTP_500_INTERNAL_SERVER_ERROR)


@application.exception_handler(StatementError)
async def statement_error_handler(request: Request, exc: StatementError):
    if isinstance(exc.orig, CustomHTTPException):
        raise exc.orig
    else:
        raise exc


@application.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    errors = {}

    for error in exc.errors():
        current = errors

        if len(error["loc"]) <= 1:
            current[error["loc"][0]] = error["msg"]
            break

        keys = error["loc"][1:]
        for loc in keys[:-1]:
            current = current.setdefault(loc, {})
        current[keys[-1]] = error["msg"]

    return ErrorResponse(
        message="Invalid request",
        errors=errors,
    ).get_response(status.HTTP_422_UNPROCESSABLE_ENTITY)


@application.exception_handler(CustomHTTPException)
async def http_exception_handler(request: Request, exc: CustomHTTPException):
    return exc.get_response(exc.status_code)


@application.head("/ping")
async def ping():
    return HTMLResponse(content=None, status_code=status.HTTP_204_NO_CONTENT)
