"""The hourly billing sweep: graces running out, and trials about to bill.

Three jobs, each row in its own transaction so one bad row cannot stop the
rest:

- a grace ending within two days, not yet reminded: the club is mailed and
  the admins alerted (`grace_ending`);
- a grace that has run out: cancelled at Razorpay (best effort -- logged if
  Razorpay refuses), the row cancelled and the club back on Free, or on the
  admin's grant it had before, the club mailed and the admins alerted
  (`subscription_downgraded`). Access has already ended by then whatever the
  sweep does: `is_entitled` stops counting a grace the moment it runs out;
- a trial ending within three days: once, "your trial ends on ...".

One worker at a time, by a Postgres advisory lock held on its own connection
(a session gives its connection back at every commit, and a session-level
lock must be released on the connection that took it).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import selectinload

from app.api.admin_alerts import notify
from app.api.subscriptions import billing, notices, service
from app.api.subscriptions.models import ClubSubscriptions, SubscriptionStatus

logger = logging.getLogger(__name__)

SWEEP_LOCK = 773_110
SWEEP_INTERVAL = 3600
#: Rows per job per run; anything left waits an hour.
BATCH = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _ids(session, *criteria) -> list[int]:
    return list(
        (
            await session.execute(
                select(ClubSubscriptions.id)
                .where(ClubSubscriptions.is_deleted == False, *criteria)  # noqa: E712
                .order_by(ClubSubscriptions.id)
                .limit(BATCH)
            )
        ).scalars()
    )


async def _lock_row(session, sub_id: int, *criteria) -> ClubSubscriptions | None:
    """The row, locked, if it still matches -- a webhook may have moved it
    since the list was read. A row another transaction holds is skipped."""
    return await session.scalar(
        select(ClubSubscriptions)
        .where(ClubSubscriptions.id == sub_id, *criteria)
        .options(selectinload(ClubSubscriptions.plan), selectinload(ClubSubscriptions.club))
        .with_for_update(skip_locked=True, of=ClubSubscriptions)
        .execution_options(populate_existing=True)
    )


def _grace_ending(now):
    return (
        ClubSubscriptions.status == SubscriptionStatus.past_due,
        ClubSubscriptions.grace_ends_at > now,
        ClubSubscriptions.grace_ends_at <= now + service.GRACE_REMINDER,
        ClubSubscriptions.grace_reminded_at.is_(None),
    )


def _grace_over(now):
    return (
        ClubSubscriptions.status == SubscriptionStatus.past_due,
        ClubSubscriptions.grace_ends_at <= now,
    )


def _trial_ending(now):
    return (
        ClubSubscriptions.status == SubscriptionStatus.trial,
        ClubSubscriptions.razorpay_subscription_id.isnot(None),
        ClubSubscriptions.cancel_at_period_end == False,  # noqa: E712
        ClubSubscriptions.trial_ends_at > now,
        ClubSubscriptions.trial_ends_at <= now + service.TRIAL_REMINDER,
        ClubSubscriptions.trial_reminded_at.is_(None),
    )


async def remind_grace(session, sub: ClubSubscriptions, now: datetime) -> None:
    name = sub.club.name if sub.club is not None else f"Club {sub.club_id}"
    plan = sub.plan.name if sub.plan is not None else "its plan"
    sub.grace_reminded_at = now
    await notices.email_grace_ending(session, sub, name)
    await notify(
        session, "grace_ending",
        title=f"{name}'s grace ends {notices.day(sub.grace_ends_at)}",
        body=f"Still unpaid. {name} moves from {plan} to Free on {notices.day(sub.grace_ends_at)} unless the charge goes through.",
        club=sub.club, subscription=sub,
        data=notices._data(sub, grace_ends_at=sub.grace_ends_at, reason=sub.last_failure_reason),
    )


async def expire_grace(session, sub: ClubSubscriptions, now: datetime) -> str:
    """The grace ran out: stop Razorpay, move the club to Free (or back to an
    admin's grant), tell the club and the admins. Returns what happened."""
    name = sub.club.name if sub.club is not None else f"Club {sub.club_id}"
    plan = sub.plan.name if sub.plan is not None else "its plan"
    grace_ends = sub.grace_ends_at
    data = notices._data(sub, halted_at=sub.halted_at, grace_ends_at=grace_ends, reason=sub.last_failure_reason)

    stopped = None
    if sub.razorpay_subscription_id and sub.cancel_requested_at is not None:
        # The club (or an admin) already had Razorpay cancel it.
        stopped = "already_requested"
    elif sub.razorpay_subscription_id:
        try:
            await asyncio.to_thread(billing.cancel_subscription, sub.razorpay_subscription_id, at_period_end=False)
            stopped = True
        except Exception:  # noqa: BLE001
            # Halted subscriptions do not charge on their own; the row is
            # cancelled here either way, and the admin is told to check.
            stopped = False
            logger.warning("Razorpay would not cancel %s after its grace ran out", sub.razorpay_subscription_id, exc_info=True)

    # Recorded against the subscription that lapsed, before the row moves on.
    await notices.email_downgraded(session, sub, name, plan_name=plan, when=now)
    outcome = service.end_now(sub, now)
    await session.flush()
    await notify(
        session, "subscription_downgraded",
        title=f"{name} moved to {'its earlier plan' if outcome == 'restored' else 'Free'} after its grace ran out",
        body=(
            f"{plan} was not paid by {notices.day(grace_ends)}."
            + ("" if stopped is not False else " Razorpay did not confirm the cancellation: check it in Razorpay's dashboard.")
        ),
        club=sub.club, subscription=sub,
        data={**data, "outcome": outcome, "razorpay_cancelled": stopped},
    )
    return outcome


async def remind_trial(session, sub: ClubSubscriptions, now: datetime) -> None:
    name = sub.club.name if sub.club is not None else f"Club {sub.club_id}"
    sub.trial_reminded_at = now
    await notices.email_trial_ending(session, sub, name)


async def sweep(session, now: datetime | None = None) -> dict:
    """One run of the three jobs. Returns how many rows each touched."""
    now = now or _now()
    done = {"downgraded": 0, "grace_reminded": 0, "trial_reminded": 0}
    for key, criteria, job in (
        ("downgraded", _grace_over(now), expire_grace),
        ("grace_reminded", _grace_ending(now), remind_grace),
        ("trial_reminded", _trial_ending(now), remind_trial),
    ):
        for sub_id in await _ids(session, *criteria):
            try:
                sub = await _lock_row(session, sub_id, *criteria)
                if sub is None:
                    continue
                await job(session, sub, now)
                await session.commit()
                done[key] += 1
            except Exception:  # noqa: BLE001
                logger.exception("billing sweep: %s failed for subscription %s", key, sub_id)
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
    """Runs for the life of the process."""
    from app.api.deliveries import service as deliveries

    await asyncio.sleep(13)
    while True:
        try:
            async with deliveries.session_scope() as session:
                done = await run_sweep(session)
                if done and any(done.values()):
                    logger.info("billing sweep: %s", done)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("billing sweep failed")
        await asyncio.sleep(interval)
