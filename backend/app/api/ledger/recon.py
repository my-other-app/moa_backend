"""Each day, Razorpay's word against the ledger's: the payments and refunds of
the day before (an IST day), for the platform's account and every club's own
(club_payment_gateways), one account at a time.

What is ours on an account: a payment on an order we made on it
(payment_orders, by the account), and on the platform's, a payment for a
club's subscription (it carries an invoice or a subscription id, or the
ledger has it as a charge). Anything else on a club's account is the club's
own business and is left alone; on the platform's it is counted, not judged.

The payments judged are those Razorpay lists for the day, the ones the
ledger moved money on that day (fetched one by one when the listing does
not have them: created the day before, captured or refunded on this one),
and those Razorpay refunded that day. Each, against what the ledger took in
(captures, subscription charges) and gave back (refunds):

    missing_in_ledger           Razorpay captured a payment of ours the ledger never took in
    amount                      the ledger took it in at another amount
    not_captured                the ledger took in a payment Razorpay has not captured
    missing_at_razorpay         the ledger took in a payment the account does not have
    refund_missing_in_ledger    Razorpay refunded more of it than the ledger gave back
                                (a refund made in Razorpay's dashboard that never reached
                                us, or a partial one, which the app does not apply to a ticket)
    refund_missing_at_razorpay  the ledger gave back more than Razorpay refunded

An account's mismatches for the day are one admin alert (`ledger_mismatch`)
with every one in its data. Razorpay is only read (listed and fetched), never
asked to do anything, and nothing is held open while it answers. The suite
never reaches it: tests hand in fake clients.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, or_, select

from app.api.ledger import book
from app.api.ledger.models import LedgerEntries as E

logger = logging.getLogger(__name__)

#: Razorpay's largest page.
PAGE = 100
#: Pages of one listing, at most (100 pages = 10,000 payments a day an account).
MAX_PAGES = 100
#: Between pages and between fetches: Razorpay's rate limits.
PAUSE = 0.3
#: Payments fetched one by one, at most, an account a day.
MAX_FETCH = 300
#: Mismatches kept in an alert's data.
KEEP = 100

IN_KINDS = (book.CAPTURE, book.SUB_CHARGE)
OUT_KINDS = (book.REFUND, book.SUB_REFUND)


@dataclass
class Account:
    label: str
    gateway: Any = None
    client: Any = None
    error: Optional[str] = None

    @property
    def gateway_id(self) -> Optional[int]:
        return self.gateway.id if self.gateway is not None else None

    @property
    def club_id(self) -> Optional[int]:
        return self.gateway.club_id if self.gateway is not None else None


@dataclass
class Found:
    payments: int = 0
    refunds: int = 0
    judged: int = 0
    other: int = 0
    unchecked: int = 0
    mismatches: list = field(default_factory=list)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=book.IST)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


async def _call(fn, *args):
    from app.api.payments import rzp

    return await rzp.call(fn, *args)


async def _listing(fn, start: datetime, end: datetime, *, pause: float) -> tuple[list[dict], bool]:
    """Every item of a Razorpay listing created in [start, end), page by page.
    The flag says it stopped at MAX_PAGES."""
    out: list[dict] = []
    skip = 0
    for page in range(MAX_PAGES):
        if page and pause:
            await asyncio.sleep(pause)
        got = await _call(fn, {"from": int(start.timestamp()), "to": int(end.timestamp()) - 1, "count": PAGE, "skip": skip}) or {}
        items = got.get("items") or []
        out.extend(items)
        if len(items) < PAGE:
            return out, False
        skip += PAGE
    return out, True


async def accounts(session, start: datetime) -> list[Account]:
    """The platform's account, and every club account that may have moved
    money on the day: live, ended lately, or named by the ledger then."""
    from app.api.payment_gateways import crypto
    from app.api.payment_gateways import service as gateways
    from app.api.payment_gateways.models import LIVE, ClubPaymentGateways

    out = [Account("platform", None, gateways.platform().client)]
    named = select(E.gateway_id).where(E.account == book.GATEWAY, E.gateway_id.isnot(None),
                                       E.occurred_at >= start - timedelta(days=1))
    rows = (await session.scalars(
        select(ClubPaymentGateways).where(or_(
            ClubPaymentGateways.status.in_(LIVE),
            ClubPaymentGateways.ended_at >= start - timedelta(days=30),
            ClubPaymentGateways.id.in_(named),
        )).order_by(ClubPaymentGateways.id)
    )).all()
    for gateway in rows:
        label = f"club:{gateway.club_id}:gateway:{gateway.id}"
        try:
            out.append(Account(label, gateway, gateways.client_for(gateway)))
        except crypto.Unavailable as exc:
            out.append(Account(label, gateway, None, error=f"keys cannot be read: {exc}"))
    return out


async def _ledger_sums(session, payment_ids: list[str]) -> dict[str, dict]:
    """What the ledger took in and gave back on each payment, in paise."""
    out: dict[str, dict] = {}
    ids = sorted(set(payment_ids))
    for i in range(0, len(ids), 1000):
        for pid, kind, paise in (await session.execute(
            select(E.razorpay_payment_id, E.kind, func.sum(E.amount_paise))
            .where(E.account == book.GATEWAY, E.razorpay_payment_id.in_(ids[i:i + 1000]))
            .group_by(E.razorpay_payment_id, E.kind)
        )).all():
            row = out.setdefault(pid, {"in": 0, "out": 0, "kinds": set()})
            row["kinds"].add(kind)
            if kind in IN_KINDS:
                row["in"] += int(paise)
            elif kind in OUT_KINDS:
                row["out"] += -int(paise)
    return out


async def _in_window(session, account: Account, start: datetime, end: datetime) -> list[str]:
    """Payments the ledger moved money on in the window, on this account."""
    gateway = E.gateway_id.is_(None) if account.gateway is None else E.gateway_id == account.gateway_id
    return [pid for (pid,) in (await session.execute(
        select(E.razorpay_payment_id).where(
            E.account == book.GATEWAY, E.occurred_at >= start, E.occurred_at < end, gateway,
            E.razorpay_payment_id.isnot(None),
        ).distinct()
    )).all()]


async def _our_orders(session, account: Account, order_ids: list[str]) -> set[str]:
    from app.api.payments.models import PaymentOrders

    ids = sorted({o for o in order_ids if o})
    if not ids:
        return set()
    same = PaymentOrders.payment_gateway_id.is_(None) if account.gateway is None else PaymentOrders.payment_gateway_id == account.gateway_id
    out: set[str] = set()
    for i in range(0, len(ids), 1000):
        out |= set((await session.scalars(
            select(PaymentOrders.razorpay_order_id).where(PaymentOrders.razorpay_order_id.in_(ids[i:i + 1000]), same)
        )).all())
    return out


def _judge(account: Account, payment: dict, ledger: Optional[dict], found: Found) -> None:
    pid = payment.get("id")
    amount = int(payment.get("amount") or 0)
    refunded = int(payment.get("amount_refunded") or 0)
    status = payment.get("status")
    taken = (ledger or {}).get("in", 0)
    given = (ledger or {}).get("out", 0)
    has_in = bool(ledger) and bool((ledger.get("kinds") or set()) & set(IN_KINDS))
    found.judged += 1

    def mismatch(kind: str, **detail) -> None:
        found.mismatches.append({"kind": kind, "payment_id": pid, "order_id": payment.get("order_id"),
                                 "razorpay_status": status, **detail})

    if status in ("captured", "refunded"):
        if not has_in:
            mismatch("missing_in_ledger", razorpay_paise=amount)
        elif taken != amount:
            mismatch("amount", razorpay_paise=amount, ledger_paise=taken)
    elif has_in and taken:
        mismatch("not_captured", ledger_paise=taken)
    if refunded > given:
        mismatch("refund_missing_in_ledger", razorpay_refunded_paise=refunded, ledger_refunded_paise=given,
                 partial=refunded < amount)
    elif given > refunded:
        mismatch("refund_missing_at_razorpay", razorpay_refunded_paise=refunded, ledger_refunded_paise=given)


async def check_account(session, account: Account, day: date, *, pause: float = PAUSE,
                        deadline: Optional[float] = None) -> Found:
    """One account's day: listed at Razorpay, looked up in the ledger, judged."""
    from razorpay.errors import BadRequestError

    start, end = day_bounds(day)
    await session.commit()  # nothing held while Razorpay answers
    payments, cut_p = await _listing(account.client.payment.all, start, end, pause=pause)
    refunds, cut_r = await _listing(account.client.refund.all, start, end, pause=pause)
    found = Found(payments=len(payments), refunds=len(refunds))
    if cut_p or cut_r:
        found.mismatches.append({"kind": "listing_cut", "detail": f"more than {MAX_PAGES * PAGE} items; the rest were not checked"})

    by_id = {p["id"]: p for p in payments if p.get("id")}
    window = await _in_window(session, account, start, end)
    refunded_ids = [r.get("payment_id") for r in refunds if r.get("payment_id")]
    ledger = await _ledger_sums(session, [*by_id, *window, *refunded_ids])
    ours = await _our_orders(session, account, [p.get("order_id") for p in payments])
    await session.commit()

    def is_ours(payment: dict) -> bool:
        if payment.get("order_id") in ours:
            return True
        if payment.get("id") in ledger:
            return True
        # On the platform's account, a club's subscription payment.
        return account.gateway is None and bool(payment.get("invoice_id") or payment.get("subscription_id"))

    for payment in payments:
        if is_ours(payment):
            _judge(account, payment, ledger.get(payment["id"]), found)
        else:
            found.other += 1

    # The ledger's own and the day's refunds that the listing did not have: one by one.
    missing = [pid for pid in dict.fromkeys([*window, *refunded_ids]) if pid not in by_id]
    fetched = 0
    for pid in missing:
        if fetched >= MAX_FETCH or (deadline is not None and time.monotonic() > deadline):
            found.unchecked += 1
            continue
        if fetched and pause:
            await asyncio.sleep(pause)
        fetched += 1
        try:
            payment = await _call(account.client.payment.fetch, pid) or {}
        except BadRequestError:
            if pid in ledger:
                found.mismatches.append({"kind": "missing_at_razorpay", "payment_id": pid,
                                         "ledger_paise": ledger[pid]["in"]})
            continue
        if pid not in ledger:
            # Refunded at Razorpay, never taken in here: ours only if its order is.
            if not await _our_orders(session, account, [payment.get("order_id")]):
                if not (account.gateway is None and (payment.get("invoice_id") or payment.get("subscription_id"))):
                    found.other += 1
                    continue
        _judge(account, payment, ledger.get(pid), found)
    return found


