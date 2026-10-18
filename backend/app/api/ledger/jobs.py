"""The ledger's daily job, on one worker, once a day after LEDGER_DAILY_AT (IST).

1. retry: postings that failed since the last run (each left an admin alert,
   `ledger_post_failed`, naming its source) are posted again from the
   source's rows as they stand now.
2. reconcile: ledger_monthly worked out again from the entries, club by
   club, and ledger_platform_monthly from ledger_monthly, month by month;
   what had drifted is corrected and the admins hear (`ledger_drift`). The
   clubs with entries since the last run, and a rolling slice of the rest,
   so every club is checked within ROLLING_DAYS.
3. balance: postings since the last run whose lines do not sum to zero
   (`ledger_unbalanced`).
4. razorpay: the day before's payments and refunds at Razorpay, for the
   platform's account and every club's own, against the ledger
   (recon.reconcile_day; `ledger_mismatch`).

One worker at a time, as the payments sweep: the run is claimed in
scheduled_runs under a transaction-scoped advisory lock, let go as the claim
commits. The run then holds nothing across Razorpay's answers, and each club
or month it corrects is its own short transaction.

Why a correction can be made while payments land: the rows being corrected
are locked first (FOR UPDATE), then the entries summed, and the difference
is ADDED to the row, never written over it. A posting that has written its
entries but not yet its sums is waiting for the lock; its entries are not
visible to the sum (not committed) and its own UPSERT adds them after. A
posting that makes a row the correction does not see adds to it the same way.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import Date, cast, delete, func, literal_column, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.ledger import book
from app.api.ledger.models import LedgerEntries as E
from app.api.ledger.models import LedgerMonthly as LM
from app.api.ledger.models import LedgerPlatformMonthly as LP

logger = logging.getLogger(__name__)

JOB = "ledger_daily"
LOCK = 773_131
#: How often each worker looks whether the job is due.
LOOK_EVERY_SECONDS = 300
FIRST_LOOK_SECONDS = 180
#: Every club's sums are checked at least this often (the rolling slice).
ROLLING_DAYS = 7
#: How long one run may take before it leaves the rest to the next.
RUN_BUDGET_SECONDS = 3 * 3600

# Rendered inline, not bound: the same expression in SELECT and GROUP BY
# must read the same to Postgres (app/core/utils/time_buckets.py).
_IST_ZONE = literal_column("'Asia/Kolkata'")
_MONTH = literal_column("'month'")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ist_month(column):
    """`column`'s calendar month in India, as its first day (book.month_of, in SQL)."""
    return cast(func.date_trunc(_MONTH, func.timezone(_IST_ZONE, column)), Date)


# ─── Reconcile: the sums against the entries ─────────────────────────────


async def reconcile_club(session, club_id: int) -> int:
    """ledger_monthly's rows for one club (0: entries with no club) made to
    agree with its entries. Rows corrected; the caller commits."""
    owner = E.club_id.is_(None) if club_id == 0 else E.club_id == club_id
    seen = {
        tuple(r[:7]): (int(r[7]), int(r[8]), int(r[9]))
        for r in (await session.execute(
            select(LM.club_id, LM.event_id, LM.gateway_id, LM.month, LM.origin_month, LM.account, LM.kind,
                   LM.amount_paise, LM.units, LM.entries)
            .where(LM.club_id == club_id)
            .with_for_update()
        )).all()
    }
    month, origin = ist_month(E.occurred_at), ist_month(E.origin_at)
    event, gateway = func.coalesce(E.event_id, 0), func.coalesce(E.gateway_id, 0)
    computed = {
        (club_id, int(r[0]), int(r[1]), r[2], r[3], r[4], r[5]): (int(r[6]), int(r[7]), int(r[8]))
        for r in (await session.execute(
            select(event, gateway, month, origin, E.account, E.kind,
                   func.sum(E.amount_paise), func.sum(E.units), func.count())
            .where(owner)
            .group_by(event, gateway, month, origin, E.account, E.kind)
        )).all()
    }
    deltas = {}
    for key in set(seen) | set(computed):
        have, want = seen.get(key, (0, 0, 0)), computed.get(key, (0, 0, 0))
        if have != want:
            deltas[key] = [want[0] - have[0], want[1] - have[1], want[2] - have[2]]
    if deltas:
        await book.add_to(session, LM, book.MONTHLY_KEY, deltas)
        # Rows that summed nothing (no entries) are taken away.
        await session.execute(delete(LM).where(LM.club_id == club_id, LM.entries == 0, LM.amount_paise == 0, LM.units == 0))
    return len(deltas)


