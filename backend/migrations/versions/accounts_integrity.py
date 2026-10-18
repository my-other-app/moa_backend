"""Accounts integrity: sessions that rotate, single-use second factors, one
token per device, uniqueness the code relied on, and the indexes the inbox,
the admin list and the housekeeping sweep need.

Columns (one short transaction; each is nullable or has a constant default,
so only the catalog changes):
- user_sessions.refresh_generation (bigint, default 0), rotated_at,
  expires_at, impersonated_by -> users.id (ON DELETE SET NULL): refresh
  rotation with reuse detection, and an admin's session as someone else.
- users.totp_last_step (bigint): a code is good once. users.totp_secret
  widened to varchar(255) for the encrypted form (a widening is catalog-only).

Data, then indexes, each outside a transaction and built CONCURRENTLY (any
invalid leftover of a failed run is dropped first), so writes go on:
- user_device_tokens: a token is unique (one row per device), not (user,
  platform). Duplicate tokens are removed first, keeping the live, most
  recently registered row. The (user_id, platform) unique constraint goes
  (found by its columns: it is uq_user_device_platform in production,
  uq_user_platform in the models), and the plain fcm_token index with it.
- users: uq_users_email_user_type and uq_users_phone_user_type become
  partial on NOT is_deleted, so a deleted account no longer holds its
  address for ever. Made under new names, then swapped in. Relaxing a
  unique index needs no clean-up; but where production already holds two
  live accounts on one (email, user_type) -- possible where the 2025 schema
  never enforced it -- the index is made without UNIQUE, as the existing one
  then is, and a warning says so.
- user_profiles: one live profile per account. Extra live profiles are
  soft-deleted first, keeping the most recently updated.
- user_interests: (user_id, interest_id) unique. Duplicates are deleted
  first, keeping a live row, then the oldest.
- notifications (user_id, created_at DESC) and (user_id) WHERE unread, both
  WHERE NOT is_deleted: the inbox, its count and mark-all-read.
- user_interests (interest_id, user_id): the new-event push by interest.
- users (created_at DESC, id DESC) and trigram indexes on full_name, email
  and username, all WHERE NOT is_deleted: the admin's user list and search.
- sign_in_attempts (identifier, created_at) and (created_at); user_sessions
  (last_seen_at) and (expires_at): the account-wide lockout and the sweep.

Downgrade drops what was added and puts the old uniqueness back -- after
removing what the new rules allowed and the old ones forbid (extra device
tokens per (user, platform), keeping the newest); the users indexes go back
to covering deleted accounts, without UNIQUE where that now finds a clash.
Accounts whose two-factor secret was encrypted have two-factor turned off
(the code before cannot read it; they enrol again), and recovery codes in
the new form are dropped.

Revision ID: accounts_integrity
Revises: money_integrity
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "accounts_integrity"
down_revision = "money_integrity"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

DEDUPE_TOKENS = """
DELETE FROM user_device_tokens WHERE id IN (
  SELECT id FROM (
    SELECT id, row_number() OVER (
      PARTITION BY fcm_token ORDER BY is_deleted ASC, updated_at DESC NULLS LAST, id DESC
    ) AS rn FROM user_device_tokens
  ) ranked WHERE rn > 1
)
"""

DEDUPE_PROFILES = """
UPDATE user_profiles SET is_deleted = true, deleted_at = now() WHERE id IN (
  SELECT id FROM (
    SELECT id, row_number() OVER (
      PARTITION BY user_id ORDER BY updated_at DESC NULLS LAST, id DESC
    ) AS rn FROM user_profiles WHERE NOT is_deleted
  ) ranked WHERE rn > 1
)
"""

DEDUPE_INTERESTS = """
DELETE FROM user_interests WHERE id IN (
  SELECT id FROM (
    SELECT id, row_number() OVER (
      PARTITION BY user_id, interest_id ORDER BY is_deleted ASC, id ASC
    ) AS rn FROM user_interests
  ) ranked WHERE rn > 1
)
"""

# (name, definition) -- each made CONCURRENTLY.
INDEXES = (
    ("ux_user_device_tokens_token", "UNIQUE", "user_device_tokens (fcm_token)"),
    ("ix_user_device_tokens_user_id", "", "user_device_tokens (user_id)"),
    ("ux_user_profiles_user_live", "UNIQUE", "user_profiles (user_id) WHERE NOT is_deleted"),
    ("ux_user_interests_user_interest", "UNIQUE", "user_interests (user_id, interest_id)"),
    ("ix_user_interests_interest", "", "user_interests (interest_id, user_id)"),
    ("ix_notifications_user_created", "", "notifications (user_id, created_at DESC) WHERE NOT is_deleted"),
    ("ix_notifications_user_unread", "", "notifications (user_id) WHERE status = 'unread' AND NOT is_deleted"),
    ("ix_users_created_id", "", "users (created_at DESC, id DESC) WHERE NOT is_deleted"),
    ("ix_users_full_name_trgm", "", "users USING gin (full_name gin_trgm_ops) WHERE NOT is_deleted"),
    ("ix_users_email_trgm", "", "users USING gin (email gin_trgm_ops) WHERE NOT is_deleted"),
    ("ix_users_username_trgm", "", "users USING gin (username gin_trgm_ops) WHERE NOT is_deleted"),
    ("ix_sign_in_attempts_identifier_created", "", "sign_in_attempts (identifier, created_at)"),
    ("ix_sign_in_attempts_created", "", "sign_in_attempts (created_at)"),
    ("ix_user_sessions_last_seen", "", "user_sessions (last_seen_at)"),
    ("ix_user_sessions_expires", "", "user_sessions (expires_at) WHERE expires_at IS NOT NULL"),
)

# The (user_id, platform) unique constraint on user_device_tokens, whatever it is called.
UNIQUE_BY_COLUMNS = """
SELECT con.conname
FROM pg_constraint con
JOIN pg_class rel ON rel.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid = rel.relnamespace
WHERE rel.relname = :table AND ns.nspname = current_schema() AND con.contype = 'u'
  AND (
    SELECT array_agg(att.attname::text ORDER BY att.attname::text)
    FROM unnest(con.conkey) AS k(attnum)
    JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum
  ) = CAST(:columns AS text[])
