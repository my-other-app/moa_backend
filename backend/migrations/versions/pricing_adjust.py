"""WhatsApp extra ₹1.50, a promotional WhatsApp cap, and the trial's allowances.

The owner's changes of 2026-10-25. What a WhatsApp message costs us, with
GST: ₹0.136 for a UTILITY template, ₹1.02 for a MARKETING one.

Schema (every column nullable or with a constant default: catalog changes):

- plan_limits.whatsapp_marketing_per_month: of a plan's monthly WhatsApp,
  how many may be MARKETING (promotional) templates (null: no cap of their
  own). Past it a promotional WhatsApp is held back on every plan
  (app/api/message_quota); tickets, certificates and reminders never are.
- plan_limits.trial_emails_per_month, trial_whatsapp_per_month,
  trial_whatsapp_marketing_per_month and trial_ai_actions_per_month: the
  free trial's own allowances while this plan is given as the trial (null:
  the plan's own). The trial keeps the plan's features.
- message_usage.marketing: of a month's WhatsApp, the promotional ones
  (NOT NULL DEFAULT 0).
- plans.whatsapp_overage_paise: its default for a plan made from now on is
  150 (₹1.50), not 100.

Data, each statement bounded to the handful of plan_limits / plans rows,
and only where the value is still the one the seed wrote, so an owner's own
edit is never overwritten:

- whatsapp_overage_paise 100 -> 150 on the six seeded plans (free, starter,
  growth, pro, campus, enterprise; Free never bills an extra, so its price
  is only ever shown). A plan whose price was changed is left alone.
- whatsapp_marketing_per_month = 20% of whatsapp_per_month, rounded down,
  on every plan with a WhatsApp allowance and no cap yet (the column is
  new, so there is no edit to keep): Free 0 of 0, Starter 10 of 50, Growth
  50 of 250, Pro 200 of 1,000. Unlimited WhatsApp (Enterprise, Campus)
  stays uncapped.
- On Pro, the free trial's plan: 2,000 emails, 50 WhatsApp (10 of them
  promotional) and 50 AI actions a month during the trial.

Every step checks first, so running it again changes nothing.

Downgrade drops the columns (the promotional counts go with them) and puts
₹1 back on the plans that still have the ₹1.50 this set.

Revision ID: pricing_adjust
Revises: club_whatsapp_reach
"""
import sqlalchemy as sa
from alembic import op

revision = "pricing_adjust"
down_revision = "club_whatsapp_reach"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"

#: The plans club_plans seeded, whose WhatsApp extra message_quotas set to ₹1.
SEEDED_PLANS = ("free", "starter", "growth", "pro", "campus", "enterprise")
OLD_WHATSAPP_OVERAGE = 100
NEW_WHATSAPP_OVERAGE = 150

#: Promotional WhatsApp as a share of each plan's monthly WhatsApp: 1/5.
MARKETING_SHARE_DIVISOR = 5

#: The free trial's plan (subscriptions.service.FREE_TRIAL_PLAN_KEY) and its
#: allowances: column -> value.
TRIAL_PLAN = "pro"
TRIAL_LIMITS = {
    "trial_emails_per_month": 2_000,
    "trial_whatsapp_per_month": 50,
    "trial_whatsapp_marketing_per_month": 10,
    "trial_ai_actions_per_month": 50,
}

NEW_LIMIT_COLUMNS = ("whatsapp_marketing_per_month", *TRIAL_LIMITS)


def raise_whatsapp_overage(bind) -> int:
    """₹1 -> ₹1.50 on the seeded plans still at ₹1. Returns rows changed."""
    return bind.execute(
        sa.text(
            "UPDATE plans SET whatsapp_overage_paise = :new, updated_at = now() "
            "WHERE key = ANY(:keys) AND NOT is_deleted AND whatsapp_overage_paise = :old"
        ),
        {"new": NEW_WHATSAPP_OVERAGE, "old": OLD_WHATSAPP_OVERAGE, "keys": list(SEEDED_PLANS)},
    ).rowcount or 0


def cap_marketing(bind) -> int:
    """20% of each plan's monthly WhatsApp, where no cap is set yet."""
    return bind.execute(
        sa.text(
            "UPDATE plan_limits SET whatsapp_marketing_per_month = whatsapp_per_month / :divisor, updated_at = now() "
            "WHERE whatsapp_per_month IS NOT NULL AND whatsapp_marketing_per_month IS NULL "
            "AND plan_id IN (SELECT id FROM plans WHERE NOT is_deleted)"
        ),
        {"divisor": MARKETING_SHARE_DIVISOR},
    ).rowcount or 0


def set_trial_limits(bind) -> int:
    """The trial's allowances on its plan, each only where it is not set."""
    changed = 0
    for column, value in TRIAL_LIMITS.items():
        changed += bind.execute(
            sa.text(
                f"UPDATE plan_limits SET {column} = :value, updated_at = now() "  # noqa: S608 -- a column named above
                f"WHERE {column} IS NULL AND plan_id IN (SELECT id FROM plans WHERE key = :key AND NOT is_deleted)"
            ),
            {"value": value, "key": TRIAL_PLAN},
        ).rowcount or 0
    return changed


def seed(bind) -> dict:
    """The data steps above, in order. Safe to run again."""
    return {
        "whatsapp_overage": raise_whatsapp_overage(bind),
        "marketing_caps": cap_marketing(bind),
        "trial_limits": set_trial_limits(bind),
    }


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    have = {c["name"] for c in inspector.get_columns("plan_limits")}
    for column in NEW_LIMIT_COLUMNS:
        if column not in have:
            op.add_column("plan_limits", sa.Column(column, sa.Integer(), nullable=True))
    if "marketing" not in {c["name"] for c in inspector.get_columns("message_usage")}:
        op.add_column("message_usage", sa.Column("marketing", sa.Integer(), nullable=False, server_default="0"))
    op.alter_column("plans", "whatsapp_overage_paise", server_default=str(NEW_WHATSAPP_OVERAGE))
    seed(bind)


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE plans SET whatsapp_overage_paise = :old, updated_at = now() "
            "WHERE key = ANY(:keys) AND NOT is_deleted AND whatsapp_overage_paise = :new"
        ),
        {"new": NEW_WHATSAPP_OVERAGE, "old": OLD_WHATSAPP_OVERAGE, "keys": list(SEEDED_PLANS)},
    )
    op.alter_column("plans", "whatsapp_overage_paise", server_default=str(OLD_WHATSAPP_OVERAGE))
    op.drop_column("message_usage", "marketing")
    for column in reversed(NEW_LIMIT_COLUMNS):
        op.drop_column("plan_limits", column)
