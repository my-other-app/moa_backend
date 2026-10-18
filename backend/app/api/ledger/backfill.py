"""Posting the history: every money row from before the ledger, as it stands.

Run once after the deploy that brings the ledger (scripts/ledger_backfill.py),
while the app keeps taking payments; safe to run again, and again after that.
Each walk goes in id order a batch at a time, each batch its own short
transaction, and builds the postings the live paths would have made, with
the same functions and under the same keys (app.api.ledger.service) -- so a
payment posted live and here is one posting, and a run that stops half-way is
finished by the next. A batch's postings go in together (book.post_many): a
few INSERTs and one UPSERT a sums table, not three statements a posting.

- payments: every payment log captured or refunded, its capture (and its
  refund). Nothing locked: the keys make it one posting.
- registrations: every registration holding money, its ticket and store
  orders as they stand, dated as the finance pages have always dated them
  (the ticket when the guest registered, the orders when they were paid).
  Each takes the registration's posting lock without waiting: one a live
  payment is posting for is skipped, and tried again at the end.
- subscription charges: each charge, its refunds and its subscription's
  first-charge marker; the rows locked SKIP LOCKED and the subscription's
  marker lock tried without waiting, the busy ones tried again at the end.
- complimentary plans: each grant in the admin audit log, at zero.

Deleted rows are posted too: money a deleted registration paid was money the
club took.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime

from sqlalchemy import exists, func, or_, select, text

from app.api.ledger import book
from app.api.ledger import service as ledger
from app.api.ledger.models import LedgerEntries as E

logger = logging.getLogger(__name__)

BATCH = 500
#: Passes over what was busy before it is reported as skipped.
BUSY_PASSES = 3


def _blank() -> dict:
    return {"seen": 0, "lines": 0}


async def _try_locks(session, values) -> set:
    """Which of `values` this transaction now holds the posting lock of
    (service._lock, without waiting)."""
    keys = {value: ledger._key32(value) for value in values}
    if not keys:
        return set()
    got = dict((await session.execute(
        text("SELECT k, pg_try_advisory_xact_lock(:ns, k) FROM unnest(CAST(:keys AS int[])) AS k"),
        {"ns": ledger.LOCK_NS, "keys": sorted(set(keys.values()))},
    )).all())
    return {value for value, key in keys.items() if got.get(key)}


async def _clubs(session, event_ids) -> dict:
    """event id -> club id, through the session's cache (service._club_of)."""
    from app.api.events.models import Events

    known = session.info.setdefault("ledger_event_clubs", {})
    missing = sorted({e for e in event_ids if e is not None and e not in known})
    if missing:
        rows = await session.execute(
            select(Events.id, Events.club_id).where(Events.id.in_(missing)).execution_options(include_deleted=True)
        )
        known.update(dict(rows.all()))
    return known


# ─── Payments ────────────────────────────────────────────────────────────


async def _registrations_of_orders(session, orders) -> dict:
    """order id -> its registration, as payments.handlers.registration_for_order
    finds it (by the id in the order's payload, checked against the receipt;
    else the first by the receipt), deleted ones included."""
    from app.api.events.models import EventRegistrationsLink as Reg

    wanted = {}
    for order in orders:
        raw = (order.payload or {}).get("event_registration_id")
        try:
            wanted[order.id] = uuid.UUID(str(raw)) if raw else None
        except ValueError:
            wanted[order.id] = None
    ids = sorted({i for i in wanted.values() if i is not None}, key=str)
    by_id = {}
    if ids:
        by_id = {r.id: r for r in (await session.scalars(
            select(Reg).where(Reg.id.in_(ids)).execution_options(include_deleted=True)
        )).all()}
    out, by_receipt = {}, []
    for order in orders:
        reg = by_id.get(wanted[order.id])
        if reg is not None and reg.payment_receipt in (None, order.receipt):
            out[order.id] = reg
        else:
            by_receipt.append(order)
    if by_receipt:
        receipts = sorted({o.receipt for o in by_receipt})
        first = {}
        for reg in (await session.scalars(
            select(Reg).where(Reg.payment_receipt.in_(receipts)).order_by(Reg.created_at).execution_options(include_deleted=True)
        )).all():
            first.setdefault(reg.payment_receipt, reg)
        for order in by_receipt:
            out[order.id] = first.get(order.receipt)
    return out


