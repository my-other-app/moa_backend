"""What MyOtherApp owes each club, and what it has paid.

A club without its own Razorpay account sells its tickets and store pieces
through MyOtherApp's: the money lands in the platform's account and is owed
to the club. Per payment:

    gross        what Razorpay captured (the ticket and the store picks on it)
    fee          Razorpay's fee on it, GST included -- read from Razorpay
                 (`payment_fees`, filled by "Fetch fees"), else estimated at
                 ESTIMATED_FEE_RATE and marked so
    refunded     the whole payment when it was refunded in full, else the
                 parts given back by an admin (`payment_refunds`) or by the
                 club itself (`club_refunds`)
    chargeback   a dispute the platform lost on it
    net          gross - fee - refunded - chargeback

A club's balance is its payments' net less the payouts recorded
(`club_payouts`), which may be partial. A payout is voided, never deleted.

Read from the payments themselves (payment_logs, their orders and the
registrations they paid for), the same rows the finance page reads with
LEDGER_READS off -- so the figures are there whether or not the money
ledger has been backfilled. A payment taken through a club's own account
(`payment_gateway_id`) is the club's already and never counts here.

Reading is for every admin; recording or voiding a payout, and fetching the
fees, needs MANAGE_MONEY and is in the admin audit log.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import Numeric, case, cast, func, literal, select, union_all
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.admin_ops.models import ClubPayouts, PaymentDisputes, PaymentFees, PaymentRefunds
from app.api.audit import service as audit
from app.api.clubs.models import Clubs
from app.api.events.models import EventRegistrationsLink as Reg, Events
from app.api.payments.models import PaymentLogs, PaymentOrders, PaymentStatus
from app.api.users.models import Users
from app.core import jobs, money
from app.core.auth.dependencies import AdminAuth
from app.core.auth.permissions import MoneyAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/payouts", tags=["admin-payouts"])

#: Razorpay's standard 2% plus 18% GST, for a payment whose fee is not read yet.
ESTIMATED_FEE_RATE = Decimal("0.0236")
#: Payments a statement lists inline; the CSV has them all.
STATEMENT_ROWS = 500
#: Payments one fee fetch asks Razorpay about.
FEE_BATCH = 200
#: Statuses that are money taken.
TAKEN = (PaymentStatus.captured, PaymentStatus.refunded, PaymentStatus.disputed)
CENT = Decimal("0.01")
_DAY = timedelta(days=1)


def _q(x) -> Decimal:
    return Decimal(x or 0).quantize(CENT, rounding=ROUND_HALF_UP)


def _f(x) -> float:
    return float(_q(x))


def _iso(at) -> Optional[str]:
    if at is None:
        return None
    if isinstance(at, datetime):
        return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).isoformat()
    return at.isoformat()


# ─── The payments, one row each ──────────────────────────────────────────


def _receipts():
    """Each receipt's event: the registration it paid for (deleted ones too:
    the money was still taken)."""
    return (
        select(Reg.payment_receipt.label("receipt"), func.min(Reg.event_id).label("event_id"))
        .where(Reg.payment_receipt.isnot(None))
        .group_by(Reg.payment_receipt)
        .subquery("receipts")
    )


def _partials():
    """Parts of each payment given back: an admin's (payment_refunds) and the
    club's own that went through (club_refunds)."""
    from app.api.club_ops.models import SUCCEEDED, ClubRefunds

    parts = union_all(
        select(PaymentRefunds.payment_log_id.label("log_id"), PaymentRefunds.amount.label("amount")),
        select(ClubRefunds.payment_log_id.label("log_id"), ClubRefunds.amount.label("amount")).where(ClubRefunds.status == SUCCEEDED),
    ).subquery("parts")
    return (
        select(parts.c.log_id, func.sum(parts.c.amount).label("total"))
        .group_by(parts.c.log_id)
        .subquery("partials")
    )


