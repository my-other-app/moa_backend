import asyncio
import gc
import logging
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import HTMLResponse
from starlette import status
from sqlalchemy.exc import IntegrityError, StatementError
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.router import api_router
from app.config import settings
from app.db.listeners import *
from app.response import ErrorResponse, CustomHTTPException
from app.core.utils.discord import report_error
from app.core.middlewares.process_time_middleware import ProcessingTimeMiddleware
from app.core.middlewares.security_headers_middleware import SecurityHeadersMiddleware
from app.core.rate_limit import limiter
from app.core import redact

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
    # Every worker's pool against the server's max_connections: a warning
    # in the log when WORKERS x (pool + overflow) could ask for more.
    from app.db.core import check_connection_budget

    await check_connection_budget()
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
    # The going/follower counter check: every six hours, on one process.
    from app.api.discover.counters import check_loop as counter_check_loop

    counters = asyncio.create_task(counter_check_loop())

    # Clubs' own domains: pending ones polled at Cloudflare, plans followed, every five minutes.
    from app.api.club_domains.sweep import sweep_loop as domains_sweep_loop

    domains = asyncio.create_task(domains_sweep_loop())
    # Spent sessions and old sign-in attempts out, hourly, on one process.
    from app.api.auth.retention import sweep_loop as account_sweep_loop

    accounts = asyncio.create_task(account_sweep_loop())
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
    counters.cancel()
    domains.cancel()
    accounts.cancel()


# No default_response_class: FastAPI's own JSON response is the default. It
# was ORJSONResponse, which FastAPI now deprecates -- it warned in the log on
# every response -- because with a response model or a return type FastAPI
# writes the JSON bytes itself through Pydantic, faster than orjson after
# jsonable_encoder, and a custom class turned that path off.
application = FastAPI(lifespan=lifespan)

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
async def unhandled_exception_handler(request: Request, exc: Exception):
    """The 500. The Discord report goes in the background (throttled, one
    per kind of error and path at a time): it used to be awaited here, and a
    500 took as long as two tries at the webhook did."""
    track_id = str(uuid.uuid4())
    try:
        report_error(request, exc, track_id)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("Error while scheduling the error report", exc_info=True)
    return ErrorResponse(
        message="Internal Server Error",
        errors={"error": "An error occurred while processing the request"},
        track_id=track_id,
    ).get_response(status.HTTP_500_INTERNAL_SERVER_ERROR)


#: What a unique, foreign-key or check violation says to the caller. Never the
#: constraint's name or the row: those describe the schema and other people's data.
_CONFLICTS = {
    "23505": "That already exists.",
    "23503": "That refers to something that does not exist, or is still in use.",
    "23502": "A required value is missing.",
    "23514": "That value is not allowed.",
    "23P01": "That conflicts with something that already exists.",
}


@application.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):
    """A constraint the database held that the code did not check first --
    most often two requests racing to make the same thing -- is a 409, not a
    500 with a Discord report."""
    from app.db.errors import constraint_of, sqlstate_of

    code = sqlstate_of(exc)
    logging.getLogger(__name__).warning(
        "integrity error on %s %s: %s (%s)", request.method, request.url.path, code, constraint_of(exc)
    )
    return ErrorResponse(
        message=_CONFLICTS.get(code or "", "That conflicts with something that already exists."),
        error_code="CONFLICT",
    ).get_response(status.HTTP_409_CONFLICT)


@application.exception_handler(StatementError)
async def statement_error_handler(request: Request, exc: StatementError):
    """A refusal raised while SQLAlchemy was building a statement (a column
    type checking its value) arrives wrapped. It used to be re-raised from
    here, which Starlette does not hand to another handler: it became a bare
    500. Now the wrapped refusal is answered, and anything else is the 500."""
    if isinstance(exc.orig, CustomHTTPException):
        return exc.orig.get_response(exc.orig.status_code)
    return await unhandled_exception_handler(request, exc)


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
async def custom_http_exception_handler(request: Request, exc: CustomHTTPException):
    return exc.get_response(exc.status_code)


@application.head("/ping")
async def ping():
    return HTMLResponse(content=None, status_code=status.HTTP_204_NO_CONTENT)