"""


def _make(name: str, unique: str, definition: str) -> None:
    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(f"CREATE {unique} INDEX CONCURRENTLY {name} ON {definition}".replace("  ", " "))


def _has_duplicates(column: str, live_only: bool) -> bool:
    live = "AND NOT is_deleted" if live_only else ""
    return bool(
        op.get_bind().scalar(
            sa.text(
                f"SELECT EXISTS (SELECT 1 FROM users WHERE user_type <> 'guest' AND {column} IS NOT NULL {live} "
                f"GROUP BY {column}, user_type HAVING count(*) > 1)"
            )
        )
    )


def _swap_users_unique(column: str, live_only: bool) -> None:
    """Rebuild uq_users_<column>_user_type under a new name, then swap it in."""
    name = f"uq_users_{column}_user_type"
    temp = f"{name}_new"
    where = "user_type <> 'guest' AND NOT is_deleted" if live_only else "user_type <> 'guest'"
    unique = "UNIQUE"
    if _has_duplicates(column, live_only):
        log.warning("users has duplicate (%s, user_type) among %s accounts; %s made without UNIQUE", column, "live" if live_only else "all", name)
        unique = ""
    _make(temp, unique, f"users ({column}, user_type) WHERE {where}")
    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(f"ALTER INDEX {temp} RENAME TO {name}")


def upgrade() -> None:
    op.add_column("user_sessions", sa.Column("refresh_generation", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("user_sessions", sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_sessions", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_sessions", sa.Column("impersonated_by", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_user_sessions_impersonated_by", "user_sessions", "users", ["impersonated_by"], ["id"], ondelete="SET NULL",
    )
    op.add_column("users", sa.Column("totp_last_step", sa.BigInteger(), nullable=True))
    op.alter_column("users", "totp_secret", type_=sa.String(255), existing_type=sa.String(64), existing_nullable=True)

    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for row in bind.execute(sa.text(UNIQUE_BY_COLUMNS), {"table": "user_device_tokens", "columns": ["platform", "user_id"]}):
            op.execute(f'ALTER TABLE user_device_tokens DROP CONSTRAINT "{row.conname}"')
        op.execute(DEDUPE_TOKENS)
        op.execute(DEDUPE_PROFILES)
        op.execute(DEDUPE_INTERESTS)
        for name, unique, definition in INDEXES:
            _make(name, unique, definition)
        # Made redundant by the unique one.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_user_device_tokens_fcm_token")
        _swap_users_unique("email", live_only=True)
        _swap_users_unique("phone", live_only=True)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        _swap_users_unique("email", live_only=False)
        _swap_users_unique("phone", live_only=False)
        for name, _unique, _definition in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        # One row per (user, platform) again: the newest stays.
        op.execute(
            """
            DELETE FROM user_device_tokens WHERE id IN (
              SELECT id FROM (
                SELECT id, row_number() OVER (
                  PARTITION BY user_id, platform ORDER BY is_deleted ASC, updated_at DESC NULLS LAST, id DESC
                ) AS rn FROM user_device_tokens
              ) ranked WHERE rn > 1
            )
            """
        )
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_user_device_tokens_fcm_token ON user_device_tokens (fcm_token)")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_user_device_tokens_user_id ON user_device_tokens (user_id)")
    op.create_unique_constraint("uq_user_device_platform", "user_device_tokens", ["user_id", "platform"])
    # The code before this cannot read an encrypted secret or a digest
    # recovery code. An account whose secret is encrypted has two-factor
    # turned off (it enrols again) rather than locked out; digest recovery
    # codes are dropped and bcrypt ones kept.
    op.execute(
        "UPDATE users SET totp_enabled = false, totp_secret = NULL, totp_recovery_codes = NULL "
        "WHERE totp_secret LIKE 'fernet:%'"
    )
    op.execute(
        """
        UPDATE users SET totp_recovery_codes = (
          SELECT coalesce(json_agg(c), '[]'::json) FROM json_array_elements_text(totp_recovery_codes) AS c WHERE c LIKE '$2%'
        ) WHERE totp_recovery_codes IS NOT NULL
        """
    )
    op.alter_column("users", "totp_secret", type_=sa.String(64), existing_type=sa.String(255), existing_nullable=True)
    op.drop_column("users", "totp_last_step")
    op.drop_constraint("fk_user_sessions_impersonated_by", "user_sessions", type_="foreignkey")
    op.drop_column("user_sessions", "impersonated_by")
    op.drop_column("user_sessions", "expires_at")
    op.drop_column("user_sessions", "rotated_at")
    op.drop_column("user_sessions", "refresh_generation")
