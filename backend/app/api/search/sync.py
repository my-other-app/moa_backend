"""Carrying the outbox to Meilisearch, building the indexes, and checking them.

One loop on every web worker (`loop`, started at boot); each tick, whichever
process holds SEARCH_LOCK does the work, so it runs once however many
workers there are (app.db.locks.singleton, as the other sweeps). When the
durable job queue lands (app/core/jobs.py) the drain is a natural job; until
then it is a leader loop like the others.

Each tick:

1. With Meilisearch configured, every CHECK_SECONDS: the indexes are there
   and built for this DOC_VERSION (else they are built again: `rebuild`),
   and their settings are ours (`search.apply_settings`, a no-op when so).
2. The outbox, a batch at a time (`drain_once`), until it is empty or the
   tick's budget is spent. Category, interest and topic changes rewrite the
   search_text of the rows they touch, in batches that commit
   (`rebuild_taxonomy`) -- this happens with Meilisearch off too, since the
   Postgres search reads search_text. Event and club rows are sent to
   Meilisearch when it is configured and built, and simply dropped
   otherwise (a build reads every row afresh).
3. Every SEARCH_RECONCILE_HOURS: search_text and the grid cells checked
   against their rows and put right (text.recompute, cells.recompute), and
   the indexes compared with the rows, document by document, by hash
   (`reconcile`).

A failure leaves the outbox rows in place: the next tick sends them again
(adding a document is idempotent). The state sent is read after the outbox
row that asked for it, so an older state never overwrites a newer one.

`rebuild` builds each index under another name and swaps it in, so searches
keep the old one until the new one is whole; it holds SEARCH_LOCK, so the
drain waits, and what changed meanwhile is in the outbox, sent after the swap.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.discover import cells
from app.api.search import docs, text as stext
from app.config import settings
from app.core import search
from app.db.locks import singleton

logger = logging.getLogger(__name__)

SEARCH_LOCK = 773_140
#: Outbox rows read per batch.
BATCH = 500
#: Rows a taxonomy change rewrites per statement.
TAXONOMY_BATCH = 1000
#: Documents per request when building an index.
BUILD_BATCH = 2000
#: Rows compared per step of the reconcile.
RECONCILE_BATCH = 1000
#: How long one tick may drain before letting the loop breathe, in seconds.
TICK_BUDGET = 20.0
#: How often the indexes' existence, version and settings are checked.
CHECK_SECONDS = 300.0
#: The loop's pace: with Meilisearch configured, without.
DRAIN_EVERY = 1.0
IDLE_EVERY = 5.0
FIRST_SECONDS = 5.0

BUILD_JOB = "search-build"
RECONCILE_JOB = "search-reconcile"

_last_check: Optional[float] = None


def reset() -> None:
    """Forget when the indexes were last checked (tests)."""
    global _last_check
    _last_check = None


# ─── Whether the indexes are built ────────────────────────────────────────


async def _run(session, name: str):
    from app.api.models import ScheduledRuns

    return await session.scalar(select(ScheduledRuns).where(ScheduledRuns.name == name))


async def _record(session, name: str, **values) -> None:
    from app.api.models import ScheduledRuns

    stmt = pg_insert(ScheduledRuns.__table__).values(name=name, **values)
    await session.execute(stmt.on_conflict_do_update(index_elements=["name"], set_=values))
    await session.commit()


def _built_for(result: Optional[dict]) -> bool:
    """Whether a build's record is for these documents and these index names."""
    return bool(result) and result.get("version") == docs.DOC_VERSION and result.get("prefix") == settings.SEARCH_INDEX_PREFIX


async def is_built(session) -> bool:
    run = await _run(session, BUILD_JOB)
    return run is not None and run.finished_at is not None and _built_for(run.result)


# ─── The drain ────────────────────────────────────────────────────────────

_TAXONOMY_EVENTS = {
    "category": "SELECT id FROM events WHERE category_id = :ref AND id > :last ORDER BY id LIMIT :n",
    "interest": (
        "SELECT DISTINCT event_id FROM event_interests_link WHERE interest_id = :ref AND NOT is_deleted AND event_id > :last "
        "ORDER BY event_id LIMIT :n"
    ),
    "topic": (
        "SELECT DISTINCT l.event_id FROM event_interests_link l JOIN interests i ON i.id = l.interest_id "
        "WHERE i.category_id = :ref AND NOT l.is_deleted AND l.event_id > :last ORDER BY l.event_id LIMIT :n"
    ),
}
_TAXONOMY_CLUBS = {
    "interest": (
        "SELECT DISTINCT club_id FROM clubs_interests_link WHERE interest_id = :ref AND NOT is_deleted AND club_id > :last "
        "ORDER BY club_id LIMIT :n"
    ),
    "topic": (
        "SELECT DISTINCT l.club_id FROM clubs_interests_link l JOIN interests i ON i.id = l.interest_id "
        "WHERE i.category_id = :ref AND NOT l.is_deleted AND l.club_id > :last ORDER BY l.club_id LIMIT :n"
    ),
}
_REWRITE = {
    "event": "UPDATE events e SET search_text = {t} WHERE e.id = ANY(:ids) AND e.search_text IS DISTINCT FROM {t}".format(
        t="moa_event_search_text(e.id, e.name, e.location_name, e.event_tag, e.club_id, e.category_id)"),
    "club": "UPDATE clubs c SET search_text = {t} WHERE c.id = ANY(:ids) AND c.search_text IS DISTINCT FROM {t}".format(
        t="moa_club_search_text(c.id, c.name, c.tagline, c.location_name)"),
}


