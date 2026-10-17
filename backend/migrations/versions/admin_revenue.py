"""Admin revenue: complimentary plans, and the ledger of what subscriptions paid.

- club_subscriptions gains the complimentary grant: `complimentary` (false
  for every existing row), `comp_reason`, `comp_ends_at`, `granted_by` (the
  admin, SET NULL if that account goes) and `comp_reminded_at`. No existing
  row is a comp, so nothing to backfill.
- subscription_charges: one row per payment Razorpay took (or failed to
  take: status `failed`, never summed) for a club's subscription, unique on
  the Razorpay payment id so a repeated webhook is a no-op. Existing payments are not copied here by the migration -- it cannot
  reach Razorpay. Run POST /admin/revenue/backfill (or
  scripts/backfill_subscription_charges.py) once after deploying.

Downgrade drops the table and the five columns.

Revision ID: admin_revenue
Revises: billing_alerts
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "admin_revenue"
down_revision = "billing_alerts"
branch_labels = None
depends_on = None

# The type exists since add_subscriptions; never created or dropped here.
BILLING_CYCLE = postgresql.ENUM("monthly", "yearly", name="billingcycle", create_type=False)


def upgrade() -> None:
    op.add_column(
        "club_subscriptions",
        sa.Column("complimentary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("club_subscriptions", sa.Column("comp_reason", sa.String(200), nullable=True))
    op.add_column("club_subscriptions", sa.Column("comp_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "club_subscriptions",
        sa.Column(
            "granted_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_club_subscriptions_granted_by"),
            nullable=True,
        ),
    )
    op.add_column("club_subscriptions", sa.Column("comp_reminded_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "subscription_charges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("club_subscriptions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id"), nullable=False),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id"), nullable=True),
        sa.Column("cycle", BILLING_CYCLE, nullable=True),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("razorpay_payment_id", sa.String(64), nullable=False),
        sa.Column("razorpay_invoice_id", sa.String(64), nullable=True),
        sa.Column("razorpay_subscription_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="captured"),
        sa.Column("refunded_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("charged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("razorpay_payment_id", name="subscription_charges_razorpay_payment_id_key"),
        sa.CheckConstraint("amount_paise >= 0", name="ck_subscription_charge_amount_non_negative"),
        sa.CheckConstraint(
            "refunded_paise >= 0 AND refunded_paise <= amount_paise",
            name="ck_subscription_charge_refund_in_range",
        ),
        sa.CheckConstraint(
            "status IN ('captured', 'refunded', 'partially_refunded', 'failed')",
            name="ck_subscription_charge_status",
        ),
    )
    op.create_index("ix_subscription_charges_subscription_id", "subscription_charges", ["subscription_id"])
    op.create_index("ix_subscription_charges_club_id", "subscription_charges", ["club_id"])
    op.create_index("ix_subscription_charges_charged_at", "subscription_charges", ["charged_at"])
    op.create_index(
        "ix_subscription_charges_razorpay_subscription_id", "subscription_charges", ["razorpay_subscription_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_subscription_charges_razorpay_subscription_id", table_name="subscription_charges")
    op.drop_index("ix_subscription_charges_charged_at", table_name="subscription_charges")
    op.drop_index("ix_subscription_charges_club_id", table_name="subscription_charges")
    op.drop_index("ix_subscription_charges_subscription_id", table_name="subscription_charges")
    op.drop_table("subscription_charges")
    op.drop_column("club_subscriptions", "comp_reminded_at")
    op.drop_constraint("fk_club_subscriptions_granted_by", "club_subscriptions", type_="foreignkey")
    op.drop_column("club_subscriptions", "granted_by")
    op.drop_column("club_subscriptions", "comp_ends_at")
    op.drop_column("club_subscriptions", "comp_reason")
    op.drop_column("club_subscriptions", "complimentary")
