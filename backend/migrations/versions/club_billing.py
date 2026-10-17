"""Club billing: nothing is granted until Razorpay confirms it.

- subscriptionstatus gains 'pending' (checkout opened, nothing confirmed;
  grants nothing) and 'paused' (paused at Razorpay; grants nothing). Added
  with ADD VALUE IF NOT EXISTS outside the migration's transaction, which
  Postgres requires before a new value can be used.
- club_subscriptions gains the state machine's timestamps: pending_since,
  authenticated_at, activated_at, trial_started_at, trial_ends_at,
  cancel_at_period_end, cancel_requested_at, and previous_state (what the club
  had before a checkout, put back if the checkout is abandoned).

Backfill, from what the rows already say:
- activated_at = updated_at where the row is active, or past_due after a
  charge (current_period_end set) or by an admin's hand (no Razorpay id):
  those were paid for or granted, so a later failed charge keeps access.
- authenticated_at and trial_started_at = created_at where the status is
  'trial' and there is no Razorpay id (an admin's trial, which stands).
- trial_started_at = created_at on every other 'trial' row too: it counts as
  the club's one trial.

Left unconfirmed on purpose: a 'trial' or 'past_due' row that the old
checkout wrote before Razorpay confirmed anything (a Razorpay id, no charge
seen). Under the new rules those grant nothing until Razorpay says so.

Downgrade drops the columns. Postgres cannot drop an enum value, so
'pending' and 'paused' stay in the type; rows using them become 'cancelled'
so the older code can read every row.

Revision ID: club_billing
Revises: club_plans
"""
import sqlalchemy as sa
from alembic import op

revision = "club_billing"
down_revision = "club_plans"
branch_labels = None
depends_on = None

TIMESTAMPS = (
    "pending_since",
    "authenticated_at",
    "activated_at",
    "trial_started_at",
    "trial_ends_at",
    "cancel_requested_at",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE subscriptionstatus ADD VALUE IF NOT EXISTS 'pending'")
        op.execute("ALTER TYPE subscriptionstatus ADD VALUE IF NOT EXISTS 'paused'")

    for name in TIMESTAMPS:
        op.add_column("club_subscriptions", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "club_subscriptions",
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("club_subscriptions", sa.Column("previous_state", sa.JSON(), nullable=True))

    op.execute(
        "UPDATE club_subscriptions SET activated_at = updated_at "
        "WHERE status = 'active' "
        "OR (status = 'past_due' AND (current_period_end IS NOT NULL OR razorpay_subscription_id IS NULL))"
    )
    op.execute(
        "UPDATE club_subscriptions SET authenticated_at = created_at "
        "WHERE status = 'trial' AND razorpay_subscription_id IS NULL"
    )
    op.execute("UPDATE club_subscriptions SET trial_started_at = created_at WHERE status = 'trial'")


def downgrade() -> None:
    op.execute("UPDATE club_subscriptions SET status = 'cancelled' WHERE status IN ('pending', 'paused')")
    op.drop_column("club_subscriptions", "previous_state")
    op.drop_column("club_subscriptions", "cancel_at_period_end")
    for name in reversed(TIMESTAMPS):
        op.drop_column("club_subscriptions", name)