def _lost():
    return (
        select(PaymentDisputes.razorpay_payment_id.label("pid"), func.sum(PaymentDisputes.amount_paise).label("paise"))
        .where(PaymentDisputes.status == "lost", PaymentDisputes.razorpay_payment_id.isnot(None))
        .group_by(PaymentDisputes.razorpay_payment_id)
        .subquery("lost")
    )


def payments_query(club_id: Optional[int] = None, *, frm: Optional[date] = None, to: Optional[date] = None):
    """One row per payment taken on the platform's account for a club's
    event: its figures, as columns (see the module's notes)."""
    receipts, partials, lost = _receipts(), _partials(), _lost()
    amount = PaymentLogs.amount
    fee_known = PaymentFees.fee_paise.isnot(None)
    fee = case(
        (fee_known, cast(PaymentFees.fee_paise, Numeric(14, 2)) / 100),
        else_=func.round(amount * literal(ESTIMATED_FEE_RATE, Numeric(6, 4)), 2),
    )
    refunded = case((PaymentLogs.status == PaymentStatus.refunded, amount), else_=func.coalesce(partials.c.total, 0))
    chargeback = cast(func.coalesce(lost.c.paise, 0), Numeric(14, 2)) / 100
    q = (
        select(
            PaymentLogs.id.label("log_id"),
            PaymentLogs.razorpay_payment_id.label("razorpay_payment_id"),
            PaymentLogs.status.label("status"),
            PaymentLogs.created_at.label("paid_at"),
            PaymentLogs.updated_at.label("updated_at"),
            amount.label("gross"),
            fee.label("fee"),
            fee_known.label("fee_known"),
            refunded.label("refunded"),
            chargeback.label("chargeback"),
            Events.id.label("event_id"),
            Events.name.label("event_name"),
            Events.club_id.label("club_id"),
        )
        .join(PaymentOrders, PaymentOrders.id == PaymentLogs.order_id)
        .join(receipts, receipts.c.receipt == PaymentOrders.receipt)
        .join(Events, Events.id == receipts.c.event_id)
        .outerjoin(PaymentFees, PaymentFees.razorpay_payment_id == PaymentLogs.razorpay_payment_id)
        .outerjoin(partials, partials.c.log_id == PaymentLogs.id)
        .outerjoin(lost, lost.c.pid == PaymentLogs.razorpay_payment_id)
        .where(
            PaymentOrders.payment_gateway_id.is_(None),
            PaymentLogs.status.in_(TAKEN),
            Events.club_id.isnot(None),
        )
        .execution_options(include_deleted=True)
    )
    if club_id is not None:
        q = q.where(Events.club_id == club_id)
    if frm is not None:
        q = q.where(PaymentLogs.created_at >= datetime(frm.year, frm.month, frm.day, tzinfo=timezone.utc))
    if to is not None:
        q = q.where(PaymentLogs.created_at < datetime(to.year, to.month, to.day, tzinfo=timezone.utc) + _DAY)
    return q


def _blank() -> dict:
    return {"gross": Decimal(0), "fees": Decimal(0), "refunds": Decimal(0), "chargebacks": Decimal(0), "net": Decimal(0),
            "payments": 0, "fees_estimated": 0, "paid_out": Decimal(0), "outstanding": Decimal(0), "payouts": 0,
            "last_payout_on": None}


