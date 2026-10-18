"""Finance: what an event brought and what it cost, for one event and for the club's season.

Income is tickets — every paid registration's `paid_amount`, cash at the door
included — and whatever else the ledger says came in: sponsorship, a grant,
a donation. A refund resets the registration, so what is held is already net
of refunds; the refund itself is read from the payment log that was flipped
to `refunded`. Expenses are the club's own ledger, logged against an event or
against the club.

The period picks events by their date, and every figure on the page is a sum
over those events, so the tiles, the months, the types and the table agree.
An event's own page needs no period: its money is its money.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import AsyncIterator
from uuid import UUID

from sqlalchemy import and_, func, or_, select

from app.api.closeout.models import EventStatus
from app.api.events.models import EventCategories, EventRegistrationsLink as Reg, Events
from app.api.finance.models import ClubExpenses
from app.api.finance.schemas import CATEGORIES, EXPENSE_CATEGORIES, INCOME_CATEGORIES, ExpenseCreate, ExpensePatch
from app.api.payments.models import PaymentLogs, PaymentOrders, PaymentStatus
from app.api.store.models import EventStoreOrders
from app.core.utils.time_buckets import utc_trunc
from app.response import CustomHTTPException

CLUB_ROW = "Club"  # overheads logged against no event
_SHOWN = (EventStatus.published, EventStatus.live, EventStatus.closed, EventStatus.archived)
_MONEY = ("gross", "tickets", "merch", "refunds", "expenses", "net")
EXPENSE, INCOME = "expense", "income"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _f(x) -> float:
    return float(round(Decimal(x or 0), 2))


def _pct(cur, prev) -> float | None:
    cur, prev = Decimal(cur or 0), Decimal(prev or 0)
    return None if prev == 0 else float(round((cur - prev) / prev * 100))


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _month_of(d) -> str:
    return f"{d.year:04d}-{d.month:02d}"


# ─── Period ──────────────────────────────────────────────────────────────


def _shift_month(y: int, m: int, by: int) -> tuple[int, int]:
    i = y * 12 + (m - 1) + by
    return i // 12, i % 12 + 1


def resolve_period(frm: date | None, to: date | None) -> tuple[date, date]:
    """Inclusive dates. Default: four months back through the end of next month, so
    the events selling now sit beside the ones just held."""
    today = _now().date()
    if to is None:
        y, m = _shift_month(today.year, today.month, 2)
        to = date(y, m, 1) - timedelta(days=1)
    if frm is None:
        y, m = _shift_month(today.year, today.month, -4)
        frm = date(y, m, 1)
    if frm > to:
        raise CustomHTTPException(422, message="The period ends before it starts")
    return frm, to


def _bounds(frm: date, to: date) -> tuple[datetime, datetime]:
    start = datetime(frm.year, frm.month, frm.day, tzinfo=timezone.utc)
    return start, datetime(to.year, to.month, to.day, tzinfo=timezone.utc) + timedelta(days=1)


def _previous(frm: date, to: date) -> tuple[date, date]:
    span = (to - frm).days + 1
    return frm - timedelta(days=span), frm - timedelta(days=1)


def _months(frm: date, to: date) -> list[str]:
    out, y, m = [], frm.year, frm.month
    while (y, m) <= (to.year, to.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ─── Money, per event ────────────────────────────────────────────────────

def _refunded_receipts():
    """The receipts with a refunded payment, each once: a small set (refunds
    are few, and ix_payment_logs_refunded_order reads only them), built once
    per statement and joined, where a correlated EXISTS inside the aggregate's
    FILTER was evaluated for every registration counted."""
    return (
        select(PaymentOrders.receipt.label("receipt"))
        .join(PaymentLogs, PaymentLogs.order_id == PaymentOrders.id)
        .where(PaymentLogs.status == PaymentStatus.refunded)
        .group_by(PaymentOrders.receipt)
        .subquery("refunded")
    )


def _blank() -> dict:
    return dict(
        regs=0, paying=0, tickets=Decimal(0), merch=Decimal(0), online=Decimal(0), online_count=0, cash=Decimal(0), cash_count=0,
        outstanding=Decimal(0), outstanding_count=0, refunds=Decimal(0), refund_count=0, expenses=Decimal(0), expense_count=0,
        other_income=Decimal(0), other_income_count=0,
    )


async def _per_event(session, event_ids: list[int]) -> dict[int, dict]:
    """Registrations, what they paid, what is still owed, what was refunded and what was spent — per event, all time."""
    out = {i: _blank() for i in event_ids}
    if not event_ids:
        return out
    paid = Reg.is_paid == True  # noqa: E712
    refunded = _refunded_receipts()
    owed = and_(Reg.is_paid == False, Events.has_fee == True, refunded.c.receipt.is_(None))  # noqa: E712
    # Paid online carries the provider's receipt; paid at the door, the desk took the money and there is none.
    online = and_(paid, Reg.payment_receipt.isnot(None))
    cash = and_(paid, Reg.payment_receipt.is_(None))
    q = (
        select(
            Reg.event_id,
            func.count(),
            func.count().filter(paid),
            func.coalesce(func.sum(Reg.paid_amount).filter(paid), 0),
            func.count().filter(owed),
            func.coalesce(func.sum(Reg.actual_amount).filter(owed), 0),
            func.coalesce(func.sum(Reg.paid_amount).filter(online), 0),
            func.count().filter(online),
            func.coalesce(func.sum(Reg.paid_amount).filter(cash), 0),
            func.count().filter(cash),
        )
        .join(Events, Events.id == Reg.event_id)
        .outerjoin(refunded, refunded.c.receipt == Reg.payment_receipt)
        .where(Reg.event_id.in_(event_ids), Reg.is_deleted == False)  # noqa: E712
        .group_by(Reg.event_id)
    )
    for eid, regs, paying, tickets, owed_n, owed_sum, online_sum, online_n, cash_sum, cash_n in (await session.execute(q)).all():
        out[eid].update(regs=regs, paying=paying, tickets=Decimal(tickets), outstanding_count=owed_n, outstanding=Decimal(owed_sum), online=Decimal(online_sum), online_count=online_n, cash=Decimal(cash_sum), cash_count=cash_n)
    q = (
        select(Reg.event_id, func.coalesce(func.sum(PaymentLogs.amount), 0), func.count())
        .select_from(PaymentLogs)
        .join(PaymentOrders, PaymentOrders.id == PaymentLogs.order_id)
        .join(Reg, Reg.payment_receipt == PaymentOrders.receipt)
        .where(PaymentLogs.status == PaymentStatus.refunded, Reg.event_id.in_(event_ids))
        .group_by(Reg.event_id)
    )
    for eid, amount, n in (await session.execute(q)).all():
        out[eid].update(refunds=Decimal(amount), refund_count=n)
    q = (
        select(ClubExpenses.event_id, ClubExpenses.kind, func.coalesce(func.sum(ClubExpenses.amount), 0), func.count())
        .where(ClubExpenses.event_id.in_(event_ids), ClubExpenses.is_deleted == False)  # noqa: E712
        .group_by(ClubExpenses.event_id, ClubExpenses.kind)
    )
    for eid, kind, amount, n in (await session.execute(q)).all():
        if kind == INCOME:
            out[eid].update(other_income=Decimal(amount), other_income_count=n)
        else:
            out[eid].update(expenses=Decimal(amount), expense_count=n)
    # Merchandise paid for with the ticket, kept apart from the ticket itself.
    q = select(EventStoreOrders.event_id, func.coalesce(func.sum(EventStoreOrders.total), 0)).where(EventStoreOrders.event_id.in_(event_ids), EventStoreOrders.status == "paid").group_by(EventStoreOrders.event_id)
    for eid, amount in (await session.execute(q)).all():
        out[eid].update(merch=Decimal(amount))
    return out


def _totals(rows: list[dict], overheads: list[ClubExpenses] | None = None) -> dict:
    t = _blank()
    for x in overheads or []:
        if x.kind == INCOME:
            t["other_income"] += Decimal(x.amount)
            t["other_income_count"] += 1
        else:
            t["expenses"] += Decimal(x.amount)
            t["expense_count"] += 1
    for r in rows:
        for k in t:
            t[k] += r[k]
    t["gross"] = t["tickets"] + t["merch"] + t["other_income"] + t["refunds"]
    t["net"] = t["tickets"] + t["merch"] + t["other_income"] - t["expenses"]
    return t


def _margin(t: dict) -> float | None:
    income = t["tickets"] + t["merch"] + t["other_income"]
    return float(round(t["net"] / income * 100)) if income else None


def _figures(t: dict, events: dict | None = None) -> dict:
    return dict(
        events=events, regs=t["regs"], paying=t["paying"],
        gross=_f(t["gross"]), tickets=_f(t["tickets"]), merch=_f(t["merch"]), other_income=_f(t["other_income"]), other_income_count=t["other_income_count"], refunds=_f(t["refunds"]), refund_count=t["refund_count"],
        online=_f(t["online"]), online_count=t["online_count"], cash=_f(t["cash"]), cash_count=t["cash_count"],
        expenses=_f(t["expenses"]), expense_count=t["expense_count"], net=_f(t["net"]), margin=_margin(t),
        outstanding=_f(t["outstanding"]), outstanding_count=t["outstanding_count"],
        avg_ticket=_f(t["tickets"] / t["paying"]) if t["paying"] else None,
        cost_per_head=_f(t["expenses"] / t["regs"]) if t["regs"] and t["expenses"] else None,
    )


def _event_row(e, m: dict, kind: str, now: datetime) -> dict:
    net = m["tickets"] + m["merch"] + m["other_income"] - m["expenses"]
    return dict(
        id=e.id, name=e.name, starts_at=_iso(e.event_datetime), type=kind, status=e.status, held=e.event_datetime <= now, has_fee=bool(e.has_fee), fee=_f(e.reg_fee),
        regs=m["regs"], paying=m["paying"], tickets=_f(m["tickets"]), merch=_f(m["merch"]), other_income=_f(m["other_income"]), online=_f(m["online"]), cash=_f(m["cash"]), refunds=_f(m["refunds"]),
        expenses=_f(m["expenses"]), expense_count=m["expense_count"], net=_f(net), margin=_margin({**m, "net": net}),
        outstanding=_f(m["outstanding"]), outstanding_count=m["outstanding_count"],
    )


def _attention(rows: list[dict]) -> dict:
    held = [r for r in rows if r["held"]]
    return dict(
        at_a_loss=[dict(id=r["id"], name=r["name"], net=r["net"], tickets=r["tickets"], expenses=r["expenses"]) for r in held if r["net"] < 0 and r["expenses"] > 0],
        owed=sorted((dict(id=r["id"], name=r["name"], outstanding=r["outstanding"], outstanding_count=r["outstanding_count"]) for r in held if r["outstanding"] > 0), key=lambda r: -r["outstanding"]),
        unlogged=[dict(id=r["id"], name=r["name"], tickets=r["tickets"]) for r in held if r["has_fee"] and r["tickets"] > 0 and r["expense_count"] == 0],
    )


def _ahead(rows: list[dict]) -> dict:
    coming = [r for r in rows if not r["held"]]
    return dict(
        events=len(coming), paid_events=sum(1 for r in coming if r["has_fee"]), regs=sum(r["regs"] for r in coming),
        tickets=_f(sum(Decimal(str(r["tickets"])) + Decimal(str(r["merch"])) + Decimal(str(r["other_income"])) for r in coming)), outstanding=_f(sum(Decimal(str(r["outstanding"])) for r in coming)),
        outstanding_count=sum(r["outstanding_count"] for r in coming), expenses=_f(sum(Decimal(str(r["expenses"])) for r in coming)),
    )


def _channels(t: dict) -> list[dict]:
    return [
        dict(key="online", label="Paid online", amount=_f(t["online"]), count=t["online_count"]),
        dict(key="cash", label="Cash at the door", amount=_f(t["cash"]), count=t["cash_count"]),
        dict(key="merch", label="Merchandise", amount=_f(t["merch"]), count=0),
        dict(key="other", label="Sponsorship & other", amount=_f(t["other_income"]), count=t["other_income_count"]),
    ]


def _deltas(cur: dict, prev: dict | None) -> dict:
    return {k: (None if prev is None else _pct(cur[k], prev[k])) for k in _MONEY}


def _month_rows(months: dict[str, dict]) -> list[dict]:
    return [dict(month=m, tickets=_f(r["tickets"]), merch=_f(r["merch"]), other=_f(r["other"]), refunds=_f(r["refunds"]), expenses=_f(r["expenses"]), net=_f(r["tickets"] + r["merch"] + r["other"] - r["expenses"])) for m, r in months.items()]


def _new_month(m: str) -> dict:
    return dict(tickets=Decimal(0), merch=Decimal(0), other=Decimal(0), refunds=Decimal(0), expenses=Decimal(0))


# ─── The club's season ───────────────────────────────────────────────────


async def _events_in(session, club_id: int, frm: date, to: date) -> list[Events]:
    start, end = _bounds(frm, to)
    rows = await session.scalars(
        select(Events)
        .where(Events.club_id == club_id, Events.is_deleted == False, Events.lifecycle.in_(_SHOWN), Events.event_datetime >= start, Events.event_datetime < end)  # noqa: E712
        .order_by(Events.event_datetime.desc(), Events.id.desc())
    )
    return list(rows.all())


async def _type_names(session, event_ids: list[int]) -> dict[int, str]:
    if not event_ids:
        return {}
    rows = await session.execute(select(Events.id, EventCategories.name).join(EventCategories, EventCategories.id == Events.category_id).where(Events.id.in_(event_ids)))
    return {eid: name for eid, name in rows.all()}


async def _overheads(session, club_id: int, frm: date, to: date) -> list[ClubExpenses]:
    rows = await session.scalars(
        select(ClubExpenses).where(ClubExpenses.club_id == club_id, ClubExpenses.event_id.is_(None), ClubExpenses.is_deleted == False, ClubExpenses.incurred_on >= frm, ClubExpenses.incurred_on <= to)  # noqa: E712
    )
    return list(rows.all())


async def _season(session, club_id: int, frm: date, to: date):
    events = await _events_in(session, club_id, frm, to)
    money = await _per_event(session, [e.id for e in events])
    overheads = await _overheads(session, club_id, frm, to)
    return events, money, overheads, _totals([money[e.id] for e in events], overheads)


async def club_summary(session, club, frm: date, to: date) -> dict:
    now = _now()
    events, money, overheads, cur = await _season(session, club.id, frm, to)
    pf, pt = _previous(frm, to)
    prev_events, _, _, prev = await _season(session, club.id, pf, pt)
    names = await _type_names(session, [e.id for e in events])

    conducted = sum(1 for e in events if e.event_datetime <= now)
    prev_conducted = sum(1 for e in prev_events if e.event_datetime <= now)
    upcoming = await session.scalar(
        select(func.count()).select_from(Events).where(Events.club_id == club.id, Events.is_deleted == False, Events.lifecycle.in_((EventStatus.published, EventStatus.live)), Events.event_datetime > now)  # noqa: E712
    )

    # An event's money sits in the event's month; an overhead in its own.
    months = {m: _new_month(m) for m in _months(frm, to)}
    types: dict[str, dict] = {}
    by_event = []
    for e in events:
        m = money[e.id]
        row = months.get(_month_of(e.event_datetime.astimezone(timezone.utc)))
        if row is not None:
            row["tickets"] += m["tickets"]
            row["merch"] += m["merch"]
            row["other"] += m["other_income"]
            row["refunds"] += m["refunds"]
            row["expenses"] += m["expenses"]
        kind = names.get(e.id, "Uncategorised")
        t = types.setdefault(kind, dict(type=kind, events=0, tickets=Decimal(0), merch=Decimal(0), other=Decimal(0), expenses=Decimal(0)))
        t["events"] += 1
        t["tickets"] += m["tickets"]
        t["merch"] += m["merch"]
        t["other"] += m["other_income"]
        t["expenses"] += m["expenses"]
        by_event.append(_event_row(e, m, kind, now))
    for x in overheads:
        row = months.get(_month_of(x.incurred_on))
        if row is not None:
            row["other" if x.kind == INCOME else "expenses"] += Decimal(x.amount)
    if overheads:
        types[CLUB_ROW] = dict(
            type=CLUB_ROW, events=0, tickets=Decimal(0), merch=Decimal(0),
            other=sum((Decimal(x.amount) for x in overheads if x.kind == INCOME), Decimal(0)), expenses=sum((Decimal(x.amount) for x in overheads if x.kind != INCOME), Decimal(0)),
        )
    by_type = sorted(types.values(), key=lambda t: (-t["tickets"], -t["events"], t["type"]))

    return dict(
        scope="club", period=dict(start=frm, end=to),
        figures=_figures(cur, dict(conducted=conducted, upcoming=upcoming or 0, delta=_pct(conducted, prev_conducted))),
        deltas=_deltas(cur, prev), by_month=_month_rows(months),
        by_type=[dict(type=t["type"], events=t["events"], tickets=_f(t["tickets"]), merch=_f(t["merch"]), other=_f(t["other"]), expenses=_f(t["expenses"]), net=_f(t["tickets"] + t["merch"] + t["other"] - t["expenses"])) for t in by_type],
        by_event=by_event, expenses=await _expense_rows(session, club.id, [e.id for e in events], frm, to),
        by_category=await _by_category(session, club.id, [e.id for e in events], frm, to), income_by_category=await _by_category(session, club.id, [e.id for e in events], frm, to, kind=INCOME), channels=_channels(cur),
        attention=_attention(by_event), ahead=_ahead(by_event),
        categories=list(EXPENSE_CATEGORIES), income_categories=list(INCOME_CATEGORIES), generated_at=_iso(now),
    )


# ─── One event ───────────────────────────────────────────────────────────


async def event_summary(session, event) -> dict:
    now = _now()
    money = (await _per_event(session, [event.id]))[event.id]
    cur = _totals([money])

    # Here the months say when the money moved, not when the event was.
    months: dict[str, dict] = {}
    paid = Reg.is_paid == True  # noqa: E712
    by_reg_month = utc_trunc("month", Reg.created_at)
    q = select(by_reg_month, func.sum(Reg.paid_amount)).where(Reg.event_id == event.id, Reg.is_deleted == False, paid).group_by(by_reg_month)  # noqa: E712
    for when, amount in (await session.execute(q)).all():
        months.setdefault(_month_of(when), _new_month(""))["tickets"] += Decimal(amount or 0)
    by_refund_month = utc_trunc("month", PaymentLogs.updated_at)
    q = (
        select(by_refund_month, func.sum(PaymentLogs.amount))
        .select_from(PaymentLogs)
        .join(PaymentOrders, PaymentOrders.id == PaymentLogs.order_id)
        .join(Reg, Reg.payment_receipt == PaymentOrders.receipt)
        .where(PaymentLogs.status == PaymentStatus.refunded, Reg.event_id == event.id)
        .group_by(by_refund_month)
    )
    for when, amount in (await session.execute(q)).all():
        months.setdefault(_month_of(when), _new_month(""))["refunds"] += Decimal(amount or 0)
    by_merch_month = utc_trunc("month", EventStoreOrders.paid_at)
    q = select(by_merch_month, func.sum(EventStoreOrders.total)).where(EventStoreOrders.event_id == event.id, EventStoreOrders.status == "paid", EventStoreOrders.paid_at.is_not(None)).group_by(by_merch_month)
    for when, amount in (await session.execute(q)).all():
        months.setdefault(_month_of(when), _new_month(""))["merch"] += Decimal(amount or 0)
    expenses = await _expense_rows(session, event.club_id, [event.id], None, None)
    for x in expenses:
        months.setdefault(x["incurred_on"][:7], _new_month(""))["other" if x["kind"] == INCOME else "expenses"] += Decimal(str(x["amount"]))
    keys = sorted(months) or [_month_of(event.event_datetime.astimezone(timezone.utc))]
    first, last = date.fromisoformat(keys[0] + "-01"), date.fromisoformat(keys[-1] + "-01")
    filled = {m: months.get(m, _new_month(m)) for m in _months(first, last)}

    row = _event_row(event, money, "", now)
    return dict(
        scope="event", period=dict(start=first, end=event.event_datetime.astimezone(timezone.utc).date()),
        figures=_figures(cur), deltas=_deltas(cur, None), by_month=_month_rows(filled), by_type=[], by_event=[],
        expenses=expenses, by_category=await _by_category(session, event.club_id, [event.id], None, None), income_by_category=await _by_category(session, event.club_id, [event.id], None, None, kind=INCOME), channels=_channels(cur),
        attention=_attention([row]), ahead=_ahead([row]),
        categories=list(EXPENSE_CATEGORIES), income_categories=list(INCOME_CATEGORIES), generated_at=_iso(now),
    )


# ─── Expenses ────────────────────────────────────────────────────────────


def _expense_out(x: ClubExpenses, event_name: str | None) -> dict:
    return dict(
        id=str(x.id), event_id=x.event_id, event_name=event_name, kind=x.kind or EXPENSE, title=x.title, category=x.category, amount=_f(x.amount),
        incurred_on=x.incurred_on.isoformat(), note=x.note, created_at=_iso(x.created_at),
    )


def _expense_conds(club_id: int, event_ids: list[int], frm: date | None, to: date | None) -> list | None:
    """The period's entries: the events' own, and the club's overheads dated inside it."""
    conds = [ClubExpenses.club_id == club_id, ClubExpenses.is_deleted == False]  # noqa: E712
    own = ClubExpenses.event_id.in_(event_ids) if event_ids else None
    if frm is not None and to is not None:
        overhead = and_(ClubExpenses.event_id.is_(None), ClubExpenses.incurred_on >= frm, ClubExpenses.incurred_on <= to)
        conds.append(or_(own, overhead) if own is not None else overhead)
    elif own is not None:
        conds.append(own)
    else:
        return None
    return conds


