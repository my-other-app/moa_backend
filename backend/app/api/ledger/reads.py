"""The finance and revenue figures, read from the ledger's sums.

The same figures app/api/finance/service.py and app/api/subscriptions/revenue.py
work out from registrations, store orders, payment logs and subscription
charges, read instead from ledger_monthly (a club's events) and
ledger_platform_monthly (the platform's months): a few rows per event or per
month, however many registrations or charges made them. Which one a page
uses is settings.LEDGER_READS (`enabled`); scripts/ledger_compare.py and
the tests run both on the same data and expect the same numbers.

What does not come from the ledger, because it is not money:

- an event's registrations and paid registrations: the event's own
  `going_count` and `paid_count`, which triggers on registrations keep;
- what is still owed (unpaid registrations on a paid event): read from the
  unpaid registrations only;
- expenses and other income: the club's own book (club_expenses), as before;
- trials started and cancellations on the revenue page.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import and_, func, select

from app.api.ledger import book
from app.api.ledger.models import LedgerEntries as E
from app.api.ledger.models import LedgerMonthly as LM
from app.api.ledger.models import LedgerPlatformMonthly as LP
from app.config import settings
from app.core import money
from app.core.utils.time_buckets import utc_trunc

_override: ContextVar[bool | None] = ContextVar("ledger_reads", default=None)


def enabled() -> bool:
    """Whether the pages read the ledger: settings.LEDGER_READS, unless
    `reading` says otherwise for this task (the compare runs both)."""
    forced = _override.get()
    return settings.LEDGER_READS if forced is None else forced


@contextmanager
def reading(ledger: bool):
    """Within the block, this task's pages read the ledger (True) or the rows (False)."""
    token = _override.set(ledger)
    try:
        yield
    finally:
        _override.reset(token)


def _rupees(paise) -> Decimal:
    return money.from_paise(int(paise or 0))


# ─── A club's events ─────────────────────────────────────────────────────


async def per_event(session, event_ids: list[int]) -> dict[int, dict]:
    """finance.service._per_event's figures, per event, all time."""
    from app.api.events.models import Events
    from app.api.finance import service as finance

    out = {i: finance._blank() for i in event_ids}
    if not event_ids:
        return out
    # Counts of registrations, from the event's own counters.
    for eid, going, paid in (await session.execute(
        select(Events.id, Events.going_count, Events.paid_count).where(Events.id.in_(event_ids))
    )).all():
        out[eid].update(regs=int(going or 0), paying=int(paid or 0))
    # The money, from the sums.
    for eid, account, kind, paise, units in (await session.execute(
        select(LM.event_id, LM.account, LM.kind, func.sum(LM.amount_paise), func.sum(LM.units))
        .where(LM.event_id.in_(event_ids))
        .group_by(LM.event_id, LM.account, LM.kind)
    )).all():
        m = out.get(eid)
        if m is None:
            continue
        amount = -_rupees(paise)  # credits: what came in
        if account == book.TICKETS_ONLINE:
            m["tickets"] += amount
            m["online"] += amount
            m["online_count"] += int(units or 0)
        elif account == book.TICKETS_DESK:
            m["tickets"] += amount
            m["cash"] += amount
            m["cash_count"] += int(units or 0)
        elif account == book.STORE:
            m["merch"] += amount
        elif account == book.GATEWAY and kind == book.REFUND:
            m["refunds"] += amount
            m["refund_count"] += int(units or 0)
    await _owed(session, event_ids, out)
    await finance._expense_totals(session, event_ids, out)
    return out


async def _owed(session, event_ids: list[int], out: dict[int, dict]) -> None:
    """What is still owed: unpaid registrations on a paid event whose
    payment was not refunded. Only the unpaid rows are read."""
    from app.api.events.models import EventRegistrationsLink as Reg
    from app.api.events.models import Events
    from app.api.finance import service as finance

    refunded = finance._refunded_receipts()
    rows = await session.execute(
        select(Reg.event_id, func.count(), func.coalesce(func.sum(Reg.actual_amount), 0))
        .join(Events, Events.id == Reg.event_id)
        .outerjoin(refunded, refunded.c.receipt == Reg.payment_receipt)
        .where(
            Reg.event_id.in_(event_ids), Reg.is_deleted == False, Reg.is_paid == False,  # noqa: E712
            Events.has_fee == True, refunded.c.receipt.is_(None),  # noqa: E712
        )
        .group_by(Reg.event_id)
    )
    for eid, n, amount in rows.all():
        out[eid].update(outstanding_count=int(n), outstanding=Decimal(amount))


