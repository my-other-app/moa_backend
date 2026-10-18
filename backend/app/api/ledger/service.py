"""What is posted, and when: each money transition the app already has,
turned into a posting in the transaction that makes it (book.post).

Tickets and the store, from the club's side:

- `capture(log)`: Razorpay captured a payment (payments.service.apply_payment,
  and for a payment marked to go back, its duplicate and event-full paths).
  Gateway debited, unapplied credited.
- `sync_registration(reg, kind)`: the registration's ticket and store money
  as its rows now stand, against what the ledger holds for it; the
  difference is posted, balanced against `unapplied` (money paid online),
  `desk` (paid at the door) or `comps` (given free). Called after a capture
  is applied (APPLY), a refund takes it back (UNAPPLY), a walk-up pays at the
  desk (DESK_SALE), an oversold order is handed over (FULFIL), a free pick
  (COMP). One function for every path, so a path that changes the rows in a
  way nobody planned for is still recorded as what it did.
- `refund(log)`: Razorpay gave a payment back (payments.service._apply_refund).
  Unapplied debited, gateway credited. Its capture is posted first when it
  is not there yet (a payment taken before the ledger existed).
- `ensure_history(reg)`: at the start of a transition, a registration the
  ledger has never heard of that already holds money (paid before the ledger
  existed) is posted as it stands, with its payments, before the change.

Subscriptions, from the platform's side (subscriptions.ledger):

- `charge(charge)`: a charge taken (gateway / subscriptions), its refunds
  brought up to date, and the subscription's first-charge marker.
- `charge_refunds(charge)`: what is refunded on it, against what the ledger
  has given back; the difference is posted (Razorpay reports a running total).
- `comp_granted(...)`: a complimentary plan, a zero-rupee marker.

Every posting's key is fixed by what it records (a payment id, a
registration's transition from one balance to another), so posting it again
-- a replayed webhook, the backfill meeting a live posting -- writes nothing.
"""
from __future__ import annotations

import logging
import zlib
from datetime import datetime

from sqlalchemy import func, select

from app.api.ledger import book
from app.api.ledger.book import Line, Txn
from app.api.ledger.models import LedgerEntries as E
from app.core import money

logger = logging.getLogger(__name__)

#: First key of the advisory locks taken here: (LOCK_NS, a registration or a subscription).
LOCK_NS = 7_420_455

ZERO = (0, 0)

#: Where a registration posting's difference is balanced, by kind.
_BALANCING = {
    book.APPLY: book.UNAPPLIED,
    book.UNAPPLY: book.UNAPPLIED,
    book.FULFIL: book.UNAPPLIED,
    book.DESK_SALE: book.DESK,
    book.COMP: book.COMPS,
}

ONLINE, DESK = "online", "desk"


def _key32(value) -> int:
    raw = zlib.crc32(str(value).encode())
    return raw - 2**32 if raw >= 2**31 else raw


async def _lock(session, value, *, wait: bool = True) -> bool:
    """The transaction's advisory lock on `value` (a registration, a
    subscription): two postings about it run one after the other. With
    `wait=False`, whether it was free (the backfill skips what is busy)."""
    if wait:
        await session.execute(select(func.pg_advisory_xact_lock(LOCK_NS, _key32(value))))
        return True
    return bool(await session.scalar(select(func.pg_try_advisory_xact_lock(LOCK_NS, _key32(value)))))


async def _club_of(session, event_id: int | None) -> int | None:
    if event_id is None:
        return None
    from app.api.events.models import Events

    return await session.scalar(select(Events.club_id).where(Events.id == event_id))


# ─── Payments: capture and refund ────────────────────────────────────────


