"""The admin portal's operator tools (app/api/admin_ops).

New tables, nothing changed on an existing one:

- platform_settings: runtime settings an owner changes from the portal
  (key, JSON value, who and when).
- admin_invites: an admin added by email, with the set-password link's
  hashed token.
- club_payouts: money paid to a club for sales on the platform's Razorpay
  account; voided, never deleted.
- payment_fees: Razorpay's fee on a payment, read from Razorpay.
- payment_refunds: an admin's refund of part of a payment.
- payment_disputes: Razorpay disputes from the platform webhook.

Data: `admin_2fa_grace_started_at` = now(), the moment two-factor became
required for admins: the admins there now have seven days from it
(app/api/admin_ops/two_factor.py). Written once; running again keeps the
first.

Everything under lock_timeout 3s. The tables are new; the foreign keys to
users and clubs take a brief lock there.

Downgrade drops the tables (the payouts, notes and settings go with them).

Revision ID: admin_ops
Revises: club_mailbox
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "admin_ops"
down_revision = "club_mailbox"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
NOW = sa.text("now()")


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        if_not_exists=True,
    )
    op.create_table(
        "admin_invites",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("invited_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("promoted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        if_not_exists=True,
    )
    op.create_index("ix_admin_invites_user_id", "admin_invites", ["user_id"], if_not_exists=True)
    op.create_table(
        "club_payouts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("reference", sa.String(120), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("void_reason", sa.String(300), nullable=True),
        sa.CheckConstraint("amount > 0", name="ck_club_payouts_amount_positive"),
        if_not_exists=True,
    )
    op.create_index("ix_club_payouts_club_id", "club_payouts", ["club_id"], if_not_exists=True)
    op.create_table(
        "payment_fees",
        sa.Column("razorpay_payment_id", sa.String(64), primary_key=True),
        sa.Column("fee_paise", sa.BigInteger(), nullable=False),
        sa.Column("tax_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        if_not_exists=True,
    )
    op.create_table(
        "payment_refunds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("payment_log_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("razorpay_payment_id", sa.String(64), nullable=False),
        sa.Column("razorpay_refund_id", sa.String(64), nullable=True, unique=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("store_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("registration_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("club_id", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("amount > 0", name="ck_payment_refunds_amount_positive"),
        if_not_exists=True,
    )
    op.create_index("ix_payment_refunds_payment_log_id", "payment_refunds", ["payment_log_id"], if_not_exists=True)
    op.create_index("ix_payment_refunds_razorpay_payment_id", "payment_refunds", ["razorpay_payment_id"], if_not_exists=True)
    op.create_table(
        "payment_disputes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("razorpay_dispute_id", sa.String(64), nullable=False, unique=True),
        sa.Column("razorpay_payment_id", sa.String(64), nullable=True),
        sa.Column("payment_log_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("subscription_charge_id", sa.Integer(), nullable=True),
        sa.Column("club_id", sa.Integer(), nullable=True),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("amount_deducted_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("reason_code", sa.String(80), nullable=True),
        sa.Column("reason_description", sa.String(500), nullable=True),
        sa.Column("phase", sa.String(40), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="open"),
        sa.Column("respond_by", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event", sa.String(60), nullable=True),
        sa.Column("notes", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        if_not_exists=True,
    )
    op.create_index("ix_payment_disputes_razorpay_payment_id", "payment_disputes", ["razorpay_payment_id"], if_not_exists=True)
    op.create_index("ix_payment_disputes_club_id", "payment_disputes", ["club_id"], if_not_exists=True)
    op.create_index(
        "ix_payment_disputes_open", "payment_disputes", ["respond_by"],
        postgresql_where=sa.text("status IN ('open', 'under_review', 'action_required')"), if_not_exists=True,
    )
    # When two-factor became required for admins: the grace counts from here.
    op.execute(
        "INSERT INTO platform_settings (key, value, updated_at) "
        "VALUES ('admin_2fa_grace_started_at', to_json(to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"+00:00\"')), now()) "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    for table in ("payment_disputes", "payment_refunds", "payment_fees", "club_payouts", "admin_invites", "platform_settings"):
        op.drop_table(table, if_exists=True)
