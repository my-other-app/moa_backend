"""Club payment gateways: a club's own Razorpay account for what it sells.

- club_payment_gateways: one row per connection (keys pasted today; `oauth`
  reserved for Razorpay's partner flow). The key secret and the webhook
  secret are Fernet-encrypted with APP_PAYMENT_KEYS_SECRET. At most one row
  per club is live (active or failed): a partial unique index. Replaced,
  disconnected and disabled rows are kept for the orders made on them.
- payment_orders gains payment_gateway_id (null = the platform's account).
  Every existing order is the platform's: nothing to backfill.

Downgrade drops the column and the table. Run it only while no order has a
gateway: one that does would lose the record of which account holds its
money.

Revision ID: club_gateways
Revises: billing_alerts
"""
import sqlalchemy as sa
from alembic import op

revision = "club_gateways"
down_revision = "billing_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "club_payment_gateways",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id"), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False, server_default="razorpay"),
        sa.Column("mode", sa.String(10), nullable=False, server_default="keys"),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("key_secret_enc", sa.Text(), nullable=True),
        sa.Column("webhook_secret_enc", sa.Text(), nullable=False),
        sa.Column("webhook_token", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_webhook_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("webhook_token", name="club_payment_gateways_webhook_token_key"),
    )
    op.create_index("ix_club_payment_gateways_club_id", "club_payment_gateways", ["club_id"])
    op.create_index(
        "uq_club_payment_gateways_live",
        "club_payment_gateways",
        ["club_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'failed')"),
    )

    op.add_column("payment_orders", sa.Column("payment_gateway_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "payment_orders_payment_gateway_id_fkey",
        "payment_orders",
        "club_payment_gateways",
        ["payment_gateway_id"],
        ["id"],
    )
    op.create_index("ix_payment_orders_payment_gateway_id", "payment_orders", ["payment_gateway_id"])


def downgrade() -> None:
    op.drop_index("ix_payment_orders_payment_gateway_id", table_name="payment_orders")
    op.drop_constraint("payment_orders_payment_gateway_id_fkey", "payment_orders", type_="foreignkey")
    op.drop_column("payment_orders", "payment_gateway_id")
    op.drop_index("uq_club_payment_gateways_live", table_name="club_payment_gateways")
    op.drop_index("ix_club_payment_gateways_club_id", table_name="club_payment_gateways")
    op.drop_table("club_payment_gateways")