async def event_months(session, event_id: int) -> dict[str, dict]:
    """The event's money by the month it moved (UTC months, as the event's
    page has always shown them): tickets and merchandise in the month of the
    sale (a refund takes them back in that month), refunds in the month they
    were made."""
    from app.api.finance import service as finance

    months: dict[str, dict] = {}
    sale_month = utc_trunc("month", E.origin_at)
    for when, account, paise, units in (await session.execute(
        select(sale_month, E.account, func.sum(E.amount_paise), func.sum(E.units))
        .where(E.event_id == event_id, E.account.in_((*book.TICKETS, book.STORE)))
        .group_by(sale_month, E.account)
    )).all():
        if not paise and not units:
            continue  # sold and refunded: nothing there now
        key = "tickets" if account in book.TICKETS else "merch"
        months.setdefault(finance._month_of(when), finance._new_month(""))[key] += -_rupees(paise)
    refund_month = utc_trunc("month", E.occurred_at)
    for when, paise in (await session.execute(
        select(refund_month, func.sum(E.amount_paise))
        .where(E.event_id == event_id, E.account == book.GATEWAY, E.kind == book.REFUND)
        .group_by(refund_month)
    )).all():
        months.setdefault(finance._month_of(when), finance._new_month(""))["refunds"] += -_rupees(paise)
    return months


# ─── The platform's revenue ──────────────────────────────────────────────


async def _platform(session, *where) -> dict[tuple, tuple[int, int]]:
    rows = await session.execute(
        select(LP.origin_month, LP.account, LP.kind, func.sum(LP.amount_paise), func.sum(LP.units))
        .where(*where)
        .group_by(LP.origin_month, LP.account, LP.kind)
    )
    return {(m, a, k): (int(p or 0), int(u or 0)) for m, a, k, p, u in rows.all()}


def _first(year: int, month: int) -> date:
    return date(year, month, 1)


async def collected(session, now: datetime) -> dict:
    """revenue.collected: subscription charges by the month they were taken;
    refunds against the month of the charge."""
    from app.api.subscriptions import revenue

    year, month = revenue._this_month(now)
    this, last = _first(year, month), _first(*revenue._shift(year, month, -1))
    year_start = _first(year, 1)
    sums = await _platform(session, LP.account == book.GATEWAY, LP.kind.in_((book.SUB_CHARGE, book.SUB_REFUND)))
    charged = {m: p for (m, _a, k), (p, _u) in sums.items() if k == book.SUB_CHARGE}
    refunded = sum(-p for (_m, _a, k), (p, _u) in sums.items() if k == book.SUB_REFUND)

    def between(lo: date, hi: date) -> int:
        return sum(p for m, p in charged.items() if lo <= m <= hi)

    return {
        "this_month_paise": charged.get(this, 0),
        "last_month_paise": charged.get(last, 0),
        "year_to_date_paise": between(year_start, this),
        "all_time_paise": sum(charged.values()),
        "refunded_paise": refunded,
    }


async def series_money(session, keys: list[str], since: date) -> dict[str, dict]:
    """revenue.series' money and markers by month key ("2026-10"): collected
    and refunded (against the charge's month), new paying subscriptions (a
    subscription's first charge) and complimentary plans given."""
    out = {key: {"collected_paise": 0, "refunded_paise": 0, "new_paying": 0, "new_complimentary": 0} for key in keys}
    sums = await _platform(
        session,
        LP.origin_month >= since,
        (
            and_(LP.account == book.GATEWAY, LP.kind.in_((book.SUB_CHARGE, book.SUB_REFUND)))
            | and_(LP.account == book.SUBSCRIBERS, LP.kind == book.FIRST_CHARGE)
            | and_(LP.account == book.COMPS, LP.kind == book.COMP)
        ),
    )
    for (m, account, kind), (paise, units) in sums.items():
        bucket = out.get(f"{m.year:04d}-{m.month:02d}")
        if bucket is None:
            continue
        if kind == book.SUB_CHARGE:
            bucket["collected_paise"] += paise
        elif kind == book.SUB_REFUND:
            bucket["refunded_paise"] += -paise
        elif kind == book.FIRST_CHARGE:
            bucket["new_paying"] += units
        elif kind == book.COMP:
            bucket["new_complimentary"] += units
    return out


async def ticket_sales(session, now: datetime) -> tuple[int, int]:
    """revenue.ticket_sales: ticket and store payments captured in the
    platform's own Razorpay account and not refunded, by the month they were
    taken: (this month, all time), in paise."""
    from app.api.subscriptions import revenue

    year, month = revenue._this_month(now)
    sums = await _platform(
        session, LP.account == book.GATEWAY, LP.club_gateway == False,  # noqa: E712
        LP.kind.in_((book.CAPTURE, book.REFUND)),
    )
    this = _first(year, month)
    return (sum(p for (m, _a, _k), (p, _u) in sums.items() if m == this), sum(p for (p, _u) in sums.values()))