async def _by_category(session, club_id: int, event_ids: list[int], frm: date | None, to: date | None, kind: str = EXPENSE) -> list[dict]:
    """Where the money went — or, for income, where it came from: the period's entries by category, largest first."""
    conds = _expense_conds(club_id, event_ids, frm, to)
    if conds is None:
        return []
    q = select(ClubExpenses.category, func.coalesce(func.sum(ClubExpenses.amount), 0), func.count()).where(*conds, ClubExpenses.kind == kind).group_by(ClubExpenses.category)
    rows = [dict(category=c, amount=_f(a), count=n) for c, a, n in (await session.execute(q)).all()]
    return sorted(rows, key=lambda r: (-r["amount"], r["category"]))


async def _expense_rows(session, club_id: int, event_ids: list[int], frm: date | None, to: date | None) -> list[dict]:
    conds = _expense_conds(club_id, event_ids, frm, to)
    if conds is None:
        return []
    rows = list((await session.scalars(select(ClubExpenses).where(*conds).order_by(ClubExpenses.incurred_on.desc(), ClubExpenses.created_at.desc()).limit(400))).all())
    names = {}
    ids = sorted({x.event_id for x in rows if x.event_id is not None})
    if ids:
        names = {eid: name for eid, name in (await session.execute(select(Events.id, Events.name).where(Events.id.in_(ids)))).all()}
    return [_expense_out(x, names.get(x.event_id)) for x in rows]


