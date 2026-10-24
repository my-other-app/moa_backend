"""The hourly billing sweep: graces running out, trials about to bill, and
complimentary plans ending.

Five jobs, each row in its own transaction so one bad row cannot stop the
rest:

- a grace ending within two days, not yet reminded: the club is mailed and
  the admins alerted (`grace_ending`);
- a grace that has run out: cancelled at Razorpay (best effort -- logged if
  Razorpay refuses), the row cancelled and the club back on Free, or on the
  admin's grant it had before, the club mailed and the admins alerted
  (`subscription_downgraded`). Access has already ended by then whatever the
  sweep does: `is_entitled` stops counting a grace the moment it runs out;
- a trial ending within three days: once, "your trial ends on ...";
- a complimentary plan whose end has passed: back to the admin's grant the
  club had before it (previous_state) or to Free, the club mailed "Your
  complimentary ... has ended" and the admins alerted (`comp_ended`). As with
  a grace, access has already ended: `is_entitled` stops honouring a comp the
  moment its end passes;
- a complimentary plan ending within seven days: once, the club mailed and
  the admins alerted (`comp_ending`);
- a free trial (Pro, no card) whose end has passed: put away
  (service.end_free_trial) -- the club is on the paid plan it picked during
  it, or Free, mailed once "you're on Free" in that case, and the admins
  alerted (`free_trial_ended`). A club with a paid subscription running is
  never moved: only the trial's marker is cleared;
- a free trial ending within three days: once, "3 days left";
- an Enterprise trial whose end has passed: put away, the club back on what
  it had, mailed once and the admins alerted (`enterprise_trial_ended`);
- an Enterprise trial ending within three days: once, the club mailed and the
  admins alerted (`enterprise_trial_ending`);
- the months that have ended: each club's extra emails and WhatsApp messages
  on a statement, added to its Razorpay subscription as an add-on for its
  next invoice, never twice (app/api/message_quota/billing.py).

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
from app.api.subscriptions.models import ClubSubscriptions, Plans, SubscriptionStatus
from app.db import lock_keys

logger = logging.getLogger(__name__)

SWEEP_LOCK = lock_keys.SUBSCRIPTIONS_SWEEP
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
        .options(
            selectinload(ClubSubscriptions.plan), selectinload(ClubSubscriptions.club),
            selectinload(ClubSubscriptions.trial_plan), selectinload(ClubSubscriptions.enterprise_trial_plan),
        )
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
        # A plan picked during the free trial hears from _free_trial_ending.
        ClubSubscriptions.trial_plan_id.is_(None),
        ClubSubscriptions.razorpay_subscription_id.isnot(None),
        ClubSubscriptions.cancel_at_period_end == False,  # noqa: E712
        ClubSubscriptions.trial_ends_at > now,
        ClubSubscriptions.trial_ends_at <= now + service.TRIAL_REMINDER,
        ClubSubscriptions.trial_reminded_at.is_(None),
    )


def _free_trial_over(now):
    return (
        ClubSubscriptions.trial_plan_id.isnot(None),
        ClubSubscriptions.trial_ends_at <= now,
    )


def _free_trial_ending(now):
    return (
        ClubSubscriptions.trial_plan_id.isnot(None),
        ClubSubscriptions.trial_ends_at > now,
        ClubSubscriptions.trial_ends_at <= now + service.TRIAL_REMINDER,
        ClubSubscriptions.trial_reminded_at.is_(None),
    )


def _enterprise_trial_over(now):
    return (
        ClubSubscriptions.enterprise_trial_plan_id.isnot(None),
        ClubSubscriptions.enterprise_trial_ends_at <= now,
    )


def _enterprise_trial_ending(now):
    return (
        ClubSubscriptions.enterprise_trial_plan_id.isnot(None),
        ClubSubscriptions.enterprise_trial_ends_at > now,
        ClubSubscriptions.enterprise_trial_ends_at <= now + service.ENTERPRISE_TRIAL_REMINDER,
        ClubSubscriptions.enterprise_trial_reminded_at.is_(None),
    )


def _comp_over(now):
    return (
        ClubSubscriptions.complimentary == True,  # noqa: E712
        ClubSubscriptions.comp_ends_at <= now,
    )


def _comp_ending(now):
    return (
        ClubSubscriptions.complimentary == True,  # noqa: E712
        ClubSubscriptions.status == SubscriptionStatus.active,
        ClubSubscriptions.comp_ends_at > now,
        ClubSubscriptions.comp_ends_at <= now + service.COMP_REMINDER,
        ClubSubscriptions.comp_reminded_at.is_(None),
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
            await billing.cancel_subscription(sub.razorpay_subscription_id, at_period_end=False)
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


async def _after_comp(session, sub: ClubSubscriptions, now: datetime) -> tuple[str, str | None]:
    """What the club has once its comp is put away: ("the Free plan", None),
    or the earlier grant's name and its plan key."""
    if service.restorable(sub, now):
        earlier = await session.get(Plans, (sub.previous_state or {}).get("plan_id"))
        if earlier is not None:
            return earlier.name, earlier.key
    return "the Free plan", None


