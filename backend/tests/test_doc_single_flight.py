"""One render per document, however many ask for it at once (app/api/events/doc_cache.py).

Two first downloads of the same ticket at the same moment both drew it: an
email blast's "Download your ticket" clicked by a guest twice, a resend beside
a bundle, two API processes behind the same link. Now one process claims the
document (a Postgres advisory lock, or a Redis key with APP_REDIS_URL), draws
and keeps it, and the rest read what it kept.

"Another process" here is another connection: each `_fill` takes its claim on
a connection of its own from the suite's engine, exactly as a second API
process would on its pool.
"""
import asyncio
import io
import os
import re
import threading
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.api.events import doc_cache, tickets
from app.config import settings
from app.core.storage import objects

KEY = f"{settings.S3_BASE_PATH}/docs/ticket/3f1c2b4a-0000-4000-8000-000000000001/0-{'a' * 24}.pdf"


@pytest.fixture
def bucket(monkeypatch):
    """The bucket as a dict, the cache on."""
    held: dict = {}
    monkeypatch.setattr(settings, "RENDER_DOC_CACHE", True)
    monkeypatch.setattr(objects, "fetch", lambda key, quiet_if_missing=False: held.get(key))
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: held.__setitem__(key, data))
    return held


@pytest.fixture
def claims_on(_engine, monkeypatch):
    """Claims taken on the suite's database, each on a connection of its own."""
    monkeypatch.setattr(doc_cache, "lock_engine", lambda: _engine)
    return _engine


class Renderer:
    """A draw that takes a while on a render thread, and counts."""

    def __init__(self, seconds: float = 0.25, body: bytes = b"%PDF-1.4 the ticket"):
        self.seconds, self.body, self.calls = seconds, body, 0
        self._lock = threading.Lock()

    def __call__(self) -> tuple[bytes, bool]:
        with self._lock:
            self.calls += 1
            n = self.calls
        time.sleep(self.seconds)
        return self.body + f" #{n}".encode(), True


async def _held_elsewhere(engine, key: str):
    """Another process mid-render: a connection holding the document's lock."""
    conn = await engine.connect()
    conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
    await conn.scalar(select(func.pg_advisory_lock(doc_cache.RENDER_LOCK, doc_cache.lock_id(key))))
    return conn


async def _let_go(conn, key: str) -> None:
    await conn.scalar(select(func.pg_advisory_unlock(doc_cache.RENDER_LOCK, doc_cache.lock_id(key))))
    await conn.close()


async def _render_locks(engine) -> int:
    async with engine.connect() as conn:
        result = await conn.exec_driver_sql(
            f"SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = {int(doc_cache.RENDER_LOCK)}"
        )
        return int(result.scalar())


# First in the module, as it uses the database: a module whose first test
# does not leaves its database tests on another event loop (tests/test_redis.py).


async def test_n_first_downloads_at_once_draw_it_once_and_all_get_the_same_bytes(session, bucket, claims_on):
    draw = Renderer()

    results = await asyncio.gather(*(doc_cache.cached(KEY, draw) for _ in range(12)))

    assert draw.calls == 1
    assert len(set(results)) == 1 and results[0] == bucket[KEY]
    assert await _render_locks(claims_on) == 0  # let go once kept


async def test_across_processes_one_claim_draws_it(session, bucket, claims_on):
    """Each `_fill` is what one process does: its own connection, its own claim."""
    draw = Renderer(seconds=0.3)

    results = await asyncio.gather(*(doc_cache._fill(KEY, draw) for _ in range(6)))

    assert draw.calls == 1
    assert len(set(results)) == 1 and results[0] == bucket[KEY]
    assert await _render_locks(claims_on) == 0


async def test_a_document_another_process_is_drawing_is_waited_for_then_read(session, bucket, claims_on):
    other = await _held_elsewhere(claims_on, KEY)
    draw = Renderer()
    waiting = asyncio.create_task(doc_cache.cached(KEY, draw))
    await asyncio.sleep(0.4)
    assert not waiting.done() and draw.calls == 0

    bucket[KEY] = b"%PDF-1.4 kept by the other process"
    await _let_go(other, KEY)

    assert await asyncio.wait_for(waiting, 5) == b"%PDF-1.4 kept by the other process"
    assert draw.calls == 0


