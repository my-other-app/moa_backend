"""Both ways, the same numbers: the finance and revenue figures worked out
from the rows and read from the ledger, side by side, per club and month.

Run after the backfill and before turning LEDGER_READS on
(scripts/ledger_compare.py); the tests run it on a seeded dataset and expect
no difference. It calls the pages' own code twice (ledger_reads.reading), so
what it compares is what a club and an admin would see:

- each club's finance page over the whole of its history: every month's
  tickets, merchandise, other income, refunds, expenses and net, the
  period's figures, and every event's row;
- each club's subscription charges and refunds by IST month;
- the admin revenue page: collected, the twelve-month series, ticket sales;
- with `events`, every event's own page too.

A difference names the club, the month (or the event), the figure, and both
values. Where the rows and the ledger can rightly differ -- a paid
registration deleted later (its money stays in the ledger), a ticket paid in
a later month than it was registered (the event's page dates it by the
payment) -- the difference is real and is reported like any other: look at
it before switching.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from app.api.ledger import book
from app.api.ledger.models import LedgerMonthly as LM
from app.api.ledger.reads import reading

FIGURES = (
    "regs", "paying", "gross", "tickets", "merch", "other_income", "refunds", "refund_count", "online", "online_count",
    "cash", "cash_count", "expenses", "net", "outstanding", "outstanding_count",
)
MONTH_FIGURES = ("tickets", "merch", "other", "refunds", "expenses", "net")
EVENT_FIGURES = ("regs", "paying", "tickets", "merch", "other_income", "online", "cash", "refunds", "expenses", "net", "outstanding", "outstanding_count")


def _diff(out: list, club_id, month, what: str, rows, ledger, **extra) -> None:
    if rows != ledger:
        out.append({"club_id": club_id, "month": month, "what": what, "rows": rows, "ledger": ledger, **extra})


async def _span(session, club_id: int):
    from app.api.events.models import Events
    from app.api.finance.service import _SHOWN

    lo, hi = (await session.execute(
        select(func.min(Events.event_datetime), func.max(Events.event_datetime))
        .where(Events.club_id == club_id, Events.is_deleted == False, Events.lifecycle.in_(_SHOWN))  # noqa: E712
    )).one()
    if lo is None:
        return None
    return lo.astimezone(timezone.utc).date(), hi.astimezone(timezone.utc).date()


async def _finance(session, club, out: list) -> int:
    from app.api.finance import service as finance

    span = await _span(session, club.id)
    if span is None:
        return 0
    frm, to = span
    with reading(False):
        rows = await finance.club_summary(session, club, frm, to)
    with reading(True):
        ledger = await finance.club_summary(session, club, frm, to)
    by_month = {m["month"]: m for m in ledger["by_month"]}
    for m in rows["by_month"]:
        other = by_month.get(m["month"], {})
        for key in MONTH_FIGURES:
            _diff(out, club.id, m["month"], f"finance.month.{key}", m[key], other.get(key))
    for key in FIGURES:
        _diff(out, club.id, "all", f"finance.{key}", rows["figures"][key], ledger["figures"][key])
    by_event = {e["id"]: e for e in ledger["by_event"]}
    for e in rows["by_event"]:
        other = by_event.get(e["id"], {})
        for key in EVENT_FIGURES:
            _diff(out, club.id, e["starts_at"][:7], f"finance.event.{key}", e[key], other.get(key), event_id=e["id"])
    return len(rows["by_month"])


async def _charges(session, club_id: int, out: list) -> int:
    """The club's subscription money by IST month: charged, and refunded
    against the month of the charge."""
    from app.api.subscriptions.ledger import MONEY
    from app.api.subscriptions.models import SubscriptionCharges
    from app.api.subscriptions.revenue import month_key

    rows: dict[str, list[int]] = {}
    for charged_at, amount, refunded in (await session.execute(
        select(SubscriptionCharges.charged_at, SubscriptionCharges.amount_paise, SubscriptionCharges.refunded_paise)
        .where(SubscriptionCharges.club_id == club_id, MONEY)
    )).all():
        bucket = rows.setdefault(month_key(charged_at), [0, 0])
        bucket[0] += int(amount)
        bucket[1] += int(refunded or 0)
    ledger: dict[str, list[int]] = {}
    for month, kind, paise in (await session.execute(
        select(LM.origin_month, LM.kind, func.sum(LM.amount_paise))
        .where(LM.club_id == club_id, LM.account == book.GATEWAY, LM.kind.in_((book.SUB_CHARGE, book.SUB_REFUND)))
        .group_by(LM.origin_month, LM.kind)
    )).all():
        bucket = ledger.setdefault(f"{month.year:04d}-{month.month:02d}", [0, 0])
        if kind == book.SUB_CHARGE:
            bucket[0] += int(paise)
        else:
            bucket[1] += -int(paise)
    for month in sorted(set(rows) | set(ledger)):
        a, b = rows.get(month, [0, 0]), ledger.get(month, [0, 0])
        _diff(out, club_id, month, "subscriptions.charged_paise", a[0], b[0])
        _diff(out, club_id, month, "subscriptions.refunded_paise", a[1], b[1])
    return len(set(rows) | set(ledger))


async def _events(session, club_id: int, out: list) -> None:
    from app.api.events.models import Events
    from app.api.finance import service as finance

    for event in (await session.scalars(
        select(Events).where(Events.club_id == club_id, Events.is_deleted == False)  # noqa: E712
    )).all():
        with reading(False):
            rows = await finance.event_summary(session, event)
        with reading(True):
            ledger = await finance.event_summary(session, event)
        by_month = {m["month"]: m for m in ledger["by_month"]}
        for m in rows["by_month"]:
            other = by_month.get(m["month"], {})
            for key in MONTH_FIGURES:
                _diff(out, club_id, m["month"], f"event.month.{key}", m[key], other.get(key), event_id=event.id)
        for key in FIGURES:
            _diff(out, club_id, "all", f"event.{key}", rows["figures"][key], ledger["figures"][key], event_id=event.id)


async def _platform(session, out: list, now: datetime) -> None:
    from app.api.subscriptions import revenue

    with reading(False):
        rows = {"collected": await revenue.collected(session, now), "series": await revenue.series(session, 12, now),
                "ticket_sales": await revenue.ticket_sales(session, now)}
    with reading(True):
        ledger = {"collected": await revenue.collected(session, now), "series": await revenue.series(session, 12, now),
                  "ticket_sales": await revenue.ticket_sales(session, now)}
    for key, value in rows["collected"].items():
        _diff(out, None, "all", f"revenue.collected.{key}", value, ledger["collected"][key])
    for a, b in zip(rows["series"], ledger["series"]):
        for key, value in a.items():
            _diff(out, None, a["month"], f"revenue.series.{key}", value, b.get(key))
    for key in ("this_month_paise", "all_time_paise"):
        _diff(out, None, "all", f"revenue.ticket_sales.{key}", rows["ticket_sales"][key], ledger["ticket_sales"][key])


async def compare(session, *, club_ids: list[int] | None = None, events: bool = False,
                  now: datetime | None = None) -> dict:
    """Every club's figures (or those of `club_ids`), and the admin's, both
    ways. Returns {"clubs", "club_months", "differences": [...]}."""
    from app.api.clubs.models import Clubs
    from app.api.events.models import Events
    from app.api.subscriptions.models import SubscriptionCharges

    now = now or datetime.now(timezone.utc)
    if club_ids is None:
        with_events = select(Events.club_id).where(Events.club_id.isnot(None))
        with_charges = select(SubscriptionCharges.club_id)
        club_ids = sorted({cid for (cid,) in (await session.execute(with_events.union(with_charges))).all()})
    out: list = []
    months = 0
    for club_id in club_ids:
        club = await session.scalar(select(Clubs).where(Clubs.id == club_id).execution_options(include_deleted=True))
        if club is None:
            continue
        months += await _finance(session, club, out)
        months += await _charges(session, club_id, out)
        if events:
            await _events(session, club_id, out)
        await session.commit()
    await _platform(session, out, now)
    await session.commit()
    return {"clubs": len(club_ids), "club_months": months, "differences": out}