async def reconcile_platform_month(session, month: date) -> int:
    """ledger_platform_monthly's rows for one month made to agree with
    ledger_monthly (the shards summed; a difference goes on shard 0)."""
    seen: dict[tuple, list[int]] = {}
    for r in (await session.execute(
        select(LP.month, LP.origin_month, LP.club_gateway, LP.account, LP.kind, LP.amount_paise, LP.units, LP.entries)
        .where(LP.month == month)
        .with_for_update()
    )).all():
        row = seen.setdefault(tuple(r[:5]), [0, 0, 0])
        row[0] += int(r[5])
        row[1] += int(r[6])
        row[2] += int(r[7])
    club_gateway = LM.gateway_id != 0
    computed = {
        (r[0], r[1], bool(r[2]), r[3], r[4]): [int(r[5]), int(r[6]), int(r[7])]
        for r in (await session.execute(
            select(LM.month, LM.origin_month, club_gateway, LM.account, LM.kind,
                   func.sum(LM.amount_paise), func.sum(LM.units), func.sum(LM.entries))
            .where(LM.month == month)
            .group_by(LM.month, LM.origin_month, club_gateway, LM.account, LM.kind)
        )).all()
    }
    deltas = {}
    for key in set(seen) | set(computed):
        have, want = seen.get(key, [0, 0, 0]), computed.get(key, [0, 0, 0])
        if have != want:
            deltas[(*key, 0)] = [want[0] - have[0], want[1] - have[1], want[2] - have[2]]
    if deltas:
        await book.add_to(session, LP, book.PLATFORM_KEY, deltas)
    return len(deltas)


async def _clubs_since(session, watermark: int) -> list[int]:
    return sorted({int(c or 0) for (c,) in (await session.execute(
        select(E.club_id).where(E.id > watermark).distinct()
    )).all()})


async def _rolling_clubs(session, after: int, limit: int) -> list[int]:
    return [int(c) for (c,) in (await session.execute(
        select(LM.club_id).where(LM.club_id > after).group_by(LM.club_id).order_by(LM.club_id).limit(limit)
    )).all()]


async def _months_since(session, watermark: int) -> list[date]:
    return sorted({m for (m,) in (await session.execute(
        select(ist_month(E.occurred_at)).where(E.id > watermark).distinct()
    )).all()})


async def reconcile(session, *, clubs: Optional[list[int]] = None, months: Optional[list[date]] = None,
                    deadline: Optional[float] = None) -> dict:
    """The given clubs' sums (every club when None), then the given months
    of the platform's (every month when None), each corrected in its own
    transaction. Alerts the admins when anything had drifted."""
    if clubs is None:
        clubs = sorted({int(c) for (c,) in (await session.execute(select(LM.club_id).distinct())).all()}
                       | set(await _clubs_since(session, 0)))
    done = {"clubs": 0, "rows_fixed": 0, "clubs_fixed": [], "months": 0, "platform_rows_fixed": 0}
    for club_id in clubs:
        if deadline is not None and time.monotonic() > deadline:
            break
        fixed = await reconcile_club(session, club_id)
        await session.commit()
        done["clubs"] += 1
        if fixed:
            done["rows_fixed"] += fixed
            done["clubs_fixed"].append(club_id)
    if months is None:
        months = sorted({m for (m,) in (await session.execute(select(LM.month).distinct())).all()}
                        | {m for (m,) in (await session.execute(select(LP.month).distinct())).all()})
    for month in months:
        fixed = await reconcile_platform_month(session, month)
        await session.commit()
        done["months"] += 1
        done["platform_rows_fixed"] += fixed
    if done["rows_fixed"] or done["platform_rows_fixed"]:
        from app.api.admin_alerts import notify

        await notify(
            session, "ledger_drift",
            title=f"The ledger's sums had drifted for {len(done['clubs_fixed'])} club(s); corrected",
            body=(f"{done['rows_fixed']} club row(s) and {done['platform_rows_fixed']} platform row(s) did not "
                  "agree with the ledger's entries and were put right. The entries were not touched. Something "
                  "wrote the sums other than a posting: look for it."),
            data={"rows": done["rows_fixed"], "platform_rows": done["platform_rows_fixed"],
                  "clubs": done["clubs_fixed"][:50]},
            dedupe=False,
        )
        await session.commit()
    logger.info("ledger reconcile: %s", {k: v for k, v in done.items() if k != "clubs_fixed"})
    return done


