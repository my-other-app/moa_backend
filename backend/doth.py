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

# Error tracking starts before any route is built: Sentry's integrations wrap
# routes as they are made. Off, and sentry_sdk never imported, unless
# APP_SENTRY_DSN is set (app/core/observability.py).
from app.core import observability

observability.init_sentry()

from app.api.router import api_router  # noqa: E402
from app.config import settings
from app.db.listeners import *
from app.response import ErrorResponse, CustomHTTPException
from app.core.utils.discord import report_error
from app.core.middlewares.etag_middleware import ETagMiddleware
from app.core.middlewares.private_cache_middleware import PrivateCacheMiddleware
from app.core.middlewares.process_time_middleware import ProcessingTimeMiddleware
from app.core.middlewares.security_headers_middleware import SecurityHeadersMiddleware
from app.core.middlewares.rate_limit_middleware import RateLimitMiddleware
from app.core.rate_limit import limiter
from app.core import redact
# Tells the public site when an event's or a club's page changes (inert without APP_SITE_REVALIDATE_SECRET).
from app.core import site_revalidate  # noqa: F401

# A club webhook's token never reaches the access log whole.
redact.install()


def start_loops() -> list[asyncio.Task]:
    """The leader-only loops: each takes an advisory lock on every pass, so
    however many processes start them, one runs each pass. Started here by
    the API, or by the worker under APP_SWEEPS_IN_WORKER (app/worker.py)."""
    # Timed journey emails: one sweep a minute, one worker at a time.
    from app.api.emails.service import sweep_loop
    from app.api.closeout.service import sweep_loop as closeout_sweep_loop
    from app.api.live.service import sweep_loop as feedback_sweep_loop
    # Billing: graces running out and trials about to bill, once an hour.
    from app.api.subscriptions.sweep import sweep_loop as billing_sweep_loop
    # The going/follower counter check: every six hours, on one process.
    from app.api.discover.counters import check_loop as counter_check_loop
    # Clubs' own domains: pending ones polled at Cloudflare, plans followed, every five minutes.
    from app.api.club_domains.sweep import sweep_loop as domains_sweep_loop
    # Store holds that ran out given back, every minute.
    from app.api.store.sweep import sweep_loop as store_sweep_loop
    # Payments: open orders reconciled with Razorpay, unapplied captures
    # repaired, automatic refunds retried -- every ten minutes, one worker.
    from app.api.payments.sweep import sweep_loop as payments_sweep_loop
    # Spent sessions and old sign-in attempts out, hourly, on one process.
    from app.api.auth.retention import sweep_loop as account_sweep_loop
    # The money ledger, once a day on one process: failed postings retried,
    # the monthly sums checked against the entries, and yesterday's payments
    # and refunds at Razorpay against the ledger. Its postings are made in
    # each money transition's own transaction, never here or in a job: this
    # only repairs and checks them.
    from app.api.ledger.jobs import daily_loop as ledger_daily_loop
    # Search: the outbox drained (to Meilisearch when APP_SEARCH_URL is set;
    # taxonomy renames into search_text always), the indexes built and
    # checked -- every second or five, on one process (app.api.search.sync).
    from app.api.search.sync import loop as search_sync_loop

    return [
        asyncio.create_task(loop())
        for loop in (
            sweep_loop, closeout_sweep_loop, feedback_sweep_loop, billing_sweep_loop, counter_check_loop,
            domains_sweep_loop, store_sweep_loop, payments_sweep_loop, account_sweep_loop, ledger_daily_loop,
            search_sync_loop,
        )
    ]


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
    # The sweeps, here unless the worker runs them (APP_SWEEPS_IN_WORKER).
    loops = [] if settings.SWEEPS_IN_WORKER else start_loops()
    # The jobs this process writes, run as soon as they commit -- unless the
    # worker runs them (APP_JOBS_WORKER), when this process only writes them.
    from app.core import jobs

    runner = None
    if not settings.JOBS_WORKER:
        runner = jobs.Runner(kind="api").start()
        jobs.use(runner)
    # What import and startup built (models, routes, schemas, the S3 service
    # model: some 740,000 objects) lives as long as the process. Frozen out
    # of the collector's reach, a full collection no longer walks all of it:
    # on the discover benchmark those walks were the 100+ ms tail of requests
    # that allocate a lot (scripts/bench/RESULTS.md).
    gc.collect()
    gc.freeze()
    yield
    await shutdown(loops, runner=runner)


#: Seconds shutdown may take, in all. Docker kills the container ten seconds
#: after asking it to stop, and uvicorn has used up to four of those waiting
#: for requests (main.py), so this has what is left, with a margin.
SHUTDOWN_BUDGET = 5.0


