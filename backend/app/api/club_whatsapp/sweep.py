"""Clubs' own WhatsApp numbers, every fifteen minutes, one worker at a time.

- Quality: each live number not asked about for CLUB_WHATSAPP_QUALITY_HOURS
  is read from Meta again -- its quality rating and messaging limit, logged
  when they change, the club told when they drop. A failed number that Meta
  answers for again is active again (the club fixed it in Meta).
- Retention: each club's messages older than its retention period (12
  months unless it chose otherwise) are deleted, with the conversations left
  empty.

Each number, and each club's retention, in its own transaction: one bad row
cannot stop the rest. One worker at a time, by a Postgres advisory lock on
its own connection, as the domains sweep does.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.club_whatsapp import crypto, inbox, live
from app.api.club_whatsapp import service as accounts
from app.api.club_whatsapp.models import DEFAULT_RETENTION_MONTHS, LIVE, ClubWhatsAppAccounts, ClubWhatsAppMessages
from app.config import settings
from app.db import lock_keys

logger = logging.getLogger(__name__)

SWEEP_LOCK = lock_keys.CLUB_WHATSAPP_SWEEP
SWEEP_INTERVAL = 900
#: Numbers asked about per run; the rest wait for the next.
BATCH = 50


def _now() -> datetime:
    return datetime.now(timezone.utc)


def retention_cutoff(months: Optional[int], now: datetime) -> datetime:
    return now - relativedelta(months=int(months or DEFAULT_RETENTION_MONTHS))


async def refresh_quality(session, now: Optional[datetime] = None) -> int:
    """Ask Meta about each live number due a check. Returns how many were asked."""
    now = now or _now()
    if not crypto.available():
        return 0
    due = now - timedelta(hours=int(settings.CLUB_WHATSAPP_QUALITY_HOURS or 6))
    ids = (await session.scalars(
        select(ClubWhatsAppAccounts.id)
        .where(ClubWhatsAppAccounts.status.in_(LIVE),
               or_(ClubWhatsAppAccounts.quality_checked_at.is_(None), ClubWhatsAppAccounts.quality_checked_at <= due))
        .order_by(ClubWhatsAppAccounts.quality_checked_at.asc().nulls_first(), ClubWhatsAppAccounts.id)
        .limit(BATCH)
    )).all()
    done = 0
    for account_id in ids:
        try:
            account = await session.scalar(
                select(ClubWhatsAppAccounts).where(ClubWhatsAppAccounts.id == account_id, ClubWhatsAppAccounts.status.in_(LIVE))
                .with_for_update(skip_locked=True).execution_options(populate_existing=True)
            )
            if account is None:
                continue
            await accounts.recheck(session, account, source="sweep")
            account.quality_checked_at = now
            await session.commit()
            done += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("whatsapp sweep: number %s could not be checked (%s)", account_id, type(exc).__name__)
            await session.rollback()
    return done


async def retention(session, now: Optional[datetime] = None) -> int:
    """Each club's messages past its retention period, deleted. Returns how many."""
    now = now or _now()
    clubs = (await session.scalars(select(func.distinct(ClubWhatsAppMessages.club_id)))).all()
    deleted = 0
    for club_id in clubs:
        try:
            latest = await accounts.latest_account(session, club_id)
            cutoff = retention_cutoff(latest.retention_months if latest is not None else None, now)
            mine = 0
            while True:
                n = await inbox.forget_before(session, club_id, cutoff)
                await session.commit()
                mine += n
                if n < 1000:
                    break
            deleted += mine
            if mine:
                await live.changed(club_id, None, "conversation")
        except Exception as exc:  # noqa: BLE001
            logger.warning("whatsapp sweep: retention failed for club %s (%s)", club_id, type(exc).__name__)
            await session.rollback()
    return deleted


async def sweep(session, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    return {"checked": await refresh_quality(session, now), "forgotten": await retention(session, now)}


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

    await asyncio.sleep(29)
    while True:
        try:
            async with deliveries.session_scope() as session:
                done = await run_sweep(session)
                if done and any(done.values()):
                    logger.info("whatsapp sweep: %s", done)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("whatsapp sweep failed (%s)", type(exc).__name__)
        await asyncio.sleep(interval)