async def rebuild_taxonomy(session, kind: str, ref: int, *, batch: int = TAXONOMY_BATCH) -> int:
    """A category, interest or topic changed: the search_text of every event
    and club it touches written again, and their documents queued, `batch`
    rows a statement, each committed. Returns how many rows it went through."""
    seen = 0
    for target, queries in (("event", _TAXONOMY_EVENTS), ("club", _TAXONOMY_CLUBS)):
        sql = queries.get(kind)
        if sql is None:
            continue
        last = 0
        while True:
            ids = list((await session.execute(text(sql), {"ref": ref, "last": last, "n": batch})).scalars().all())
            if not ids:
                await session.commit()
                break
            await session.execute(text(_REWRITE[target]), {"ids": ids})
            await session.execute(text("INSERT INTO search_outbox (kind, ref_id) SELECT :k, unnest(CAST(:ids AS integer[]))"), {"k": target, "ids": ids})
            await session.commit()
            seen += len(ids)
            last = ids[-1]
    return seen


async def drain_once(session, *, batch: int = BATCH) -> dict:
    """One batch of the outbox; what it did ({} when the outbox was empty).
    Raises SearchError when Meilisearch refused (the rows stay for next time)."""
    rows = (await session.execute(text("SELECT id, kind, ref_id FROM search_outbox ORDER BY id LIMIT :n"), {"n": batch})).all()
    await session.commit()
    if not rows:
        return {}
    seen = [r.id for r in rows]
    refs: dict[str, set[int]] = {}
    for r in rows:
        refs.setdefault(r.kind, set()).add(r.ref_id)
    out: dict = {"rows": len(rows)}

    for kind in ("category", "interest", "topic"):
        for ref in sorted(refs.get(kind, ())):
            out[kind] = out.get(kind, 0) + await rebuild_taxonomy(session, kind, ref)
    if refs.get("club_events"):
        # A club's listing changed: every one of its events' documents says so.
        await session.execute(
            text("INSERT INTO search_outbox (kind, ref_id) SELECT 'event', id FROM events WHERE club_id = ANY(:ids)"),
            {"ids": sorted(refs["club_events"])},
        )
        await session.commit()

    if search.configured() and await is_built(session):
        tasks = []
        for kind, make in ((search.EVENTS, docs.event_docs), (search.CLUBS, docs.club_docs)):
            ids = sorted(refs.get("event" if kind == search.EVENTS else "club", ()))
            if not ids:
                continue
            upserts, gone = await make(session, ids)
            await session.commit()
            uid = search.index(kind)
            tasks += [await search.add_documents(uid, upserts), await search.delete_documents(uid, gone)]
            out[kind] = dict(sent=len(upserts), removed=len(gone))
        for task in tasks:
            if task is not None:
                await search.wait_task(task)

    await session.execute(text("DELETE FROM search_outbox WHERE id = ANY(:ids)"), {"ids": seen})
    await session.commit()
    return out


async def drain(session, *, budget: float = TICK_BUDGET, batch: int = BATCH) -> int:
    """Batches until the outbox is empty or `budget` seconds are spent; how many rows."""
    deadline = time.monotonic() + budget
    done = 0
    while time.monotonic() < deadline:
        got = await drain_once(session, batch=batch)
        if not got:
            break
        done += got["rows"]
    return done


# ─── Building and checking the indexes ────────────────────────────────────


async def _ids(session, kind: str, last: int, n: int) -> list[int]:
    ids = list((await session.execute(text(docs.CANDIDATES[kind]), {"last": last, "n": n})).scalars().all())
    await session.commit()
    return ids


async def rebuild(session, *, batch: int = BUILD_BATCH) -> dict:
    """Every index built afresh from the rows, under another name, then
    swapped in; the build recorded. Returns how many documents each has.
    The caller holds SEARCH_LOCK."""
    await _record(session, BUILD_JOB, started_at=datetime.now(timezone.utc))
    counts: dict = {}
    for kind in search.KINDS:
        live, building = search.index(kind), search.index(kind) + "__build"
        await search.delete_index(building)
        await search.create_index(building)
        await search.apply_settings(building, kind)
        pending: list[int] = []
        last, n = 0, 0
        while True:
            ids = await _ids(session, kind, last, batch)
            if not ids:
                break
            made, _ = await docs.DOCS[kind](session, ids)
            await session.commit()
            task = await search.add_documents(building, made)
            if task is not None:
                pending.append(task)
            if len(pending) >= 8:
                await search.wait_task(pending.pop(0))
            n += len(made)
            last = ids[-1]
        for task in pending:
            await search.wait_task(task)
        await search.create_index(live)
        await search.swap(live, building)
        await search.delete_index(building)
        counts[kind] = n
        logger.info("search index %s built: %s documents", live, n)
    await _record(session, BUILD_JOB, finished_at=datetime.now(timezone.utc), result=dict(
        version=docs.DOC_VERSION, prefix=settings.SEARCH_INDEX_PREFIX, **counts,
    ))
    return counts


