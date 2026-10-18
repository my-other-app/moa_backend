"""Posting to the ledger: the accounts, the kinds, and `post`.

A posting (`Txn`) records one source event -- a payment captured, a refund,
a ticket paid at the door, a subscription charged -- as lines that sum to
zero: debits positive, credits negative, whole paise. Its `key` names the
event ("capture:pay_X"), and the lines are unique by (key, line), so posting
the same event twice writes nothing the second time: a webhook delivered
again, a verify racing the webhook, the backfill meeting a live posting.

`post` writes the lines and adds them to ledger_monthly and
ledger_platform_monthly in one SAVEPOINT of the caller's transaction, so they
land with the state change they record or not at all. It never raises: a
posting that fails is logged and the admins hear (`ledger_post_failed`, with
the source), the state change still commits, and the daily job posts it
again from the source (app.api.ledger.jobs.retry_failed). A ledger that cannot
be written must never cost a guest their ticket.

The accounts, from the club's side for ticket and store money and from the
platform's for subscriptions:

    gateway         money at Razorpay, in the platform's account or a club's
                    own (`gateway_id`); debited on capture, credited on refund
    desk            cash taken at the door
    unapplied       the clearing account between the two sides: a capture
                    credits it, applying the money to a ticket or an order
                    debits it. A balance is money taken that pays for nothing
                    (yet): being given back, or waiting to be applied
    tickets_online  ticket revenue paid online (credit)
    tickets_desk    ticket revenue paid at the door (credit)
    store           store orders sold (credit)
    store_held      paid for pieces that had sold out (oversold): handed over
                    once stock comes in (`fulfil`, moved to store) or refunded
    subscriptions   the platform's subscription revenue (credit)
    comps           given for nothing: zero-rupee markers (a complimentary
                    plan, a free pick from the store)
    subscribers     zero-rupee markers: a subscription's first charge (the
                    revenue page's "new paying")
"""
from __future__ import annotations

import hashlib
import logging
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.ledger.models import SHARDS, LedgerEntries, LedgerMonthly, LedgerPlatformMonthly

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ─── Accounts ────────────────────────────────────────────────────────────
GATEWAY = "gateway"
DESK = "desk"
UNAPPLIED = "unapplied"
TICKETS_ONLINE = "tickets_online"
TICKETS_DESK = "tickets_desk"
STORE = "store"
STORE_HELD = "store_held"
SUBSCRIPTIONS = "subscriptions"
COMPS = "comps"
SUBSCRIBERS = "subscribers"
ACCOUNTS = (GATEWAY, DESK, UNAPPLIED, TICKETS_ONLINE, TICKETS_DESK, STORE, STORE_HELD, SUBSCRIPTIONS, COMPS, SUBSCRIBERS)
TICKETS = (TICKETS_ONLINE, TICKETS_DESK)
#: What a registration's money is held in, by registration.
REGISTRATION_ACCOUNTS = (TICKETS_ONLINE, TICKETS_DESK, STORE, STORE_HELD)

# ─── Kinds: what happened ────────────────────────────────────────────────
CAPTURE = "capture"          # Razorpay captured a payment for a ticket or the store
REFUND = "refund"            # ... and gave it back
APPLY = "apply"              # captured money paid for a ticket or an order
UNAPPLY = "unapply"          # a refund took the ticket or the order back
DESK_SALE = "desk"           # paid at the door
FULFIL = "fulfil"            # an oversold order handed over after all
COMP = "comp"                # given for nothing, at zero
SUB_CHARGE = "sub_charge"    # a club's subscription charged
SUB_REFUND = "sub_refund"    # ... refunded (all or part)
FIRST_CHARGE = "first_charge"  # a subscription's first charge (a marker)
BACKFILL = "backfill"        # history posted from the rows it left
REPAIR = "repair"            # the daily catch-up, for a posting that failed
KINDS = (CAPTURE, REFUND, APPLY, UNAPPLY, DESK_SALE, FULFIL, COMP, SUB_CHARGE, SUB_REFUND, FIRST_CHARGE, BACKFILL, REPAIR)

#: Sources: the table a posting came from.
SRC_PAYMENT = "payment_logs"
SRC_REGISTRATION = "event_registrations_link"
SRC_CHARGE = "subscription_charges"
SRC_SUBSCRIPTION = "subscription"
SRC_AUDIT = "admin_audit_logs"


def now() -> datetime:
    return datetime.now(timezone.utc)