async def _payments(session, done: dict, *, batch: int, since: datetime | None) -> None:
    from app.api.payments.models import PaymentLogs, PaymentOrders, PaymentStatus

    last = None
    while True:
        q = (
            select(PaymentLogs, PaymentOrders)
            .join(PaymentOrders, PaymentOrders.id == PaymentLogs.order_id)
            .where(PaymentLogs.status.in_((PaymentStatus.captured, PaymentStatus.refunded)))
            .order_by(PaymentLogs.id)
            .limit(batch)
            .execution_options(include_deleted=True)
        )
        if last is not None:
            q = q.where(PaymentLogs.id > last)
        if since is not None:
            q = q.where(PaymentLogs.updated_at >= since)
        rows = (await session.execute(q)).all()
        if not rows:
            return
        regs = await _registrations_of_orders(session, [order for _, order in rows])
        clubs = await _clubs(session, [r.event_id for r in regs.values() if r is not None])
        txns = []
        for log, order in rows:
            done["seen"] += 1
            reg = regs.get(order.id)
            club_id = clubs.get(reg.event_id) if reg is not None else None
            txns.append(ledger.capture_txn(log, order, reg, club_id))
            if log.status == PaymentStatus.refunded:
                txns.append(ledger.refund_txn(log, order, reg, club_id, at=log.updated_at or log.created_at))
        done["lines"] += await book.post_many(session, txns)
        last = rows[-1][0].id
        await session.commit()


# ─── Registrations ───────────────────────────────────────────────────────


def _holds_money():
    from app.api.events.models import EventRegistrationsLink as Reg
    from app.api.store.models import EventStoreOrders as Orders

    return or_(
        Reg.is_paid == True,  # noqa: E712
        Reg.paid_amount != 0,
        exists().where(Orders.registration_id == Reg.id, Orders.status.in_(("paid", "oversold"))),
    )


async def _registrations_batch(session, ids: list, done: dict) -> list:
    """One batch of registrations posted as they stand; returns those busy."""
    from app.api.events.models import EventRegistrationsLink as Reg
    from app.api.store.models import EventStoreOrders as Orders

    mine = await _try_locks(session, ids)
    busy = [i for i in ids if i not in mine]
    ids = [i for i in ids if i in mine]
    if not ids:
        return busy
    regs = (await session.execute(
        select(Reg.id, Reg.event_id, Reg.paid_amount, Reg.is_paid, Reg.payment_receipt, Reg.created_at)
        .where(Reg.id.in_(ids)).execution_options(include_deleted=True)
    )).all()
    have: dict = {i: {} for i in ids}
    seq: dict = {i: 0 for i in ids}
    for reg_id, account, paise, units, n in (await session.execute(
        select(E.registration_id, E.account, func.sum(E.amount_paise), func.sum(E.units), func.count())
        .where(E.registration_id.in_(ids), E.account.in_(book.REGISTRATION_ACCOUNTS))
        .group_by(E.registration_id, E.account)
    )).all():
        have[reg_id][account] = (int(paise), int(units))
        seq[reg_id] += int(n)
    orders: dict = {i: {} for i in ids}
    paid_at: dict = {}
    for reg_id, status, total, n, last_paid in (await session.execute(
        select(Orders.registration_id, Orders.status, func.coalesce(func.sum(Orders.total), 0), func.count(), func.max(Orders.paid_at))
        .where(Orders.registration_id.in_(ids), Orders.status.in_(("paid", "oversold")))
        .group_by(Orders.registration_id, Orders.status)
    )).all():
        orders[reg_id][status] = (total, int(n))
        if last_paid is not None and (reg_id not in paid_at or last_paid > paid_at[reg_id]):
            paid_at[reg_id] = last_paid
    clubs = await _clubs(session, [r.event_id for r in regs])
    txns = []
    for reg_id, event_id, paid_amount, is_paid, receipt, created_at in regs:
        done["seen"] += 1
        channel = ledger.ONLINE if receipt else ledger.DESK
        created = created_at or book.now()
        want = ledger.want_for(paid_amount, is_paid, orders[reg_id], channel=channel, have=have[reg_id])
        common = dict(kind=book.BACKFILL, channel=channel, want=want)
        tickets = ledger.registration_txn(reg_id, event_id, clubs.get(event_id), have=have[reg_id], seq=seq[reg_id],
                                          accounts=book.TICKETS, at=created, **common)
        if tickets is not None:
            txns.append(tickets)
            # What the store's posting sees, as if the ticket's had been posted and read back first.
            for line in tickets.kept():
                if line.account in book.TICKETS:
                    old = have[reg_id].get(line.account, ledger.ZERO)
                    have[reg_id][line.account] = (old[0] + line.amount, old[1] + line.units)
            seq[reg_id] += len(tickets.kept())
        store = ledger.registration_txn(reg_id, event_id, clubs.get(event_id), have=have[reg_id], seq=seq[reg_id],
                                        accounts=(book.STORE, book.STORE_HELD), at=paid_at.get(reg_id) or created, **common)
        if store is not None:
            txns.append(store)
    done["lines"] += await book.post_many(session, txns)
    return busy


