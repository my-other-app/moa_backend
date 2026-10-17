"""The platform's revenue ledger: every payment Razorpay took for a club's
subscription (SubscriptionCharges).

Four ways in, all idempotent on the Razorpay payment id:

- `record_charge`, from each `subscription.charged` webhook (payments.service);
- `record_failure`, from `payment.failed` on a subscription: a `failed` row,
  so the admin sees the attempt; every sum leaves those out (`MONEY`);
- `apply_refund`, from `refund.processed` for a payment that is one of ours;
- `backfill`, which asks Razorpay (read-only) for the invoices of every
  subscription we know of and adds whatever is missing -- for the payments
  taken before this ledger existed, or a webhook that never arrived.

A complimentary plan has no Razorpay subscription and is never charged, so it
never has a row here. Nothing in this module moves money: the only Razorpay
calls are reads.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.subscriptions import billing
from app.api.subscriptions.models import (
    BillingCycle,
    ChargeStatus,
    ClubSubscriptions,
    SubscriptionCharges,
)

logger = logging.getLogger(__name__)

#: Razorpay's page size for listing invoices.
PAGE = 100

#: The rows that are money: everything but a failed attempt. Every sum of
#: the ledger filters on this.
MONEY = SubscriptionCharges.status != ChargeStatus.failed.value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _at(unix) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(unix), tz=timezone.utc) if unix else None
    except (TypeError, ValueError, OSError):
        return None


def _cycle(value) -> BillingCycle | None:
    if value is None:
        return None
    try:
        return BillingCycle(getattr(value, "value", value))
    except ValueError:
        return None


def status_for(amount: int, refunded: int) -> str:
    if refunded <= 0:
        return ChargeStatus.captured.value
    if refunded >= amount:
        return ChargeStatus.refunded.value
    return ChargeStatus.partially_refunded.value


def _set_refunded(charge: SubscriptionCharges, total: int) -> bool:
    """Razorpay's running total of what was refunded on the payment. Never
    goes down (a refund is not undone by a late, older figure), never above
    the payment. Returns whether anything changed."""
    refunded = min(max(int(charge.refunded_paise or 0), int(total or 0)), int(charge.amount_paise))
    status = status_for(int(charge.amount_paise), refunded)
    if refunded == charge.refunded_paise and status == charge.status:
        return False
    charge.refunded_paise = refunded
    charge.status = status
    return True


async def _insert(
    session,
    *,
    subscription_id: int | None,
    club_id: int,
    plan_id: int | None,
    cycle,
    razorpay_subscription_id: str | None,
    payment: dict,
    invoice_id: str | None = None,
    failed: bool = False,
) -> int | None:
    """One payment into the ledger, unless it is there already. Returns the
    new row's id, or None (already recorded, or not a payment we can read).
    In a SAVEPOINT: a ledger row that cannot be written never takes the
    webhook's state change with it -- the backfill can add it later."""
    payment_id = payment.get("id")
    amount = payment.get("amount")
    if not payment_id or amount is None:
        logger.warning("subscription payment without an id or amount: %r", {k: payment.get(k) for k in ("id", "amount")})
        return None
    amount = int(amount)
    refunded = min(int(payment.get("amount_refunded") or 0), amount)
    values = dict(
        subscription_id=subscription_id,
        club_id=club_id,
        plan_id=plan_id,
        cycle=_cycle(cycle),
        amount_paise=amount,
        currency=(payment.get("currency") or "INR")[:3],
        razorpay_payment_id=str(payment_id)[:64],
        razorpay_invoice_id=(invoice_id or payment.get("invoice_id") or None),
        razorpay_subscription_id=razorpay_subscription_id,
        status=ChargeStatus.failed.value if failed else status_for(amount, refunded),
        refunded_paise=0 if failed else refunded,
        charged_at=_at(payment.get("created_at")) or _now(),
    )
    try:
        async with session.begin_nested():
            return await session.scalar(
                pg_insert(SubscriptionCharges)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["razorpay_payment_id"])
                .returning(SubscriptionCharges.id)
            )
    except Exception:  # noqa: BLE001
        logger.warning("subscription charge %s could not be recorded", payment_id, exc_info=True)
        return None


