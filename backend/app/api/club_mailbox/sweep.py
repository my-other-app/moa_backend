"""Clubs' mailboxes, every fifteen minutes, one worker at a time.

- The plan: each mailbox whose club's plan no longer includes it lapses
  (service.check_lapse: the owner told; nothing new accepted or sent; the
  mail readable 30 days, then export or delete, the owner told again); one
  whose plan includes it again opens as it was.
- Retention: each club's mail older than its retention period (12 months
  unless it chose 1 to 36) is deleted with its files, and the threads left
  empty with it.
- Old addresses: a changed handle whose 90 days of forwarding are over is
  let go, so another club may take it.

Each club in its own transaction: one bad row can't stop the rest. One
worker at a time, by a Postgres advisory lock on its own connection, as the
WhatsApp sweep does.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from dateutil.relativedelta import relativedelta
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.club_mailbox import live, mail, service, store
from app.api.club_mailbox.models import DEFAULT_RETENTION_MONTHS, ClubMailboxAddresses, ClubMailboxes
from app.db import lock_keys

logger = logging.getLogger(__name__)

SWEEP_LOCK = lock_keys.CLUB_MAILBOX_SWEEP
SWEEP_INTERVAL = 900


def _now() -> datetime:
    return datetime.now(timezone.utc)


def retention_cutoff(months: Optional[int], now: datetime) -> datetime:
    return now - relativedelta(months=int(months or DEFAULT_RETENTION_MONTHS))


async def lapses(session, now: Optional[datetime] = None) -> dict:
    """Each mailbox's lapse brought in line with its plan. {what changed: how many}."""
    now = now or _now()
    out: dict = {}
    ids = (await session.scalars(select(ClubMailboxes.id).order_by(ClubMailboxes.id))).all()
    for mailbox_id in ids:
        try:
            mailbox = await session.get(ClubMailboxes, mailbox_id)
            if mailbox is None:
                continue
            what = await service.check_lapse(session, mailbox, now)
            await session.commit()
            if what:
                out[what] = out.get(what, 0) + 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("mailbox sweep: the plan of mailbox %s couldn't be checked (%s)", mailbox_id, type(exc).__name__)
            await session.rollback()
    return out


async def retention(session, now: Optional[datetime] = None) -> int:
    """Each club's mail past its retention period, deleted. Returns how many mails."""
    now = now or _now()
    rows = (await session.execute(select(ClubMailboxes.club_id, ClubMailboxes.retention_months))).all()
    deleted = 0
    for club_id, months in rows:
        try:
            cutoff = retention_cutoff(months, now)
            mine = 0
            while True:
                n, keys = await mail.forget_before(session, club_id, cutoff)
                await session.commit()
                await asyncio.to_thread(store.delete, keys)
                mine += n
                if n < 500:
                    break
            deleted += mine
            if mine:
                await live.changed(club_id, None, "thread")
        except Exception as exc:  # noqa: BLE001
            logger.warning("mailbox sweep: retention failed for club %s (%s)", club_id, type(exc).__name__)
            await session.rollback()
    return deleted


async def release_addresses(session, now: Optional[datetime] = None) -> int:
    """Old handles whose forwarding has ended, let go. Returns how many."""
    now = now or _now()
    A = ClubMailboxAddresses
    result = await session.execute(delete(A).where(A.retired_at.isnot(None), A.forward_until <= now))
    await session.commit()
    return int(result.rowcount or 0)


async def sweep(session, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    return {
        "lapses": await lapses(session, now),
        "forgotten": await retention(session, now),
        "released": await release_addresses(session, now),
    }


async def run_sweep(session, now: Optional[datetime] = None) -> Optional[dict]:
    """The sweep, if no other worker is running it. None when one is."""
    bind = session.bind
    if isinstance(bind, AsyncEngine):
        async with bind.connect() as lock_conn:
            return await _locked(lock_conn, session, now)
    return await _locked(bind, session, now)


async def _locked(conn, session, now) -> Optional[dict]:
    if not await conn.scalar(text(f"SELECT pg_try_advisory_lock({SWEEP_LOCK})")):
        return None
    try:
        return await sweep(session, now)
    finally:
        await conn.scalar(text(f"SELECT pg_advisory_unlock({SWEEP_LOCK})"))


async def sweep_loop(interval: int = SWEEP_INTERVAL) -> None:
    from app.api.deliveries import service as deliveries

    await asyncio.sleep(37)
    while True:
        try:
            # Whether mailboxes are on or not: mail kept before still has its retention.
            async with deliveries.session_scope() as session:
                done = await run_sweep(session)
                if done and any(done.values()):
                    logger.info("mailbox sweep: %s", done)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("mailbox sweep failed (%s)", type(exc).__name__)
        await asyncio.sleep(interval)