#: Events whose registrations are read together in the registrations walk.
EVENTS_PER_BATCH = 200


async def _registration_ids(session, *, batch: int, since: datetime | None):
    """The registrations holding money, a batch of ids at a time: event by
    event for the live ones (an event's registrations sit together on disk,
    and the walk reads them through the event's index, where a walk in id
    order -- random uuids -- read a page for every row), then the deleted
    ones, found in one pass."""
    from app.api.events.models import EventRegistrationsLink as Reg
    from app.api.events.models import Events

    last_event = 0
    while True:
        events = list((await session.scalars(
            select(Events.id).where(Events.id > last_event).order_by(Events.id).limit(EVENTS_PER_BATCH)
            .execution_options(include_deleted=True)
        )).all())
        if not events:
            break
        q = select(Reg.id).where(Reg.event_id.in_(events), Reg.is_deleted == False, _holds_money())  # noqa: E712
        if since is not None:
            q = q.where(Reg.updated_at >= since)
        ids = list((await session.scalars(q.execution_options(include_deleted=True))).all())
        for start in range(0, len(ids), batch):
            yield ids[start:start + batch]
        last_event = events[-1]
    # The deleted ones are few: one pass over the table finds them all (a
    # walk in id order would read a page for every row it skipped).
    q = select(Reg.id).where(Reg.is_deleted == True, _holds_money()).execution_options(include_deleted=True)  # noqa: E712
    if since is not None:
        q = q.where(Reg.updated_at >= since)
    deleted = list((await session.scalars(q)).all())
    await session.commit()
    for start in range(0, len(deleted), batch):
        yield deleted[start:start + batch]


async def _registrations(session, done: dict, *, batch: int, since: datetime | None) -> None:
    busy: list = []
    async for ids in _registration_ids(session, batch=batch, since=since):
        busy += await _registrations_batch(session, ids, done)
        # The posting locks go with the commit.
        await session.commit()
    for _ in range(BUSY_PASSES):
        if not busy:
            break
        await asyncio.sleep(0.5)  # let the live payments holding them finish
        again = []
        for start in range(0, len(busy), batch):
            part = busy[start:start + batch]
            done["seen"] -= len(part)  # counted once, when they were first met
            again += await _registrations_batch(session, part, done)
            done["seen"] += len([i for i in part if i in again])
            await session.commit()
        busy = again
    done["busy_skipped"] = len(busy)
    if busy:
        logger.warning("ledger backfill: %s registration(s) stayed busy; run it again", len(busy))


# ─── Subscription charges ────────────────────────────────────────────────


async def _charges_batch(session, rows: list, done: dict) -> list:
    """One batch of charges (locked by the caller); returns those whose
    subscription's marker lock was busy."""
    groups = {ledger._first_group(c) for c in rows}
    mine = await _try_locks(session, [f"first:{g}" for g in groups])
    busy = [c for c in rows if f"first:{ledger._first_group(c)}" not in mine]
    rows = [c for c in rows if f"first:{ledger._first_group(c)}" in mine]
    if not rows:
        return busy
    groups = {ledger._first_group(c) for c in rows}
    refunded = {pid: -int(paise) for pid, paise in (await session.execute(
        select(E.razorpay_payment_id, func.sum(E.amount_paise))
        .where(E.razorpay_payment_id.in_([c.razorpay_payment_id for c in rows]), E.account == book.GATEWAY,
               E.kind == book.SUB_REFUND)
        .group_by(E.razorpay_payment_id)
    )).all()}
    current = {}
    for group, origin in (await session.execute(
        select(E.source_id, E.origin_at)
        .where(E.account == book.SUBSCRIBERS, E.kind == book.FIRST_CHARGE, E.source_id.in_(sorted(groups)))
        .group_by(E.source_id, E.origin_at).having(func.sum(E.units) > 0)
    )).all():
        if group not in current or origin < current[group]:
            current[group] = origin
    txns = []
    for charge in rows:
        done["seen"] += 1
        txns.append(ledger.charge_txn(charge))
        refund = ledger.charge_refund_txn(charge, int(refunded.get(charge.razorpay_payment_id) or 0), at=charge.updated_at)
        if refund is not None:
            txns.append(refund)
    for group, charge in (await _earliest_charges(session, groups)).items():
        txns += ledger.first_charge_txns(group, charge, current.get(group))
    done["lines"] += await book.post_many(session, txns)
    return busy