async def capture(session, log, order, *, registration=None) -> int:
    """A captured payment: the gateway holds it, nothing has been bought
    with it yet. Lines written (0: posted before)."""
    from app.api.payments.handlers import registration_for_order

    if registration is None:
        registration = await registration_for_order(session, order)
    event_id = registration.event_id if registration is not None else None
    paise = money.to_paise(log.amount)
    at = log.created_at or book.now()
    return await book.post(session, Txn(
        key=f"{book.CAPTURE}:{log.razorpay_payment_id}", kind=book.CAPTURE,
        lines=[Line(book.GATEWAY, paise, 1), Line(book.UNAPPLIED, -paise)],
        occurred_at=at, origin_at=at, source=book.SRC_PAYMENT, source_id=str(log.id),
        club_id=await _club_of(session, event_id), event_id=event_id,
        registration_id=registration.id if registration is not None else None,
        gateway_id=order.payment_gateway_id, razorpay_payment_id=log.razorpay_payment_id,
        razorpay_order_id=order.razorpay_order_id, note=log.refund_reason,
    ))


async def refund(session, log, order, *, registration=None, refund_id: str | None = None,
                 at: datetime | None = None) -> int:
    """A payment given back in full. Its capture first, if the ledger does
    not have it (refunded after the ledger began, taken before it)."""
    from app.api.payments.handlers import registration_for_order

    if registration is None:
        registration = await registration_for_order(session, order)
    await capture(session, log, order, registration=registration)
    event_id = registration.event_id if registration is not None else None
    paise = money.to_paise(log.amount)
    return await book.post(session, Txn(
        key=f"{book.REFUND}:{log.razorpay_payment_id}", kind=book.REFUND,
        lines=[Line(book.UNAPPLIED, paise), Line(book.GATEWAY, -paise, 1)],
        occurred_at=at or book.now(), origin_at=log.created_at or book.now(),
        source=book.SRC_PAYMENT, source_id=str(log.id),
        club_id=await _club_of(session, event_id), event_id=event_id,
        registration_id=registration.id if registration is not None else None,
        gateway_id=order.payment_gateway_id, razorpay_payment_id=log.razorpay_payment_id,
        razorpay_order_id=order.razorpay_order_id, razorpay_refund_id=refund_id, note=log.refund_reason,
    ))


# ─── A registration's ticket and store money ─────────────────────────────


async def balances(session, registration_id) -> dict[str, tuple[int, int]]:
    """What the ledger holds for a registration, by account: (paise, units)."""
    have, _ = await _balances(session, registration_id)
    return have


async def _balances(session, registration_id) -> tuple[dict[str, tuple[int, int]], int]:
    """The balances, and how many lines made them: the count only grows
    (nothing is ever deleted), so it tells one posting about the
    registration from the next even when they change the same amounts."""
    rows = await session.execute(
        select(E.account, func.coalesce(func.sum(E.amount_paise), 0), func.coalesce(func.sum(E.units), 0), func.count())
        .where(E.registration_id == registration_id, E.account.in_(book.REGISTRATION_ACCOUNTS))
        .group_by(E.account)
    )
    have, seq = {}, 0
    for account, paise, units, n in rows.all():
        have[account] = (int(paise), int(units))
        seq += int(n)
    return have, seq


def _ticket_account(have: dict, channel: str) -> str:
    """Where the registration's ticket money is kept: where it is already
    (paid at the door and later buying online leaves the ticket at the door),
    or by how it is being paid now."""
    online, desk = have.get(book.TICKETS_ONLINE, ZERO), have.get(book.TICKETS_DESK, ZERO)
    if online != ZERO:
        return book.TICKETS_ONLINE
    if desk != ZERO:
        return book.TICKETS_DESK
    return book.TICKETS_DESK if channel == DESK else book.TICKETS_ONLINE


