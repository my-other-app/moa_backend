"""Private developer docs: the signed links that open them
(app/api/partner/docs_links.py, feature-register/private-dev-docs-contract.md).

One new table, empty:

- developer_docs_links: a link to the private docs (myotherapp.in/developers/...),
  by the sha256 of its token only (uq_developer_docs_links_token_hash). A
  club's owner or admin makes one for themselves or to share with their
  developer, a platform admin their own or one for a partner app's
  developers. Expiry, revocation, last use and a use count; who made it
  (the account, and the team member when one did).

Nothing existing changes. Lock-safe: the table is new, so its indexes are
built on nothing; the foreign keys take a brief SHARE ROW EXCLUSIVE on clubs,
partner_apps, users and club_members, under lock_timeout 3s.

Downgrade drops the table (every docs link with it: the website then refuses
every token, as it would an unknown one).

Revision ID: developer_docs_links
Revises: api_webhooks
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "developer_docs_links"
down_revision = "api_webhooks"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
NOW = sa.text("now()")
TABLE = "developer_docs_links"


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("purpose", sa.String(8), nullable=False, server_default="self"),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("partner_app_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("partner_apps.id", ondelete="CASCADE"), nullable=True),
        sa.Column("label", sa.String(80), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="CASCADE"), nullable=True),
        sa.Column("created_by_name", sa.String(120), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("kind IN ('club', 'partner_app', 'admin')", name="ck_developer_docs_links_kind"),
        sa.CheckConstraint("purpose IN ('self', 'share')", name="ck_developer_docs_links_purpose"),
        sa.CheckConstraint("kind <> 'club' OR club_id IS NOT NULL", name="ck_developer_docs_links_club"),
        sa.CheckConstraint("kind <> 'partner_app' OR partner_app_id IS NOT NULL", name="ck_developer_docs_links_partner_app"),
        sa.CheckConstraint("use_count >= 0", name="ck_developer_docs_links_use_count"),
        if_not_exists=True,
    )
    op.create_index("uq_developer_docs_links_token_hash", TABLE, ["token_hash"], unique=True, if_not_exists=True)
    op.create_index("ix_developer_docs_links_club", TABLE, ["club_id", "created_at"],
                    postgresql_where=sa.text("club_id IS NOT NULL"), if_not_exists=True)
    op.create_index("ix_developer_docs_links_partner_app", TABLE, ["partner_app_id"],
                    postgresql_where=sa.text("partner_app_id IS NOT NULL"), if_not_exists=True)
    op.create_index("ix_developer_docs_links_created_at", TABLE, ["created_at"], if_not_exists=True)


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute(f"DROP TABLE IF EXISTS {TABLE}")
