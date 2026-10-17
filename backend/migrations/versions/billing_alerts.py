"""Billing alerts: the admins' notifications, the club's billing mails, and a
grace period after Razorpay stops retrying.

- admin_notifications: one row per alert (a club applied, a payment failed,
  a grace began or ran out, ...), read or unread, shared by every admin.
  Indexed for the bell (read_at, created_at DESC) and for the ten-minute
  duplicate check (kind, subscription_id, created_at).
- subscription_emails: each billing mail a club was sent, unique on
  (subscription_id, kind, episode) so a mail goes at most once even when two
  webhooks race.
- club_subscriptions gains halted_at, grace_ends_at, grace_reminded_at and
  trial_reminded_at. All null: no row is in a grace today, and no trial has
  been reminded. Nothing to backfill.

Downgrade drops both tables and the four columns.

Revision ID: billing_alerts
Revises: club_billing
"""
import sqlalchemy as sa
from alembic import op

revision = "billing_alerts"
down_revision = "club_billing"
branch_labels = None
depends_on = None

GRACE_COLUMNS = ("halted_at", "grace_ends_at", "grace_reminded_at", "trial_reminded_at")


def upgrade() -> None:
    op.create_table(
        "admin_notifications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("club_subscriptions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_admin_notifications_read_created",
        "admin_notifications",
        ["read_at", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_admin_notifications_dedupe",
        "admin_notifications",
        ["kind", "subscription_id", "created_at"],
    )

    op.create_table(
        "subscription_emails",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("club_subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("episode", sa.String(160), nullable=False),
        sa.Column("recipient", sa.String(254), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("subscription_id", "kind", "episode", name="uq_subscription_email_once"),
    )

    for name in GRACE_COLUMNS:
        op.add_column("club_subscriptions", sa.Column(name, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for name in reversed(GRACE_COLUMNS):
        op.drop_column("club_subscriptions", name)
    op.drop_table("subscription_emails")
    op.drop_index("ix_admin_notifications_dedupe", table_name="admin_notifications")
    op.drop_index("ix_admin_notifications_read_created", table_name="admin_notifications")
    op.drop_table("admin_notifications")
