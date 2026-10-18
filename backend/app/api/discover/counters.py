"""Counters kept beside their rows, checked against the rows.

events.going_count and clubs.follower_count; a post's likes_count,
comments_count and views_count and its poll options' votes_count; a live
question's votes. (events.page_views is not among them: it is a running
total older than the rows that could check it, and it now moves only by
batched UPDATE ... + n, never by read-modify-write.)

Triggers keep the first two, and a post's likes, comments and votes, exact
in the writer's own transaction (GOING_COUNT_SQL in app/api/events/models.py,
FOLLOWER_COUNT_SQL in app/api/clubs/models.py, THREAD_COUNTS_SQL in
app/api/threads/models.py); views and a live question's votes move by one
atomic UPDATE beside the rows they count.
What can still put them out: something that writes with triggers switched off
(a data-only restore with --disable-triggers, session_replication_role =
replica) or edits a counter by hand. `reconcile` finds any row whose counter
disagrees with its rows and recounts it; an admin can run it at any time, and
`check_loop` runs it every six hours.

Each disagreeing row is recounted while holding that row's lock: a
registration or follow in flight either finishes first (and is counted) or
waits for the recount and then adds itself, so a reconcile running alongside
writes cannot itself leave a counter wrong.

How it is kept cheap:

- One process runs it, every EVERY_SECONDS, whichever worker holds
  COUNTERS_LOCK; the last run is a row in scheduled_runs that every worker
  reads. It used to run in every worker on its first sweep after a deploy
  (the schedule was a module variable): four full checks at once with four
  workers, on the boxes that had just restarted.
- It compares in slices of ids, one short statement each (an index-only read
  of ix_registrations_event_created for the events), with a pause between,
  so it never holds one long scan: the whole-table form took 3 s at 15,000
  clubs and 19 s at a lakh (docs/SCALING.md).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.locks import singleton

logger = logging.getLogger(__name__)

# A counter differs from what its rows say, among ids lo..hi.
_GOING_DRIFT = """
SELECT e.id FROM events e
LEFT JOIN (
  SELECT event_id, count(*) AS n FROM event_registrations_link
  WHERE NOT is_deleted AND event_id BETWEEN :lo AND :hi
  GROUP BY event_id
) c ON c.event_id = e.id
WHERE e.id BETWEEN :lo AND :hi AND e.going_count <> coalesce(c.n, 0)
"""
_FOLLOWER_DRIFT = """
SELECT k.id FROM clubs k
LEFT JOIN (
  SELECT club_id, count(*) AS n FROM club_users_link
  WHERE is_following AND NOT is_deleted AND club_id BETWEEN :lo AND :hi
  GROUP BY club_id
) c ON c.club_id = k.id
WHERE k.id BETWEEN :lo AND :hi AND k.follower_count <> coalesce(c.n, 0)
"""
_GOING_FIX = """
UPDATE events SET going_count = (
  SELECT count(*) FROM event_registrations_link r WHERE r.event_id = events.id AND NOT r.is_deleted
) WHERE id = :id
"""
_FOLLOWER_FIX = """
UPDATE clubs SET follower_count = (
  SELECT count(*) FROM club_users_link f WHERE f.club_id = clubs.id AND f.is_following AND NOT f.is_deleted
) WHERE id = :id
"""

#: Ids per statement. An event has a few registrations and a club many
#: follows, so clubs go in smaller slices; either way one statement reads a
#: few hundred thousand rows at most at a lakh of clubs.
EVENT_SLICE = 20_000
CLUB_SLICE = 2_000
#: The scheduled run's pause between slices, so the database serves others meanwhile.
PAUSE = 0.05

EVERY_SECONDS = 6 * 3600
COUNTERS_LOCK = 773_104
JOB = "counters"
#: The first look after a worker starts: never in the rush of a deploy.
FIRST_LOOK_SECONDS = 300
#: How often each worker looks whether the check is due.
LOOK_EVERY_SECONDS = 600


def slices(lo: Optional[int], hi: Optional[int], size: int):
    """(lo, hi) id ranges of `size` covering lo..hi; none for an empty table."""
    if lo is None or hi is None:
        return
    start = lo
    while start <= hi:
        yield start, min(start + size - 1, hi)
        start += size


async def _scan(session, table: str, sql: str, size: int, pause: float) -> list[int]:
    lo, hi = (await session.execute(text(f"SELECT min(id), max(id) FROM {table}"))).one()
    found: list[int] = []
    for a, b in slices(lo, hi, size):
        found.extend((await session.execute(text(sql), {"lo": a, "hi": b})).scalars().all())
        if pause:
            await asyncio.sleep(pause)
    return found


async def drift(session, *, pause: float = 0.0, event_slice: int = EVENT_SLICE, club_slice: int = CLUB_SLICE) -> dict:
    """The ids whose counter disagrees with its rows, without changing anything."""
    return dict(
        events=await _scan(session, "events", _GOING_DRIFT, event_slice, pause),
        clubs=await _scan(session, "clubs", _FOLLOWER_DRIFT, club_slice, pause),
    )


async def reconcile(session, *, pause: float = 0.0) -> dict:
    """Recount every counter that disagrees with its rows; returns how many were put right."""
    found = await drift(session, pause=pause)
    for table, fix, ids in (("events", _GOING_FIX, found["events"]), ("clubs", _FOLLOWER_FIX, found["clubs"])):
        for row_id in ids:
            await session.execute(text(f"SELECT id FROM {table} WHERE id = :id FOR UPDATE"), {"id": row_id})
            await session.execute(text(fix), {"id": row_id})
    await session.commit()
    return dict(events=len(found["events"]), clubs=len(found["clubs"]))


# ─── Posts and the live room: ids are UUIDs, so slices are keyset ranges ──

#: Rows per slice; each recounts its rows' likes, comments and views.
POST_SLICE = 2_000
_ZERO = "00000000-0000-0000-0000-000000000000"

_POST_DRIFT = """
SELECT t.id FROM club_threads t
WHERE t.id = ANY(CAST(:ids AS uuid[])) AND (
  t.likes_count <> (SELECT count(*) FROM thread_likes l WHERE l.thread_id = t.id)
  OR t.comments_count <> (SELECT count(*) FROM thread_comments c WHERE c.thread_id = t.id AND NOT c.is_deleted)
  OR t.views_count <> (SELECT count(*) FROM thread_views v WHERE v.thread_id = t.id)
)
"""
_POST_FIX = """
UPDATE club_threads t SET
  likes_count = (SELECT count(*) FROM thread_likes l WHERE l.thread_id = t.id),
  comments_count = (SELECT count(*) FROM thread_comments c WHERE c.thread_id = t.id AND NOT c.is_deleted),
  views_count = (SELECT count(*) FROM thread_views v WHERE v.thread_id = t.id)
