"""Prepaid message credits; overage billing retired.

The owner's decision of 2026-10-25: clubs buy top-up packs in advance
instead of being billed for extra messages at the end of the month. The
first month-end charge would have been made by the first billing sweep
after 1 November 2026 (IST).

Schema:

- message_credit_packs: the packs on sale (admin-editable; never deleted,
  taken off sale with `active`). Seeded below.
- message_credit_purchases: one per pack a club set out to buy -- its
  one-time Razorpay order, then the payment, then any refund.
- message_credit_lots: credits a club holds per channel (bought: one per
  paid purchase, `purchase_id` unique; or granted by an admin), with
  `remaining`, `expires_at` (12 months on) and when the club was told they
  run low or expire.
- message_usage.plan_used, credits_used, uncovered (NOT NULL DEFAULT 0): how a
  month's messages were paid for -- the plan's allowance, credit lots, or
  neither (a ticket with nothing left; never charged).
- message_statements: the status 'void' is allowed.

Data, each statement bounded (a handful of packs; message_usage and
message_statements have a row or two per club and month):

- The six default packs, GST included as plan prices are, only when the
  table has none: email 1,000 for ₹99, 5,000 for ₹449, 20,000 for ₹1,599;
  WhatsApp 100 for ₹150, 500 for ₹699, 2,000 for ₹2,599.
- Existing counters: `plan_used` is what fell within the limit the club was
  sending under (`quota`; all of it when unlimited), `uncovered` the rest.
- Every statement the retired billing could still have charged or left to
  invoice by hand (pending, charging, failed, unconfirmed, gave_up, manual)
  is made 'void': never charged. How many is printed. Charged ones stay.

Every step checks first, so running it again changes nothing.

Downgrade drops the three tables (the purchases' record goes with them: a
downgrade after a club has bought credits loses what it holds) and the
three columns, and puts the voided statements at 'not_billable', which the
old code never charges.

Revision ID: message_credits
Revises: club_mailbox
"""
import sqlalchemy as sa
from alembic import op

revision = "message_credits"
down_revision = "club_mailbox"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"

#: (channel, quantity, price in paise, sort order): app/api/message_quota/credits.DEFAULT_PACKS.
DEFAULT_PACKS = (
    ("email", 1_000, 9_900, 10),
    ("email", 5_000, 44_900, 20),
    ("email", 20_000, 159_900, 30),
    ("whatsapp", 100, 15_000, 10),
    ("whatsapp", 500, 69_900, 20),
    ("whatsapp", 2_000, 259_900, 30),
)

USAGE_COLUMNS = ("plan_used", "credits_used", "uncovered")

#: What the retired billing could still have charged, or left to invoice.
OPEN_STATEMENTS = ("pending", "charging", "failed", "unconfirmed", "gave_up", "manual")
VOIDED = "Overage billing was retired for prepaid credits: never charged"

OLD_STATUSES = (
    "'nothing_due', 'pending', 'charging', 'charged', 'failed', 'unconfirmed', "
    "'gave_up', 'manual', 'not_billable', 'waived'"
)
NEW_STATUSES = OLD_STATUSES + ", 'void'"


def _timestamps() -> list:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def _channel_check(table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint("channel IN ('email', 'whatsapp')", name=f"ck_{table}_channel")


def seed_packs(bind) -> int:
    """The default packs, when there are none. Returns how many were made."""
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM message_credit_packs)")).scalar():
        return 0
    for channel, quantity, price, order in DEFAULT_PACKS:
        bind.execute(
            sa.text(
                "INSERT INTO message_credit_packs (channel, quantity, price_paise, active, sort_order, created_at, updated_at) "
                "VALUES (:channel, :quantity, :price, true, :order, now(), now())"
            ),
            {"channel": channel, "quantity": quantity, "price": price, "order": order},
        )
    return len(DEFAULT_PACKS)


def split_usage(bind) -> int:
    """Existing counters: what fell within the limit, and the rest."""
    return bind.execute(
        sa.text(
            "UPDATE message_usage SET "
            "plan_used = CASE WHEN quota IS NULL THEN used ELSE LEAST(used, GREATEST(quota, 0)) END, "
            "uncovered = used - CASE WHEN quota IS NULL THEN used ELSE LEAST(used, GREATEST(quota, 0)) END "
            "WHERE used > 0 AND plan_used = 0 AND credits_used = 0 AND uncovered = 0"
        )
    ).rowcount or 0