# ─── Balance: every posting sums to zero ─────────────────────────────────


async def unbalanced(session, watermark: int = 0) -> list[str]:
    """Postings (since `watermark`) whose lines do not sum to zero."""
    return [k for (k,) in (await session.execute(
        select(E.txn_key).where(E.id > watermark).group_by(E.txn_key).having(func.sum(E.amount_paise) != 0).limit(100)
    )).all()]


# ─── Retry: postings that failed ─────────────────────────────────────────


async def retry_failed(session, since: datetime) -> dict:
    """Each source named by a `ledger_post_failed` alert since `since`,
    posted again from its rows as they stand (every posting is idempotent)."""
    from app.api.admin_alerts.models import AdminNotifications

    rows = (await session.execute(
        select(AdminNotifications.data).where(
            AdminNotifications.kind == "ledger_post_failed", AdminNotifications.created_at >= since,
        )
    )).scalars().all()
    sources = sorted({((d or {}).get("source"), (d or {}).get("source_id")) for d in rows if d})
    done = {"sources": len(sources), "lines": 0, "missing": 0}
    for source, source_id in sources:
        try:
            written = await repost(session, source, source_id)
        except Exception:  # noqa: BLE001
            logger.warning("ledger retry of %s %s failed", source, source_id, exc_info=True)
            await session.rollback()
            continue
        if written is None:
            done["missing"] += 1
        else:
            done["lines"] += written
        await session.commit()
    return done


async def repost(session, source: str, source_id: str) -> Optional[int]:
    """Post one source again from its rows. None when the row is gone."""
    from app.api.ledger import service as ledger

    if source == book.SRC_PAYMENT:
        from app.api.payments.models import PaymentLogs, PaymentOrders, PaymentStatus

        row = (await session.execute(
            select(PaymentLogs, PaymentOrders).join(PaymentOrders, PaymentOrders.id == PaymentLogs.order_id)
            .where(PaymentLogs.id == source_id).execution_options(include_deleted=True)
        )).first()
        if row is None:
            return None
        log, order = row
        written = 0
        if log.status in (PaymentStatus.captured, PaymentStatus.refunded):
            written += await ledger.capture(session, log, order)
        if log.status == PaymentStatus.refunded:
            written += await ledger.refund(session, log, order, at=log.updated_at)
        return written
    if source == book.SRC_REGISTRATION:
        from app.api.events.models import EventRegistrationsLink as Reg

        reg = await session.scalar(select(Reg).where(Reg.id == source_id).execution_options(include_deleted=True))
        if reg is None:
            return None
        if not await ledger.balances(session, reg.id):
            return await ledger.post_as_it_stands(session, reg)
        written = await ledger.post_payments(session, reg)
        written += await ledger.sync_registration(session, reg, kind=book.REPAIR, channel=ledger._channel_of(reg)) or 0
        return written
    if source in (book.SRC_CHARGE, book.SRC_SUBSCRIPTION):
        from app.api.subscriptions.models import SubscriptionCharges as Charges

        if source == book.SRC_CHARGE:
            where = Charges.id == int(source_id)
        else:  # a first-charge marker: every charge of that subscription
            where = (Charges.razorpay_subscription_id == source_id) | (Charges.razorpay_payment_id == source_id)
            if source_id.isdigit():
                where = where | (Charges.subscription_id == int(source_id))
        charges = (await session.scalars(select(Charges).where(where))).all()
        if not charges:
            return None
        return sum([await ledger.charge(session, c) for c in charges])
    if source == book.SRC_AUDIT:
        from app.api.audit.models import AdminAuditLogs

        row = await session.get(AdminAuditLogs, int(source_id))
        if row is None:
            return None
        return await ledger.comp_granted(session, audit_id=row.id, club_id=int(row.entity_id),
                                         subscription_id=(row.details or {}).get("subscription_id"), at=row.created_at)
    return None


# ─── The daily run ───────────────────────────────────────────────────────


def _daily_at() -> tuple[int, int]:
    from app.config import settings

    try:
        hh, mm = (int(x) for x in (settings.LEDGER_DAILY_AT or "02:30").split(":", 1))
        return hh, mm
    except ValueError:
        return 2, 30


def due(last_started: Optional[datetime], now: datetime) -> bool:
    """Once a day: the first look after LEDGER_DAILY_AT (IST) on a day it has not run."""
    local = now.astimezone(book.IST)
    hh, mm = _daily_at()
    if (local.hour, local.minute) < (hh, mm):
        return False
    return last_started is None or last_started.astimezone(book.IST).date() < local.date()


