"""
Test configuration. Sets required env vars *before* importing any `app.*`
module, since `app.config.settings = AppConfig()` is constructed at import
time and will fail to build without them.

Schema is created via `AbstractSQLModel.metadata.create_all()` rather than
Alembic migrations, since `app/db/registry.py` already imports every model
module (so the metadata is complete) and this is much faster for a test
suite that creates/tears down its schema per session.
"""
import os

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault(
    "APP_DATABASE_URL",
    "postgresql+asyncpg://localhost:5432/moa_backend_test",
)
os.environ.setdefault(
    "APP_DATABASE_URL_SYNC", "postgresql://localhost:5432/moa_backend_test"
)
os.environ.setdefault("APP_CORS_ORIGINS", '["http://testserver"]')
os.environ.setdefault("APP_S3_BUCKET", "test-bucket")
os.environ.setdefault("APP_S3_ACCESS_KEY", "test-access-key")
os.environ.setdefault("APP_S3_SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_S3_BASE_PATH", "https://test-bucket.s3.amazonaws.com")
os.environ.setdefault("APP_SES_ACCESS_KEY", "test-ses-access-key")
os.environ.setdefault("APP_SES_SECRET_KEY", "test-ses-secret-key")
os.environ.setdefault("APP_SES_DEFAULT_SENDER", "test@finnet.app")
os.environ.setdefault("APP_GOOGLE_WEB_CLIENT_ID", "test-google-web-client-id")
os.environ.setdefault("APP_GOOGLE_ANDROID_CLIENT_ID", "test-google-android-client-id")
os.environ.setdefault("APP_GOOGLE_IOS_CLIENT_ID", "test-google-ios-client-id")
os.environ.setdefault("APP_RAZORPAY_KEY_ID", "test-razorpay-key-id")
os.environ.setdefault("APP_RAZORPAY_KEY_SECRET", "test-razorpay-key-secret")
os.environ.setdefault("APP_RAZORPAY_WEBHOOK_SECRET", "test-razorpay-webhook-secret")
os.environ.setdefault("APP_ACCESS_TOKEN_EXPIRE_MINUTES", "15")
os.environ.setdefault("APP_REFRESH_TOKEN_EXPIRE_DAYS", "7")
# Rendered documents are not kept in a bucket the suite does not have
# (app.api.events.doc_cache); a test of that cache turns it on itself.
os.environ.setdefault("APP_RENDER_DOC_CACHE", "false")

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Imported only after the env vars above are set.
from app.db.base import AbstractSQLModel
from app.db import registry  # noqa: F401 (registers every model on AbstractSQLModel.metadata)
from app.config import settings
from app.core.auth.jwt import create_access_token
from app.api.auth.schemas import AuthTokenData
from app.api.users.models import UserTypes, Users
from app.api.clubs.models import Clubs
from app.core.auth.authentication import get_password_hash
import razorpay.client as _razorpay_client

#: The SDK's own request method, for the one test that checks what it sends
#: (on a faked requests session).
REAL_RAZORPAY_REQUEST = _razorpay_client.Client.request