async def _event_of_club(session, club_id: int, event_id: int) -> Events:
    event = await session.scalar(select(Events).where(Events.id == event_id, Events.club_id == club_id, Events.is_deleted == False))  # noqa: E712
    if event is None:
        raise CustomHTTPException(404, message="Event not found")
    return event


async def _event_name(session, event_id: int | None) -> str | None:
    return None if event_id is None else await session.scalar(select(Events.name).where(Events.id == event_id))


async def create_expense(session, club, body: ExpenseCreate, user) -> dict:
    event = await _event_of_club(session, club.id, body.event_id) if body.event_id is not None else None
    x = ClubExpenses(
        club_id=club.id, event_id=event.id if event else None, kind=body.kind, title=body.title, category=body.category, amount=body.amount,
        incurred_on=body.incurred_on or _now().date(), note=body.note, created_by=user.id,
    )
    session.add(x)
    await session.commit()
    await session.refresh(x)
    return _expense_out(x, event.name if event else None)


async def expense_for(session, club, expense_id: UUID) -> ClubExpenses:
    x = await session.scalar(select(ClubExpenses).where(ClubExpenses.id == expense_id, ClubExpenses.club_id == club.id, ClubExpenses.is_deleted == False))  # noqa: E712
    if x is None:
        raise CustomHTTPException(404, message="Expense not found")
    return x