async def _club(session, sub: ClubSubscriptions):
    """The club, without a lazy load: the club's own routes come here with a
    row whose `club` was never loaded."""
    from app.api.clubs.models import Clubs

    return await session.get(Clubs, sub.club_id)


async def remind_comp(session, sub: ClubSubscriptions, now: datetime) -> None:
    club = await _club(session, sub)
    name = club.name if club is not None else f"Club {sub.club_id}"
    plan = sub.plan.name if sub.plan is not None else "its plan"
    then, _ = await _after_comp(session, sub, now)
    sub.comp_reminded_at = now
    await notices.email_comp_ending(session, sub, name, then=then)
    await notify(
        session, "comp_ending",
        title=f"{name}'s complimentary {plan} ends {notices.day(sub.comp_ends_at)}",
        body=f"Then {name} is on {then}. Extend it by granting it again with a later end, or let it run out.",
        club=club, subscription=sub,
        data=notices._data(sub, complimentary=True, comp_ends_at=sub.comp_ends_at, reason=sub.comp_reason),
    )


async def expire_comp(session, sub: ClubSubscriptions, now: datetime) -> str:
    """A complimentary plan's end has passed: put back the admin's grant the
    club had before it, or move it to Free; tell the club (once) and the
    admins. Returns "restored" or "cancelled". Also run by the club's own
    billing routes on a comp the sweep has not reached yet."""
    club = await _club(session, sub)
    name = club.name if club is not None else f"Club {sub.club_id}"
    plan = sub.plan.name if sub.plan is not None else "its plan"
    ends = sub.comp_ends_at
    then, then_key = await _after_comp(session, sub, now)
    data = notices._data(sub, complimentary=True, comp_ends_at=ends, reason=sub.comp_reason)

    # Recorded against the comp, before the row moves on.
    await notices.email_comp_ended(session, sub, name, plan_name=plan, then=then)
    outcome = service.end_comp(sub, now)
    await session.flush()
    await notify(
        session, "comp_ended",
        title=f"{name}'s complimentary {plan} ended",
        body=f"It ran to {notices.day(ends)}. {name} is on {then} now.",
        club=club if club is not None else sub.club_id, subscription=sub.id,
        data={**data, "outcome": outcome, "now_on": then_key or "free"},
    )
    return outcome


async def expire_free_trial(session, sub: ClubSubscriptions, now: datetime) -> str:
    """The free trial's end has passed: the club is on the plan it picked, or
    Free (mailed once). Returns "chosen_plan" or "free". Access has already
    followed the date: a trial grants nothing past its end, sweep or not."""
    club = await _club(session, sub)
    name = club.name if club is not None else f"Club {sub.club_id}"
    trial_name = sub.trial_plan.name if sub.trial_plan is not None else "Pro"
    ends = sub.trial_ends_at
    outcome = service.end_free_trial(sub, now)
    await session.flush()
    if outcome == "free":
        free = await service.plan_by_key(session, service.DEFAULT_PLAN_KEY)
        await notices.email_free_trial_ended(session, sub, name, trial=trial_name, free_plan=free)
    await notify(
        session, "free_trial_ended",
        title=f"{name}'s free {trial_name} trial ended",
        body=(f"It ran to {notices.day(ends)}. "
              + (f"{name} is on {sub.plan.name if sub.plan else 'the plan it picked'} now."
                 if outcome == "chosen_plan" else f"{name} is on Free now.")),
        club=club if club is not None else sub.club_id, subscription=sub.id,
        data=notices._data(sub, trial_ends_at=ends, outcome=outcome),
    )
    return outcome