async def target(session, registration, *, channel: str, have: dict) -> dict[str, tuple[int, int]]:
    """What the ledger should hold for the registration, from its rows: the
    ticket's paid amount (and whether it is paid), the store orders sold, and
    those paid for when the pieces were gone (oversold)."""
    from app.api.store.models import EventStoreOrders as Orders

    want = {account: ZERO for account in book.REGISTRATION_ACCOUNTS}
    want[_ticket_account(have, channel)] = (-money.to_paise(registration.paid_amount), 1 if registration.is_paid else 0)
    rows = await session.execute(
        select(Orders.status, func.coalesce(func.sum(Orders.total), 0), func.count())
        .where(Orders.registration_id == registration.id, Orders.status.in_(("paid", "oversold")))
        .group_by(Orders.status)
    )
    for status, total, n in rows.all():
        want[book.STORE if status == "paid" else book.STORE_HELD] = (-money.to_paise(total), int(n))
    return want


def _channel_of(registration) -> str:
    """How a registration from before the ledger paid: online when it went
    through checkout (it carries a receipt), at the door otherwise -- the
    same rule the finance page has always used."""
    return ONLINE if registration.payment_receipt else DESK


async def sync_registration(
    session,
    registration,
    *,
    kind: str,
    channel: str = ONLINE,
    at: datetime | None = None,
    origin_at: datetime | None = None,
    accounts: tuple = book.REGISTRATION_ACCOUNTS,
    balancing: str | None = None,
    wait: bool = True,
    note: str | None = None,
) -> int | None:
    """
    Post the difference between what the registration's rows say it holds
    (`target`) and what the ledger holds for it (`balances`), in `accounts`,
    balanced against the kind's account. The caller has made the change and
    not committed. Lines written; None when `wait=False` and another
    transaction is posting for it (the backfill comes back to it).
    """
    from app.api.events.models import EventRegistrationsLink as Reg

    await session.flush()
    if not await _lock(session, registration.id, wait=wait):
        return None
    # As it stands now: another transaction may have changed it while this one waited.
    paid_amount, is_paid, receipt = (await session.execute(
        select(Reg.paid_amount, Reg.is_paid, Reg.payment_receipt).where(Reg.id == registration.id)
    )).one()
    have, seq = await _balances(session, registration.id)
    current = _Row(registration.id, registration.event_id, paid_amount, is_paid, receipt)
    want = await target(session, current, channel=channel, have=have)
    changed = sorted(
        (account, have.get(account, ZERO), want[account])
        for account in accounts if have.get(account, ZERO) != want[account]
    )
    if not changed:
        return 0
    lines = [Line(account, w[0] - h[0], w[1] - h[1]) for account, h, w in changed]
    if balancing is None:
        balancing = _BALANCING.get(kind) or (book.DESK if channel == DESK else book.UNAPPLIED)
    lines.append(Line(balancing, -sum(line.amount for line in lines)))
    when = at or book.now()
    event_id = registration.event_id
    return await book.post(session, Txn(
        key=f"{kind}:{registration.id}:{book.digest(seq, changed)}", kind=kind, lines=lines,
        occurred_at=when, origin_at=origin_at or when, source=book.SRC_REGISTRATION,
        source_id=str(registration.id), club_id=await _club_of(session, event_id), event_id=event_id,
        registration_id=registration.id, note=note,
    ))


class _Row:
    """The registration's money columns as read under the lock."""

    def __init__(self, id, event_id, paid_amount, is_paid, payment_receipt):
        self.id, self.event_id = id, event_id
        self.paid_amount, self.is_paid, self.payment_receipt = paid_amount, is_paid, payment_receipt


async def _payments_of(session, registration):
    """The registration's captured and refunded payments, with their orders."""
    from app.api.payments.models import PaymentLogs, PaymentOrders, PaymentStatus

    if not registration.payment_receipt:
        return []
    return (await session.execute(
        select(PaymentLogs, PaymentOrders)
        .join(PaymentOrders, PaymentOrders.id == PaymentLogs.order_id)
        .where(PaymentOrders.receipt == registration.payment_receipt,
               PaymentLogs.status.in_((PaymentStatus.captured, PaymentStatus.refunded)))
        .order_by(PaymentLogs.created_at)
        .execution_options(include_deleted=True)
    )).all()


