"""Posting the history: every money row from before the ledger, as it stands.

Run once after the deploy that brings the ledger (scripts/ledger_backfill.py),
while the app keeps taking payments; safe to run again, and again after that.
Each walk goes in id order a batch at a time, every batch its own short
transaction, and posts through the same functions the live paths use, under
the same keys -- so a payment posted live and here is one posting, and a run
that stops half-way is finished by the next.

- payments: every payment log captured or refunded, its capture (and its
  refund). Nothing locked: the keys make it one posting.
- registrations: every registration holding money, its ticket and store
  orders as they stand (service.post_as_it_stands), dated as the finance
  pages have always dated them. Each takes the registration's posting lock
  without waiting: one a live payment is posting for is skipped, and tried
  again at the end.
- subscription charges: each charge, its refunds and its first-charge
  marker, the row locked SKIP LOCKED (a refund webhook holding it is waited
  out the same way).
- complimentary plans: each grant in the admin audit log, at zero.

Deleted rows are posted too: money a deleted registration paid was money the
club took.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from sqlalchemy import exists, or_, select

from app.api.ledger import book
from app.api.ledger import service as ledger

logger = logging.getLogger(__name__)

BATCH = 200
#: Passes over the registrations that were busy before they are reported as skipped.
BUSY_PASSES = 3


def _blank() -> dict:
    return {"seen": 0, "lines": 0}


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
        for log, order in rows:
            done["seen"] += 1
            done["lines"] += await ledger.capture(session, log, order)
            if log.status == PaymentStatus.refunded:
                done["lines"] += await ledger.refund(session, log, order, at=log.updated_at or log.created_at)
        last = rows[-1][0].id
        await session.commit()


def _holds_money():
    from app.api.events.models import EventRegistrationsLink as Reg
    from app.api.store.models import EventStoreOrders as Orders

    return or_(
        Reg.is_paid == True,  # noqa: E712
        Reg.paid_amount != 0,
        exists().where(Orders.registration_id == Reg.id, Orders.status.in_(("paid", "oversold"))),
    )


async def _registration(session, reg_id, done: dict) -> bool:
    """One registration's money as it stands; False when it was busy."""
    from app.api.events.models import EventRegistrationsLink as Reg

    reg = await session.scalar(
        select(Reg).where(Reg.id == reg_id).execution_options(include_deleted=True, populate_existing=True)
    )
    if reg is None:
        return True
    written = await ledger.post_as_it_stands(session, reg, wait=False, payments=False)
    if written is None:
        return False
    done["lines"] += written
    return True


async def _registrations(session, done: dict, *, batch: int, since: datetime | None) -> None:
    from app.api.events.models import EventRegistrationsLink as Reg

    busy: list = []
    last = None
    while True:
        q = select(Reg.id).where(_holds_money()).order_by(Reg.id).limit(batch).execution_options(include_deleted=True)
        if last is not None:
            q = q.where(Reg.id > last)
        if since is not None:
            q = q.where(Reg.updated_at >= since)
        ids = (await session.scalars(q)).all()
        if not ids:
            break
        for reg_id in ids:
            done["seen"] += 1
            if not await _registration(session, reg_id, done):
                busy.append(reg_id)
        last = ids[-1]
        # The posting locks go with the commit.
        await session.commit()
    for _ in range(BUSY_PASSES):
        if not busy:
            break
        await asyncio.sleep(0.5)  # let the live payments holding them finish
        again = []
        for reg_id in busy:
            if not await _registration(session, reg_id, done):
                again.append(reg_id)
            await session.commit()
        busy = again
    done["busy_skipped"] = len(busy)
    if busy:
        logger.warning("ledger backfill: %s registration(s) stayed busy; run it again", len(busy))


async def _charges(session, done: dict, *, batch: int, since: datetime | None) -> None:
    from app.api.subscriptions.models import SubscriptionCharges

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
        rows = (await session.scalars(q)).all()
        if not rows:
            return
        for charge in rows:
            done["seen"] += 1
            done["lines"] += await ledger.charge(session, charge, refunded_at=charge.updated_at)
        last = rows[-1].id
        await session.commit()


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
    the ledger. Returns what it saw and wrote, per walk, and how many
    postings failed (each is also an admin alert, retried by the daily job)."""
    failures = book.failures()
    done = {"payments": _blank(), "registrations": _blank(), "charges": _blank(), "comps": _blank()}
    started = time.monotonic()
    await _payments(session, done["payments"], batch=batch, since=since)
    await _registrations(session, done["registrations"], batch=batch, since=since)
    await _charges(session, done["charges"], batch=batch, since=since)
    await _comps(session, done["comps"], batch=batch, since=since)
    await session.commit()
    done["failed"] = book.failures() - failures
    done["seconds"] = round(time.monotonic() - started, 1)
    return done