async def remind_free_trial(session, sub: ClubSubscriptions, now: datetime) -> None:
    club = await _club(session, sub)
    name = club.name if club is not None else f"Club {sub.club_id}"
    sub.trial_reminded_at = now
    await notices.email_free_trial_ending(session, sub, name, chosen=service.chosen_plan(sub, now))


async def _then_name(session, sub: ClubSubscriptions, now: datetime) -> str:
    then = await service.plan_after_trials(session, sub, now)
    if then is None or then.key == service.DEFAULT_PLAN_KEY:
        return "the Free plan"
    if service.free_trial_live(sub, now):
        return f"its {then.name} trial (to {notices.day(sub.trial_ends_at)})"
    return then.name


async def expire_enterprise_trial(session, sub: ClubSubscriptions, now: datetime) -> str:
    """An Enterprise trial's end has passed: the club is back on what it had.
    Returns the plan key it is on now."""
    club = await _club(session, sub)
    name = club.name if club is not None else f"Club {sub.club_id}"
    ends = sub.enterprise_trial_ends_at
    service.end_enterprise_trial(sub, now)
    then = await _then_name(session, sub, now)
    after = await service.plan_after_trials(session, sub, now)
    await session.flush()
    await notices.email_enterprise_trial_ended(session, sub, name, then=then)
    await notify(
        session, "enterprise_trial_ended",
        title=f"{name}'s Enterprise trial ended",
        body=f"It ran to {notices.day(ends)}. {name} is on {then} now. Set a price or a complimentary plan if you agreed one.",
        club=club if club is not None else sub.club_id, subscription=sub.id,
        data=notices._data(sub, enterprise_trial_ends_at=ends, now_on=after.key if after else "free"),
    )
    return after.key if after else service.DEFAULT_PLAN_KEY


async def remind_enterprise_trial(session, sub: ClubSubscriptions, now: datetime) -> None:
    club = await _club(session, sub)
    name = club.name if club is not None else f"Club {sub.club_id}"
    sub.enterprise_trial_reminded_at = now
    # What the club has once the trial is over, as of its end.
    then = await _then_name(session, sub, sub.enterprise_trial_ends_at)
    await notices.email_enterprise_trial_ending(session, sub, name, then=then)
    await notify(
        session, "enterprise_trial_ending",
        title=f"{name}'s Enterprise trial ends {notices.day(sub.enterprise_trial_ends_at)}",
        body=f"Then {name} is on {then}. Grant it again for longer, or set the price you agreed.",
        club=club, subscription=sub,
        data=notices._data(sub, enterprise_trial_ends_at=sub.enterprise_trial_ends_at),
    )


async def sweep(session, now: datetime | None = None) -> dict:
    """One run of the jobs. Returns how many rows each touched."""
    now = now or _now()
    done = {"downgraded": 0, "grace_reminded": 0, "trial_reminded": 0, "comp_ended": 0, "comp_reminded": 0,
            "free_trial_ended": 0, "free_trial_reminded": 0,
            "enterprise_trial_ended": 0, "enterprise_trial_reminded": 0}
    for key, criteria, job in (
        ("downgraded", _grace_over(now), expire_grace),
        ("grace_reminded", _grace_ending(now), remind_grace),
        ("trial_reminded", _trial_ending(now), remind_trial),
        ("comp_ended", _comp_over(now), expire_comp),
        ("comp_reminded", _comp_ending(now), remind_comp),
        ("enterprise_trial_ended", _enterprise_trial_over(now), expire_enterprise_trial),
        ("enterprise_trial_reminded", _enterprise_trial_ending(now), remind_enterprise_trial),
        ("free_trial_ended", _free_trial_over(now), expire_free_trial),
        ("free_trial_reminded", _free_trial_ending(now), remind_free_trial),
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
    from app.api.message_quota import billing as message_billing

    done.update({"message_statements": 0, "overage_charged": 0, "overage_failed": 0})
    try:
        done.update(await message_billing.run(session, now))
    except Exception:  # noqa: BLE001
        logger.exception("billing sweep: the message statements failed")
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
