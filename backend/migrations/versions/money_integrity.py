"""Money integrity: store holds, paid places, retryable webhooks, automatic
refunds, the subscriber's own price, and the indexes the payment paths read
(app/api/payments/service.py, store/service.py, subscriptions/billing.py).

- event_store_items / event_store_variants.reserved_count: pieces held for
  pending orders; event_store_orders.held_until: until when an order holds
  them. CHECK (sold_count <= stock) on both (stock IS NULL on an item with
  sizes passes), and the counts never negative.
- events.paid_count: a paid event's paid places, kept by a trigger on
  event_registrations_link (moa_paid_count, the same SQL as
  app/api/events/models.PAID_COUNT_SQL) and filled here.
- payment_logs.refund_reason / refund_requested_at: a captured payment being
  given back on its own (event full, paid twice) until Razorpay confirms.
- razorpay_webhook_logs.claimed_at / processed_at / attempts / last_error: an
  event is applied until processed_at is stamped; every row already there was
  processed under the old rules, and is stamped so.
- club_subscriptions.price_paise: what the subscriber pays a cycle -- its
  latest charge in the revenue ledger where there is one, else its plan's
  price -- and the MRR of the Razorpay-billed rows worked out again from it;
  last_event_at: the newest Razorpay event applied.
- Indexes, built CONCURRENTLY: registrations by payment_receipt (the ones
  paid online); payment_logs by order, its refunded ones, and the refunds
  due; payment_orders still open, and paid lately (the sweeps' walks);
  store orders holding something; notifications by event (declared on the
  model now; created here only where an old database lacks it).

Lock-light, in steps: the columns (nullable, or NOT NULL with a constant
default: catalog-only), the trigger, and the CHECKs as NOT VALID, in one
short transaction, after putting right any row that would break a CHECK
(stock raised to what was sold: those pieces were sold); then, each in its
own transaction, the same clean-up again (a write that crossed the first),
VALIDATE (which does not block writes), the backfills (the webhook log in
slices), and the indexes. Each index is dropped first if a failed run left
it invalid. paid_count writes that cross the backfill are put right by the
counter check (app/api/discover/counters, every six hours).

Downgrade drops what this added; the stock clean-up stays.

Revision ID: money_integrity
Revises: discover_scale
"""
import sqlalchemy as sa
from alembic import op

revision = "money_integrity"
down_revision = "discover_scale"
branch_labels = None
depends_on = None

PAID_COUNT_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_paid_count() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'UPDATE' AND OLD.event_id = NEW.event_id AND OLD.is_deleted = NEW.is_deleted
         AND OLD.is_paid = NEW.is_paid THEN
        RETURN NULL;
      END IF;
      IF TG_OP IN ('UPDATE', 'DELETE') AND NOT OLD.is_deleted AND OLD.is_paid THEN
        UPDATE events SET paid_count = paid_count - 1 WHERE id = OLD.event_id;
      END IF;
      IF TG_OP IN ('UPDATE', 'INSERT') AND NOT NEW.is_deleted AND NEW.is_paid THEN
        UPDATE events SET paid_count = paid_count + 1 WHERE id = NEW.event_id;
      END IF;
      RETURN NULL;
    END $$
    """,
    """
    CREATE TRIGGER trg_registrations_paid_count
    AFTER INSERT OR DELETE OR UPDATE OF event_id, is_deleted, is_paid ON event_registrations_link
    FOR EACH ROW EXECUTE FUNCTION moa_paid_count()
    """,
)

#: Rows that would break a CHECK: put right (stock raised to what was sold).
CLEAN_UP = (
    "UPDATE event_store_items SET sold_count = 0 WHERE sold_count < 0",
    "UPDATE event_store_items SET stock = sold_count WHERE stock IS NOT NULL AND sold_count > stock",
    "UPDATE event_store_variants SET sold_count = 0 WHERE sold_count < 0",
    "UPDATE event_store_variants SET stock = sold_count WHERE sold_count > stock",
)

CHECKS = (
    ("event_store_items", "ck_event_store_items_sold_within_stock", "stock IS NULL OR sold_count <= stock"),
    ("event_store_items", "ck_event_store_items_counts_non_negative", "sold_count >= 0 AND reserved_count >= 0"),
    ("event_store_variants", "ck_event_store_variants_sold_within_stock", "sold_count <= stock"),
    ("event_store_variants", "ck_event_store_variants_counts_non_negative", "sold_count >= 0 AND reserved_count >= 0"),
    ("club_subscriptions", "ck_subscription_price_non_negative", "price_paise IS NULL OR price_paise >= 0"),
)

PAID_COUNT_BACKFILL = """
UPDATE events e SET paid_count = c.n
FROM (
  SELECT event_id, count(*) AS n FROM event_registrations_link
  WHERE NOT is_deleted AND is_paid
  GROUP BY event_id
) c
WHERE c.event_id = e.id AND e.paid_count <> c.n
"""

#: Every webhook already in the log was processed under the old rules.
WEBHOOKS_PROCESSED = """
UPDATE razorpay_webhook_logs SET processed_at = created_at
WHERE id IN (SELECT id FROM razorpay_webhook_logs WHERE processed_at IS NULL LIMIT 5000)
"""

PRICE_BACKFILL = """
UPDATE club_subscriptions s SET price_paise = coalesce(
  (SELECT c.amount_paise FROM subscription_charges c
   WHERE s.razorpay_subscription_id IS NOT NULL AND c.razorpay_subscription_id = s.razorpay_subscription_id
     AND c.status <> 'failed' AND c.amount_paise > 0
   ORDER BY c.charged_at DESC LIMIT 1),
  CASE WHEN s.complimentary THEN 0
       WHEN s.cycle = 'yearly' THEN p.price_yearly_paise
       ELSE p.price_monthly_paise END
)
FROM plans p
WHERE p.id = s.plan_id AND s.price_paise IS NULL
"""

#: The Razorpay-billed rows' MRR, at the price they pay.
MRR_BACKFILL = """
UPDATE club_subscriptions SET mrr_paise = CASE WHEN cycle = 'yearly' THEN price_paise / 12 ELSE price_paise END
WHERE razorpay_subscription_id IS NOT NULL AND granted_by IS NULL AND NOT complimentary
  AND mrr_paise > 0 AND price_paise IS NOT NULL
  AND mrr_paise IS DISTINCT FROM (CASE WHEN cycle = 'yearly' THEN price_paise / 12 ELSE price_paise END)
