"""People a club lets into its dashboard with their own login and a role,
and which sessions are theirs.

Revision ID: add_club_members
Revises: add_club_sites
"""
import sqlalchemy as sa
from alembic import op

revision = "add_club_members"
down_revision = "add_club_sites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "club_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(100), nullable=False),
        sa.Column("full_name", sa.String(80), nullable=True),
        sa.Column("role", sa.String(16), nullable=False, server_default="editor"),
        sa.Column("status", sa.String(16), nullable=False, server_default="invited"),
        sa.Column("password", sa.String(255), nullable=True),
        sa.Column("invite_token_hash", sa.String(64), nullable=True),
        sa.Column("invite_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_club_members_club_id", "club_members", ["club_id"])
    op.create_index("ix_club_members_invite_token_hash", "club_members", ["invite_token_hash"])
    op.create_index(
        "uq_club_members_live_email", "club_members", ["email"], unique=True,
        postgresql_where=sa.text("status <> 'removed'"),
    )
    op.add_column("user_sessions", sa.Column("member_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_user_sessions_member_id", "user_sessions", "club_members", ["member_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_user_sessions_member_id", "user_sessions", ["member_id"])


def downgrade() -> None:
    op.drop_index("ix_user_sessions_member_id", table_name="user_sessions")
    op.drop_constraint("fk_user_sessions_member_id", "user_sessions", type_="foreignkey")
    op.drop_column("user_sessions", "member_id")
    op.drop_index("uq_club_members_live_email", table_name="club_members")
    op.drop_index("ix_club_members_invite_token_hash", table_name="club_members")
    op.drop_index("ix_club_members_club_id", table_name="club_members")
    op.drop_table("club_members")