async def _tell(session, account: Account, day: date, found: Found) -> None:
    from app.api.admin_alerts import notify

    kinds: dict[str, int] = {}
    for m in found.mismatches:
        kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
    where = "the platform's Razorpay account" if account.gateway is None else f"a club's own Razorpay account ({account.label})"
    await notify(
        session, "ledger_mismatch",
        title=f"Razorpay and the ledger disagree on {len(found.mismatches)} payment(s) for {day.isoformat()}",
        body=(f"On {where}: " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(kinds.items()))
              + ". Each is in this alert's data; the ledger is put right by posting the missing money "
                "(the payments sweep, a refund applied, or scripts/ledger_backfill.py), never by editing it."),
        club=account.club_id,
        data={"day": day.isoformat(), "gateway": account.label, "gateway_id": account.gateway_id,
              "counts": kinds, "mismatches": found.mismatches[:KEEP], "kept": min(len(found.mismatches), KEEP),
              "total": len(found.mismatches)},
        dedupe_on={"day": day.isoformat(), "gateway": account.label},
    )
    await session.commit()


async def reconcile_day(session, day: Optional[date] = None, *, pause: float = PAUSE,
                        deadline: Optional[float] = None, only: Optional[list[Account]] = None) -> dict:
    """Every account's payments and refunds of `day` (yesterday in India by
    default) against the ledger; an alert per account that disagrees.
    Returns what each account found."""
    day = day or (datetime.now(book.IST) - timedelta(days=1)).date()
    start, _ = day_bounds(day)
    done: dict = {"day": day.isoformat(), "accounts": {}}
    for account in only if only is not None else await accounts(session, start):
        if deadline is not None and time.monotonic() > deadline:
            done["accounts"][account.label] = {"skipped": "out of time"}
            continue
        if account.client is None:
            done["accounts"][account.label] = {"error": account.error}
            found = Found(mismatches=[{"kind": "account_unreadable", "detail": account.error}])
            await _tell(session, account, day, found)
            continue
        try:
            found = await check_account(session, account, day, pause=pause, deadline=deadline)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ledger reconciliation: %s for %s could not be read", account.label, day, exc_info=True)
            await session.rollback()
            done["accounts"][account.label] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
            continue
        done["accounts"][account.label] = {
            "payments": found.payments, "refunds": found.refunds, "judged": found.judged, "other": found.other,
            "unchecked": found.unchecked, "mismatches": len(found.mismatches),
        }
        if found.mismatches:
            await _tell(session, account, day, found)
    return done
