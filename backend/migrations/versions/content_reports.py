"""Reports and blocks: what a reader can do about a comment, a post, a person
or a club they don't want to see (App Store guideline 1.2).

- content_reports: one person's report on one thread, comment, user or
  club, open until an admin resolves it. At most one open report per person
  per thing (a partial unique index); the target's id is text, since threads
  and comments have UUIDs and users and clubs integers.
- user_blocks: who blocked whom; a blocked person's comments never show to
  the blocker.

New tables only: nothing existing changes and there is nothing to backfill.
Downgrade drops both.

Revision ID: content_reports
Revises: admin_revenue
"""
import sqlalchemy as sa
from alembic import op

revision = "content_reports"
down_revision = "admin_revenue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("reporter_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_type", sa.String(10), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="open"),
        sa.Column("target_author_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("resolution", sa.String(20), nullable=True),
        sa.Column("resolution_note", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("target_type IN ('thread', 'comment', 'user', 'club')", name="ck_content_reports_target_type"),
        sa.CheckConstraint(
            "reason IN ('spam', 'harassment', 'hate', 'sexual', 'violence', 'misinformation', 'other')",
            name="ck_content_reports_reason",
        ),
        sa.CheckConstraint("status IN ('open', 'resolved')", name="ck_content_reports_status"),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN ('dismiss', 'remove_content', 'suspend_author')",
            name="ck_content_reports_resolution",
        ),
    )
    op.create_index(
        "uq_content_reports_open",
        "content_reports",
        ["reporter_id", "target_type", "target_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index("ix_content_reports_target", "content_reports", ["target_type", "target_id", "status"])
    op.create_index("ix_content_reports_queue", "content_reports", ["status", sa.text("created_at DESC")])
    op.create_index("ix_content_reports_reporter_created", "content_reports", ["reporter_id", "created_at"])

    op.create_table(
        "user_blocks",
        sa.Column("blocker_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("blocked_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("blocker_id <> blocked_id", name="ck_user_blocks_not_self"),
    )
    op.create_index("ix_user_blocks_blocked", "user_blocks", ["blocked_id"])


def downgrade() -> None:
    op.drop_index("ix_user_blocks_blocked", table_name="user_blocks")
    op.drop_table("user_blocks")
    op.drop_index("ix_content_reports_reporter_created", table_name="content_reports")
    op.drop_index("ix_content_reports_queue", table_name="content_reports")
    op.drop_index("ix_content_reports_target", table_name="content_reports")
    op.drop_index("uq_content_reports_open", table_name="content_reports")
    op.drop_table("content_reports")