@pytest.fixture(scope="session")
def event_loop():
    # pytest-asyncio's newer loop_scope mechanism doesn't (yet) correctly
    # share a loop between session-scoped async fixtures and function-scoped
    # tests in this version — this explicit override does, despite the
    # deprecation warning it prints.
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def _engine(event_loop):
    engine = create_async_engine(settings.DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(AbstractSQLModel.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(AbstractSQLModel.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(_engine) -> AsyncSession:
    """A DB session wrapped in a transaction that's rolled back after each test."""
    connection = await _engine.connect()
    transaction = await connection.begin()
    # Mirrors production's session semantics via the shared kwargs. Hardcoding
    # them here is what let the MissingGreenlet outage ship: the suite ran with
    # expire_on_commit=False while production ran with the default True, so no
    # test could reproduce it.
    from app.db.core import SESSION_KWARGS
    from app.db.listeners import add_loader_criteria

    # Each of the session's transactions is a SAVEPOINT inside the test's
    # own: a commit releases it and a rollback undoes just that unit of work,
    # as in production. (The default joined the test's transaction for
    # rollbacks, so code that rolls back after a failure -- the webhook's
    # retry, a sweep skipping one row -- wiped the whole test's data.)
    session_factory = async_sessionmaker(bind=connection, join_transaction_mode="create_savepoint", **SESSION_KWARGS)
    session = session_factory()
    # And production hides soft-deleted rows from every ORM query
    # (app.db.core.get_session). Without this here, the admin's Trash passed
    # every test while finding nothing in production: its `is_deleted ==
    # True` was ANDed with the listener's `is_deleted IS false`.
    add_loader_criteria(session)
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The rate limiter's in-memory storage is process-global, so without this,
    login attempts from one test would count against another test's budget."""
    from app.core.rate_limit import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _fresh_discover_state():
    """The discover site keeps a minute's answers, and looks for map links to
    read at most once a minute, process-wide. Each test starts without either;
    a test of the cache turns it on itself."""
    from app.api.discover import cache, geo

    cache.reset(enabled=False)
    geo.reset_scan()
    yield
    cache.reset(enabled=False)


@pytest.fixture(autouse=True)
def _no_geocoder(monkeypatch):
    """The venue geocoder never runs in the suite unless a test turns it on,
    and never reaches the network even then: a test that wants answers puts
    its own fake in front of it (tests/test_geocoding.py)."""
    import httpx

    from app.api.discover import geocode

    def unreachable(request):
        raise AssertionError(f"the geocoder tried to reach the network: {request.url}")

    monkeypatch.setattr(settings, "GEOCODER_ENABLED", False)
    monkeypatch.setattr(geocode, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(unreachable)))
    geocode.reset()
    yield
    geocode.reset()


@pytest.fixture(autouse=True)
def _offline_bucket(monkeypatch):
    """Reading an object never reaches S3 from the suite: the renderer now
    reads a design's images by key (app.core.storage.objects), and a test
    that wants bytes there puts its own `get_bytes` or `fetch` in place.
    Each test starts with an empty image cache."""
    from app.core.storage import objects

    objects.cache_clear()
    monkeypatch.setattr(objects, "fetch", lambda key, quiet_if_missing=False: None)
    yield
    objects.cache_clear()


@pytest.fixture(autouse=True)
def _razorpay_unreachable(monkeypatch):
    """Razorpay is live in production, and no test may reach it: the real
    client's request method fails the test. A test fakes the calls it
    expects (the platform client's methods, or a whole fake client)."""
    import razorpay.client

    from app.api.payment_gateways import service as gateways

    def unreachable(self, method, path, **options):
        raise AssertionError(f"a test tried to reach Razorpay: {method.upper()} {path}")

    monkeypatch.setattr(razorpay.client.Client, "request", unreachable)
    gateways.forget_clients()
    yield
    gateways.forget_clients()


@pytest.fixture(autouse=True)
def _whatsapp_unreachable(monkeypatch):
    """Meta's API is never called from the suite, whatever a test sets the
    WhatsApp keys to: every call fails as an unreachable host would. A test
    that sends stands in for the client itself (tests/test_whatsapp.py)."""
    import httpx

    from app.core import whatsapp

    def unreachable(request):
        raise httpx.ConnectError(f"the test suite never reaches Meta: {request.method} {request.url}", request=request)

    monkeypatch.setattr(whatsapp, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(unreachable)))
    yield


@pytest.fixture(autouse=True)
def _quiet_email(monkeypatch):
    """Transactional mail never leaves the suite. A sign-in from a device the
    account has not seen sends one, which is every first login in every test;
    a test that cares about mail patches `send_email` itself and wins."""
    from app.core.email import email as email_mod

    monkeypatch.setattr(email_mod, "send_email", lambda **kwargs: None)
    yield


@pytest_asyncio.fixture(autouse=True)
async def _after_commit_finished(_quiet_email):
    """Mail queued to go after a commit (app.core.after_commit) runs on a
    worker thread. Every test waits for it before its patches come off, so a
    mail from one test never reaches the real provider after it."""
    from app.api.admin_alerts import service as admin_alerts
    from app.core import after_commit

    admin_alerts.reset_for_tests()
    yield
    await after_commit.drain()
    admin_alerts.reset_for_tests()


@pytest.fixture(autouse=True)
def _jobs_inline(request, monkeypatch):
    """Jobs (app/core/jobs.py) run when the test waits for them -- `await
    jobs.settle()`, or `after_commit.drain()` -- one at a time, and, in a
    test that has a session, on that session: they see its rows and are
    rolled back with them. The client below waits for them after every
    request, as the API's own runner runs them right after the response."""
    from contextlib import asynccontextmanager

    from app.core import jobs

    runner = jobs.InlineRunner()
    before = jobs.use(runner)
    if "session" in request.fixturenames:
        test_session = request.getfixturevalue("session")

        @asynccontextmanager
        async def scope():
            yield test_session

        monkeypatch.setattr(jobs, "session_scope", scope)
    yield runner
    jobs.use(before)


@pytest_asyncio.fixture
async def client(session):
    """An httpx client wired to the real FastAPI app, with get_session overridden
    to hand out the per-test rolled-back session so requests and test setup
    share the same transaction. The jobs a request's commits set off have run
    by the time it answers (app.core.jobs, run inline in the suite)."""
    from doth import application
    from app.core import jobs
    from app.db.core import get_session

    async def _get_test_session():
        yield session

    async def app_then_jobs(scope, receive, send):
        await application(scope, receive, send)
        if scope["type"] == "http":
            await jobs.settle()

    application.dependency_overrides[get_session] = _get_test_session
    transport = ASGITransport(app=app_then_jobs)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    application.dependency_overrides.clear()


async def _make_user(session, *, email, user_type=UserTypes.app_user, username=None, password="Password123!"):
    user = Users(
        full_name="Test User",
        username=username or email.split("@")[0],
        email=email,
        password=get_password_hash(password),
        user_type=user_type,
    )
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def make_user(session):
    async def _factory(**kwargs):
        return await _make_user(session, **kwargs)

    return _factory


@pytest_asyncio.fixture
async def make_club(session):
    async def _factory(*, name, email, is_verified=True):
        from app.core.utils.keys import generate_slug

        user = await _make_user(session, email=email, user_type=UserTypes.club)
        club = Clubs(
            slug=generate_slug(name),
            name=name,
            user_id=user.id,
            is_verified=is_verified,
        )
        session.add(club)
        await session.flush()
        user.club = club
        await session.flush()
        return user, club

    return _factory



@pytest.fixture
def fake_s3(monkeypatch):
    """Let a test save a row with an image without reaching S3.

    The image columns are a custom SQLAlchemy TypeDecorator that uploads
    inside `process_bind_param`, so saving a row carrying an image is a
    network call. Nothing else in the suite uploads one, so there was no
    pattern for this.

    The patch goes on the boto3 client *object*, not on the column type and
    not on `boto3.client`. Two earlier attempts explain why:

      - Replacing the type instance's `s3_client` misses, because SQLAlchemy
        adapts a type per dialect by copying it, and the copy that the flush
        actually calls kept the original client.
      - Replacing the class's `process_bind_param` misses too, because the
        bind processor is built once per type and cached, so a processor
        created earlier in the session still calls the original.

    Every one of those copies holds a reference to the same client object,
    so patching that object's methods reaches all of them.
    """
    uploaded: list[str] = []

    from app.api.certificate_templates.models import CertificateTemplates
    from app.api.clubs.models import ClubPartners, Clubs, ClubTeamMembers
    from app.api.threads.models import ClubThreads
    from app.api.ticket_templates.models import TicketTemplates

    def upload_fileobj(fileobj, bucket, key, **kwargs):
        uploaded.append(key)

    def generate_presigned_url(operation, Params=None, **kwargs):
        key = (Params or {}).get("Key", "unknown")
        return f"https://test-bucket.s3.amazonaws.com/{key}"

    clients = set()
    for model in (TicketTemplates, CertificateTemplates, Clubs, ClubPartners, ClubTeamMembers, ClubThreads):
        for column in model.__table__.columns:
            client = getattr(column.type, "s3_client", None)
            if client is not None:
                clients.add(id(client))
                monkeypatch.setattr(client, "upload_fileobj", upload_fileobj)
                monkeypatch.setattr(
                    client, "generate_presigned_url", generate_presigned_url
                )
                monkeypatch.setattr(client, "delete_object", lambda **kw: {})
                monkeypatch.setattr(client, "head_object", lambda **kw: {})

    # A miss here means the field stopped holding its own client, and every
    # test using this fixture would quietly start calling S3 for real.
    assert clients, "no S3 client found on the template image columns"

    return uploaded

@pytest_asyncio.fixture
async def make_admin(session):
    async def _factory(*, email="admin@example.com", role=None):
        user = await _make_user(session, email=email, user_type=UserTypes.admin)
        if role is not None:
            user.admin_role = role
            await session.flush()
        return user

    return _factory


@pytest_asyncio.fixture
async def make_event(session):
    async def _factory(*, club, name="Test Event", created_by, **overrides):
        from app.api.events.models import Events, EventCategories
        from app.core.utils.keys import generate_slug

        category = EventCategories(name="General", created_by_id=created_by.id)
        session.add(category)
        await session.flush()

        defaults = dict(
            slug=generate_slug(name),
            name=name,
            images=[],
            event_datetime=datetime.now(timezone.utc),
            duration=2.0,
            club_id=club.id if club else None,
            category_id=category.id,
        )
        defaults.update(overrides)
        event = Events(**defaults)
        session.add(event)
        await session.flush()
        return event

    return _factory


@pytest_asyncio.fixture
async def make_registration(session):
    async def _factory(*, event, user, **overrides):
        from app.api.events.models import EventRegistrationsLink
        from app.core.utils.keys import generate_ticket_id

        defaults = dict(
            event_id=event.id,
            user_id=user.id,
            full_name=user.full_name,
            email=user.email,
            ticket_id=generate_ticket_id(),
        )
        defaults.update(overrides)
        registration = EventRegistrationsLink(**defaults)
        session.add(registration)
        await session.flush()
        return registration

    return _factory


@pytest_asyncio.fixture
async def mint_token(session):
    async def _factory(user):
        await session.refresh(user)
        access = create_access_token(
            data=AuthTokenData(
                user_id=user.id,
                token_type="access_token",
                token_version=user.token_version,
            ).model_dump(),
        )
        return access

    return _factory


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _fresh_account_caches():
    """Process-wide caches the accounts code keeps: the admin Overview's
    minute, the Discord error report's dedupe, Google's certificates and
    Apple's keys. Each test starts without them."""
    from app.api import admin_overview
    from app.api.auth import service as auth_service
    from app.core.utils import discord

    admin_overview.reset_cache()
    discord.reset_for_tests()
    auth_service._google_certs.reset()
    auth_service._apple_keys = None
    yield
    admin_overview.reset_cache()
    discord.reset_for_tests()


@pytest.fixture(autouse=True)
def _check_in_hooks_on_the_test_session(request, monkeypatch):
    """What a check-in sets off after the response (app/api/checkins/after.py)
    opens a session of its own. In a test that has a session, it uses that one
    instead, so it sees the test's rows and is rolled back with them; the
    request's BackgroundTasks finish before the client call returns, so the
    two never run at once."""
    if "session" not in request.fixturenames:
        yield
        return
    from contextlib import asynccontextmanager

    from app.api.checkins import after

    test_session = request.getfixturevalue("session")

    @asynccontextmanager
    async def scope():
        yield test_session

    monkeypatch.setattr(after, "session_scope", scope)
    yield