def month_of(when: datetime) -> date:
    """The first day of `when`'s calendar month in India."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    local = when.astimezone(IST)
    return date(local.year, local.month, 1)


def digest(*parts) -> str:
    """A short, stable name for what a posting changes (part of its key)."""
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Line:
    account: str
    amount: int
    units: int = 0


@dataclass
class Txn:
    """One posting. `lines` sum to zero; a line of zero rupees and zero
    units is dropped (a marker is a line of zero rupees and some units)."""

    key: str
    kind: str
    lines: list[Line]
    occurred_at: datetime
    source: str
    source_id: str
    origin_at: datetime | None = None
    club_id: int | None = None
    event_id: int | None = None
    registration_id: object | None = None
    gateway_id: int | None = None
    razorpay_payment_id: str | None = None
    razorpay_order_id: str | None = None
    razorpay_refund_id: str | None = None
    razorpay_subscription_id: str | None = None
    note: str | None = None
    #: Filled by `post`: the lines it wrote.
    written: list[Line] = field(default_factory=list)

    def kept(self) -> list[Line]:
        return [line for line in self.lines if line.amount or line.units]


class Unbalanced(ValueError):
    """A posting whose lines do not sum to zero: a bug in the caller."""


def _row(txn: Txn, i: int, line: Line) -> dict:
    return dict(
        txn_key=txn.key[:200], line=i, kind=txn.kind, account=line.account, amount_paise=int(line.amount),
        units=int(line.units), currency="INR", club_id=txn.club_id, event_id=txn.event_id,
        registration_id=txn.registration_id, gateway_id=txn.gateway_id, source=txn.source,
        source_id=str(txn.source_id)[:80], razorpay_payment_id=txn.razorpay_payment_id,
        razorpay_order_id=txn.razorpay_order_id, razorpay_refund_id=txn.razorpay_refund_id,
        razorpay_subscription_id=txn.razorpay_subscription_id, occurred_at=txn.occurred_at,
        origin_at=txn.origin_at or txn.occurred_at, note=(txn.note or None) and txn.note[:200],
    )


def _rows(txn: Txn, lines: list[Line]) -> list[dict]:
    return [_row(txn, i, line) for i, line in enumerate(lines)]


async def post(session, txn: Txn) -> int:
    """
    Write `txn` and add it to the monthly sums, in a SAVEPOINT of the caller's
    transaction (the caller commits). Returns the lines written: 0 when the
    posting was made before (its key is taken) or could not be made (logged;
    the admins hear, and the daily job posts it again). Never raises.
    """
    lines = txn.kept()
    if not lines:
        return 0
    try:
        if sum(line.amount for line in lines) != 0:
            raise Unbalanced(f"{txn.key}: lines sum to {sum(line.amount for line in lines)} paise, not 0")
        table = LedgerEntries.__table__
        async with session.begin_nested():
            written = (await session.execute(
                pg_insert(table).values(_rows(txn, lines))
                .on_conflict_do_nothing(index_elements=["txn_key", "line"])
                .returning(table.c.line)
            )).scalars().all()
            txn.written = [lines[i] for i in sorted(written)]
            if txn.written and len(txn.written) != len(lines):
                # Some lines of this key were there and some not: the same
                # key was posted with other lines. Kept as found; the
                # reconcile's balance check will name it.
                logger.error("ledger posting %s was partly there already (%s of %s lines new)",
                             txn.key, len(txn.written), len(lines))
            if txn.written:
                await _add_to_sums(session, txn, txn.written)
        return len(txn.written)
    except Exception as exc:  # noqa: BLE001
        txn.written = []
        logger.exception("ledger posting %s (%s %s) failed", txn.key, txn.source, txn.source_id)
        await _failed(session, txn, exc)
        return 0


def _sums() -> tuple[dict, dict]:
    return defaultdict(lambda: [0, 0, 0]), defaultdict(lambda: [0, 0, 0])


def _accumulate(monthly: dict, platform: dict, txn: Txn, lines: list[Line]) -> None:
    month = month_of(txn.occurred_at)
    origin = month_of(txn.origin_at or txn.occurred_at)
    shard = zlib.crc32(txn.key.encode()) % SHARDS
    for line in lines:
        for sums, key in (
            (monthly, (txn.club_id or 0, txn.event_id or 0, txn.gateway_id or 0, month, origin, line.account, txn.kind)),
            (platform, (month, origin, txn.gateway_id is not None, line.account, txn.kind, shard)),
        ):
            row = sums[key]
            row[0] += int(line.amount)
            row[1] += int(line.units)
            row[2] += 1


MONTHLY_KEY = ("club_id", "event_id", "gateway_id", "month", "origin_month", "account", "kind")
PLATFORM_KEY = ("month", "origin_month", "club_gateway", "account", "kind", "shard")


async def add_to(session, model, key_names: tuple, sums: dict[tuple, list[int]]) -> None:
    """Add `sums` ({key: [paise, units, entries]}) to `model`'s rows, one
    UPSERT, in key order (two postings lock the rows they share in the same
    order, and cannot deadlock on them)."""
    if not sums:
        return
    table = model.__table__
    rows = [
        {**dict(zip(key_names, key)), "amount_paise": v[0], "units": v[1], "entries": v[2]}
        for key, v in sorted(sums.items(), key=lambda kv: tuple(str(k) for k in kv[0]))
    ]
    stmt = pg_insert(table).values(rows)
    await session.execute(stmt.on_conflict_do_update(
        index_elements=list(key_names),
        set_={
            "amount_paise": table.c.amount_paise + stmt.excluded.amount_paise,
            "units": table.c.units + stmt.excluded.units,
            "entries": table.c.entries + stmt.excluded.entries,
            "updated_at": now(),
        },
    ))


async def _add_to_sums(session, txn: Txn, lines: list[Line]) -> None:
    monthly, platform = _sums()
    _accumulate(monthly, platform, txn, lines)
    await add_to(session, LedgerMonthly, MONTHLY_KEY, monthly)
    await add_to(session, LedgerPlatformMonthly, PLATFORM_KEY, platform)


#: Lines one INSERT carries in post_many (22 columns a line, under asyncpg's 32,767 parameters).
CHUNK = 1000


async def post_many(session, txns: list[Txn]) -> int:
    """
    Many postings at once, for the backfill: their lines in a few INSERTs and
    their sums in one UPSERT a table, in one SAVEPOINT. The same keys and the
    same lines as `post` (a posting made either way is the same posting), so
    what is there already writes nothing. Should the batch fail, each posting
    is tried on its own (`post`): one bad posting never costs the rest.
    Returns the lines written. Never raises.
    """
    good: list[tuple[Txn, list[Line]]] = []
    seen: set[str] = set()
    for txn in txns:
        lines = txn.kept()
        if not lines or txn.key in seen:
            continue
        seen.add(txn.key)
        if sum(line.amount for line in lines) != 0:
            await _failed(session, txn, Unbalanced(f"{txn.key}: lines sum to {sum(x.amount for x in lines)} paise, not 0"))
            continue
        good.append((txn, lines))
    if not good:
        return 0
    rows, index = [], {}
    for txn, lines in good:
        for i, line in enumerate(lines):
            rows.append(_row(txn, i, line))
            index[(txn.key[:200], i)] = (txn, line)
    table = LedgerEntries.__table__
    try:
        async with session.begin_nested():
            written = []
            for start in range(0, len(rows), CHUNK):
                written += (await session.execute(
                    pg_insert(table).values(rows[start:start + CHUNK])
                    .on_conflict_do_nothing(index_elements=["txn_key", "line"])
                    .returning(table.c.txn_key, table.c.line)
                )).all()
            monthly, platform = _sums()
            for key, line in written:
                txn, kept = index[(key, line)]
                _accumulate(monthly, platform, txn, [kept])
            await add_to(session, LedgerMonthly, MONTHLY_KEY, monthly)
            await add_to(session, LedgerPlatformMonthly, PLATFORM_KEY, platform)
        return len(written)
    except Exception:  # noqa: BLE001
        logger.warning("ledger: a batch of %s postings failed; posting each on its own", len(good), exc_info=True)
        return sum([await post(session, txn) for txn, _ in good])


_failures = 0


def failures() -> int:
    """Postings that failed in this process so far (the backfill reports the difference)."""
    return _failures


async def _failed(session, txn: Txn, exc: Exception) -> None:
    """The admins hear, once per source, with what the daily job needs to post it again."""
    global _failures
    _failures += 1
    try:
        from app.api.admin_alerts import notify

        await notify(
            session, "ledger_post_failed",
            title=f"A {txn.kind} could not be written to the ledger",
            body=(f"{txn.source} {txn.source_id} ({txn.key}): {type(exc).__name__}: {str(exc)[:200]}. "
                  "The money moved; the daily ledger job posts it again."),
            club=txn.club_id,
            data={"source": txn.source, "source_id": str(txn.source_id), "key": txn.key, "kind": txn.kind,
                  "error": f"{type(exc).__name__}: {str(exc)[:200]}"},
            dedupe_on={"source": txn.source, "source_id": str(txn.source_id)},
        )
    except Exception:  # noqa: BLE001
        logger.warning("ledger: the failure of %s could not be recorded either", txn.key, exc_info=True)