async def club_totals(session, club_ids: Optional[list[int]] = None) -> dict[int, dict]:
    """Each club's figures, all time: what was taken, kept back and paid out."""
    pp = payments_query().subquery("pp")
    q = (
        select(
            pp.c.club_id,
            func.count().label("payments"),
            func.coalesce(func.sum(pp.c.gross), 0),
            func.coalesce(func.sum(pp.c.fee), 0),
            func.count().filter(pp.c.fee_known.is_(False)),
            func.coalesce(func.sum(pp.c.refunded), 0),
            func.coalesce(func.sum(pp.c.chargeback), 0),
        )
        .group_by(pp.c.club_id)
    )
    if club_ids is not None:
        q = q.where(pp.c.club_id.in_(club_ids))
    out: dict[int, dict] = {}
    for club_id, n, gross, fees, estimated, refunds, chargebacks in (await session.execute(q)).all():
        row = out.setdefault(club_id, _blank())
        row.update(payments=int(n), gross=_q(gross), fees=_q(fees), fees_estimated=int(estimated or 0),
                   refunds=_q(refunds), chargebacks=_q(chargebacks))
    pq = (
        select(ClubPayouts.club_id, func.coalesce(func.sum(ClubPayouts.amount), 0), func.count(), func.max(ClubPayouts.paid_on))
        .where(ClubPayouts.voided_at.is_(None))
        .group_by(ClubPayouts.club_id)
    )
    if club_ids is not None:
        pq = pq.where(ClubPayouts.club_id.in_(club_ids))
    for club_id, paid, n, last in (await session.execute(pq)).all():
        row = out.setdefault(club_id, _blank())
        row.update(paid_out=_q(paid), payouts=int(n), last_payout_on=last)
    for row in out.values():
        row["net"] = _q(row["gross"] - row["fees"] - row["refunds"] - row["chargebacks"])
        row["outstanding"] = _q(row["net"] - row["paid_out"])
    return out


def _figures(row: dict) -> dict:
    return {
        "gross": _f(row["gross"]), "fees": _f(row["fees"]), "fees_estimated": row["fees_estimated"],
        "refunds": _f(row["refunds"]), "chargebacks": _f(row["chargebacks"]), "net": _f(row["net"]),
        "paid_out": _f(row["paid_out"]), "outstanding": _f(row["outstanding"]), "payments": row["payments"],
        "payouts": row["payouts"], "last_payout_on": _iso(row["last_payout_on"]),
    }


async def _club(session, club_id: int) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.id == club_id).execution_options(include_deleted=True))
    if club is None:
        raise CustomHTTPException(404, message="No such club", error_code="CLUB_NOT_FOUND")
    return club


def _club_out(club: Clubs) -> dict:
    return {"id": club.id, "name": club.name, "slug": club.slug, "is_deleted": bool(club.is_deleted)}


# ─── Reading ─────────────────────────────────────────────────────────────