async def post_payments(session, registration) -> int:
    """The registration's payments, captured and refunded, as Razorpay took
    and gave them (idempotent on each payment)."""
    from app.api.payments.models import PaymentStatus

    written = 0
    for log, order in await _payments_of(session, registration):
        written += await capture(session, log, order, registration=registration)
        if log.status == PaymentStatus.refunded:
            written += await refund(session, log, order, registration=registration, at=log.updated_at or log.created_at)
    return written


async def post_as_it_stands(session, registration, *, kind: str = book.BACKFILL, wait: bool = True,
                            payments: bool = True) -> int | None:
    """The registration's money as its rows hold it, from before the ledger:
    its payments (unless `payments=False`: the backfill walks those on their
    own), its ticket (dated when it registered, as the event's page has
    always shown it) and its store orders (when they were paid). None when
    busy and `wait=False`."""
    from app.api.store.models import EventStoreOrders as Orders

    if not await _lock(session, registration.id, wait=wait):
        return None
    written = await post_payments(session, registration) if payments else 0
    channel = _channel_of(registration)
    created = registration.created_at or book.now()
    tickets = await sync_registration(session, registration, kind=kind, channel=channel, at=created,
                                      accounts=book.TICKETS)
    paid_at = await session.scalar(
        select(func.max(Orders.paid_at)).where(Orders.registration_id == registration.id, Orders.status.in_(("paid", "oversold")))
    )
    store = await sync_registration(session, registration, kind=kind, channel=channel, at=paid_at or created,
                                    accounts=(book.STORE, book.STORE_HELD))
    return written + (tickets or 0) + (store or 0)


async def ensure_history(session, registration) -> None:
    """Before a transition: a registration that holds money the ledger has
    never seen (paid before the ledger existed) is posted as it stands, so
    the transition's own posting is only what it changes."""
    if await balances(session, registration.id):
        return
    from app.api.store.models import EventStoreOrders as Orders

    has_money = bool(registration.is_paid) or money.to_paise(registration.paid_amount) != 0 or bool(
        await session.scalar(select(Orders.id).where(
            Orders.registration_id == registration.id, Orders.status.in_(("paid", "oversold"))
        ).limit(1))
    )
    if has_money:
        await post_as_it_stands(session, registration)


# ─── Subscriptions ───────────────────────────────────────────────────────


def _first_group(charge) -> str:
    """Which subscription a charge is for, as the revenue page groups them:
    the Razorpay subscription, else our row, else the payment itself."""
    if charge.razorpay_subscription_id:
        return charge.razorpay_subscription_id
    if charge.subscription_id is not None:
        return str(charge.subscription_id)
    return charge.razorpay_payment_id


async def _post_charge(session, charge_row) -> int:
    paise = int(charge_row.amount_paise)
    at = charge_row.charged_at
    return await book.post(session, Txn(
        key=f"{book.SUB_CHARGE}:{charge_row.razorpay_payment_id}", kind=book.SUB_CHARGE,
        lines=[Line(book.GATEWAY, paise, 1), Line(book.SUBSCRIPTIONS, -paise)],
        occurred_at=at, origin_at=at, source=book.SRC_CHARGE, source_id=str(charge_row.id),
        club_id=charge_row.club_id, razorpay_payment_id=charge_row.razorpay_payment_id,
        razorpay_subscription_id=charge_row.razorpay_subscription_id,
    ))


async def charge(session, charge_row, *, refunded_at: datetime | None = None) -> int:
    """A subscription charge Razorpay took: into the gateway, earned by
    subscriptions; then its refunds and its first-charge marker. A failed
    attempt is not money and is not posted."""
    if charge_row.status == "failed":
        return 0
    written = await _post_charge(session, charge_row)
    written += await first_charge(session, charge_row)
    written += await charge_refunds(session, charge_row, at=refunded_at)
    return written