async def update_expense(session, club, x: ClubExpenses, body: ExpensePatch) -> dict:
    data = body.model_dump(exclude_unset=True)
    if "event_id" in data:
        x.event_id = (await _event_of_club(session, club.id, data["event_id"])).id if data["event_id"] is not None else None
    kind = data.get("kind") or x.kind or EXPENSE
    # The category must fit the entry's kind — the one it had, or the one it is becoming.
    category = data.get("category") or (x.category if kind == (x.kind or EXPENSE) else "other")
    if category not in CATEGORIES[kind]:
        raise CustomHTTPException(422, message=f"An {kind}'s category must be one of: {', '.join(CATEGORIES[kind])}")
    x.kind, x.category = kind, category
    data.pop("category", None)
    for k in ("title", "amount", "incurred_on", "note"):
        if k in data and data[k] is not None or (k == "note" and k in data):
            setattr(x, k, data[k])
    await session.commit()
    await session.refresh(x)
    return _expense_out(x, await _event_name(session, x.event_id))


async def delete_expense(session, x: ClubExpenses) -> None:
    x.is_deleted = True
    x.deleted_at = _now()
    await session.commit()


# ─── Statements ──────────────────────────────────────────────────────────


def _cell(v):
    """Spreadsheets run a cell that starts like a formula; a name never should."""
    return f"'{v}" if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v