async def _earliest_charges(session, groups: set) -> dict:
    """Each subscription's earliest charge in the whole table, not the
    batch's: charges stored newest first would otherwise move the first-
    charge marker once for every charge of the subscription."""
    from app.api.subscriptions.models import SubscriptionCharges as Charges

    ints = [int(g) for g in groups if g.isdigit()]
    out: dict = {}
    for charge in (await session.scalars(
        select(Charges).where(Charges.status != "failed", or_(
            Charges.razorpay_subscription_id.in_(sorted(groups)), Charges.subscription_id.in_(ints),
            Charges.razorpay_payment_id.in_(sorted(groups)),
        ))
    )).all():
        group = ledger._first_group(charge)
        if group in groups and (group not in out or charge.charged_at < out[group].charged_at):
            out[group] = charge
    return out


async def _charges(session, done: dict, *, batch: int, since: datetime | None) -> None:
    from app.api.subscriptions.models import SubscriptionCharges

    busy: list = []
    last = 0
    while True:
        q = (
            select(SubscriptionCharges)
            .where(SubscriptionCharges.id > last, SubscriptionCharges.status != "failed")
            .order_by(SubscriptionCharges.id)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
        if since is not None:
            q = q.where(SubscriptionCharges.updated_at >= since)
        rows = list((await session.scalars(q)).all())
        if not rows:
            break
        busy += [c.id for c in await _charges_batch(session, rows, done)]
        last = rows[-1].id
        await session.commit()
    for _ in range(BUSY_PASSES):
        if not busy:
            break
        await asyncio.sleep(0.5)
        rows = list((await session.scalars(
            select(SubscriptionCharges).where(SubscriptionCharges.id.in_(busy)).order_by(SubscriptionCharges.id).with_for_update()
        )).all())
        done["seen"] -= len(rows)
        busy = [c.id for c in await _charges_batch(session, rows, done)]
        done["seen"] += len(busy)
        await session.commit()
    done["busy_skipped"] = len(busy)


# ─── Complimentary plans ─────────────────────────────────────────────────


async def _comps(session, done: dict, *, batch: int, since: datetime | None) -> None:
    from app.api.audit.models import AdminAuditLogs

    last = 0
    while True:
        q = (
            select(AdminAuditLogs)
            .where(AdminAuditLogs.action == "subscription.comp_grant", AdminAuditLogs.id > last)
            .order_by(AdminAuditLogs.id)
            .limit(batch)
        )
        if since is not None:
            q = q.where(AdminAuditLogs.created_at >= since)
        rows = (await session.scalars(q)).all()
        if not rows:
            return
        for row in rows:
            done["seen"] += 1
            try:
                club_id = int(row.entity_id)
            except (TypeError, ValueError):
                continue
            details = row.details or {}
            done["lines"] += await ledger.comp_granted(
                session, audit_id=row.id, club_id=club_id, subscription_id=details.get("subscription_id"), at=row.created_at,
            )
        last = rows[-1].id
        await session.commit()


async def backfill(session, *, batch: int = BATCH, since: datetime | None = None) -> dict:
    """Post every money row (or, with `since`, those changed since then) to
    the ledger. Returns what it saw and wrote, per walk, how long each took,
    and how many postings failed (each is also an admin alert, retried by
    the daily job). A whole run with nothing failed or skipped leaves its
    mark (`complete`): the daily job's Razorpay check waits for it."""
    failures = book.failures()
    done = {"payments": _blank(), "registrations": _blank(), "charges": _blank(), "comps": _blank()}
    started = time.monotonic()
    for name, walk in (("payments", _payments), ("registrations", _registrations), ("charges", _charges), ("comps", _comps)):
        t = time.monotonic()
        await walk(session, done[name], batch=batch, since=since)
        done[name]["seconds"] = round(time.monotonic() - t, 1)
    await session.commit()
    done["failed"] = book.failures() - failures
    done["seconds"] = round(time.monotonic() - started, 1)
    if since is None and not done["failed"] and not done["registrations"].get("busy_skipped") \
            and not done["charges"].get("busy_skipped"):
        await _mark_complete(session, done)
    return done


#: The scheduled_runs row a complete backfill leaves: the daily job checks
#: Razorpay against the ledger only once the ledger holds the history.
DONE = "ledger_backfill"


async def _mark_complete(session, done: dict) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.api.models import ScheduledRuns

    now = book.now()
    stmt = pg_insert(ScheduledRuns.__table__).values(name=DONE, started_at=now, finished_at=now, result=done)
    await session.execute(stmt.on_conflict_do_update(index_elements=["name"], set_={"finished_at": now, "result": done}))
    await session.commit()


async def complete(session) -> bool:
    """Whether a backfill has run to the end, with nothing failed or skipped."""
    from app.api.models import ScheduledRuns

    return bool(await session.scalar(select(ScheduledRuns.finished_at).where(ScheduledRuns.name == DONE)))
