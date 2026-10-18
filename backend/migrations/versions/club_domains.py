"""Clubs' own domains for their websites.

- club_domains: one row per domain a club connected (Cloudflare for SaaS
  custom hostnames). At most one live row (pending, active or paused) per
  club and per host, by partial unique indexes; failed and removed rows are
  kept for the record and hold nothing.
- plan_features.domain, a placeholder flag with nothing behind it, becomes
  plan_features.custom_domain -- the column keeps whatever an admin set --
  and is switched on for the growth, pro, campus and enterprise plans
  (`grant`). Admins edit it in Admin -> Subscriptions like every other flag.

Downgrade drops the table and renames the column back; the flags it set
stay set.

Revision ID: club_domains
Revises: admin_revenue
"""
import sqlalchemy as sa
from alembic import op

revision = "club_domains"
down_revision = "admin_revenue"
branch_labels = None
depends_on = None

#: The plans that include a club's own domain, from the start.
PLANS = ("growth", "pro", "campus", "enterprise")

LIVE = "status IN ('pending', 'active', 'paused')"


def grant() -> None:
    """Switch custom_domain on for PLANS. Plans that do not exist are skipped;
    safe to run again."""
    op.get_bind().execute(
        sa.text(
            "UPDATE plan_features SET custom_domain = true, updated_at = now() "
            "WHERE plan_id IN (SELECT id FROM plans WHERE key = ANY(:keys))"
        ),
        dict(keys=list(PLANS)),
    )


def upgrade() -> None:
    op.create_table(
        "club_domains",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id"), nullable=False),
        sa.Column("host", sa.String(253), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("cf_hostname_id", sa.String(64), nullable=True),
        sa.Column("records", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("hostname_status", sa.String(40), nullable=True),
        sa.Column("ssl_status", sa.String(40), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'paused', 'failed', 'removed')",
            name="ck_club_domains_status",
        ),
    )
    op.create_index("ix_club_domains_club_id", "club_domains", ["club_id"])
    op.create_index("ix_club_domains_status", "club_domains", ["status"])
    op.create_index("uq_club_domains_live_host", "club_domains", ["host"], unique=True, postgresql_where=sa.text(LIVE))
    op.create_index("uq_club_domains_live_club", "club_domains", ["club_id"], unique=True, postgresql_where=sa.text(LIVE))

    op.alter_column("plan_features", "domain", new_column_name="custom_domain")
    grant()


def downgrade() -> None:
    op.alter_column("plan_features", "custom_domain", new_column_name="domain")
    op.drop_index("uq_club_domains_live_club", table_name="club_domains")
    op.drop_index("uq_club_domains_live_host", table_name="club_domains")
    op.drop_index("ix_club_domains_status", table_name="club_domains")
    op.drop_index("ix_club_domains_club_id", table_name="club_domains")
    op.drop_table("club_domains")