def void_open_statements(bind) -> int:
    return bind.execute(
        sa.text(
            "UPDATE message_statements SET status = 'void', next_attempt_at = NULL, charging_since = NULL, "
            "last_error = :why, updated_at = now() WHERE status = ANY(:open)"
        ),
        {"why": VOIDED, "open": list(OPEN_STATEMENTS)},
    ).rowcount or 0


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    have = {c["name"] for c in inspector.get_columns("message_usage")}
    for column in USAGE_COLUMNS:
        if column not in have:
            op.add_column("message_usage", sa.Column(column, sa.Integer(), nullable=False, server_default="0"))

    checks = {c["name"] for c in inspector.get_check_constraints("message_statements")}
    if "ck_message_statements_status" in checks:
        op.drop_constraint("ck_message_statements_status", "message_statements", type_="check")
    op.create_check_constraint("ck_message_statements_status", "message_statements", f"status IN ({NEW_STATUSES})")

    if "message_credit_packs" not in tables:
        op.create_table(
            "message_credit_packs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("channel", sa.String(16), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("price_paise", sa.Integer(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            *_timestamps(),
            _channel_check("message_credit_packs"),
            sa.CheckConstraint("quantity > 0 AND price_paise > 0", name="ck_message_credit_packs_positive"),
        )

    if "message_credit_purchases" not in tables:
        op.create_table(
            "message_credit_purchases",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("pack_id", sa.Integer(), sa.ForeignKey("message_credit_packs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("channel", sa.String(16), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("amount_paise", sa.Integer(), nullable=False),
            sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
            sa.Column("status", sa.String(20), nullable=False, server_default="created"),
            sa.Column("razorpay_order_id", sa.String(64), nullable=False, unique=True),
            sa.Column("razorpay_payment_id", sa.String(64), nullable=True, unique=True),
            sa.Column("payment_method", sa.String(40), nullable=True),
            sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("refunded_paise", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("refund_amount_paise", sa.Integer(), nullable=True),
            sa.Column("refund_reason", sa.String(300), nullable=True),
            sa.Column("razorpay_refund_id", sa.String(64), nullable=True),
            sa.Column("refund_requested_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("refunded_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("refund_error", sa.String(300), nullable=True),
            *_timestamps(),
            _channel_check("message_credit_purchases"),
            sa.CheckConstraint(
                "status IN ('created', 'paid', 'refunding', 'refund_failed', 'refunded')",
                name="ck_message_credit_purchases_status",
            ),
            sa.CheckConstraint(
                "quantity > 0 AND amount_paise > 0 AND refunded_paise >= 0 AND refunded_paise <= amount_paise",
                name="ck_message_credit_purchases_amounts",
            ),
        )
        op.create_index("ix_message_credit_purchases_club", "message_credit_purchases", ["club_id", "created_at"])
        op.create_index(
            "ix_message_credit_purchases_open", "message_credit_purchases", ["created_at"],
            postgresql_where=sa.text("status = 'created'"),
        )

    if "message_credit_lots" not in tables:
        op.create_table(
            "message_credit_lots",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("channel", sa.String(16), nullable=False),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("remaining", sa.Integer(), nullable=False),
            sa.Column("bought_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "purchase_id", sa.Integer(), sa.ForeignKey("message_credit_purchases.id", ondelete="CASCADE"),
                nullable=True, unique=True,
            ),
            sa.Column("reason", sa.String(300), nullable=True),
            sa.Column("granted_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("low_warned_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expiry_warned_at", sa.DateTime(timezone=True), nullable=True),
            *_timestamps(),
            _channel_check("message_credit_lots"),
            sa.CheckConstraint("source IN ('purchase', 'grant')", name="ck_message_credit_lots_source"),
            sa.CheckConstraint(
                "quantity > 0 AND remaining >= 0 AND remaining <= quantity", name="ck_message_credit_lots_remaining",
            ),
        )
        op.create_index(
            "ix_message_credit_lots_live", "message_credit_lots", ["club_id", "channel", "expires_at"],
            postgresql_where=sa.text("remaining > 0"),
        )

    packs = seed_packs(bind)
    split = split_usage(bind)
    voided = void_open_statements(bind)
    print(f"message_credits: {packs} packs seeded, {split} counters split, {voided} open overage statements voided")


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    bind = op.get_bind()
    op.drop_table("message_credit_lots")
    op.drop_table("message_credit_purchases")
    op.drop_table("message_credit_packs")
    bind.execute(sa.text("UPDATE message_statements SET status = 'not_billable', updated_at = now() WHERE status = 'void'"))
    op.drop_constraint("ck_message_statements_status", "message_statements", type_="check")
    op.create_check_constraint("ck_message_statements_status", "message_statements", f"status IN ({OLD_STATUSES})")
    for column in reversed(USAGE_COLUMNS):
        op.drop_column("message_usage", column)