WHERE t.id = :id
"""
_OPTION_DRIFT = """
SELECT o.id FROM thread_poll_options o
WHERE o.id = ANY(CAST(:ids AS uuid[])) AND o.votes_count <> (SELECT count(*) FROM thread_votes v WHERE v.option_id = o.id)
"""
_OPTION_FIX = "UPDATE thread_poll_options o SET votes_count = (SELECT count(*) FROM thread_votes v WHERE v.option_id = o.id) WHERE o.id = :id"
_QUESTION_DRIFT = """
SELECT q.id FROM live_questions q
WHERE q.id = ANY(CAST(:ids AS uuid[])) AND q.votes <> (SELECT count(*) FROM live_question_votes v WHERE v.question_id = q.id)
"""
_QUESTION_FIX = "UPDATE live_questions q SET votes = (SELECT count(*) FROM live_question_votes v WHERE v.question_id = q.id) WHERE q.id = :id"

#: (name, table, drift, fix)
MORE = (
    ("posts", "club_threads", _POST_DRIFT, _POST_FIX),
    ("poll_options", "thread_poll_options", _OPTION_DRIFT, _OPTION_FIX),
    ("questions", "live_questions", _QUESTION_DRIFT, _QUESTION_FIX),
)


async def _scan_keyset(session, table: str, sql: str, size: int, pause: float) -> list:
    found: list = []
    last = _ZERO
    while True:
        ids = list((await session.execute(
            text(f"SELECT id FROM {table} WHERE id > CAST(:last AS uuid) ORDER BY id LIMIT :n"), {"last": last, "n": size}
        )).scalars().all())
        if not ids:
            return found
        found.extend((await session.execute(text(sql), {"ids": ids})).scalars().all())
        last = str(ids[-1])
        if pause:
            await asyncio.sleep(pause)


async def drift_more(session, *, pause: float = 0.0, size: int = POST_SLICE) -> dict:
    """The posts, poll options and live questions whose counters disagree with their rows."""
    return {name: await _scan_keyset(session, table, sql, size, pause) for name, table, sql, _ in MORE}


async def reconcile_more(session, *, pause: float = 0.0) -> dict:
    """Recount the posts', options' and questions' counters that disagree; returns how many per kind."""
    found = await drift_more(session, pause=pause)
    for name, table, _, fix in MORE:
        for row_id in found[name]:
            await session.execute(text(f"SELECT id FROM {table} WHERE id = :id FOR UPDATE"), {"id": row_id})
            await session.execute(text(fix), {"id": row_id})
    await session.commit()
    return {name: len(found[name]) for name, *_ in MORE}


