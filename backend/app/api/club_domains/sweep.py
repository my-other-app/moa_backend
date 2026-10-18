"""The domains sweep: every five minutes, one worker at a time.

Four jobs, each row in its own transaction so one bad row cannot stop the
rest:

- pending domains: asked about at Cloudflare; active once Cloudflare says
  the hostname and its certificate are, failed after seven days;
- active domains not checked for a day: asked about again (a CNAME taken
  away, a certificate that could not renew, a hostname Cloudflare dropped);
- active and paused domains: paused when the club's plan no longer
  includes `custom_domain`, active again when it does;
- domains paused for thirty days: removed at Cloudflare, the club mailed.

The first two need Cloudflare and are skipped while it is not configured.
A row Cloudflare could not be asked about is left as it was for the next run.

One worker at a time, by a Postgres advisory lock held on its own connection,
as the billing sweep does.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.club_domains import cloudflare as cf
from app.api.club_domains import service
from app.api.club_domains.models import ACTIVE, PAUSED, PENDING, ClubDomains

logger = logging.getLogger(__name__)

SWEEP_LOCK = 773_120
SWEEP_INTERVAL = 300
#: Rows per job per run; anything left waits for the next run.
BATCH = 100


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pending(now):
    return (ClubDomains.status == PENDING,)


def _active_due(now):
    return (
        ClubDomains.status == ACTIVE,
        or_(ClubDomains.checked_at.is_(None), ClubDomains.checked_at <= now - service.RECHECK_ACTIVE),
    )


def _serving(now):
    return (ClubDomains.status.in_((ACTIVE, PAUSED)),)


def _paused_over(now):
    return (ClubDomains.status == PAUSED, ClubDomains.paused_at <= now - service.PAUSED_FOR)


# Where the jobs that leave a row as it was (the entitlement sync, mostly)
# stopped last run. Their rows never drop out of the criteria, so a plain
# "first BATCH by id" took the same rows on every run and the ones after the
# hundredth were never looked at. Per process; a restart starts over from
# the beginning, which only delays a row by a run.
_cursors: dict[str, int] = {}


async def _ids(session, *criteria, job: str | None = None) -> list[int]:
    """Up to BATCH rows for a job.

    The Cloudflare jobs go least recently asked first (`checked_at`, never
    asked before all): a poll stamps the row, so it goes to the back. The
    others go round by id from where the last run stopped, wrapping to the
    start, so every row is reached however many there are."""
    if job in ("polled", "rechecked"):
        q = select(ClubDomains.id).where(*criteria).order_by(ClubDomains.checked_at.asc().nulls_first(), ClubDomains.id)
        return list((await session.execute(q.limit(BATCH))).scalars())
    after = _cursors.get(job or "", 0)
    ids = list((await session.execute(
        select(ClubDomains.id).where(*criteria, ClubDomains.id > after).order_by(ClubDomains.id).limit(BATCH)
    )).scalars())
    if len(ids) < BATCH and after:
        ids += list((await session.execute(
            select(ClubDomains.id).where(*criteria, ClubDomains.id <= after).order_by(ClubDomains.id).limit(BATCH - len(ids))
        )).scalars())
    if job:
        _cursors[job] = ids[-1] if ids else 0
    return ids


async def _lock_row(session, domain_id: int, *criteria) -> ClubDomains | None:
    """The row, locked, if it still matches; one another transaction holds is skipped."""
    return await session.scalar(
        select(ClubDomains)
        .where(ClubDomains.id == domain_id, *criteria)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )


async def _poll(session, row, now):
    return await service.refresh(session, row, now)


async def _sync(session, row, now):
    return await service.sync_entitlement(session, row, now)


async def _expire(session, row, now):
    await service.expire_paused(session, row, now)
    return "removed"


async def sweep(session, now: datetime | None = None) -> dict:
    """One run of the four jobs. Returns how many rows each changed."""
    now = now or _now()
    done = {"polled": 0, "rechecked": 0, "synced": 0, "removed": 0}
    jobs = [
        ("synced", _serving(now), _sync),
        ("removed", _paused_over(now), _expire),
    ]
    if cf.available():
        jobs = [("polled", _pending(now), _poll), ("rechecked", _active_due(now), _poll), *jobs]
    for key, criteria, job in jobs:
        for domain_id in await _ids(session, *criteria, job=key):
            try:
                row = await _lock_row(session, domain_id, *criteria)
                if row is None:
                    continue
                before, club_id = row.status, row.club_id
                changed = await job(session, row, now)
                after = row.status
                await session.commit()
                if key in ("polled", "rechecked") or changed:
                    done[key] += 1
                await service.after_change(session, club_id, before, after)
            except cf.CloudflareError as exc:
                logger.warning("domains sweep: Cloudflare could not be asked about domain %s: %s", domain_id, exc.message)
                await session.rollback()
            except Exception:  # noqa: BLE001
                logger.exception("domains sweep: %s failed for domain %s", key, domain_id)
                await session.rollback()
    return done


async def run_sweep(session, now: datetime | None = None) -> dict | None:
    """The sweep, if no other worker is running it. None when one is."""
    bind = session.bind
    if isinstance(bind, AsyncEngine):
        async with bind.connect() as lock_conn:
            return await _locked(lock_conn, session, now)
    # A session on one connection (the tests): the lock goes on that one.
    return await _locked(bind, session, now)


async def _locked(conn, session, now) -> dict | None:
    if not await conn.scalar(text(f"SELECT pg_try_advisory_lock({SWEEP_LOCK})")):
        return None
    try:
        return await sweep(session, now)
    finally:
        await conn.scalar(text(f"SELECT pg_advisory_unlock({SWEEP_LOCK})"))


async def sweep_loop(interval: int = SWEEP_INTERVAL) -> None:
    """Runs for the life of the process. Each run also keeps the published
    club sites current (app/api/sites/sweep.py), under a lock of its own."""
    from app.api.deliveries import service as deliveries
    from app.api.sites import sweep as sites_sweep

    await asyncio.sleep(17)
    while True:
        try:
            async with deliveries.session_scope() as session:
                done = await run_sweep(session)
                if done and any(done.values()):
                    logger.info("domains sweep: %s", done)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("domains sweep failed")
        try:
            async with deliveries.session_scope() as session:
                made = await sites_sweep.run_refresh(session)
                if made:
                    logger.info("sites sweep: %d page(s) made again", made)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("sites sweep failed")
        await asyncio.sleep(interval)
