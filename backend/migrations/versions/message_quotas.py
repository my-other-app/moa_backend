"""Monthly email and WhatsApp quotas per plan, and paid overage.

Schema:

- plan_limits gains emails_per_month and whatsapp_per_month (null:
  unlimited), nullable with no default: a catalog change.
- plans gains email_overage_paise and whatsapp_overage_paise, NOT NULL with
  constant defaults (10 and 100: ₹0.10 an email, ₹1 a WhatsApp message): a
  catalog change, every plan has the owner's prices at once.
- feedback_sends gains skipped_reason (a survey mail held back past Free's
  emails), nullable: a catalog change.
- New tables (app/api/message_quota/models.py, app/api/club_alerts/models.py):
  message_usage (one counter per club, month in India and channel),
  message_usage_refs (messages already counted), message_statements (each
  club's closed month and what became of its charge), club_message_quotas
  (an Enterprise club's own limits) and club_alerts (the club dashboard's
  bell).

Data: the owner's limits on the plans that exist (by key), each statement
bounded by its WHERE to one plan's row:

    free        200 emails      0 WhatsApp
    starter   1,000 emails     50 WhatsApp
    growth    5,000 emails    250 WhatsApp
    pro      20,000 emails  1,000 WhatsApp

Enterprise (and the legacy Campus, folded into it) stay null: unlimited,
set per club. Any other plan (a hidden one an admin made) is left unlimited
for an admin to set in the plan editor.

Everything under lock_timeout 3s. The tables are new, so nothing waits on
them; the foreign keys to clubs and users take a brief lock there.

Downgrade drops the tables and columns (the counts and statements go with
them; a charge already at Razorpay stays on the club's invoice).

Revision ID: message_quotas
Revises: trial_pricing
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "message_quotas"
down_revision = "trial_pricing"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"

#: The owner's limits of 2026-10-24: (emails, WhatsApp) a month.
LIMITS = {
    "free": (200, 0),
    "starter": (1_000, 50),
    "growth": (5_000, 250),
    "pro": (20_000, 1_000),
}


def seed_limits(bind) -> int:
    """The owner's limits on the plans that exist. Returns rows changed."""
    changed = 0
    for key, (emails, whatsapp) in LIMITS.items():
        changed += bind.execute(
            sa.text(
                "UPDATE plan_limits SET emails_per_month = :emails, whatsapp_per_month = :whatsapp, updated_at = now() "
                "WHERE plan_id IN (SELECT id FROM plans WHERE key = :key AND NOT is_deleted)"
            ),
            {"emails": emails, "whatsapp": whatsapp, "key": key},
        ).rowcount or 0
    return changed


def _timestamps() -> list:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.add_column("plan_limits", sa.Column("emails_per_month", sa.Integer(), nullable=True))
    op.add_column("plan_limits", sa.Column("whatsapp_per_month", sa.Integer(), nullable=True))
    op.add_column("plans", sa.Column("email_overage_paise", sa.Integer(), nullable=False, server_default="10"))
    op.add_column("plans", sa.Column("whatsapp_overage_paise", sa.Integer(), nullable=False, server_default="100"))
    op.create_check_constraint("ck_plans_email_overage_non_negative", "plans", "email_overage_paise >= 0")
    op.create_check_constraint("ck_plans_whatsapp_overage_non_negative", "plans", "whatsapp_overage_paise >= 0")
    op.add_column("feedback_sends", sa.Column("skipped_reason", sa.String(300), nullable=True))

    op.create_table(
        "message_usage",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kinds", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("quota", sa.Integer(), nullable=True),
        sa.Column("rate_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("billing", sa.String(16), nullable=False, server_default="none"),
        sa.Column("plan_key", sa.String(40), nullable=True),
        sa.Column("warned_80_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warned_100_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("club_id", "period", "channel", name="uq_message_usage_club_period_channel"),
        sa.CheckConstraint("channel IN ('email', 'whatsapp')", name="ck_message_usage_channel"),
        sa.CheckConstraint("used >= 0 AND blocked >= 0", name="ck_message_usage_counts"),
    )
    op.create_index("ix_message_usage_period", "message_usage", ["period"])

    op.create_table(
        "message_usage_refs",
        sa.Column("ref", sa.String(200), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_message_usage_refs_created", "message_usage_refs", ["created_at"])

    op.create_table(
        "message_statements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("plan_key", sa.String(40), nullable=True),
        sa.Column("billing", sa.String(16), nullable=False),
        sa.Column("email_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("email_limit", sa.Integer(), nullable=True),
        sa.Column("email_extra", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("email_rate_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("whatsapp_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("whatsapp_limit", sa.Integer(), nullable=True),
        sa.Column("whatsapp_extra", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("whatsapp_rate_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("amount_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("razorpay_subscription_id", sa.String(64), nullable=True),
        sa.Column("razorpay_addon_id", sa.String(64), nullable=True, unique=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(400), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("charging_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("charged_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("club_id", "period", name="uq_message_statements_club_period"),
        sa.CheckConstraint(
            "status IN ('nothing_due', 'pending', 'charging', 'charged', 'failed', 'unconfirmed', "
            "'gave_up', 'manual', 'not_billable', 'waived')",
            name="ck_message_statements_status",
        ),
        sa.CheckConstraint("amount_paise >= 0", name="ck_message_statements_amount"),
    )
    op.create_index(
        "ix_message_statements_due", "message_statements", ["next_attempt_at"],
        postgresql_where=sa.text("status IN ('pending', 'charging', 'failed', 'unconfirmed')"),
    )

    op.create_table(
        "club_message_quotas",
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("emails_per_month", sa.Integer(), nullable=True),
        sa.Column("whatsapp_per_month", sa.Integer(), nullable=True),
        sa.Column("email_overage_paise", sa.Integer(), nullable=True),
        sa.Column("whatsapp_overage_paise", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "(emails_per_month IS NULL OR emails_per_month >= 0) AND (whatsapp_per_month IS NULL OR whatsapp_per_month >= 0) "
            "AND (email_overage_paise IS NULL OR email_overage_paise >= 0) "
            "AND (whatsapp_overage_paise IS NULL OR whatsapp_overage_paise >= 0)",
            name="ck_club_message_quotas_non_negative",
        ),
    )

    op.create_table(
        "club_alerts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("link", sa.String(300), nullable=True),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("dedupe_key", sa.String(160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("club_id", "dedupe_key", name="uq_club_alerts_once"),
    )
    op.create_index("ix_club_alerts_club_created", "club_alerts", ["club_id", sa.text("created_at DESC")])

    seed_limits(op.get_bind())


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.drop_index("ix_club_alerts_club_created", table_name="club_alerts")
    op.drop_table("club_alerts")
    op.drop_table("club_message_quotas")
    op.drop_index("ix_message_statements_due", table_name="message_statements")
    op.drop_table("message_statements")
    op.drop_index("ix_message_usage_refs_created", table_name="message_usage_refs")
    op.drop_table("message_usage_refs")
    op.drop_index("ix_message_usage_period", table_name="message_usage")
    op.drop_table("message_usage")
    op.drop_column("feedback_sends", "skipped_reason")
    op.drop_constraint("ck_plans_whatsapp_overage_non_negative", "plans", type_="check")
    op.drop_constraint("ck_plans_email_overage_non_negative", "plans", type_="check")
    op.drop_column("plans", "whatsapp_overage_paise")
    op.drop_column("plans", "email_overage_paise")
    op.drop_column("plan_limits", "whatsapp_per_month")
    op.drop_column("plan_limits", "emails_per_month")