async def ensure_built(session, *, force: bool = False) -> Optional[dict]:
    """The indexes there, built for this DOC_VERSION, their settings ours.
    Builds them (`rebuild`) when any is missing or older, or `force`; the
    counts when it did, else None."""
    missing = False
    for kind in search.KINDS:
        if await search.get_index(search.index(kind)) is None:
            missing = True
    if force or missing or not await is_built(session):
        return await rebuild(session)
    for kind in search.KINDS:
        if await search.apply_settings(search.index(kind), kind):
            logger.info("search index %s settings brought up to date", search.index(kind))
    return None


async def reconcile(session, *, batch: int = RECONCILE_BATCH, pause: float = 0.0) -> dict:
    """Compare each index with the rows, `batch` ids at a time: a document
    missing or different (by its hash) is sent again, one with no row (or a
    deleted or draft one) removed. Returns how many of each, per index."""
    out: dict = {}
    for kind in search.KINDS:
        uid = search.index(kind)
        sent = removed = 0
        last = 0
        while True:
            ids = await _ids(session, kind, last, batch)
            filt = f"id > {last}" + (f" AND id <= {ids[-1]}" if ids else "")
            have: dict[int, str] = {}
            offset = 0
            while True:
                got = await search.fetch_documents(uid, filter=filt, fields=["id", "h"], limit=1000, offset=offset)
                for d in got.get("results", []):
                    have[int(d["id"])] = d.get("h")
                offset += 1000
                if offset >= int(got.get("total") or 0):
                    break
            want, _ = await docs.DOCS[kind](session, ids)
            await session.commit()
            stale = [d for d in want if have.get(d["id"]) != d["h"]]
            extra = sorted(set(have) - {d["id"] for d in want})
            for task in (await search.add_documents(uid, stale), await search.delete_documents(uid, extra)):
                if task is not None:
                    await search.wait_task(task)
            sent += len(stale)
            removed += len(extra)
            if not ids:
                break
            last = ids[-1]
            if pause:
                await asyncio.sleep(pause)
        out[kind] = dict(sent=sent, removed=removed)
    return out


async def check_if_due(session, now: Optional[datetime] = None) -> Optional[dict]:
    """The six-hourly check (SEARCH_RECONCILE_HOURS): search_text against its
    rows, and the indexes against the rows when Meilisearch is on."""
    now = now or datetime.now(timezone.utc)
    run = await _run(session, RECONCILE_JOB)
    await session.commit()
    if run is not None and run.started_at is not None and now - run.started_at < timedelta(hours=settings.SEARCH_RECONCILE_HOURS):
        return None
    await _record(session, RECONCILE_JOB, started_at=now)
    fixed = await stext.recompute(session)
    if any(fixed.values()):
        logger.warning("search_text disagreed with its rows and was written again: %s", fixed)
    placed = await cells.recompute(session)
    if any(placed.values()):
        logger.warning("grid cells disagreed with their points and were written again: %s", placed)
    result: dict = dict(search_text=fixed, geocell=placed)
    if search.configured() and await is_built(session):
        result["indexes"] = await reconcile(session, pause=0.01)
        if any(v["sent"] or v["removed"] for v in result["indexes"].values()):
            logger.warning("search indexes disagreed with the rows and were put right: %s", result["indexes"])
    await _record(session, RECONCILE_JOB, finished_at=datetime.now(timezone.utc), result=result)
    return result


# ─── The loop ─────────────────────────────────────────────────────────────


async def tick(session) -> dict:
    """One round of the work above, by whoever holds it; {} when another process does."""
    global _last_check
    async with singleton(session, SEARCH_LOCK) as mine:
        if not mine:
            return {}
        out: dict = {}
        if search.configured() and (_last_check is None or time.monotonic() - _last_check >= CHECK_SECONDS):
            built = await ensure_built(session)
            _last_check = time.monotonic()
            if built is not None:
                out["built"] = built
        out["drained"] = await drain(session)
        checked = await check_if_due(session)
        if checked is not None:
            out["checked"] = checked
        return out


async def loop() -> None:
    """For the life of the process: a tick every second with Meilisearch on
    (every five without), backing off while it keeps failing."""
    from app.api.deliveries import service as deliveries

    await asyncio.sleep(FIRST_SECONDS)
    failures = 0
    while True:
        try:
            async with deliveries.session_scope() as session:
                await tick(session)
            failures = 0
        except asyncio.CancelledError:
            raise
        except search.SearchError as e:
            failures += 1
            logger.warning("search sync: %s (retrying)", e)
        except Exception:  # noqa: BLE001
            failures += 1
            logger.exception("search sync failed")
        pace = DRAIN_EVERY if search.configured() else IDLE_EVERY
        await asyncio.sleep(min(pace * (2 ** min(failures, 6)), 60.0))
