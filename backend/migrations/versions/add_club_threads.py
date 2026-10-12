"""Threads: a club's posts, their poll options, and the likes, votes, comments and views they earn.

Revision ID: add_club_threads
Revises: add_partner_logos
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_club_threads"
down_revision = "add_partner_logos"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
STAMPS = lambda: [  # noqa: E731
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
]


def upgrade() -> None:
    op.create_table(
        "club_threads",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("body", sa.String(500), nullable=False),
        sa.Column("image", sa.String(), nullable=True),
        sa.Column("kind", sa.String(10), nullable=False, server_default="post"),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notify_followers", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("likes_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comments_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("views_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *STAMPS(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_club_threads_club_id", "club_threads", ["club_id"])
    op.create_index("ix_club_threads_is_deleted", "club_threads", ["is_deleted"])
    op.create_index("ix_club_threads_club_created", "club_threads", ["club_id", "created_at"])
    op.create_table(
        "thread_poll_options",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("thread_id", UUID, sa.ForeignKey("club_threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("votes_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_thread_poll_options_thread_id", "thread_poll_options", ["thread_id"])
    op.create_table(
        "thread_votes",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("thread_id", UUID, sa.ForeignKey("club_threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("option_id", UUID, sa.ForeignKey("thread_poll_options.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        *STAMPS(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("thread_id", "user_id", name="uq_thread_votes_thread_user"),
    )
    op.create_index("ix_thread_votes_thread_id", "thread_votes", ["thread_id"])
    op.create_table(
        "thread_likes",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("thread_id", UUID, sa.ForeignKey("club_threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        *STAMPS(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("thread_id", "user_id", name="uq_thread_likes_thread_user"),
    )
    op.create_index("ix_thread_likes_thread_id", "thread_likes", ["thread_id"])
    op.create_table(
        "thread_comments",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("thread_id", UUID, sa.ForeignKey("club_threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("body", sa.String(500), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *STAMPS(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_thread_comments_thread_id", "thread_comments", ["thread_id"])
    op.create_index("ix_thread_comments_is_deleted", "thread_comments", ["is_deleted"])
    op.create_table(
        "thread_views",
        sa.Column("thread_id", UUID, sa.ForeignKey("club_threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("visitor", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("thread_id", "visitor"),
    )


def downgrade() -> None:
    op.drop_table("thread_views")
    for name, idx in (("thread_comments", ["ix_thread_comments_is_deleted", "ix_thread_comments_thread_id"]), ("thread_likes", ["ix_thread_likes_thread_id"]), ("thread_votes", ["ix_thread_votes_thread_id"]), ("thread_poll_options", ["ix_thread_poll_options_thread_id"])):
        for i in idx:
            op.drop_index(i, table_name=name)
        op.drop_table(name)
    op.drop_index("ix_club_threads_club_created", table_name="club_threads")
    op.drop_index("ix_club_threads_is_deleted", table_name="club_threads")
    op.drop_index("ix_club_threads_club_id", table_name="club_threads")
    op.drop_table("club_threads")
