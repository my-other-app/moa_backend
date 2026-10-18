"""The payments sweep: every ten minutes, on one worker.

Three jobs, each through the same idempotent paths verify and the webhooks
use, so running one twice, or alongside a webhook, changes nothing twice:

- reconcile: orders made in the last RECONCILE_WINDOW still `created` or
  `attempted` are asked about at Razorpay, through each one's own account,
  in small batches with a pause between; a captured payment found there is
  applied (service.apply_payment). This is what finds the payment whose app
  was killed before it verified and whose webhook never landed.
- repair: orders paid lately whose registration is not paid -- a payment
  applied before applying became one transaction, or by a path that failed
  half-way -- are applied again (service.ensure_applied). The admins hear of
  each, once: repaired, or still not paid.
- refunds: automatic refunds (event full, paid twice) Razorpay has not
  confirmed are asked for again (service.settle_refund).

One worker at a time, without holding anything across Razorpay's answers:
the run is claimed in `scheduled_runs` under a transaction-scoped advisory
lock (SWEEP_LOCK), which is let go as the claim commits. Another worker
finding the claim younger than EVERY_SECONDS leaves it. A run stops itself
after RUN_BUDGET_SECONDS, well inside EVERY_SECONDS, so two never overlap.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, exists, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.events.models import EventRegistrationsLink as Reg
from app.api.payments.models import OrderStatus, PaymentLogs, PaymentOrders, PaymentStatus

logger = logging.getLogger(__name__)

SWEEP_LOCK = 773_122
JOB = "payments"
EVERY_SECONDS = 600
#: How long one run may take before it stops where it is.
RUN_BUDGET_SECONDS = 480
#: How often each worker looks whether the sweep is due.
LOOK_EVERY_SECONDS = 60
FIRST_LOOK_SECONDS = 120

#: Orders a payment may still land on.
RECONCILE_WINDOW = timedelta(hours=72)
#: An order younger than this is left to its checkout.
SETTLE_AFTER = timedelta(minutes=2)
RECONCILE_LIMIT = 300
#: Orders asked about together, then a pause: Razorpay's rate limits.
BATCH = 10
PAUSE = 1.0

#: Paid orders looked at for a registration left unpaid.
REPAIR_WINDOW = timedelta(days=7)
REPAIR_LIMIT = 100
REFUND_LIMIT = 50


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _alerted(session, kind: str, key: str, value: str) -> bool:
    """Whether the admins were told about this already (ever)."""
    from app.api.admin_alerts.models import AdminNotifications

    return bool(await session.scalar(
        select(AdminNotifications.id).where(AdminNotifications.kind == kind, AdminNotifications.data[key].as_string() == value).limit(1)
    ))


async def reconcile(session, now: Optional[datetime] = None, *, limit: int = RECONCILE_LIMIT, batch: int = BATCH,
                    pause: float = PAUSE, deadline: Optional[float] = None) -> dict:
    """Open orders asked about at Razorpay; captured payments applied."""
    from app.api.payment_gateways import service as gateways
    from app.api.payments import rzp, service

    now = now or _now()
    rows = (await session.execute(
        select(PaymentOrders.id, PaymentOrders.razorpay_order_id)
        .where(
            PaymentOrders.status.in_([OrderStatus.created, OrderStatus.attempted]),
            PaymentOrders.created_at >= now - RECONCILE_WINDOW,
            PaymentOrders.created_at <= now - SETTLE_AFTER,
        )
        .order_by(PaymentOrders.created_at.desc())
        .limit(limit)
    )).all()
    await session.commit()
    done = {"asked": 0, "applied": 0, "failed": 0}
    for start in range(0, len(rows), batch):
        if deadline is not None and time.monotonic() > deadline:
            break
        if start and pause:
            await asyncio.sleep(pause)
        for order_id, razorpay_order_id in rows[start:start + batch]:
            try:
                order = await session.get(PaymentOrders, order_id)
                account = await gateways.for_order(session, order)
                # Nothing held while Razorpay answers.
                await session.commit()
                listing = await rzp.call(account.client.order.payments, razorpay_order_id) or {}
                done["asked"] += 1
                for payment in listing.get("items") or []:
                    if payment.get("status") != "captured" or payment.get("order_id") not in (None, razorpay_order_id):
                        continue
                    payment = {**payment, "order_id": razorpay_order_id}
                    known = await session.scalar(
                        select(PaymentLogs.status).where(PaymentLogs.razorpay_payment_id == payment.get("id"))
                    )
                    if known in (PaymentStatus.captured, PaymentStatus.refunded):
                        continue
                    await service.apply_payment(session, order_id, payment, send_receipt=False)
                    done["applied"] += 1
                    logger.warning("payments sweep: applied captured payment %s on order %s", payment.get("id"), razorpay_order_id)
            except Exception:  # noqa: BLE001
                done["failed"] += 1
                logger.warning("payments sweep: order %s could not be reconciled", razorpay_order_id, exc_info=True)
                await session.rollback()
    return done


async def repair(session, now: Optional[datetime] = None, *, limit: int = REPAIR_LIMIT) -> dict:
    """Paid orders whose registration is not paid, applied again."""
    from app.api.admin_alerts import notify
    from app.api.payments import service
    from app.api.payments.handlers import registration_for_order

    now = now or _now()
    captured = exists().where(
        PaymentLogs.order_id == PaymentOrders.id,
        PaymentLogs.status == PaymentStatus.captured,
        PaymentLogs.refund_requested_at.is_(None),
    )
    unpaid = exists().where(
        Reg.payment_receipt == PaymentOrders.receipt,
        Reg.is_deleted == False,  # noqa: E712
        Reg.is_paid == False,  # noqa: E712
    )
    ids = (await session.scalars(
        select(PaymentOrders.id)
        .where(
            PaymentOrders.status == OrderStatus.paid,
            PaymentOrders.source == "event_registration",
            and_(PaymentOrders.updated_at >= now - REPAIR_WINDOW, PaymentOrders.updated_at <= now - SETTLE_AFTER),
            captured,
            unpaid,
        )
        .order_by(PaymentOrders.updated_at)
        .limit(limit)
    )).all()
    await session.commit()
    done = {"found": len(ids), "repaired": 0, "unpaid": 0, "refunded": 0}
    for order_id in ids:
        try:
            outcome = await service.ensure_applied(session, order_id)
            order = await session.get(PaymentOrders, order_id)
            reg = await registration_for_order(session, order) if order is not None else None
            if outcome is not None and outcome.full:
                done["refunded"] += 1
                continue
            repaired = reg is not None and reg.is_paid
            done["repaired" if repaired else "unpaid"] += 1
            if await _alerted(session, "payment_unapplied", "order_id", str(order_id)):
                continue
            club_id = await service._club_of_order(session, order)
            who = reg.full_name if reg is not None else "a guest"
            await notify(
                session, "payment_unapplied",
                title=(
                    f"A captured payment by {who} had not given them their ticket; the sweep applied it"
                    if repaired else
                    f"{who} paid (order {order.razorpay_order_id}) but the registration is still not paid"
                ),
                body=(
                    "The order was paid at Razorpay and here, but the registration was never marked paid. "
                    + ("It is now, and the ticket has gone out." if repaired else
                       "Applying it again did not cover the ticket: look at the order and the registration, and refund if needed.")
                ),
                club=club_id,
                data={"order_id": str(order_id), "razorpay_order_id": order.razorpay_order_id,
                      "registration_id": str(reg.id) if reg is not None else None, "repaired": repaired},
                dedupe_on={"order_id": str(order_id)},
            )
            await session.commit()
        except Exception:  # noqa: BLE001
            logger.warning("payments sweep: order %s could not be repaired", order_id, exc_info=True)
            await session.rollback()
    return done


async def refunds(session, now: Optional[datetime] = None, *, limit: int = REFUND_LIMIT) -> dict:
    """Automatic refunds Razorpay has not confirmed, asked for again."""
    from app.api.payments import service

    now = now or _now()
    ids = (await session.scalars(
        select(PaymentLogs.id)
        .where(
            PaymentLogs.refund_requested_at.is_not(None),
            PaymentLogs.status == PaymentStatus.captured,
            PaymentLogs.refund_requested_at <= now - timedelta(minutes=1),
        )
        .order_by(PaymentLogs.refund_requested_at)
        .limit(limit)
    )).all()
    await session.commit()
    done = {"due": len(ids), "refunded": 0}
    for log_id in ids:
        try:
            if await service.settle_refund(session, log_id):
                done["refunded"] += 1
        except Exception:  # noqa: BLE001
            logger.warning("payments sweep: refund of %s failed", log_id, exc_info=True)
            await session.rollback()
    return done


async def sweep(session, now: Optional[datetime] = None, *, deadline: Optional[float] = None) -> dict:
    now = now or _now()
    return {
        "refunds": await refunds(session, now),
        "repair": await repair(session, now),
        "reconcile": await reconcile(session, now, deadline=deadline),
    }


async def _claim(session, now: datetime) -> bool:
    """This worker's turn: under the advisory lock (for this transaction
    only), the run is due and is marked started. Commits, letting the lock go."""
    from app.api.models import ScheduledRuns

    if not await session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": SWEEP_LOCK}):
        await session.commit()
        return False
    last = await session.scalar(select(ScheduledRuns.started_at).where(ScheduledRuns.name == JOB))
    if last is not None and now - last < timedelta(seconds=EVERY_SECONDS):
        await session.commit()
        return False
    stmt = pg_insert(ScheduledRuns.__table__).values(name=JOB, started_at=now, finished_at=None)
    await session.execute(stmt.on_conflict_do_update(index_elements=["name"], set_={"started_at": now, "finished_at": None}))
    await session.commit()
    return True


async def run_if_due(session, now: Optional[datetime] = None) -> Optional[dict]:
    """The sweep, if it is this worker's turn. None when it is not."""
    from app.api.models import ScheduledRuns

    now = now or _now()
    if not await _claim(session, now):
        return None
    done = await sweep(session, now, deadline=time.monotonic() + RUN_BUDGET_SECONDS)
    stmt = pg_insert(ScheduledRuns.__table__).values(name=JOB, finished_at=_now(), result=done)
    await session.execute(stmt.on_conflict_do_update(index_elements=["name"], set_={"finished_at": _now(), "result": done}))
    await session.commit()
    return done


async def sweep_loop(interval: int = LOOK_EVERY_SECONDS) -> None:
    """Runs for the life of the process."""
    from app.api.deliveries import service as deliveries

    await asyncio.sleep(FIRST_LOOK_SECONDS)
    while True:
        try:
            async with deliveries.session_scope() as session:
                done = await run_if_due(session)
                if done and (done["reconcile"]["applied"] or done["repair"]["found"] or done["refunds"]["due"]):
                    logger.warning("payments sweep: %s", done)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("payments sweep failed")
        await asyncio.sleep(interval)