@router.get("", summary="What MyOtherApp owes each club for sales on its Razorpay account (admin)")
async def payouts_overview(
    session: SessionDep,
    user: AdminAuth,
    q: Optional[str] = Query(None, max_length=100, description="Part of the club's name"),
    owing: bool = Query(False, description="Only clubs with something outstanding"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    totals = await club_totals(session)
    ids = list(totals)
    clubs = {}
    if ids:
        cq = select(Clubs).where(Clubs.id.in_(ids)).execution_options(include_deleted=True)
        if q and q.strip():
            cq = cq.where(Clubs.name.ilike(f"%{q.strip()}%"))
        clubs = {c.id: c for c in (await session.scalars(cq)).all()}
    rows = [(clubs[i], totals[i]) for i in ids if i in clubs]
    if owing:
        rows = [(c, t) for c, t in rows if t["outstanding"] > 0]
    rows.sort(key=lambda ct: (-ct[1]["outstanding"], ct[0].name.lower()))
    sums = _blank()
    for _, t in rows:
        for k in ("gross", "fees", "refunds", "chargebacks", "net", "paid_out", "outstanding"):
            sums[k] += t[k]
        sums["payments"] += t["payments"]
        sums["fees_estimated"] += t["fees_estimated"]
        sums["payouts"] += t["payouts"]
    page = rows[offset:offset + limit]
    return {
        "items": [{"club": _club_out(c), **_figures(t)} for c, t in page],
        "total": len(rows),
        "totals": {k: v for k, v in _figures(sums).items() if k != "last_payout_on"},
        "owing_clubs": sum(1 for _, t in rows if t["outstanding"] > 0),
        "fee_rate_estimate": float(ESTIMATED_FEE_RATE),
        "offset": offset,
        "limit": limit,
    }


async def _payouts(session, club_id: int, *, include_void: bool = True) -> list[ClubPayouts]:
    q = select(ClubPayouts).where(ClubPayouts.club_id == club_id)
    if not include_void:
        q = q.where(ClubPayouts.voided_at.is_(None))
    return list((await session.scalars(q.order_by(ClubPayouts.paid_on.desc(), ClubPayouts.id.desc()))).all())


async def _names(session, ids: set) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = (await session.execute(select(Users.id, Users.full_name).where(Users.id.in_(ids)).execution_options(include_deleted=True))).all()
    return dict(rows)


def _payout_out(p: ClubPayouts, names: dict[int, str]) -> dict:
    return {
        "id": p.id, "amount": _f(p.amount), "paid_on": _iso(p.paid_on), "reference": p.reference, "note": p.note,
        "created_at": _iso(p.created_at), "created_by": {"id": p.created_by, "name": names.get(p.created_by)} if p.created_by else None,
        "voided": p.voided_at is not None, "voided_at": _iso(p.voided_at), "void_reason": p.void_reason,
        "voided_by": {"id": p.voided_by, "name": names.get(p.voided_by)} if p.voided_by else None,
    }


def _payment_out(r) -> dict:
    gross, fee, refunded, chargeback = _q(r.gross), _q(r.fee), _q(r.refunded), _q(r.chargeback)
    return {
        "payment_log_id": str(r.log_id), "razorpay_payment_id": r.razorpay_payment_id,
        "status": getattr(r.status, "value", r.status), "paid_at": _iso(r.paid_at),
        "event": {"id": r.event_id, "name": r.event_name},
        "gross": _f(gross), "fee": _f(fee), "fee_estimated": not r.fee_known, "refunded": _f(refunded),
        "chargeback": _f(chargeback), "net": _f(gross - fee - refunded - chargeback),
    }


@router.get("/clubs/{club_id}", summary="One club's statement: its payments, payouts and balance (admin)")
async def club_statement(
    club_id: int, session: SessionDep, user: AdminAuth,
    frm: Optional[date] = Query(None, alias="from"), to: Optional[date] = Query(None),
) -> dict:
    if frm and to and frm > to:
        raise CustomHTTPException(422, message="The period ends before it starts")
    club = await _club(session, club_id)
    totals = (await club_totals(session, [club_id])).get(club_id, _blank())
    q = payments_query(club_id, frm=frm, to=to)
    count = int(await session.scalar(select(func.count()).select_from(q.subquery())) or 0)
    rows = (await session.execute(q.order_by(PaymentLogs.created_at.desc()).limit(STATEMENT_ROWS))).all()
    payouts = await _payouts(session, club_id)
    if frm or to:
        payouts = [p for p in payouts if (frm is None or p.paid_on >= frm) and (to is None or p.paid_on <= to)]
    names = await _names(session, {p.created_by for p in payouts} | {p.voided_by for p in payouts})
    return {
        "club": _club_out(club),
        "summary": _figures(totals),
        "period": {"from": _iso(frm), "to": _iso(to)},
        "payments": [_payment_out(r) for r in rows],
        "payments_total": count,
        "payouts": [_payout_out(p, names) for p in payouts],
        "fee_rate_estimate": float(ESTIMATED_FEE_RATE),
    }


def _csv(rows: list[list]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


@router.get("/clubs/{club_id}/statement.csv", summary="One club's statement as a CSV file (admin)")
async def club_statement_csv(
    club_id: int, session: SessionDep, user: AdminAuth,
    frm: Optional[date] = Query(None, alias="from"), to: Optional[date] = Query(None),
) -> Response:
    """Every line that moved the club's balance, oldest first, with the
    running balance: a payment's gross in, its fee, a refund, a chargeback
    and a payout out. No guest's name or address: payment ids only."""
    club = await _club(session, club_id)
    lines: list[tuple] = []
    for r in (await session.execute(payments_query(club_id, frm=frm, to=to))).all():
        ref = r.razorpay_payment_id
        lines.append((r.paid_at, "payment", r.event_name, ref, _q(r.gross), ""))
        lines.append((r.paid_at, "fee (estimated)" if not r.fee_known else "fee", r.event_name, ref, -_q(r.fee), ""))
        if _q(r.refunded):
            lines.append((r.updated_at or r.paid_at, "refund", r.event_name, ref, -_q(r.refunded), ""))
        if _q(r.chargeback):
            lines.append((r.updated_at or r.paid_at, "chargeback", r.event_name, ref, -_q(r.chargeback), ""))
    for p in await _payouts(session, club_id, include_void=False):
        if (frm is None or p.paid_on >= frm) and (to is None or p.paid_on <= to):
            at = datetime(p.paid_on.year, p.paid_on.month, p.paid_on.day, 23, 59, tzinfo=timezone.utc)
            lines.append((at, "payout", "", p.reference, -_q(p.amount), p.note or ""))

    def when(x):
        at = x[0]
        return at if at.tzinfo else at.replace(tzinfo=timezone.utc)

    lines.sort(key=when)
    out = [["date", "type", "event", "reference", "amount", "balance", "note"]]
    balance = Decimal(0)
    for at, kind, event, ref, amount, note in lines:
        balance += amount
        out.append([when((at,)).date().isoformat(), kind, event or "", ref or "", f"{amount:.2f}", f"{balance:.2f}", note])
    name = f"{club.slug or club.id}-payouts{('-' + frm.isoformat()) if frm else ''}{('-' + to.isoformat()) if to else ''}.csv"
    return Response(content=_csv(out), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


# ─── Recording a payout ──────────────────────────────────────────────────


class PayoutIn(BaseModel):
    amount: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    paid_on: date
    reference: str = Field(..., min_length=1, max_length=120)
    note: Optional[str] = Field(None, max_length=500)
    #: Record it even though it is more than the balance shows.
    force: bool = False


class VoidIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=300)


@router.post("/clubs/{club_id}", summary="Record a payout to a club, in full or part (money)", status_code=201)
async def record_payout(club_id: int, body: PayoutIn, session: SessionDep, user: MoneyAuth, request: Request) -> dict:
    club = await _club(session, club_id)
    if body.paid_on > datetime.now(timezone.utc).date():
        raise CustomHTTPException(422, message="A payout can't be dated in the future", error_code="PAYOUT_IN_FUTURE")
    totals = (await club_totals(session, [club_id])).get(club_id, _blank())
    amount = _q(body.amount)
    if amount > totals["outstanding"] and not body.force:
        raise CustomHTTPException(
            409, message=f"That is more than the ₹{totals['outstanding']} outstanding. Record it anyway to go ahead.",
            error_code="PAYOUT_EXCEEDS_BALANCE",
        )
    row = ClubPayouts(club_id=club_id, amount=amount, paid_on=body.paid_on, reference=body.reference.strip(),
                      note=(body.note or "").strip() or None, created_by=user.id)
    session.add(row)
    await session.flush()
    await audit.record(
        session, actor=user, action="payout.record", entity_type="club", entity_id=club_id,
        summary=f"Recorded a payout of ₹{amount} to {club.name}"[:255],
        details={"payout_id": row.id, "amount": str(amount), "paid_on": body.paid_on.isoformat(),
                 "reference": row.reference, "outstanding_before": str(totals["outstanding"]), "forced": bool(body.force)},
        request=request,
    )
    await session.commit()
    names = await _names(session, {user.id})
    return _payout_out(row, names)


@router.post("/{payout_id}/void", summary="Void a payout recorded by mistake (money)")
async def void_payout(payout_id: int, body: VoidIn, session: SessionDep, user: MoneyAuth, request: Request) -> dict:
    row = await session.scalar(select(ClubPayouts).where(ClubPayouts.id == payout_id).with_for_update())
    if row is None:
        raise CustomHTTPException(404, message="No such payout", error_code="PAYOUT_NOT_FOUND")
    if row.voided_at is not None:
        raise CustomHTTPException(409, message="That payout is already void", error_code="PAYOUT_VOID")
    row.voided_at = datetime.now(timezone.utc)
    row.voided_by = user.id
    row.void_reason = body.reason.strip()
    await audit.record(
        session, actor=user, action="payout.void", entity_type="club", entity_id=row.club_id,
        summary=f"Voided a payout of ₹{_q(row.amount)} ({row.reference})"[:255],
        details={"payout_id": row.id, "amount": str(_q(row.amount)), "reason": row.void_reason}, request=request,
    )
    await session.commit()
    names = await _names(session, {row.created_by, user.id})
    return _payout_out(row, names)


# ─── Razorpay's fees ─────────────────────────────────────────────────────


def _missing_fees_query(limit: int):
    return (
        payments_query()
        .where(PaymentFees.razorpay_payment_id.is_(None))
        .order_by(PaymentLogs.created_at.desc())
        .limit(limit)
    )


@router.post("/fees/fetch", summary="Read Razorpay's fee for payments that don't have it yet (money)")
async def fetch_fees(session: SessionDep, user: MoneyAuth, request: Request) -> dict:
    """A job asks Razorpay about up to FEE_BATCH payments (newest first) and
    keeps what it says; press again for the next batch. Read-only at Razorpay."""
    missing = int(await session.scalar(
        select(func.count()).select_from(payments_query().where(PaymentFees.razorpay_payment_id.is_(None)).subquery())
    ) or 0)
    if missing:
        await jobs.enqueue(session, "admin_ops.fetch_fees", {"limit": FEE_BATCH}, dedupe_key="admin_ops:fetch_fees")
    await audit.record(session, actor=user, action="payout.fetch_fees", entity_type="payment",
                       summary=f"Asked Razorpay for the fees on {min(missing, FEE_BATCH)} payment(s)", request=request)
    await session.commit()
    return {"missing": missing, "queued": min(missing, FEE_BATCH)}


@jobs.handler("admin_ops.fetch_fees", max_attempts=3, timeout=600.0, priority=jobs.LOW)
async def _fetch_fees_job(p: dict) -> None:
    async with jobs.scope() as s:
        written = await fetch_fee_batch(s, limit=int(p.get("limit") or FEE_BATCH))
        logger.info("payouts: %d Razorpay fee(s) recorded", written)


async def fetch_fee_batch(session, *, limit: int = FEE_BATCH) -> int:
    """Asks Razorpay (the platform's account) for each payment's fee, with no
    transaction open while it answers; a payment it won't answer for is left
    for the next batch."""
    from app.api.payments import rzp
    from app.api.payments import service as payments

    ids = [r.razorpay_payment_id for r in (await session.execute(_missing_fees_query(limit))).all()]
    await session.commit()
    found: list[dict] = []
    for pid in ids:
        try:
            fetched = await rzp.call(payments.razorpay_client.payment.fetch, pid) or {}
        except Exception:  # noqa: BLE001 - the next batch asks again
            logger.warning("payouts: Razorpay would not say the fee on %s", pid, exc_info=True)
            continue
        if fetched.get("fee") is None:
            continue
        found.append({"razorpay_payment_id": pid, "fee_paise": int(fetched.get("fee") or 0),
                      "tax_paise": int(fetched.get("tax") or 0), "fetched_at": datetime.now(timezone.utc)})
    if found:
        await session.execute(pg_insert(PaymentFees.__table__).values(found).on_conflict_do_nothing(index_elements=["razorpay_payment_id"]))
    await session.commit()
    return len(found)


