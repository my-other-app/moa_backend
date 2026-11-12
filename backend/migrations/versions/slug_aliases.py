"""The slugs a club or an event was once at (feature-register/mail-and-slugs-contract.md, Part S).

One new table, empty:

- slug_aliases: (kind 'club' | 'event', slug, target_id, created_at,
  created_by_user_id). When an admin changes a club's or an event's slug
  (PATCH /admin/clubs/{id}/slug, /admin/events/{id}/slug) the old one comes
  here, and every lookup by slug falls back to it (app/api/slugs). Unique
  per (kind, slug); indexed by (kind, target_id) for a club's or event's
  list of old ones.

Nothing existing changes. Lock-safe: the table is new, so its indexes are
built on nothing; the one foreign key takes a brief SHARE ROW EXCLUSIVE on
users, under lock_timeout 3s.

Downgrade drops the table: every old slug stops answering (404, as before
the change existed) while the current ones go on as they are.

Revision ID: slug_aliases
Revises: certificate_design
"""
import sqlalchemy as sa
from alembic import op

revision = "slug_aliases"
down_revision = "certificate_design"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
TABLE = "slug_aliases"


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.CheckConstraint("kind IN ('club', 'event')", name="ck_slug_aliases_kind"),
        if_not_exists=True,
    )
    op.create_index("uq_slug_aliases_kind_slug", TABLE, ["kind", "slug"], unique=True, if_not_exists=True)
    op.create_index("ix_slug_aliases_kind_target", TABLE, ["kind", "target_id"], if_not_exists=True)


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute(f"DROP TABLE IF EXISTS {TABLE}")