async def test_a_waiter_gives_up_after_the_wait_and_draws_it_itself(session, bucket, claims_on, monkeypatch):
    monkeypatch.setattr(settings, "DOC_RENDER_WAIT_SECONDS", 0.4)
    other = await _held_elsewhere(claims_on, KEY)  # a process that never finishes
    draw = Renderer(seconds=0.05)
    try:
        t0 = time.monotonic()
        pdf = await asyncio.wait_for(doc_cache.cached(KEY, draw), 10)
        waited = time.monotonic() - t0
    finally:
        await _let_go(other, KEY)

    assert pdf.startswith(b"%PDF-1.4 the ticket") and draw.calls == 1 and bucket[KEY] == pdf
    assert 0.3 <= waited < 5


async def test_a_waiter_reads_what_was_kept_while_it_waited(session, bucket, claims_on):
    """A process that took the claim, then found the document kept by the
    one before it (the claim's holder looks again), draws nothing."""
    bucket[KEY] = b"%PDF-1.4 kept a moment ago"
    draw = Renderer()

    assert await doc_cache._fill(KEY, draw) == b"%PDF-1.4 kept a moment ago"
    assert draw.calls == 0


async def test_no_claim_to_be_had_is_no_reason_to_fail_a_download(session, bucket, monkeypatch):
    class Down:
        async def connect(self):
            raise OSError("connection refused")

    monkeypatch.setattr(doc_cache, "lock_engine", lambda: Down())
    draw = Renderer(seconds=0.01)

    pdf = await doc_cache.cached(KEY, draw)

    assert draw.calls == 1 and bucket[KEY] == pdf


async def test_a_caller_that_goes_away_does_not_cancel_the_drawing_for_the_others(session, bucket, claims_on):
    draw = Renderer(seconds=0.3)
    first = asyncio.create_task(doc_cache.cached(KEY, draw))
    second = asyncio.create_task(doc_cache.cached(KEY, draw))
    await asyncio.sleep(0.1)
    first.cancel()

    assert (await asyncio.wait_for(second, 5)).startswith(b"%PDF-1.4 the ticket")
    assert draw.calls == 1 and KEY in bucket
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_a_render_that_fails_fails_for_everyone_waiting_and_the_next_one_tries_again(session, bucket, claims_on):
    def broken():
        time.sleep(0.1)
        raise RuntimeError("a font tripped the PDF library")

    results = await asyncio.gather(*(doc_cache.cached(KEY, broken) for _ in range(3)), return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results) and KEY not in bucket
    assert await _render_locks(claims_on) == 0

    draw = Renderer(seconds=0.01)
    assert (await doc_cache.cached(KEY, draw)).startswith(b"%PDF-1.4") and draw.calls == 1


async def test_an_incomplete_render_is_not_kept_and_not_shared_beyond_its_moment(session, bucket, claims_on):
    calls = []

    def without_logo():
        calls.append(1)
        time.sleep(0.1)
        return b"%PDF-1.4 no logo", False

    results = await asyncio.gather(*(doc_cache.cached(KEY, without_logo) for _ in range(4)))
    assert set(results) == {b"%PDF-1.4 no logo"} and len(calls) == 1 and KEY not in bucket

    await doc_cache.cached(KEY, without_logo)  # the next download tries again for the logo
    assert len(calls) == 2


async def test_with_the_cache_off_every_request_draws_and_no_claim_is_taken(session, monkeypatch):
    monkeypatch.setattr(settings, "RENDER_DOC_CACHE", False)
    monkeypatch.setattr(doc_cache, "lock_engine", lambda: pytest.fail("took a claim with the cache off"))
    draw = Renderer(seconds=0.01)

    await asyncio.gather(*(doc_cache.cached(KEY, draw) for _ in range(3)))
    assert draw.calls == 3


async def test_a_burst_of_different_documents_holds_at_most_the_claim_slots(session, bucket, claims_on, monkeypatch):
    """Each Postgres claim is a connection held while its document is drawn;
    twenty first downloads at once must not take twenty."""
    held, most = 0, 0
    real = doc_cache._pg_claim

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def counting(key):
        nonlocal held, most
        held += 1
        most = max(most, held)
        try:
            async with real(key) as got:
                yield got
        finally:
            held -= 1

    monkeypatch.setattr(doc_cache, "_pg_claim", counting)
    draw = Renderer(seconds=0.05)
    keys = [KEY.replace("0-aaaa", f"{i}-aaaa") for i in range(20)]

    await asyncio.gather(*(doc_cache.cached(k, draw) for k in keys))

    assert draw.calls == 20 and all(k in bucket for k in keys)
    assert most <= doc_cache.claim_slots() < 20