async def _claim(session, now: datetime) -> Optional[dict]:
    """This worker's turn: under the advisory lock, the run is due and is
    marked started. Returns the last run's result ({} for the first), or None."""
    from app.api.models import ScheduledRuns

    if not await session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": LOCK}):
        await session.commit()
        return None
    row = (await session.execute(select(ScheduledRuns.started_at, ScheduledRuns.result).where(ScheduledRuns.name == JOB))).first()
    if row is not None and not due(row[0], now):
        await session.commit()
        return None
    stmt = pg_insert(ScheduledRuns.__table__).values(name=JOB, started_at=now, finished_at=None)
    await session.execute(stmt.on_conflict_do_update(index_elements=["name"], set_={"started_at": now, "finished_at": None}))
    await session.commit()
    previous = dict((row[1] or {}) if row is not None else {})
    previous["started_at"] = row[0].isoformat() if row is not None and row[0] else None
    return previous


async def run_daily(session, now: Optional[datetime] = None, *, previous: Optional[dict] = None,
                    razorpay: bool = True) -> dict:
    """The four steps, each on its own: one failing leaves the others to run."""
    from app.api.ledger import recon

    now = now or _now()
    previous = previous or {}
    deadline = time.monotonic() + RUN_BUDGET_SECONDS
    watermark = int(previous.get("watermark") or 0)
    top = int(await session.scalar(select(func.coalesce(func.max(E.id), 0))) or 0)
    await session.commit()
    since = datetime.fromisoformat(previous["started_at"]) - timedelta(hours=1) if previous.get("started_at") else now - timedelta(days=2)
    done: dict = {"watermark": top}

    async def step(name, coro):
        try:
            done[name] = await coro
        except Exception as exc:  # noqa: BLE001
            logger.exception("ledger daily: %s failed", name)
            await session.rollback()
            done[name] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    await step("retry", retry_failed(session, since))
    rolling_after = int(previous.get("rolling_after") or 0)
    total_clubs = int(await session.scalar(select(func.count(func.distinct(LM.club_id)))) or 0)
    slice_size = max(50, -(-total_clubs // ROLLING_DAYS))
    rolling = await _rolling_clubs(session, rolling_after, slice_size)
    done["rolling_after"] = rolling[-1] if len(rolling) == slice_size else 0
    clubs = sorted(set(await _clubs_since(session, watermark)) | set(rolling))
    months = sorted(set(await _months_since(session, watermark)) | {book.month_of(now), book.month_of(now - timedelta(days=31))})
    await session.commit()
    await step("reconcile", reconcile(session, clubs=clubs, months=months, deadline=deadline))
    await step("unbalanced", _check_balance(session, watermark))
    if razorpay:
        await step("razorpay", recon.reconcile_day(session, (now.astimezone(book.IST) - timedelta(days=1)).date(),
                                                   deadline=deadline))
    return done


async def _check_balance(session, watermark: int) -> dict:
    keys = await unbalanced(session, watermark)
    if keys:
        from app.api.admin_alerts import notify

        await notify(
            session, "ledger_unbalanced", title=f"{len(keys)} ledger posting(s) do not sum to zero",
            body="Each posting's lines must sum to zero. These do; post a correcting entry for each.",
            data={"keys": keys[:50]}, dedupe=False,
        )
        await session.commit()
    return {"unbalanced": len(keys)}


async def run_if_due(session, now: Optional[datetime] = None) -> Optional[dict]:
    from app.api.models import ScheduledRuns

    now = now or _now()
    previous = await _claim(session, now)
    if previous is None:
        return None
    done = await run_daily(session, now, previous=previous)
    stmt = pg_insert(ScheduledRuns.__table__).values(name=JOB, finished_at=_now(), result=_jsonable(done))
    await session.execute(stmt.on_conflict_do_update(index_elements=["name"], set_={"finished_at": _now(), "result": _jsonable(done)}))
    await session.commit()
    return done


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


async def daily_loop(interval: int = LOOK_EVERY_SECONDS) -> None:
    """Runs for the life of the process."""
    from app.api.deliveries import service as deliveries

    await asyncio.sleep(FIRST_LOOK_SECONDS)
    while True:
        try:
            async with deliveries.session_scope() as session:
                done = await run_if_due(session)
                if done:
                    logger.warning("ledger daily: %s", {k: v for k, v in done.items() if k != "reconcile"})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("ledger daily job failed")
        await asyncio.sleep(interval)
