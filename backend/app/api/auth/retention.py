"""Housekeeping for sign-in records: what can never be used again goes.

- A session (user_sessions) goes once nothing can bring it back: signed out
  a day ago or more (a day kept, for anyone looking into why), past its
  hard end (an admin's session as someone else), or idle for longer than a
  refresh token lives (REFRESH_TOKEN_EXPIRE_DAYS): no token for it can be
  renewed any more. Every request reads its session row and every
  sign-in adds one; at a crore of accounts the table only grew.
- A sign-in attempt (sign_in_attempts) goes after SIGN_IN_ATTEMPTS_KEEP_DAYS
  (180): the Security page shows sixty days, the lockout an hour.

In batches of BATCH rows, each its own short transaction, so the sweep never
holds a long lock or a long transaction. One process at a time (an advisory
lock), every ACCOUNT_SWEEP_SECONDS.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, select

from app.api.auth.sessions import UserSessions
from app.api.security.models import SignInAttempts
from app.config import settings
from app.db.locks import singleton

logger = logging.getLogger(__name__)

SWEEP_LOCK = 773_130
BATCH = 5000
#: How long a signed-out session is kept before it goes.
REVOKED_KEEP = timedelta(days=1)


async def _delete_in_batches(session, model, condition) -> int:
    total = 0
    while True:
        ids = select(model.id).where(condition).limit(BATCH)
        result = await session.execute(
            delete(model).where(model.id.in_(ids)).execution_options(synchronize_session=False)
        )
        await session.commit()
        removed = int(result.rowcount or 0)
        total += removed
        if removed < BATCH:
            return total


async def purge_sessions(session, now: datetime | None = None) -> int:
    """Each condition on its own, so each is a range on one index."""
    now = now or datetime.now(timezone.utc)
    idle_since = now - timedelta(days=max(1, int(settings.REFRESH_TOKEN_EXPIRE_DAYS)))
    removed = 0
    for gone in (
        UserSessions.last_seen_at < idle_since,
        and_(UserSessions.last_seen_at.is_(None), UserSessions.created_at < idle_since),
        and_(UserSessions.revoked == True, UserSessions.last_seen_at < now - REVOKED_KEEP),  # noqa: E712
        UserSessions.expires_at < now - REVOKED_KEEP,
    ):
        removed += await _delete_in_batches(session, UserSessions, gone)
    return removed


async def purge_sign_in_attempts(session, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    before = now - timedelta(days=max(1, int(settings.SIGN_IN_ATTEMPTS_KEEP_DAYS)))
    return await _delete_in_batches(session, SignInAttempts, SignInAttempts.created_at < before)


async def purge(session, now: datetime | None = None) -> dict:
    return {
        "sessions": await purge_sessions(session, now),
        "sign_in_attempts": await purge_sign_in_attempts(session, now),
    }


async def sweep_loop() -> None:
    """For the life of the process; the work on one worker at a time."""
    from app.db.core import AsyncSessionLocal
    from app.db.listeners import add_loader_criteria

    await asyncio.sleep(90)
    while True:
        try:
            async with AsyncSessionLocal() as session:
                add_loader_criteria(session)
                async with singleton(session, SWEEP_LOCK) as mine:
                    if mine:
                        removed = await purge(session)
                        if any(removed.values()):
                            logger.info("account sweep removed %s", removed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("account sweep failed")
        await asyncio.sleep(max(60, int(settings.ACCOUNT_SWEEP_SECONDS)))