async def reconcile_all(session, *, pause: float = 0.0) -> dict:
    """Every counter: `reconcile` and `reconcile_more`."""
    return {**await reconcile(session, pause=pause), **await reconcile_more(session, pause=pause)}


async def _record(session, job: str = JOB, **values) -> None:
    from app.api.models import ScheduledRuns

    stmt = pg_insert(ScheduledRuns.__table__).values(name=job, **values)
    await session.execute(stmt.on_conflict_do_update(index_elements=["name"], set_=values))
    await session.commit()


#: The posts' and the live room's counters: their own row in scheduled_runs, the same lock and the same six hours.
JOB_MORE = "counters-posts"


async def run_more_if_due(session, now: Optional[datetime] = None) -> Optional[dict]:
    """`reconcile_more`, as run_if_due runs `reconcile`."""
    from app.api.models import ScheduledRuns

    now = now or datetime.now(timezone.utc)
    async with singleton(session, COUNTERS_LOCK) as mine:
        if not mine:
            return None
        last = await session.scalar(select(ScheduledRuns.started_at).where(ScheduledRuns.name == JOB_MORE))
        if last is not None and now - last < timedelta(seconds=EVERY_SECONDS):
            return None
        await _record(session, JOB_MORE, started_at=now)
        fixed = await reconcile_more(session, pause=PAUSE)
        await _record(session, JOB_MORE, finished_at=datetime.now(timezone.utc), result=fixed)
        return fixed


async def run_if_due(session, now: Optional[datetime] = None) -> Optional[dict]:
    """The check, if no other process is running it and the last run is
    EVERY_SECONDS old (or there has been none). Returns what was put right,
    or None when it did not run."""
    from app.api.models import ScheduledRuns

    now = now or datetime.now(timezone.utc)
    async with singleton(session, COUNTERS_LOCK) as mine:
        if not mine:
            return None
        last = await session.scalar(select(ScheduledRuns.started_at).where(ScheduledRuns.name == JOB))
        if last is not None and now - last < timedelta(seconds=EVERY_SECONDS):
            return None
        await _record(session, started_at=now)
        fixed = await reconcile(session, pause=PAUSE)
        await _record(session, finished_at=datetime.now(timezone.utc), result=fixed)
        return fixed


async def check_loop(interval: Optional[int] = None) -> None:
    """Runs for the life of the process: every few minutes the events'
    popularity is written again (discover.popularity), and the counter check
    runs when six hours have passed; each on whichever process gets to it."""
    from app.api.deliveries import service as deliveries
    from app.api.discover import popularity

    interval = interval or min(LOOK_EVERY_SECONDS, popularity.EVERY_SECONDS)
    await asyncio.sleep(FIRST_LOOK_SECONDS)
    while True:
        try:
            async with deliveries.session_scope() as session:
                await popularity.refresh_if_free(session)
            for job in (run_if_due, run_more_if_due):
                async with deliveries.session_scope() as session:
                    fixed = await job(session)
                    if fixed and any(fixed.values()):
                        logger.warning("recounted counters that disagreed with their rows: %s", fixed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("counter check failed")
        await asyncio.sleep(interval)
