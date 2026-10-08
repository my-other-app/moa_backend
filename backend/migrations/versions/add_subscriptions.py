"""Plans, their limits and features, and club subscriptions.

Revision ID: add_subscriptions
Revises: add_two_factor

Creates the subscription domain. Nothing is seeded: a platform with no plans
resolves every club to unlimited (see service._unlimited), which is the
pre-subscription status quo, so this migration cannot change what any
existing club is allowed to do.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_subscriptions"
down_revision = "add_two_factor"
branch_labels = None
depends_on = None


# `create_type=False` because the types are created explicitly in upgrade().
# Without it `create_table` emits its own CREATE TYPE, which collides with the
# explicit one -- and leaving it to create_table instead would break the
# moment a second table referenced the same type.
PLAN_VISIBILITY = postgresql.ENUM(
    "public", "hidden", "legacy", name="planvisibility", create_type=False
)
BILLING_CYCLE = postgresql.ENUM(
    "monthly", "yearly", name="billingcycle", create_type=False
)
SUBSCRIPTION_STATUS = postgresql.ENUM(
    "trial", "active", "past_due", "cancelled", name="subscriptionstatus",
    create_type=False,
)

#: NULL means unlimited throughout; 0 is a real value meaning none allowed.
LIMIT_COLUMNS = (
    "events_per_month",
    "registrations_per_month",
    "registrations_per_event",
    "ticket_types_per_event",
    "coupons_per_event",
    "merch_items_per_event",
    "committee_seats",
    "storage_gb",
    "announcements_per_month",
    "website_templates",
)

FEATURE_COLUMNS = (
    # every event
    "tickets", "certificates", "checkin", "reviews", "threads", "regform", "waitlist",
    # grow
    "coupons", "links", "merch", "swag", "quiz", "qa", "polls", "networking",
    # competitions
    "teams", "judging", "rounds", "badges", "sessions", "report",
    # club
    "website", "domain", "nobadge", "finance", "roles", "tfa", "payouts", "sso", "api",
)


def upgrade() -> None:
    bind = op.get_bind()
    PLAN_VISIBILITY.create(bind, checkfirst=True)
    BILLING_CYCLE.create(bind, checkfirst=True)
    SUBSCRIPTION_STATUS.create(bind, checkfirst=True)

    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("tagline", sa.String(length=160), nullable=True),
        # Paise, as an integer. Floats cannot hold ₹1,499.00 exactly and a
        # price that drifts by a hundredth of a rupee a month surfaces in an
        # annual reconciliation, long after the cause is forgotten.
        sa.Column("price_monthly_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("price_yearly_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trial_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("visibility", PLAN_VISIBILITY, nullable=False, server_default="hidden"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("razorpay_plan_id_monthly", sa.String(length=64), nullable=True),
        sa.Column("razorpay_plan_id_yearly", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("price_monthly_paise >= 0", name="ck_plans_monthly_non_negative"),
        sa.CheckConstraint("price_yearly_paise >= 0", name="ck_plans_yearly_non_negative"),
        sa.CheckConstraint("trial_days >= 0", name="ck_plans_trial_non_negative"),
    )
    op.create_index("ix_plans_key", "plans", ["key"], unique=True)
    op.create_index("ix_plans_is_deleted", "plans", ["is_deleted"])

    op.create_table(
        "plan_limits",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "plan_id",
            sa.Integer(),
            sa.ForeignKey("plans.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        *[sa.Column(name, sa.Integer(), nullable=True) for name in LIMIT_COLUMNS],
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "plan_features",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "plan_id",
            sa.Integer(),
            sa.ForeignKey("plans.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        # Off by default: adding a flag later must not hand every existing
        # plan a feature its subscribers have not paid for.
        *[
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.text("false"))
            for name in FEATURE_COLUMNS
        ],
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "club_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id"), nullable=False),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("cycle", BILLING_CYCLE, nullable=False, server_default="monthly"),
        sa.Column("status", SUBSCRIPTION_STATUS, nullable=False, server_default="active"),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mrr_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("razorpay_subscription_id", sa.String(length=64), nullable=True),
        sa.Column("last_failure_reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        # `is_deleted` is in the key so a club that cancels and resubscribes
        # does not collide with its own history.
        sa.UniqueConstraint("club_id", "is_deleted", name="uq_club_subscription_live"),
        sa.CheckConstraint("mrr_paise >= 0", name="ck_subscription_mrr_non_negative"),
    )
    op.create_index("ix_club_subscriptions_club_id", "club_subscriptions", ["club_id"])
    op.create_index("ix_club_subscriptions_plan_id", "club_subscriptions", ["plan_id"])
    op.create_index("ix_club_subscriptions_is_deleted", "club_subscriptions", ["is_deleted"])
    op.create_index(
        "ix_club_subscriptions_razorpay_id",
        "club_subscriptions",
        ["razorpay_subscription_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("club_subscriptions")
    op.drop_table("plan_features")
    op.drop_table("plan_limits")
    op.drop_table("plans")

    bind = op.get_bind()
    SUBSCRIPTION_STATUS.drop(bind, checkfirst=True)
    BILLING_CYCLE.drop(bind, checkfirst=True)
    PLAN_VISIBILITY.drop(bind, checkfirst=True)