def _writer():
    buf = io.StringIO()
    return buf, csv.writer(buf, lineterminator="\n")


def _totals_section(w, f: dict) -> None:
    w.writerow([])
    w.writerow(["Totals"])
    for key, label in (("gross", "Gross income"), ("refunds", "Refunds"), ("tickets", "Tickets held"), ("merch", "Merchandise"), ("other_income", "Other income"), ("expenses", "Expenses"), ("net", "Net surplus"), ("outstanding", "Outstanding")):
        w.writerow([label, f[key]])


def _expenses_section(w, rows: list[dict]) -> None:
    for kind, heading in ((EXPENSE, "Expenses"), (INCOME, "Other income")):
        mine = [x for x in rows if (x["kind"] or EXPENSE) == kind]
        if kind == INCOME and not mine:
            continue
        w.writerow([])
        w.writerow([heading])
        w.writerow(["Date", "Event", "Title", "Category", "Amount", "Note"])
        for x in mine:
            w.writerow([x["incurred_on"], _cell(x["event_name"] or CLUB_ROW), _cell(x["title"]), x["category"], x["amount"], _cell(x["note"] or "")])


async def club_statement(session, club, frm: date, to: date) -> str:
    s = await club_summary(session, club, frm, to)
    buf, w = _writer()
    w.writerow(["Statement", _cell(club.name), f"{frm.isoformat()} to {to.isoformat()}"])
    w.writerow([])
    w.writerow(["Events"])
    w.writerow(["Date", "Event", "Type", "Status", "Registrations", "Paying", "Tickets", "Merchandise", "Refunds", "Expenses", "Net"])
    for r in s["by_event"]:
        w.writerow([r["starts_at"][:10], _cell(r["name"]), _cell(r["type"]), r["status"], r["regs"], r["paying"], r["tickets"], r["merch"], r["refunds"], r["expenses"], r["net"]])
    _expenses_section(w, s["expenses"])
    _totals_section(w, s["figures"])
    return "﻿" + buf.getvalue()