async def _refunded_in_ledger(session, payment_id: str) -> int:
    return -int(await session.scalar(
        select(func.coalesce(func.sum(E.amount_paise), 0)).where(
            E.razorpay_payment_id == payment_id, E.account == book.GATEWAY, E.kind == book.SUB_REFUND,
        )
    ) or 0)


async def charge_refunds(session, charge_row, *, at: datetime | None = None) -> int:
    """What Razorpay says is refunded on the charge (a running total), less
    what the ledger has given back: the difference is posted. The charge
    first, if it is not there."""
    if charge_row.status == "failed":
        return 0
    total = int(charge_row.refunded_paise or 0)
    posted = await _refunded_in_ledger(session, charge_row.razorpay_payment_id)
    delta = total - posted
    if delta == 0:
        return 0
    await _post_charge(session, charge_row)
    return await book.post(session, Txn(
        key=f"{book.SUB_REFUND}:{charge_row.razorpay_payment_id}:{total}", kind=book.SUB_REFUND,
        lines=[Line(book.SUBSCRIPTIONS, delta), Line(book.GATEWAY, -delta, 1 if posted == 0 else 0)],
        occurred_at=at or book.now(), origin_at=charge_row.charged_at,
        source=book.SRC_CHARGE, source_id=str(charge_row.id), club_id=charge_row.club_id,
        razorpay_payment_id=charge_row.razorpay_payment_id, razorpay_subscription_id=charge_row.razorpay_subscription_id,
    ))


async def first_charge(session, charge_row) -> int:
    """The subscription's first charge, as a marker in the month it was taken
    (the revenue page's "new paying"). An earlier charge arriving later (the
    backfill) moves it: minus one in the old month, plus one in the new."""
    if charge_row.status == "failed":
        return 0
    group = _first_group(charge_row)
    await _lock(session, f"first:{group}")
    current = (await session.execute(
        select(E.origin_at).where(E.account == book.SUBSCRIBERS, E.kind == book.FIRST_CHARGE, E.source_id == group)
        .group_by(E.origin_at).having(func.sum(E.units) > 0)
        .order_by(E.origin_at).limit(1)
    )).scalar()
    at = charge_row.charged_at
    if current is not None and current <= at:
        return 0
    common = dict(kind=book.FIRST_CHARGE, source=book.SRC_SUBSCRIPTION, source_id=group, club_id=charge_row.club_id,
                  razorpay_subscription_id=charge_row.razorpay_subscription_id)
    written = await book.post(session, Txn(
        key=f"{book.FIRST_CHARGE}:{group}:{at.isoformat()}", lines=[Line(book.SUBSCRIBERS, 0, 1)],
        occurred_at=at, origin_at=at, razorpay_payment_id=charge_row.razorpay_payment_id, **common,
    ))
    if current is not None and written:
        written += await book.post(session, Txn(
            key=f"{book.FIRST_CHARGE}:{group}:{current.isoformat()}:moved", lines=[Line(book.SUBSCRIBERS, 0, -1)],
            occurred_at=current, origin_at=current, **common,
        ))
    return written


async def comp_granted(session, *, audit_id: int | None, club_id: int, subscription_id: int | None,
                       at: datetime | None = None) -> int:
    """A complimentary plan given: a zero-rupee marker, counted in the month
    it was given. Keyed by the audit entry that records the grant (the
    revenue page has always counted those)."""
    at = at or book.now()
    source_id = str(audit_id) if audit_id is not None else f"sub{subscription_id}:{at.isoformat()}"
    return await book.post(session, Txn(
        key=f"{book.COMP}:{source_id}", kind=book.COMP, lines=[Line(book.COMPS, 0, 1)],
        occurred_at=at, origin_at=at, source=book.SRC_AUDIT if audit_id is not None else book.SRC_SUBSCRIPTION,
        source_id=source_id, club_id=club_id, note=f"subscription {subscription_id}" if subscription_id else None,
    ))