async def record_charge(session, subscription: ClubSubscriptions, payload: dict) -> int | None:
    """A `subscription.charged` webhook: its payment, into the ledger, once.
    The caller commits. Never raises."""
    if subscription.complimentary:
        # Cannot happen (a comp has no Razorpay subscription to be charged
        # on); refused all the same, so a comp can never show as income.
        return None
    payment = billing.payment_from_payload(payload)
    new_id = await _insert(
        session,
        subscription_id=subscription.id,
        club_id=subscription.club_id,
        plan_id=subscription.plan_id,
        cycle=subscription.cycle,
        razorpay_subscription_id=(
            subscription.razorpay_subscription_id or billing.subscription_id_from_payload(payload)
        ),
        payment=payment,
    )
    if new_id is None and payment.get("id") and payment.get("amount") is not None:
        # Recorded as failed before (Razorpay's own retries reuse nothing,
        # but a late `payment.failed` can land first): it was taken after all.
        earlier = await session.scalar(
            select(SubscriptionCharges).where(
                SubscriptionCharges.razorpay_payment_id == payment["id"],
                SubscriptionCharges.status == ChargeStatus.failed.value,
            )
        )
        if earlier is not None:
            earlier.amount_paise = int(payment["amount"])
            earlier.refunded_paise = 0
            earlier.status = ChargeStatus.captured.value
            earlier.charged_at = _at(payment.get("created_at")) or earlier.charged_at
            return earlier.id
    return new_id


async def record_failure(session, subscription: ClubSubscriptions, payload: dict) -> int | None:
    """A `payment.failed` for one of the subscription's charges: a `failed`
    row (never summed), so the payments list shows the attempt. Once per
    payment. The caller commits. Never raises."""
    if subscription.complimentary:
        return None
    return await _insert(
        session,
        subscription_id=subscription.id,
        club_id=subscription.club_id,
        plan_id=subscription.plan_id,
        cycle=subscription.cycle,
        razorpay_subscription_id=subscription.razorpay_subscription_id,
        payment=billing.payment_from_payload(payload),
        failed=True,
    )


