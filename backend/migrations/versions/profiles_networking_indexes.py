"""Profiles and the Network: the indexes on hot tables, built CONCURRENTLY.

- ix_users_username_lower: users (lower(username)), the handle's case-blind
  lookup (/profiles/{handle}).
- ix_user_profiles_hidden: user_profiles (user_id) WHERE suspended_at IS NOT
  NULL AND NOT is_deleted, the few profiles an admin hid, which the
  Network's lists leave out by a probe of it.

Both tables are hot, so each index is built without blocking their writes,
outside a transaction (an autocommit block). They are their own revision,
after profiles_networking, so that one is stamped before either build
starts: a build that fails or is cut off leaves only this revision to run
again. Each first drops an index of its name left INVALID by such a build.

Downgrade drops both, concurrently.

Revision ID: profiles_networking_indexes
Revises: profiles_networking
"""
import sqlalchemy as sa
from alembic import op

revision = "profiles_networking_indexes"
down_revision = "profiles_networking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index("ix_users_username_lower", table_name="users", postgresql_concurrently=True, if_exists=True)
        op.create_index("ix_users_username_lower", "users", [sa.text("lower(username)")], postgresql_concurrently=True)
        op.drop_index("ix_user_profiles_hidden", table_name="user_profiles", postgresql_concurrently=True, if_exists=True)
        op.create_index(
            "ix_user_profiles_hidden", "user_profiles", ["user_id"], postgresql_concurrently=True,
            postgresql_where=sa.text("suspended_at IS NOT NULL AND NOT is_deleted"),
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index("ix_user_profiles_hidden", table_name="user_profiles", postgresql_concurrently=True, if_exists=True)
        op.drop_index("ix_users_username_lower", table_name="users", postgresql_concurrently=True, if_exists=True)
