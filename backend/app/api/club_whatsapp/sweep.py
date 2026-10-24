"""Clubs' own WhatsApp numbers, every fifteen minutes, one worker at a time.

- Quality: each live number not asked about for CLUB_WHATSAPP_QUALITY_HOURS
  is read from Meta again -- its quality rating and messaging limit, logged
  when they change, the club told when they drop. A number a check had
  failed that Meta answers for again is active again (the club fixed it in
  Meta); one Meta refused a send for stays failed until new keys. A number
  whose club's plan lapsed is not asked: its routing is turned off, once.
- Templates: each active number's, read from Meta again every
  CLUB_WHATSAPP_QUALITY_HOURS, so what a ticket send decides by is current.
- Retention: for each club that ever connected a number, its messages older
  than its retention period (12 months unless it chose otherwise) are
  deleted, with the conversations left empty and those that never had one.

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

from app.api.club_whatsapp import crypto, graph, inbox, live
from app.api.club_whatsapp import service as accounts
from app.api.club_whatsapp.models import ACTIVE, DEFAULT_RETENTION_MONTHS, LIVE, ClubWhatsAppAccounts
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
    """Ask Meta about each live number due a check. Returns how many were
    asked. No lock is held while Meta answers: the answer is applied after,
    to the row as it is then, and not at all when its keys changed meanwhile
    (the club saved new ones). A number whose plan lapsed is not asked (its
    routing is turned off, the club told once)."""
    now = now or _now()
    if not crypto.available():
        return 0
    A = ClubWhatsAppAccounts
    due = now - timedelta(hours=int(settings.CLUB_WHATSAPP_QUALITY_HOURS or 6))
    ids = (await session.scalars(
        select(A.id)
        .where(A.status.in_(LIVE), or_(A.quality_checked_at.is_(None), A.quality_checked_at <= due))
        .order_by(A.quality_checked_at.asc().nulls_first(), A.id)
        .limit(BATCH)
    )).all()
    done = 0
    for account_id in ids:
        try:
            account = await session.get(A, account_id)
            if account is None or account.status not in LIVE:
                continue
            if not await accounts.plan_allows(session, account.club_id):
                await accounts.plan_lapsed(session, account)
                account.quality_checked_at = now
                await session.commit()
                continue
            sealed = account.access_token_enc
            try:
                keys = accounts.keys_of(account)
            except crypto.Unavailable:
                keys = None
            await session.commit()
            answer = None
            if keys is not None:
                try:
                    answer = await graph.phone_number(keys)
                except graph.GraphError as exc:
                    answer = exc
            account = await session.scalar(
                select(A).where(A.id == account_id).with_for_update().execution_options(populate_existing=True)
            )
            if account is None or account.status not in LIVE or account.access_token_enc != sealed:
                await session.commit()
                continue
            if answer is None:
                await accounts.mark_failed(session, account, "The saved keys can't be read any more. Paste them again.", "KEYS",
                                           source=accounts.READ)
            else:
                await accounts.checked(session, account, answer, "sweep")
            account.quality_checked_at = now
            await session.commit()
            done += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("whatsapp sweep: number %s could not be checked (%s)", account_id, type(exc).__name__)
            await session.rollback()
    return done


async def refresh_templates(session, now: Optional[datetime] = None) -> int:
    """Each active number's templates, read from Meta again when they were
    last read CLUB_WHATSAPP_QUALITY_HOURS ago (a missed webhook, an edit in
    WhatsApp Manager): what a ticket send decides by stays current. Not for
    a number the plan no longer covers. Returns how many were read."""
    from app.api.club_whatsapp import templates

    now = now or _now()
    if not crypto.available():
        return 0
    due = now - timedelta(hours=int(settings.CLUB_WHATSAPP_QUALITY_HOURS or 6))
    A = ClubWhatsAppAccounts
    ids = (await session.scalars(
        select(A.id).where(A.status == ACTIVE, or_(A.templates_synced_at.is_(None), A.templates_synced_at <= due))
        .order_by(A.templates_synced_at.asc().nulls_first(), A.id).limit(BATCH)
    )).all()
    done = 0
    for account_id in ids:
        try:
            account = await session.get(A, account_id)
            if account is None or account.status != ACTIVE or not await accounts.plan_allows(session, account.club_id):
                continue
            keys = accounts.keys_of(account)
            await session.commit()
            # Nothing held while Meta answers.
            await templates.sync(session, account, keys)
            await session.commit()
            done += 1
        except (graph.GraphError, crypto.Unavailable) as exc:
            logger.info("whatsapp sweep: templates of number %s not read (%s)", account_id, type(exc).__name__)
            await session.rollback()
        except Exception as exc:  # noqa: BLE001
            logger.warning("whatsapp sweep: templates of number %s not read (%s)", account_id, type(exc).__name__)
            await session.rollback()
    return done


async def retention(session, now: Optional[datetime] = None) -> int:
    """Each club's messages past its retention period, deleted. Returns how many."""
    now = now or _now()
    # The clubs that ever connected a number (a disconnected one's inbox is kept too, until its time).
    clubs = (await session.scalars(select(func.distinct(ClubWhatsAppAccounts.club_id)))).all()
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


async def keys_readable(session) -> bool:
    """Whether the server can read the clubs' keys (APP_WHATSAPP_KEYS_SECRET
    set, and the key they were sealed with among its keys). When a live
    number's can't be, every club's number is unusable -- their sends go
    from the platform's -- and the admins are told, once a sweep."""
    from app.api.admin_alerts import notify

    A = ClubWhatsAppAccounts
    sealed = await session.scalar(select(A.access_token_enc).where(A.status.in_(LIVE), A.access_token_enc.isnot(None)).limit(1))
    if sealed is None:
        return True
    if crypto.available():
        try:
            crypto.decrypt(sealed)
            return True
        except crypto.Unavailable:
            why = "isn't the key the clubs' keys were saved with"
    else:
        why = "is missing or isn't a Fernet key"
    logger.error("whatsapp sweep: APP_WHATSAPP_KEYS_SECRET %s; clubs' own numbers can't be used", why)
    await notify(
        session, "club_whatsapp_keys",
        title=f"Clubs' own WhatsApp numbers can't be used: APP_WHATSAPP_KEYS_SECRET {why}",
        body="Their tickets go from the platform's number, their webhooks are answered 503 (Meta delivers again for 7 "
             "days) and nothing can be sent from their inboxes. Restore the key (a new one goes first, the old after it).",
        data={"code": "keys_secret"}, dedupe=False,
    )
    await session.commit()
    return False


async def sweep(session, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    await keys_readable(session)
    return {
        "checked": await refresh_quality(session, now),
        "templates": await refresh_templates(session, now),
        "forgotten": await retention(session, now),
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