async def apply_refund(session, data: dict) -> bool:
    """
    A `refund.processed` webhook. Returns False when the payment is not a
    subscription's (the caller handles it as a ticket refund), True when it
    is, after updating the charge and committing.

    What was refunded is taken from the payment's own running total
    (`amount_refunded`) -- on the payload, or read from Razorpay -- so the
    second of two partial refunds, or the same refund reported twice, comes
    out right. Only when neither is to hand is this refund's amount added.
    """
    payload = data.get("payload", {}) or {}
    refund = (payload.get("refund") or {}).get("entity") or {}
    payment_id = refund.get("payment_id")
    if not payment_id:
        return False
    charge = await session.scalar(
        select(SubscriptionCharges)
        .where(SubscriptionCharges.razorpay_payment_id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if charge is None:
        return False
    if charge.status == ChargeStatus.failed.value:
        # Nothing was taken, so nothing can have been given back.
        await session.commit()
        return True

    payment = (payload.get("payment") or {}).get("entity") or {}
    total = payment.get("amount_refunded") if payment.get("id") == payment_id else None
    if total is None:
        try:
            fetched = await asyncio.to_thread(billing.razorpay_client.payment.fetch, payment_id)
            total = (fetched or {}).get("amount_refunded")
        except Exception:  # noqa: BLE001
            logger.warning("Could not read Razorpay payment %s for its refunds", payment_id, exc_info=True)
    if total is None:
        total = int(charge.refunded_paise or 0) + int(refund.get("amount") or 0)
    _set_refunded(charge, int(total))
    await session.commit()
    return True


# ─── Backfill ────────────────────────────────────────────────────────────


def _invoices(client, razorpay_subscription_id: str) -> list[dict]:
    """Every invoice Razorpay has for the subscription, page by page."""
    out: list[dict] = []
    skip = 0
    while True:
        page = client.invoice.all({"subscription_id": razorpay_subscription_id, "count": PAGE, "skip": skip}) or {}
        items = page.get("items") or []
        out.extend(items)
        if len(items) < PAGE:
            return out
        skip += PAGE


async def _targets(session) -> list[tuple[str, ClubSubscriptions, int | None, object]]:
    """(razorpay id, row, plan id, cycle) for every Razorpay subscription a
    row holds now, or held before a complimentary plan replaced it."""
    rows = (
        await session.execute(
            select(ClubSubscriptions)
            .order_by(ClubSubscriptions.id)
            # Every row, removed ones too: their payments were revenue.
            .execution_options(include_deleted=True)
        )
    ).scalars().all()
    seen: set[str] = set()
    out = []
    for row in rows:
        if row.razorpay_subscription_id and row.razorpay_subscription_id not in seen:
            seen.add(row.razorpay_subscription_id)
            out.append((row.razorpay_subscription_id, row, row.plan_id, row.cycle))
        prev = row.previous_state or {}
        earlier = prev.get("razorpay_subscription_id")
        if earlier and earlier not in seen:
            seen.add(earlier)
            out.append((earlier, row, prev.get("plan_id") or row.plan_id, prev.get("cycle") or row.cycle))
    return out


async def backfill(session, *, client=None) -> dict:
    """
    Add every subscription payment Razorpay has that the ledger does not.

    For each Razorpay subscription we know of: its invoices (read-only), and
    for each paid one the payment behind it, which also says what has been
    refunded. A payment already in the ledger only has its refunds brought up
    to date. Returns {scanned, inserted, updated, failed}: subscriptions
    asked about, charges added, charges whose refunds changed, subscriptions
    Razorpay would not answer for (try again later; it is safe to rerun).
    """
    client = client or billing.razorpay_client
    done = {"scanned": 0, "inserted": 0, "updated": 0, "failed": 0}
    for razorpay_id, row, plan_id, cycle in await _targets(session):
        done["scanned"] += 1
        try:
            invoices = await asyncio.to_thread(_invoices, client, razorpay_id)
        except Exception:  # noqa: BLE001
            logger.warning("backfill: Razorpay would not list invoices for %s", razorpay_id, exc_info=True)
            done["failed"] += 1
            continue
        for invoice in invoices:
            payment_id = invoice.get("payment_id")
            if invoice.get("status") != "paid" or not payment_id:
                continue
            payment = {
                "id": payment_id,
                "amount": invoice.get("amount_paid") or invoice.get("amount"),
                "currency": invoice.get("currency"),
                "created_at": invoice.get("paid_at") or invoice.get("issued_at") or invoice.get("created_at"),
            }
            try:
                fetched = await asyncio.to_thread(client.payment.fetch, payment_id) or {}
                payment.update({k: fetched[k] for k in ("amount", "currency", "created_at", "amount_refunded") if fetched.get(k) is not None})
            except Exception:  # noqa: BLE001
                logger.info("backfill: payment %s not read; using its invoice", payment_id, exc_info=True)
            new_id = await _insert(
                session,
                subscription_id=row.id,
                club_id=row.club_id,
                plan_id=plan_id,
                cycle=cycle,
                razorpay_subscription_id=razorpay_id,
                payment=payment,
                invoice_id=invoice.get("id"),
            )
            if new_id is not None:
                done["inserted"] += 1
            elif payment.get("amount_refunded") is not None:
                charge = await session.scalar(
                    select(SubscriptionCharges).where(SubscriptionCharges.razorpay_payment_id == payment_id)
                )
                if charge is not None and _set_refunded(charge, int(payment["amount_refunded"])):
                    done["updated"] += 1
        await session.commit()
    return done