#: Registrations read per round trip while a statement streams.
STATEMENT_CHUNK = 1000


async def event_statement(session, event) -> AsyncIterator[str]:
    """The event's statement as CSV, a chunk at a time: every registration
    with whether it was refunded (a join, where an IN over every receipt
    failed past asyncpg's 32,767 parameters), read through a server-side
    cursor STATEMENT_CHUNK rows at a time, so neither the registrations nor
    the file are ever all in memory."""
    s = await event_summary(session, event)
    buf, w = _writer()

    def drain() -> str:
        chunk = buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        return chunk

    w.writerow(["Statement", _cell(event.name), _iso(event.event_datetime)[:10]])
    w.writerow([])
    w.writerow(["Registrations"])
    w.writerow(["Date", "Guest", "Email", "Ticket", "Fee", "Paid", "Status"])
    yield "﻿" + drain()

    refunded = _refunded_receipts()
    rows = await session.stream(
        select(
            Reg.created_at, Reg.full_name, Reg.email, Reg.ticket_id, Reg.actual_amount, Reg.paid_amount, Reg.is_paid,
            refunded.c.receipt.is_not(None),
        )
        .outerjoin(refunded, refunded.c.receipt == Reg.payment_receipt)
        .where(Reg.event_id == event.id, Reg.is_deleted == False)  # noqa: E712
        .order_by(Reg.created_at, Reg.id)
        .execution_options(yield_per=STATEMENT_CHUNK)
    )
    async for part in rows.partitions(STATEMENT_CHUNK):
        for created_at, name, email, ticket, fee, paid_amount, is_paid, was_refunded in part:
            status = "refunded" if was_refunded else "paid" if is_paid else "unpaid" if event.has_fee else "free"
            w.writerow([_iso(created_at)[:10], _cell(name), email, ticket, _f(fee), _f(paid_amount), status])
        yield drain()
    _expenses_section(w, s["expenses"])
    _totals_section(w, s["figures"])
    yield drain()
