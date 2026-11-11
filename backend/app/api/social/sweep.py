"""Social posting's minute and its day, on one worker at a time.

Every minute (an advisory lock on its own connection): every target that is
due and may have lost its job is queued again, and every claim gone stale
(scheduler.dispatch). Once a day after 06:00 IST, the job
`social.refresh_tokens`:

- tokens that can be refreshed and run out within seven days are (X's and
  LinkedIn's where LinkedIn gave a refresh token); one the platform refuses
  marks its account `needs_reconnect`, its posts held;
- an account whose access ends within seven days and cannot be refreshed
  (LinkedIn every 60 days) gets one `social_expiring` notice per expiry,
  and when it has ended it needs reconnecting;
- the day's posts per platform (posted, failed, waiting) are written to the
  log and kept on the run's row; a platform whose failures jump (5 or more,
  more than it posted) is an admin alert, `social_failures` -- usually the
  platform changed something. (There is no ops Telegram bot to send the
  summary to; when one exists, this is where it goes.)
- sign-in states older than two days are deleted.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, func, select

from app.api.models import ScheduledRuns
from app.api.social import accounts as accounts_mod
from app.api.social import alerts, crypto, scheduler
from app.api.social import http as h
from app.api.social.adapters import for_platform
from app.api.social.models import CONNECTED, FAILED, POSTED, WAITING, SocialAccounts, SocialOAuthStates, SocialPostTargets
from app.core import jobs
from app.db import lock_keys
from app.db.locks import singleton

logger = logging.getLogger(__name__)

SWEEP_LOCK = lock_keys.SOCIAL_SWEEP
SWEEP_INTERVAL = 60
DAILY_JOB = "social.refresh_tokens"
DAILY_RUN = "social_daily"
DAILY_AFTER_HOUR = 6
WARN_AHEAD = timedelta(days=7)
IST = scheduler.IST


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def maybe_daily(session, now: Optional[datetime] = None) -> bool:
    """Queue the day's job once per IST day, after 06:00. True when queued."""
    now = now or _now()
    local = now.astimezone(IST)
    if local.hour < DAILY_AFTER_HOUR:
        return False
    row = await session.get(ScheduledRuns, DAILY_RUN)
    if row is not None and row.started_at is not None and row.started_at.astimezone(IST).date() >= local.date():
        return False
    if row is None:
        row = ScheduledRuns(name=DAILY_RUN)
        session.add(row)
    row.started_at = now
    await jobs.enqueue(session, DAILY_JOB, {"date": local.date().isoformat()}, dedupe_key=f"{DAILY_JOB}:{local.date().isoformat()}")
    await session.commit()
    return True


async def run_sweep(session, now: Optional[datetime] = None) -> Optional[int]:
    async with singleton(session, SWEEP_LOCK) as mine:
        if not mine:
            return None
        queued = await scheduler.dispatch(session, now)
        await maybe_daily(session, now)
        return queued


async def sweep_loop(interval: int = SWEEP_INTERVAL) -> None:
    from app.api.deliveries import service as deliveries

    await asyncio.sleep(29)
    while True:
        try:
            async with deliveries.session_scope() as session:
                done = await run_sweep(session)
                if done:
                    logger.info("social sweep: queued %s post(s)", done)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("social sweep failed")
        await asyncio.sleep(interval)


# ─── The day ──────────────────────────────────────────────────────────────


async def refresh_tokens(session, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    found = {"refreshed": 0, "warned": 0, "ended": 0, "refused": 0}
    if not crypto.available():
        return found
    ids = list(await session.scalars(
        select(SocialAccounts.id).where(
            SocialAccounts.status == CONNECTED, SocialAccounts.token_expires_at.is_not(None),
            SocialAccounts.token_expires_at <= now + WARN_AHEAD,
        )
    ))
    for account_id in ids:
        try:
            account = await session.get(SocialAccounts, account_id, populate_existing=True)
            if account is None or account.status != CONNECTED:
                continue
            c = accounts_mod.creds(account)
            if c.refresh_token:
                try:
                    tokens = await for_platform(account.platform).refresh(c)
                except h.PlatformError as exc:
                    if exc.kind in (h.AUTH, h.REFUSED, h.PERMISSION):
                        await accounts_mod.needs_reconnect(session, account, "The platform no longer accepts this connection.")
                        found["refused"] += 1
                    await session.commit()
                    continue
                if tokens is not None:
                    account.access_token_enc = crypto.seal(tokens.token)
                    account.refresh_token_enc = crypto.seal(tokens.refresh_token)
                    account.token_expires_at = tokens.expires_at
                    if tokens.refresh_expires_at:
                        account.refresh_expires_at = tokens.refresh_expires_at
                    account.refreshed_at = now
                    await session.commit()
                    found["refreshed"] += 1
                    continue
            if account.token_expires_at <= now:
                await accounts_mod.needs_reconnect(session, account, "Access ended: reconnect to keep posting.")
                found["ended"] += 1
            elif account.expiry_warned_for != account.token_expires_at:
                days = max(0, (account.token_expires_at - now).days)
                await alerts.account_expiring(session, account, days)
                account.expiry_warned_for = account.token_expires_at
                found["warned"] += 1
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - one account never stops the rest
            logger.warning("social daily: account %s (%s)", account_id, type(exc).__name__)
            await session.rollback()
    return found


async def summary(session, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    since = now - timedelta(days=1)
    T = SocialPostTargets
    rows = (await session.execute(
        select(T.platform,
               func.count().filter(T.status == POSTED, T.posted_at >= since),
               func.count().filter(T.status == FAILED, T.updated_at >= since),
               func.count().filter(T.status.in_(WAITING)))
        .group_by(T.platform)
    )).all()
    out = {p: {"posted": int(a), "failed": int(b), "waiting": int(c)} for p, a, b, c in rows}
    for platform, n in out.items():
        logger.info("social daily: %s posted %s, failed %s, waiting %s", platform, n["posted"], n["failed"], n["waiting"])
        if n["failed"] >= 5 and n["failed"] > n["posted"]:
            from app.api.admin_alerts import notify

            await notify(session, "social_failures", title=f"{alerts.LABELS.get(platform, platform)} posts are failing",
                         body=f"{n['failed']} failed and {n['posted']} posted in the last day: the platform may have changed something.",
                         data={"platform": platform, **n}, dedupe_on={"platform": platform})
    await session.commit()
    return out


@jobs.handler(DAILY_JOB, max_attempts=3, timeout=900.0, concurrency=1, priority=jobs.LOW)
async def _daily(p: dict) -> None:
    async with jobs.scope() as session:
        now = _now()
        result = {"tokens": await refresh_tokens(session, now), "platforms": await summary(session, now)}
        await session.execute(delete(SocialOAuthStates).where(SocialOAuthStates.created_at < now - timedelta(days=2)))
        row = await session.get(ScheduledRuns, DAILY_RUN)
        if row is not None:
            row.finished_at, row.result = _now(), result
        await session.commit()