async def shutdown(loops: list, budget: float = SHUTDOWN_BUDGET, runner=None) -> None:
    """Stop the background loops and wait for them to stop; stop taking jobs
    and give the ones in hand, and the mail sends, time to finish (new ones
    are not started); then wait for the after-commit calls. Each wait is
    bounded, together within `budget`: what is cut off -- a job, a mail send
    -- is in the database, handed back or resumed by the next process."""
    from app.api.deliveries import pipeline
    from app.core import after_commit, background, jobs, redis

    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget

    def left() -> float:
        return max(0.1, deadline - loop.time())

    for task in loops:
        task.cancel()
    if loops:
        _, stuck = await asyncio.wait(loops, timeout=min(1.0, left()))
        if stuck:
            print(f"Warning: {len(stuck)} background loop(s) did not stop in time")
    # The runner and the mail pumps stop together: a delivery job's pump is
    # one of the jobs in hand.
    share = left() * 0.6
    stopping = [pipeline.shutdown(timeout=share)]
    if runner is not None:
        stopping.append(runner.stop(timeout=share))
    await asyncio.gather(*stopping, return_exceptions=True)
    if runner is not None and jobs.local() is runner:
        jobs.use(None)
    await after_commit.drain(timeout=left())
    # Fan-outs started after a response (app/core/background.py): bounded too.
    try:
        await asyncio.wait_for(background.drain(), timeout=left())
    except asyncio.TimeoutError:
        print("Warning: background fan-outs did not finish before shutdown")
    # The live room: what it gathered to tell, then its feeds (their streams
    # ended with their requests).
    from app.api.live import bus as live_bus, stream as live_stream

    try:
        await asyncio.wait_for(live_bus.drain(), timeout=min(0.5, left()))
    except asyncio.TimeoutError:
        pass
    await live_stream.hub.close()
    await redis.close()


# No default_response_class: FastAPI's own JSON response is the default. It
# was ORJSONResponse, which FastAPI now deprecates -- it warned in the log on
# every response -- because with a response model or a return type FastAPI
# writes the JSON bytes itself through Pydantic, faster than orjson after
# jsonable_encoder, and a custom class turned that path off.
class Application(FastAPI):
    def build_middleware_stack(self):
        """With request ids on (APP_LOG_JSON or APP_SENTRY_DSN), their
        middleware goes outside even Starlette's ServerErrorMiddleware, which
        add_middleware cannot reach: so a 500 carries X-Request-ID too, and
        the 500 handler still knows the id when it makes the track_id."""
        stack = super().build_middleware_stack()
        return observability.RequestIdMiddleware(stack) if observability.request_ids_enabled() else stack


application = Application(lifespan=lifespan)

application.state.limiter = limiter
application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

application.include_router(router=api_router)
# The short tracking link lives at the root: api.myotherapp.in/l/<code> fits on a poster.
from app.api.links.router import public_router as public_links_router  # noqa: E402

application.include_router(router=public_links_router, tags=["tracking-links"])
# Liveness and readiness at the root, beside /ping (app/api/health.py).
from app.api.health import public_router as health_router  # noqa: E402

application.include_router(router=health_router)
# Innermost: each request's plan-gate scope, so one request resolves a club's
# entitlements once and knows whether an admin is behind it
# (app/api/subscriptions/gates.py).
from app.api.subscriptions.gates import RequestScope as PlanGateScope  # noqa: E402

application.add_middleware(PlanGateScope)
# A weak ETag on the API's JSON, and a 304 when the caller already has it
# (APP_ETAGS; app/core/middlewares/etag_middleware.py). Inside CORS, so a
# 304 to another origin carries the CORS headers too.
application.add_middleware(ETagMiddleware)
# `Cache-Control: private` on an answer to a signed-in caller that set none
# (app/core/middlewares/private_cache_middleware.py): a CDN in front of the
# API must never keep one person's data. Outside the tags, so a 304 has it too.
application.add_middleware(PrivateCacheMiddleware)
# print(settings.cors_origins)
# if settings.DEBUG:
application.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # A browser lets a page read only the simple response headers unless
    # told: the live stream's 503 LIVE_STREAM_FULL says when to try again in
    # Retry-After, and the site waits by it. ETag, so a page can keep an
    # answer's tag and send it back in If-None-Match.
    expose_headers=["Retry-After", "ETag"],
)
application.add_middleware(ProcessingTimeMiddleware)
application.add_middleware(SecurityHeadersMiddleware)
# slowapi's checks, as plain ASGI (app/core/middlewares/rate_limit_middleware.py).
application.add_middleware(RateLimitMiddleware)
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
    # The request's id when request ids are on (APP_LOG_JSON or Sentry), so
    # the id a user quotes finds the log lines and the Sentry event too.
    track_id = observability.current_request_id() or str(uuid.uuid4())
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
