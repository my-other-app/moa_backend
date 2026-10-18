"""The money ledger: every paisa that moved, once, and never changed after.

Three tables:

- `ledger_entries`, append-only. Double entry: each posting (a "txn", named
  by `txn_key`) is a few lines whose amounts sum to zero, debits positive and
  credits negative, in integer paise. A database trigger refuses UPDATE,
  DELETE and TRUNCATE; a mistake is put right by a new posting.
- `ledger_monthly`, what the entries add up to per club, event, gateway,
  IST month, origin month, account and kind. Kept in the posting's own
  transaction (an UPSERT), and checked against the entries by the daily
  reconcile, which corrects drift.
- `ledger_platform_monthly`, the same summed over every club, for the admin's
  revenue page. Spread over SHARDS rows per key so that the platform's every
  payment does not queue on one row.

Why `origin_month` beside `month`: a refund moves money in the month it is
made, but the reports attribute it to the sale it refunds (the admin's
revenue page reads "refunded" against the month of the charge; ticket sales
count a payment as captured in the month it was taken). Each line carries
both: `occurred_at` (when this money moved) and `origin_at` (when the sale it
belongs to happened; the same for a sale).

No foreign keys, on purpose: the ledger outlives every row it points at (a
club, an event, a registration may be deleted; the money moved), and a key
on `events` would take a share lock on the hottest rows on every posting.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DDL,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    event as sa_event,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel

#: Rows each platform-wide key is spread over (ledger_platform_monthly.shard).
SHARDS = 8


class LedgerEntries(AbstractSQLModel):
    __tablename__ = "ledger_entries"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    #: The source event this posting records, e.g. "capture:pay_X". Unique with
    #: `line`: posting the same event again inserts nothing.
    txn_key = Column(String(200), nullable=False)
    line = Column(SmallInteger, nullable=False)
    #: What happened (book.KINDS): capture, refund, apply, unapply, desk, ...
    kind = Column(String(24), nullable=False)
    #: Where the money sits (book.ACCOUNTS): gateway, tickets_online, store, ...
    account = Column(String(24), nullable=False)
    #: Debit positive, credit negative. A posting's lines sum to zero.
    amount_paise = Column(BigInteger, nullable=False)
    #: What the line counts, signed: a ticket that became paid (+1) or
    #: unpaid (-1), a store order, a payment, a refund.
    units = Column(Integer, nullable=False, default=0, server_default="0")
    currency = Column(String(3), nullable=False, default="INR", server_default="INR")
    club_id = Column(Integer, nullable=True)
    event_id = Column(Integer, nullable=True)
    registration_id = Column(UUID(as_uuid=True), nullable=True)
    #: The club's own Razorpay account (club_payment_gateways.id) the money
    #: went through; null for the platform's, and for money no gateway saw.
    gateway_id = Column(Integer, nullable=True)
    #: The row the posting came from: its table and its id.
    source = Column(String(40), nullable=False)
    source_id = Column(String(80), nullable=False)
    razorpay_payment_id = Column(String(64), nullable=True)
    razorpay_order_id = Column(String(64), nullable=True)
    razorpay_refund_id = Column(String(64), nullable=True)
    razorpay_subscription_id = Column(String(64), nullable=True)
    #: When this money moved.
    occurred_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    #: When the sale it belongs to happened (a refund's payment was captured).
    origin_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    #: When it was written here (a backfilled line is written long after).
    recorded_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=text("now()"))
    note = Column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("txn_key", "line", name="uq_ledger_entries_txn_line"),
        # A registration's balances, read on every posting about it.
        Index("ix_ledger_entries_registration", "registration_id", "account",
              postgresql_where=text("registration_id IS NOT NULL")),
        # One event's lines by month (the event's finance page).
        Index("ix_ledger_entries_event", "event_id", "account",
              postgresql_include=["kind", "amount_paise", "units", "occurred_at", "origin_at"],
              postgresql_where=text("event_id IS NOT NULL")),
        # The daily reconciliation: what the ledger holds for a Razorpay payment.
        Index("ix_ledger_entries_payment", "razorpay_payment_id",
              postgresql_where=text("razorpay_payment_id IS NOT NULL")),
        # The daily reconciliation's walk: gateway lines of one day.
        Index("ix_ledger_entries_gateway_day", "occurred_at", postgresql_where=text("account = 'gateway'")),
        # The drift reconcile, one club at a time; and lines since a watermark.
        Index("ix_ledger_entries_club", "club_id", "id"),
        # A subscription's first charge (book.FIRST_CHARGE markers).
        Index("ix_ledger_entries_subscribers", "source_id", postgresql_where=text("account = 'subscribers'")),
    )


class LedgerMonthly(AbstractSQLModel):
    """The entries summed per club, event, gateway, month, origin month,
    account and kind. 0 stands for "none" in club_id, event_id and
    gateway_id (a subscription has no event; most money no club gateway)."""

    __tablename__ = "ledger_monthly"

    club_id = Column(Integer, primary_key=True)
    event_id = Column(Integer, primary_key=True)
    gateway_id = Column(Integer, primary_key=True)
    #: The IST calendar month the money moved in (its first day).
    month = Column(Date, primary_key=True)
    #: The IST calendar month of the sale it belongs to.
    origin_month = Column(Date, primary_key=True)
    account = Column(String(24), primary_key=True)
    kind = Column(String(24), primary_key=True)
    amount_paise = Column(BigInteger, nullable=False, default=0, server_default="0")
    units = Column(BigInteger, nullable=False, default=0, server_default="0")
    #: Lines summed (the reconcile compares this too).
    entries = Column(BigInteger, nullable=False, default=0, server_default="0")
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        # A club's events (finance): the key's own (club_id, event_id) prefix
        # serves a club; this serves an event's page, which knows no club.
        Index("ix_ledger_monthly_event", "event_id", postgresql_where=text("event_id <> 0")),
        # The platform table's reconcile, a month at a time.
        Index("ix_ledger_monthly_month", "month"),
    )


class LedgerPlatformMonthly(AbstractSQLModel):
    """ledger_monthly summed over every club, spread over SHARDS rows a key."""

    __tablename__ = "ledger_platform_monthly"

    month = Column(Date, primary_key=True)
    origin_month = Column(Date, primary_key=True)
    #: The money went through a club's own Razorpay account, not the platform's.
    club_gateway = Column(Boolean, primary_key=True)
    account = Column(String(24), primary_key=True)
    kind = Column(String(24), primary_key=True)
    shard = Column(SmallInteger, primary_key=True)
    amount_paise = Column(BigInteger, nullable=False, default=0, server_default="0")
    units = Column(BigInteger, nullable=False, default=0, server_default="0")
    entries = Column(BigInteger, nullable=False, default=0, server_default="0")
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=text("now()"))


# ─── Append-only, kept by the database ─────────────────────────────────────
#
# Nothing in the app updates or deletes a ledger line, and the database
# makes sure nothing else does either: a correction is a new posting. The
# migration w2_ledger installs the same function and triggers.

APPEND_ONLY_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_ledger_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      RAISE EXCEPTION 'ledger_entries is append-only: % refused (post a correcting entry instead)', TG_OP
        USING ERRCODE = 'restrict_violation';
    END $$
    """,
    """
    CREATE TRIGGER trg_ledger_entries_append_only
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION moa_ledger_append_only()
    """,
    """
    CREATE TRIGGER trg_ledger_entries_no_truncate
    BEFORE TRUNCATE ON ledger_entries
    FOR EACH STATEMENT EXECUTE FUNCTION moa_ledger_append_only()
    """,
)

for _sql in APPEND_ONLY_SQL:
    # DDL formats its text with % (table names); the RAISE's own % is escaped.
    sa_event.listen(LedgerEntries.__table__, "after_create", DDL(_sql.replace("%", "%%")).execute_if(dialect="postgresql"))