async def test_n_ticket_downloads_at_once_render_the_ticket_once(session, bucket, claims_on, monkeypatch, make_club, make_event, make_user, make_registration):
    """Through the ticket itself: the download, the email's attachment and
    the bundles all ask tickets.ticket_document for it."""
    from app.api.events import service

    owner, club = await make_club(name="Yukthi", email="single-flight@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night",
                             event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc), duration=3.0)
    guest = await make_user(email="single-flight-guest@example.com")
    made = await make_registration(event=event, user=guest, full_name="Ananya Iyer")
    await session.commit()
    reg = await service.get_ticket_details(session, ticket_id=made.ticket_id)
    style = await tickets.ticket_style(session, reg)

    drawn = []

    def pdf(template_path, context, options=None, template_dir=None):
        drawn.append(context["email_payload"]["participant_name"])
        time.sleep(0.2)
        return io.BytesIO(b"%PDF-1.4 " + context["email_payload"]["participant_name"].encode())

    monkeypatch.setattr(tickets, "generate_pdf_bytes", pdf)

    docs = await asyncio.gather(*(tickets.ticket_document(session, reg, style) for _ in range(8)))

    assert drawn == ["Ananya Iyer"]
    assert len({d.content for d in docs}) == 1
    (kept,) = [k for k in bucket if "/docs/ticket/" in k]
    assert re.fullmatch(rf"{re.escape(settings.S3_BASE_PATH)}/docs/ticket/{reg.id}/0-[0-9a-f]{{24}}\.pdf", kept)


def test_every_kind_the_code_keeps_is_one_the_sweep_knows():
    """The sweep deletes only keys of KINDS; a kind kept by the code and
    missing there would be skipped as unknown for ever."""
    root = os.path.join(os.path.dirname(__file__), "..", "app")
    kinds = set()
    for folder, _, files in os.walk(root):
        for name in files:
            if name.endswith(".py"):
                text = open(os.path.join(folder, name)).read()
                kinds.update(re.findall(r"(?:cache_key|key_for)\(\s*\"(\w+)\"", text))
    assert kinds and kinds <= set(doc_cache.KINDS)


# ─── With Redis (APP_REDIS_URL): the claim is a Redis key ───────────────────

REDIS_URL = os.environ.get("APP_TEST_DOC_REDIS_URL", "redis://localhost:6379/12")


def _redis_reachable() -> bool:
    try:
        import redis as sync_redis

        return bool(sync_redis.from_url(REDIS_URL, socket_timeout=0.5, socket_connect_timeout=0.5).ping())
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def with_redis(monkeypatch):
    from app.core import redis

    monkeypatch.setattr(settings, "REDIS_URL", REDIS_URL)
    # What is tested is the claim, not Redis's latency: on a loaded machine a
    # first call past the 0.25 s default is a fall back to Postgres (as it
    # should be), which these tests would read as a failure.
    monkeypatch.setattr(settings, "REDIS_TIMEOUT", 2.0)
    redis.reset_for_tests()
    yield
    redis.reset_for_tests()


@pytest.mark.skipif(not _redis_reachable(), reason=f"no Redis at {REDIS_URL}")
async def test_with_redis_the_claim_holds_no_database_connection(session, bucket, with_redis, monkeypatch):
    monkeypatch.setattr(doc_cache, "lock_engine", lambda: pytest.fail("took a database connection for the claim"))
    draw = Renderer(seconds=0.3)

    results = await asyncio.gather(*(doc_cache._fill(KEY, draw) for _ in range(6)))

    assert draw.calls == 1 and len(set(results)) == 1 and results[0] == bucket[KEY]
    import redis as sync_redis

    assert sync_redis.from_url(REDIS_URL).exists(doc_cache._redis_key(KEY)) == 0  # released by its holder


async def test_with_redis_down_the_claim_is_postgres_again(session, bucket, claims_on, monkeypatch):
    from app.core import redis

    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")
    redis.reset_for_tests()
    try:
        draw = Renderer(seconds=0.3)
        results = await asyncio.gather(*(doc_cache._fill(KEY, draw) for _ in range(4)))
    finally:
        redis.reset_for_tests()

    assert draw.calls == 1 and len(set(results)) == 1