"""

INDEXES = (
    ("ix_registrations_payment_receipt", "event_registrations_link (payment_receipt) WHERE payment_receipt IS NOT NULL"),
    ("ix_payment_logs_order_id", "payment_logs (order_id)"),
    ("ix_payment_logs_refunded_order", "payment_logs (order_id) WHERE status = 'refunded'"),
    ("ix_payment_logs_refund_due", "payment_logs (refund_requested_at) WHERE refund_requested_at IS NOT NULL AND status = 'captured'"),
    ("ix_payment_orders_open_created", "payment_orders (created_at) WHERE status IN ('created', 'attempted')"),
    ("ix_payment_orders_paid_updated", "payment_orders (updated_at) WHERE status = 'paid'"),
    ("ix_event_store_orders_held_until", "event_store_orders (held_until) WHERE held_until IS NOT NULL"),
)


def _invalid(name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text(
        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :n AND NOT i.indisvalid"
    ), {"n": name}).first())


def upgrade() -> None:
    op.add_column("event_store_items", sa.Column("reserved_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("event_store_variants", sa.Column("reserved_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("event_store_orders", sa.Column("held_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("events", sa.Column("paid_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("payment_logs", sa.Column("refund_reason", sa.String(40), nullable=True))
    op.add_column("payment_logs", sa.Column("refund_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("razorpay_webhook_logs", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("razorpay_webhook_logs", sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("razorpay_webhook_logs", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("razorpay_webhook_logs", sa.Column("last_error", sa.String(300), nullable=True))
    op.add_column("club_subscriptions", sa.Column("price_paise", sa.Integer(), nullable=True))
    op.add_column("club_subscriptions", sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True))
    for sql in PAID_COUNT_SQL:
        op.execute(sql)
    for sql in CLEAN_UP:
        op.execute(sql)
    for table, name, check in CHECKS:
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({check}) NOT VALID")

    with op.get_context().autocommit_block():
        bind = op.get_bind()
        # Anything a write slipped in between the clean-up and the CHECKs.
        for sql in CLEAN_UP:
            op.execute(sql)
        for table, name, _ in CHECKS:
            op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")
        op.execute(PAID_COUNT_BACKFILL)
        while bind.execute(sa.text(WEBHOOKS_PROCESSED)).rowcount:
            pass
        op.execute(PRICE_BACKFILL)
        op.execute(MRR_BACKFILL)
        for name, definition in INDEXES:
            if _invalid(name):
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {definition}")
        # Declared on the model now; made by add_event_id_notifications, so
        # only a database without it gets it here (and downgrade leaves it).
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_notifications_event_id ON notifications (event_id)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _ in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    for table, name, _ in reversed(CHECKS):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute("DROP TRIGGER IF EXISTS trg_registrations_paid_count ON event_registrations_link")
    op.execute("DROP FUNCTION IF EXISTS moa_paid_count()")
    op.drop_column("club_subscriptions", "last_event_at")
    op.drop_column("club_subscriptions", "price_paise")
    op.drop_column("razorpay_webhook_logs", "last_error")
    op.drop_column("razorpay_webhook_logs", "attempts")
    op.drop_column("razorpay_webhook_logs", "processed_at")
    op.drop_column("razorpay_webhook_logs", "claimed_at")
    op.drop_column("payment_logs", "refund_requested_at")
    op.drop_column("payment_logs", "refund_reason")
    op.drop_column("events", "paid_count")
    op.drop_column("event_store_orders", "held_until")
    op.drop_column("event_store_variants", "reserved_count")
    op.drop_column("event_store_items", "reserved_count")
